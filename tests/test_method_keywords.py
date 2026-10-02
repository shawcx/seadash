"""The built-in types' methods take the keywords Python's own do: for each method seadash has, a
keyword Python accepts isn't refused, and one it refuses (a positional-only parameter, or any keyword
for a method that takes none) is refused too.

Python's answer comes from the method's signature, or (where it has none) from calling it with a
keyword. Arguments seadash doesn't support yet (str.encode's `errors`) are listed in UNSUPPORTED."""

import collections
import inspect
import sys

import pytest

from seadash import builtins
from seadash.checker import check
from seadash.errors import CheckError
from seadash.parser import parse

TYPES = {
    str: ('"a"', builtins.STR_METHODS),
    bytes: ('b"a"', builtins.BYTES_METHODS),
    bytearray: ('bytearray(b"a")', builtins.BYTEARRAY_METHODS),
    list: ("[1]", builtins.LIST_METHODS),
    dict: ("{1: 1}", builtins.DICT_METHODS),
    set: ("{1}", builtins.SET_METHODS),
    tuple: ("(1,)", builtins.TUPLE_METHODS),
    int: ("(1)", builtins.INT_METHODS),
    float: ("(1.0)", builtins.FLOAT_METHODS),
    collections.deque: ("collections.deque([1])", builtins.DEQUE_METHODS),
    collections.Counter: ("collections.Counter([1])", builtins.COUNTER_METHODS),
}
# Methods taking any keywords (d.update(a=1)), and seadash's own for `del`.
SKIP = {"str.format", "str.format_map", "dict.update", "Counter.update", "Counter.subtract"}
# Keywords Python takes that its methods' signatures don't show, or that a newer Python added.
KEYWORDS = {"bytes.hex": {"sep", "bytes_per_sep"}, "bytearray.hex": {"sep", "bytes_per_sep"}}
NEWER = {"str.replace": ((3, 13), {"count"})}
# Keywords Python takes that seadash doesn't support yet.
UNSUPPORTED = {"str.encode": {"errors"}, "bytes.decode": {"errors"}, "bytearray.decode": {"errors"},
               "bytes.hex": {"sep", "bytes_per_sep"}, "bytearray.hex": {"sep", "bytes_per_sep"}}

CASES = [(cls, name) for cls, (_, table) in TYPES.items() for name in sorted(table)
         if f"{cls.__name__}.{name}" not in SKIP and not name.startswith("__")]


def python_keywords(cls: type, name: str) -> set[str]:
    """The names Python's method takes as keywords."""
    qualified = f"{cls.__name__}.{name}"
    if qualified in NEWER:
        return NEWER[qualified][1]
    if qualified in KEYWORDS:
        return KEYWORDS[qualified]
    method = getattr(cls, name)
    try:
        params = inspect.signature(method).parameters.values()
    except ValueError:  # no signature: a method taking no keywords says so
        with pytest.raises(TypeError, match="takes no keyword arguments"):
            getattr(cls(), name)(probe=1)
        return set()
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return {p.name for p in params if p.kind in kinds and p.name != "self"}


def positional_only(cls: type, name: str) -> list[str]:
    try:
        params = inspect.signature(getattr(cls, name)).parameters.values()
    except ValueError:
        return []
    return [p.name for p in params if p.kind == inspect.Parameter.POSITIONAL_ONLY and p.name != "self"]


def keyword_error(receiver: str, call: str) -> str | None:
    """The checker's complaint about a keyword in `receiver.call`, if any (other errors don't count)."""
    try:
        check(parse(f"import collections\nx = {receiver}\nx.{call}\n"))
    except CheckError as e:
        if "keyword" in e.message:
            return e.message
    return None


@pytest.mark.parametrize("cls, name", CASES, ids=[f"{c.__name__}.{n}" for c, n in CASES])
def test_keywords_match_python(cls, name):
    qualified = f"{cls.__name__}.{name}"
    if qualified in NEWER and sys.version_info < NEWER[qualified][0]:
        pytest.skip(f"Python {'.'.join(map(str, NEWER[qualified][0]))} changed it")
    receiver = TYPES[cls][0]
    keywords = python_keywords(cls, name) - UNSUPPORTED.get(qualified, set())
    for kw in sorted(keywords):
        assert keyword_error(receiver, f"{name}({kw}=x)") is None, f"{qualified}() takes {kw}= in Python"
    if not python_keywords(cls, name):
        assert keyword_error(receiver, f"{name}(probe=1)") is not None, f"{qualified}() takes no keywords in Python"
    declared = [p[0] for p in getattr(TYPES[cls][1][name], "params", ())]  # (seadash's names for its parameters)
    for kw in dict.fromkeys(positional_only(cls, name) + declared):
        if kw not in python_keywords(cls, name):
            assert keyword_error(receiver, f"{name}({kw}=x)") is not None, f"{qualified}() takes no {kw}= in Python"
