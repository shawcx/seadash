"""`struct`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, exception_class, MANY, module_type, module_with_params, MODULES, runtime_module, signature,
)
from ..types import assignable, BOOL, BYTES, FLOAT, GeneratorType, INT, STR, StructFormatType, TupleType, Type


STRUCT_CODES = {**{c: INT for c in "bBhHiIlLqQnNP"}, "?": BOOL, "e": FLOAT, "f": FLOAT, "d": FLOAT, "c": BYTES, "s": BYTES, "p": BYTES}


def struct_types(fmt: str) -> list[Type]:
    """The value types a format packs/unpacks, in order (the runtime checks the same rules)."""
    i = 0
    native = True
    if fmt and fmt[0] in "@=<>!":
        native = fmt[0] == "@"
        i = 1
    out: list[Type] = []
    while i < len(fmt):
        if fmt[i].isspace():
            i += 1
            continue
        count = 1
        if fmt[i].isdigit():
            start = i
            while i < len(fmt) and fmt[i].isdigit():
                i += 1
            count = int(fmt[start:i])
            if i >= len(fmt):
                raise ValueError("repeat count given without format specifier")
        c = fmt[i]
        i += 1
        if c == "x":
            continue
        if c not in STRUCT_CODES or (not native and c in "nNP"):
            raise ValueError("bad char in struct format")
        out.extend([BYTES] if c in "sp" else [STRUCT_CODES[c]] * count)
    return out


def struct_format(ctx: CallContext) -> list[Type]:
    """The format literal of a struct call (its first argument), as value types."""
    if isinstance(ctx.receiver, StructFormatType):
        fmt = ctx.receiver.fmt
    else:
        node = ctx.args[0] if ctx.args else ctx.keyword_arg("format")
        if not isinstance(node, A.StrLit):
            raise ctx.error(f"{ctx.what} needs its format as a string literal (it decides the types)", node)
        ctx.checker.check_expr(node)
        fmt = node.value
    try:
        return struct_types(fmt)
    except ValueError as e:
        raise ctx.error(str(e), ctx.args[0] if ctx.args else None)


def struct_pack(ctx: CallContext) -> Type:
    ctx.arity(0 if isinstance(ctx.receiver, StructFormatType) else 1, MANY)
    wanted = struct_format(ctx)
    values = ctx.args[0 if isinstance(ctx.receiver, StructFormatType) else 1:]
    if len(values) != len(wanted):
        raise ctx.error(f"pack expected {len(wanted)} items for packing (got {len(values)})")
    for node, t in zip(values, wanted):
        actual = ctx.checker.check_expr(node, t)
        ok = assignable(actual, t) or (t == FLOAT and actual == INT) or (t == BOOL and actual == INT) or (t == INT and actual == BOOL)
        if not ok:
            raise ctx.error(f"{ctx.what}: this value must be {t}, not {actual}", node)
    ctx.call.notes["struct_args"] = wanted
    return BYTES


def struct_unpack(kind: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        on_object = isinstance(ctx.receiver, StructFormatType)
        lo = 1 if on_object else 2
        n = ctx.arity(lo, lo + 1 if kind == "unpack_from" else lo, keywords=("offset",) if kind == "unpack_from" else ())
        wanted = struct_format(ctx)
        ctx.expect(lo - 1, BYTES)
        if kind == "unpack_from":
            if n == lo + 1:
                ctx.expect(lo, INT)
            else:
                ctx.keyword("offset", INT)
        row = TupleType(tuple(wanted))
        ctx.call.notes["struct_row"] = row
        return GeneratorType(row) if kind == "iter_unpack" else row

    return handler


def struct_new(ctx: CallContext) -> Type:
    ctx.arity(1)
    node = ctx.args[0]
    if not isinstance(node, A.StrLit):
        raise ctx.error("Struct() needs its format as a string literal (it decides the types)", node)
    ctx.checker.check_expr(node)
    try:
        struct_types(node.value)
    except ValueError as e:
        raise ctx.error(str(e), node)
    return StructFormatType(node.value)


STRUCT_METHODS = {
    "pack": struct_pack,
    "unpack": struct_unpack("unpack"),
    "unpack_from": struct_unpack("unpack_from"),
    "iter_unpack": struct_unpack("iter_unpack"),
}
MODULES["struct"] = module_with_params(runtime_module(
    "struct", "modules/struct.hpp",
    pack=(struct_pack, None),
    unpack=(struct_unpack("unpack"), None),
    unpack_from=(struct_unpack("unpack_from"), None),
    iter_unpack=(struct_unpack("iter_unpack"), None),
    calcsize=(signature(INT, ("format", STR)), "sd::structmod::calcsize"),
    Struct=(struct_new, None),
    error=exception_class("error", "sd::structmod::error"),
))
module_type(StructFormatType, methods=STRUCT_METHODS, attributes={"size": lambda t: INT, "format": lambda t: STR})
