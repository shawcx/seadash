"""`tomllib`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from ..builtins import CallContext, MODULES, exception_class, runtime_module
from ..errors import Loc
from ..types import BINARY_FILE, DictType, Field, INT, JSON_VALUE, OptionalType, STR, StructType, TEXT_FILE, Type
from .json import json_problem


def toml_loads_fn(from_file: bool) -> Callable[[CallContext], Type]:
    """tomllib.loads(text) / tomllib.load(file): decodes into the type the context asks for,
    like json.loads, and without one gives the document as a dict[str, json.Value]."""

    def handler(ctx: CallContext) -> Type:
        ctx.arity(1)
        if from_file:
            if ctx.arg(0) == TEXT_FILE:
                raise ctx.error(f"{ctx.what} needs a file opened in binary mode, e.g. `open('config.toml', 'rb')` "
                                f"(Python raises TypeError for a text file)", ctx.args[0])
            ctx.expect(0, BINARY_FILE)
        else:
            ctx.expect(0, STR)
        target = ctx.expected
        if target is None:
            return DictType(STR, JSON_VALUE)
        table = target.inner if isinstance(target, OptionalType) else target
        if not (isinstance(table, DictType) or table == JSON_VALUE
                or (isinstance(table, StructType) and not table.is_exception and table.enum is None)):
            raise ctx.error(f"a TOML document is a table, so {ctx.what} can't produce {target}; "
                            f"annotate a class, a dict[str, ...] or json.Value")
        if problem := json_problem(target, decoding=True, toml=True):
            raise ctx.error(problem)
        return target

    return handler


def toml_decode_error() -> StructType:
    st = exception_class("TOMLDecodeError", "sd::tomllib::TOMLDecodeError", "ValueError")
    for name, t in (("msg", STR), ("doc", STR), ("pos", INT), ("lineno", INT), ("colno", INT)):
        st.fields[name] = Field(name, t, None, Loc(0, 0))
    return st


MODULES["tomllib"] = runtime_module(
    "tomllib", "modules/tomllib.hpp",
    loads=(toml_loads_fn(False), "sd::tomllib::loads<{T}>"),
    load=(toml_loads_fn(True), "sd::tomllib::load<{T}>"),
    TOMLDecodeError=toml_decode_error(),
)
