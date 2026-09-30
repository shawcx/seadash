"""Compile-time knowledge about regular expressions written as string literals.

Python's own parser checks the pattern (so a bad pattern is a compile error with
Python's message) and tells us its groups: how many, their names, and which ones
may not take part in a match -- `(a)?`, one side of `x|y`. That makes
`m.group(1)` a `str` when the group always matches, and `str?` only when it can
really be None.
"""

import re
from re import _constants as C
from re import _parser as P

from .types import RegexInfo

FLAGS: dict[str, int] = {  # re.I, re.MULTILINE, ... as plain ints
    name: int(getattr(re, name))
    for name in ("ASCII", "A", "IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "VERBOSE", "X", "UNICODE", "U", "NOFLAG")
}


def analyze(pattern: str, flags: int) -> RegexInfo:
    """Raises re.error for an invalid pattern."""
    parsed = P.parse(pattern, flags)
    optional: set[int] = set()
    _walk(parsed, False, optional)
    names = tuple(sorted(parsed.state.groupdict.items(), key=lambda kv: kv[1]))
    return RegexInfo(parsed.state.groups - 1, names, frozenset(optional))


def _walk(items, optional: bool, out: set[int]) -> None:
    for op, av in items:
        if op is C.SUBPATTERN:
            group, _, _, sub = av
            if group is not None and optional:
                out.add(group)
            _walk(sub, optional, out)
        elif op in (C.MAX_REPEAT, C.MIN_REPEAT, C.POSSESSIVE_REPEAT):
            lo, _, sub = av
            _walk(sub, optional or lo == 0, out)
        elif op is C.BRANCH:
            for branch in av[1]:
                _walk(branch, True, out)  # only one side of `a|b` matches
        elif op in (C.ASSERT, C.ASSERT_NOT):
            _walk(av[1], optional or op is C.ASSERT_NOT, out)
        elif op is C.GROUPREF_EXISTS:
            _, yes, no = av
            _walk(yes, True, out)
            if no is not None:
                _walk(no, True, out)
        elif op is C.ATOMIC_GROUP:
            _walk(av, optional, out)
