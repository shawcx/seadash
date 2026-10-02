"""`sqlite3`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, exception_class, module_with_params, MODULES, NamedType, PATH_LIKE, runtime_module, signature,
    sync_method,
)
from ..types import (
    BOOL, BYTES, DictType, FLOAT, INT, ListType, NONE, OptionalType, SQLITE_CONNECTION, SQLITE_CURSOR, STR,
    StructType, TupleType, Type, VarTupleType,
)


SQLITE_VALUE_TYPES = (INT, FLOAT, STR, BYTES, BOOL)


def sqlite_value(t: Type) -> bool:
    return t in SQLITE_VALUE_TYPES or t == NONE or (isinstance(t, OptionalType) and t.inner in SQLITE_VALUE_TYPES)


def sqlite_parameters(ctx: CallContext, node: A.Expr, what: str) -> None:
    """execute()'s parameters: a tuple (any mix), a list, or a dict of :names, holding
    int, float, str, bytes, bool or None."""
    t = ctx.checker.check_expr(node)
    ok = (isinstance(t, TupleType) and all(sqlite_value(e) for e in t.elts)) or \
        (isinstance(t, (ListType, VarTupleType)) and sqlite_value(t.elem)) or \
        (isinstance(t, DictType) and t.key == STR and sqlite_value(t.value))
    if not ok:
        raise ctx.error(f"{what} must be a tuple, list or dict of int, float, str, bytes, bool or None values, not {t}", node)


def sqlite_execute(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("parameters",))
    ctx.expect(0, STR)
    node = ctx.args[1] if n == 2 else ctx.keyword_arg("parameters")
    if node is not None:
        sqlite_parameters(ctx, node, f"{ctx.what} parameters")
    return SQLITE_CURSOR


def sqlite_executemany(ctx: CallContext) -> Type:
    ctx.arity(2)
    ctx.expect(0, STR)
    t = ctx.checker.check_expr(ctx.args[1])
    if not isinstance(t, ListType):
        raise ctx.error(f"{ctx.what} takes a list of parameter tuples, not {t}", ctx.args[1])
    elem = t.elem
    ok = (isinstance(elem, TupleType) and all(sqlite_value(e) for e in elem.elts)) or \
        (isinstance(elem, (ListType, VarTupleType)) and sqlite_value(elem.elem)) or \
        (isinstance(elem, DictType) and elem.key == STR and sqlite_value(elem.value))
    if not ok:
        raise ctx.error(f"{ctx.what} takes a list of tuples (or lists, or dicts) of int, float, str, bytes, bool or None values, not {t}", ctx.args[1])
    return SQLITE_CURSOR


def sqlite_fetch(kind: str) -> Callable[[CallContext], Type]:
    """fetchone() / fetchmany(size) / fetchall(): the row type comes from the context, as
    with json.loads: `row: tuple[int, str] | None = cur.fetchone()`."""

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(0, 1 if kind == "many" else 0, keywords=("size",) if kind == "many" else ())
        if kind == "many":
            if n == 1:
                ctx.expect(0, INT)
            else:
                ctx.keyword("size", INT)
        target = ctx.expected
        row = None
        if kind == "one" and isinstance(target, OptionalType):
            row = target.inner
        elif kind == "one" and isinstance(target, TupleType):
            row = target
        elif kind != "one" and isinstance(target, ListType):
            row = target.elem
        example = "row: tuple[int, str] | None = cur.fetchone()" if kind == "one" else f"rows: list[tuple[int, str]] = cur.fetch{kind}()"
        if not isinstance(row, TupleType):
            raise ctx.error(f"{ctx.what} needs to know the row type; annotate the variable, e.g. `{example}`")
        for elt in row.elts:
            if not (elt in SQLITE_VALUE_TYPES or (isinstance(elt, OptionalType) and elt.inner in SQLITE_VALUE_TYPES)):
                raise ctx.error(f"a row column is int, float, str, bytes or bool (or one of those | None), not {elt}")
        ctx.call.notes["sqlite_row"] = row
        return OptionalType(row) if kind == "one" else ListType(row)

    return handler


SQLITE_CURSOR.methods.update({
    "execute": sqlite_execute,
    "executemany": sqlite_executemany,
    "executescript": sync_method(SQLITE_CURSOR, ("sql_script", STR)),
    "fetchone": sqlite_fetch("one"),
    "fetchmany": sqlite_fetch("many"),
    "fetchall": sqlite_fetch("all"),
    "close": sync_method(NONE),
})
SQLITE_CONNECTION.methods.update({
    "cursor": sync_method(SQLITE_CURSOR),
    "execute": sqlite_execute,
    "executemany": sqlite_executemany,
    "executescript": sync_method(SQLITE_CURSOR, ("sql_script", STR)),
    "commit": sync_method(NONE),
    "rollback": sync_method(NONE),
    "close": sync_method(NONE),
})
SQLITE_CONNECTION.attributes.update(
    {"in_transaction": lambda t: BOOL, "total_changes": lambda t: INT, "isolation_level": lambda t: OptionalType(STR)})
SQLITE_CURSOR.attributes.update({
    "rowcount": lambda t: INT, "lastrowid": lambda t: OptionalType(INT),
    "description": lambda t: OptionalType(ListType(TupleType((STR, NONE, NONE, NONE, NONE, NONE, NONE)))),
})
SQLITE_ERRORS: dict[str, StructType] = {}


def sqlite_exception(name: str, base: str) -> StructType:
    st = exception_class(name, f"sd::sqlite3::{name}", SQLITE_ERRORS.get(base) or base)
    SQLITE_ERRORS[name] = st
    return st


MODULES["sqlite3"] = module_with_params(runtime_module(
    "sqlite3", "modules/sqlite3.hpp", ("sqlite3",),
    connect=(signature(SQLITE_CONNECTION, ("database", PATH_LIKE), ("timeout", FLOAT, "5.0"),
                       ("isolation_level", OptionalType(STR), "std::string()")), "sd::sqlite3::connect"),
    Connection=NamedType("Connection", SQLITE_CONNECTION),
    Cursor=NamedType("Cursor", SQLITE_CURSOR),
    sqlite_version=(STR, "sd::sqlite3::version()"),
    Warning=sqlite_exception("Warning", "Exception"),
    Error=sqlite_exception("Error", "Exception"),
    InterfaceError=sqlite_exception("InterfaceError", "Error"),
    DatabaseError=sqlite_exception("DatabaseError", "Error"),
    DataError=sqlite_exception("DataError", "DatabaseError"),
    OperationalError=sqlite_exception("OperationalError", "DatabaseError"),
    IntegrityError=sqlite_exception("IntegrityError", "DatabaseError"),
    InternalError=sqlite_exception("InternalError", "DatabaseError"),
    ProgrammingError=sqlite_exception("ProgrammingError", "DatabaseError"),
    NotSupportedError=sqlite_exception("NotSupportedError", "DatabaseError"),
))
