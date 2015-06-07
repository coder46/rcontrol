# -*- coding: utf-8
import sys
import six
import threading
from collections import OrderedDict
from rcontrol import fs
import abc


class TimeoutError(Exception):
    """Raise on a timeout error"""


class ExitCodeError(Exception):
    """Raised when the exit code of a command is unexpected"""


class Task(object):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def is_running(self):
        """
        Return True if the task is running.
        """

    @abc.abstractmethod
    def error(self):
        """
        Return an instance of Exception if any, else None.
        """
        return None

    def raise_if_error(self):
        """
        Check if an error occured and raise it if any.
        """
        error = self.error()
        if error:
            raise error

    @abc.abstractmethod
    def wait(self, raise_if_error=True):
        """
        Block and wait until the task is finished.

        :param raise_if_error: if True, call :meth:`raise_if_error` at
            the end.
        """


class BaseSession(object):
    """
    Represent an abstraction of a session on a remote or local machine.
    """

    def __init__(self):
        self._lock = threading.Lock()  # a lock for tasks access
        self._tasks = []

    def _register_task(self, task):
        assert isinstance(task, Task)
        with self._lock:
            self._tasks.append(task)

    def _unregister_task(self, task):
        with self._lock:
            try:
                self._tasks.remove(task)
            except ValueError:
                pass  # this should not happen

    def tasks(self):
        """
        Return a copy of the currently active tasks.
        """
        with self._lock:
            return self._tasks[:]

    def open(self, filename, mode='r', bufsize=-1):
        """
        Return an opened file object.

        :param filename: the file path to open
        :param mode: the mode used to open the file
        :param bufsize: buffer size
        """
        raise NotImplementedError

    def execute(self, command, **kwargs):
        """
        Execute a command in an asynchronous way.

        Return an instance of a subclass of a :class:`StreamReadersExec`.

        :param command: the command to execute (a string)
        :param kwargs: named arguments passed to the constructor of the
            class:`StreamReadersExec` subclass.
        """
        raise NotImplementedError

    def copy_file(self, src, dest_os, dest, chunk_size=16384):
        """
        Copy a file from this session to another session.

        This is done in an asynchronous way and return an instance of
        :class:`ThreadableTask`.

        :param src: full path of the file to copy in this session
        :param dest_os: session to copy to
        :param dest: full path of the file to copy in the dest session
        """
        task = ThreadableTask(fs.copy_file,
                              (self, src, dest_os, dest),
                              dict(chunk_size=chunk_size),
                              finished_callback=self._unregister_task)
        self._register_task(task)
        return task

    def close(self):
        """
        Close the session.
        """


class SessionManager(OrderedDict):
    """
    A specialized OrderedDict that keep sessions instances.

    It can be used like a namespace: ::

      sess_manager.local = LocalSession()
      # equivalent to:
      # sess_manager['local'] = LocalSession()

    A session manager is useful because it has a close() method, that
    will close each registered session. So you can do: ::

      from contextlib import closing

      with closing(SessionManager()) as sessions:
          # do everything you want, sessions will be closed automatically
          # after this with block

    Note that this is required on python3, else python may hang.
    """

    def __setattr__(self, name, value):
        if isinstance(value, BaseSession):
            self[name] = value
        else:
            self.__dict__[name] = value

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            return OrderedDict.__getattr__(self, name)

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            OrderedDict.__delattr__(self, name)

    def close(self):
        for session in self.values():
            session.close()


class StreamReadersExec(Task):
    """
    Base class that execute a command in an asynchronous way.

    It uses an internal stream reader (a subclass of
    :class:`streamreader.StreamsReader`)

    :param session: the session that run this command
    :param reader_class: the :class:`streamreader.StreamsReader` class
        to use
    :param command: the command to execute (a string)
    :param expected_exit_code: the expected exit code of the command. If
        None, there is no exit code expected.

    Other params are passed to the :class:`streamreader.StreamsReader`
    constructor.
    """
    def __init__(self, session, reader_class, command, expected_exit_code=0,
                 timeout=None, combine_stderr=None, output_timeout=None,
                 finished_callback=None, timeout_callback=None,
                 stdout_callback=None, stderr_callback=None):

        self.session = session
        self.session._register_task(self)

        if combine_stderr is None:
            combine_stderr = not stderr_callback
        self._combine_stderr = combine_stderr

        self.__exit_code = None
        self.__expected_exit_code = expected_exit_code
        self.__timed_out = False
        self.__finished_callback = finished_callback
        self.__timeout_callback = timeout_callback
        self.__stdout_callback = stdout_callback
        self.__stderr_callback = stderr_callback

        self._reader = reader_class(
            stdout_callback=self._on_stdout,
            stderr_callback=self._on_stderr,
            timeout=timeout,
            output_timeout=output_timeout,
            timeout_callback=self._on_timeout,
            finished_callback=self._on_finished
        )

    def _set_exit_code(self, exit_code):
        self.__exit_code = exit_code

    def _on_stdout(self, line):
        if self.__stdout_callback:
            self.__stdout_callback(self, line)

    def _on_stderr(self, line):
        if self.__stderr_callback:
            self.__stderr_callback(self, line)

    def _on_timeout(self):
        self.__timed_out = True
        if self.__timeout_callback:
            self.__timeout_callback(self)

    def _on_finished(self):
        self.session._unregister_task(self)
        if self.__finished_callback:
            self.__finished_callback(self)

    def timed_out(self):
        """
        Return True if a timeout occured.
        """
        return self.__timed_out

    def is_running(self):
        """
        Return True if the command is still running.
        """
        return self._reader.is_alive()

    def error(self):
        """
        Return an instance of Exception if any, else None.

        Actually check for a :class:`TimeoutError` or a
        :class:`ExitCodeError`.
        """
        if self.is_running():
            return None
        if self.__timed_out:
            return TimeoutError("timeout")
        if self.__expected_exit_code is not None and \
                self.__exit_code != self.__expected_exit_code:
            return ExitCodeError('bad exit code: Got %s' % self.__exit_code)

    def wait(self, raise_if_error=True):
        """
        Block and wait until the command is finished or we got a timeout
        error.

        :param raise_if_error: if True, call :meth:`raise_if_error` at
            the end.
        """
        if self._reader.is_alive():
            self._reader.thread.join()
        if raise_if_error:
            self.raise_if_error()
        return self.__exit_code


class ThreadableTask(Task):
    """
    A task ran in a background thread.
    """
    def __init__(self, callable, args, kwargs, finished_callback=None):
        # Set up exception handling
        self.exception = None

        def wrapper(*args, **kwargs):
            try:
                callable(*args, **kwargs)
            except BaseException:
                self.exception = sys.exc_info()
            finally:
                if finished_callback:
                    finished_callback(self)

        # Kick off thread
        name = getattr(callable, '__name__', None)
        thread = threading.Thread(None, wrapper, name, args, kwargs)
        thread.setDaemon(True)
        thread.start()
        # Make thread available to instantiator
        self.thread = thread

    def is_running(self):
        """
        Return True if the thread is still running.
        """
        return self.thread.is_alive()

    def error(self):
        """
        Return an instance of Exception if any, else None.
        """
        return self.exception[1] if self.exception else None

    def raise_if_error(self):
        """
        Check if an error occured and raise it if any.
        """
        if self.exception:
            if six.PY2:
                raise (self.exception[0], self.exception[1], self.exception[2])
            else:
                raise self.exception[1]

    def wait(self, raise_if_error=True):
        """
        Block and wait until the thread is finished.

        :param raise_if_error: if True, call :meth:`raise_if_error` at
            the end.
        """
        if self.thread.is_alive():
            self.thread.join()
        if raise_if_error:
            self.raise_if_error()
