"""`json`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, MODULES, NamedType, bytes_like, exception_class, module_with_params, runtime_module, signature,
)
from ..types import (
    BOOL, DATE, DATETIME, DictType, FLOAT, INT, JSON_VALUE, ListType, NONE, OptionalType, STR, SetType, StructType,
    TEXT_FILE, TIME, TupleType, Type,
)


def json_problem(t: Type, decoding: bool, seen: frozenset = frozenset(), toml: bool = False) -> str | None:
    """Why `t` can't be converted to/from JSON (or, with toml, decoded from TOML, which
    has dates and times too), or None if it can."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, JSON_VALUE):
            return None
        case _ if toml and t in (DATE, TIME, DATETIME):
            return None
        case ListType(elem) | SetType(elem) | OptionalType(elem):
            return json_problem(elem, decoding, seen, toml)
        case TupleType(elts):
            return next((p for e in elts if (p := json_problem(e, decoding, seen, toml))), None)
        case DictType(key, value):
            keys_ok = key == STR if decoding else key in (STR, INT, FLOAT, BOOL)
            if not keys_ok:
                return (f"{'TOML table' if toml else 'JSON object'} keys are strings, "
                        f"so {t} can't be {'decoded' if decoding else 'encoded'}")
            return json_problem(value, decoding, seen, toml)
        case StructType() if t.enum is not None:
            return f"{t} is an enum; convert its members with .value (and back with {t}(value))"
        case StructType() if not t.is_exception:
            if t in seen:
                return None
            for f in t.all_fields().values():
                if p := json_problem(f.type, decoding, seen | {t}, toml):
                    return p
            return None
    if toml:
        return f"{t} can't be decoded from TOML"
    return f"{t} can't be converted to or from JSON"


def json_loads_fn(from_file: bool) -> Callable[[CallContext], Type]:
    """json.loads(text) / json.load(file): the result type comes from the context."""

    def handler(ctx: CallContext) -> Type:
        ctx.arity(1)
        if from_file:
            ctx.expect(0, TEXT_FILE)
        else:
            ctx.need(0, bytes_like, "str or bytes")
        target = ctx.expected
        if target is None:
            raise ctx.error(
                f"{ctx.what} needs to know what type to produce; annotate the variable, e.g. "
                f"`data: dict[str, int] = ...`, or use `json.Value` for any JSON"
            )
        if problem := json_problem(target, decoding=True):
            raise ctx.error(problem)
        return target

    return handler


def json_dumps_fn(to_file: bool) -> Callable[[CallContext], Type]:
    options = (
        ("indent", OptionalType(INT), "std::nullopt"),
        ("sort_keys", BOOL, "false"),
        ("ensure_ascii", BOOL, "true"),
        ("separators", OptionalType(TupleType((STR, STR))), "std::nullopt"),
    )
    params = (("obj", None),) + ((("fp", TEXT_FILE),) if to_file else ()) + options

    def handler(ctx: CallContext) -> Type:
        if not ctx.args:
            raise ctx.error(f"{ctx.what} is missing argument 'obj'")
        obj = ctx.arg(0)
        if problem := json_problem(obj, decoding=False):
            raise ctx.error(problem, ctx.args[0])
        rest = signature(NONE, *params[1:])
        shifted = CallContext(ctx.checker, A.Call(ctx.call.func, ctx.args[1:], ctx.call.keywords, loc=ctx.call.loc),
                              ctx.what, None)
        rest(shifted)
        return NONE if to_file else STR

    handler.params = params
    return handler


MODULES["json"] = module_with_params(runtime_module(
    "json", "modules/json.hpp",
    loads=(json_loads_fn(False), "sd::json::loads<{T}>"),
    load=(json_loads_fn(True), "sd::json::load<{T}>"),
    dumps=(json_dumps_fn(False), "sd::json::dumps"),
    dump=(json_dumps_fn(True), "sd::json::dump"),
    JSONDecodeError=exception_class("JSONDecodeError", "sd::json::JSONDecodeError", "ValueError"),
))
MODULES["json"].members["Value"] = NamedType("Value", JSON_VALUE)
