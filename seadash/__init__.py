"""seadash: Python's syntax, C++'s speed.

When a seadash program runs under python3 instead, `from seadash import ...` finds the Python
versions below, so programs that use them still run. They behave as seadash's do within a
thread; but Python's threads share what they're given where seadash's get copies, so a
program's output only matches when its threads don't change what they were handed.
"""

import copy
import dataclasses
import functools
import threading

__all__ = ["value", "Atomic", "Mutex", "RWMutex", "Synchronized"]


def value(cls):
    """@value: a value type in seadash (copied on assignment). Python has no such thing."""
    return cls


class Atomic:
    """An int that threads can change at once without a lock of their own."""

    def __init__(self, value: int = 0):
        self._value = value
        self._lock = threading.Lock()

    def get(self) -> int:
        return self._value

    def set(self, value: int) -> None:
        with self._lock:
            self._value = value

    def add(self, n: int = 1) -> int:
        """Adds n and returns the new value."""
        with self._lock:
            self._value += n
            return self._value

    def sub(self, n: int = 1) -> int:
        with self._lock:
            self._value -= n
            return self._value

    def compare_and_set(self, expected: int, value: int) -> bool:
        with self._lock:
            if self._value != expected:
                return False
            self._value = value
            return True

    def __repr__(self) -> str:
        return f"Atomic({self._value})"


class Mutex:
    """Owns its value: `with m as data:` holds the lock while the block uses it."""

    def __init__(self, value):
        self._value = value
        self._lock = threading.Lock()

    def __class_getitem__(cls, item):  # Mutex[list[int]]
        return cls

    def __enter__(self):
        self._lock.acquire()
        return self._value

    def __exit__(self, *exc) -> None:
        self._lock.release()

    def get(self):
        """A copy of the value (seadash's is a copy too, so nothing outside the lock shares it)."""
        with self._lock:
            return copy.deepcopy(self._value)

    def set(self, value) -> None:
        with self._lock:
            self._value = value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._value!r})"


class _Held:
    """What RWMutex.read() and write() give: the value, while the lock is held."""

    def __init__(self, mutex: Mutex):
        self._mutex = mutex

    def __enter__(self):
        return Mutex.__enter__(self._mutex)

    def __exit__(self, *exc) -> None:
        Mutex.__exit__(self._mutex, *exc)


class RWMutex(Mutex):
    """`with m.read() as data:` or `with m.write() as data:`. (Here readers take turns: Python
    has no reader-writer lock, and its threads don't run Python code at the same time anyway.)"""

    def __enter__(self):
        raise TypeError("say which: `with m.read() as data:` or `with m.write() as data:`")

    def read(self) -> _Held:
        return _Held(self)

    def write(self) -> _Held:
        return _Held(self)


def _holding_the_lock(method):
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self._sd_lock:
            return method(self, *args, **kwargs)

    return locked


# (Python 3.14 keeps a class's annotations in a function, `__annotate_func__`.)
_NOT_LOCKED = {"__new__", "__init_subclass__", "__annotate__", "__annotate_func__"}


class Synchronized:
    """A class whose methods each hold the object's lock. Like any seadash class with fields and
    no __init__, it's constructed from its fields in order and prints them."""

    def __new__(cls, *args, **kwargs):
        obj = super().__new__(cls)
        obj._sd_lock = threading.RLock()
        return obj

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, member in list(vars(cls).items()):
            if callable(member) and not isinstance(member, type) and name not in _NOT_LOCKED:
                setattr(cls, name, _holding_the_lock(member))
        dataclasses.dataclass(eq=False)(cls)  # (keeps an __init__ or __repr__ the class has)
