"""`csv`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    CallContext, MODULES, bind_args, exception_class, iterable_of, mark_tuple_iterable, module_with_params,
    printable, runtime_module, sync_method,
)
from ..types import (
    BOOL, CSV_DICT_READER, CSV_DICT_WRITER, CSV_WRITER, DictType, GeneratorType, INT, ListType, NONE, OptionalType,
    STR, TEXT_FILE, TupleType, Type, element_type,
)


CSV_FORMAT = {"dialect": STR, "delimiter": STR, "quotechar": OptionalType(STR), "escapechar": OptionalType(STR),
              "doublequote": BOOL, "skipinitialspace": BOOL, "lineterminator": STR, "quoting": INT, "strict": BOOL}


def csv_format(ctx: CallContext, allowed: dict) -> dict:
    kw = {}
    for k in ctx.call.keywords:
        if k.name not in allowed and k.name not in CSV_FORMAT:
            raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{k.name}'", k)
        kw[k.name] = k.value
    for name, t in CSV_FORMAT.items():
        if name in kw:
            ctx.checker.expect_type(kw[name], t, name)
    return kw


def csv_lines(ctx: CallContext, node: A.Expr) -> None:
    elem = iterable_of(ctx, node, "csvfile")
    if elem != STR:
        raise ctx.error(f"{ctx.what} reads lines of text (a file opened in text mode, or strs), not {elem}", node)


def csv_reader(ctx: CallContext) -> Type:
    if len(ctx.args) not in (1, 2):
        raise ctx.error(f"{ctx.what} takes a file (or lines) and an optional dialect")
    csv_lines(ctx, ctx.args[0])
    if len(ctx.args) == 2:
        ctx.expect(1, STR)
    ctx.call.notes["csv"] = csv_format(ctx, {})
    return GeneratorType(ListType(STR))


def csv_writer(ctx: CallContext) -> Type:
    if len(ctx.args) not in (1, 2):
        raise ctx.error(f"{ctx.what} takes a file and an optional dialect")
    ctx.expect(0, TEXT_FILE)
    if len(ctx.args) == 2:
        ctx.expect(1, STR)
    ctx.call.notes["csv"] = csv_format(ctx, {})
    return CSV_WRITER


def csv_dict_reader(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("f", None), ("fieldnames", None, True), ("restkey", None, True), ("restval", None, True),
                           ("dialect", STR, True), *((k, t, True) for k, t in CSV_FORMAT.items() if k != "dialect")))
    csv_lines(ctx, args["f"])
    if "fieldnames" in args and not isinstance(args["fieldnames"], A.NoneLit):
        if iterable_of(ctx, args["fieldnames"], "fieldnames") != STR:
            raise ctx.error("fieldnames must be strs", args["fieldnames"])
    if "restkey" in args and not isinstance(args["restkey"], A.NoneLit):
        raise ctx.error("restkey isn't supported: seadash's DictReader rows are dict[str, str] (extra fields are dropped)",
                        args["restkey"])
    if "restval" in args and not isinstance(args["restval"], A.NoneLit):
        ctx.checker.expect_type(args["restval"], STR, "restval")
    for name, t in CSV_FORMAT.items():
        if name in args:
            ctx.checker.expect_type(args[name], t, name)
    ctx.call.notes["csv"] = args
    return CSV_DICT_READER


def csv_dict_writer(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("f", None), ("fieldnames", None), ("restval", None, True), ("extrasaction", STR, True),
                           ("dialect", STR, True), *((k, t, True) for k, t in CSV_FORMAT.items() if k != "dialect")))
    ctx.checker.expect_type(args["f"], TEXT_FILE, "DictWriter file")
    if iterable_of(ctx, args["fieldnames"], "fieldnames") != STR:
        raise ctx.error("fieldnames must be strs", args["fieldnames"])
    if "restval" in args:
        ctx.checker.check_printable(ctx.checker.check_expr(args["restval"]), args["restval"])
    for name, t in (*CSV_FORMAT.items(), ("extrasaction", STR)):
        if name in args:
            ctx.checker.expect_type(args[name], t, name)
    ctx.call.notes["csv"] = args
    return CSV_DICT_WRITER


def csv_row(ctx: CallContext, node: A.Expr) -> None:
    t = ctx.checker.check_expr(node)
    if isinstance(t, TupleType):
        for e, et in zip(node.elts if isinstance(node, A.TupleLit) else [node] * len(t.elts), t.elts):
            ctx.checker.check_printable(et, e)
        return
    elem = element_type(t)
    if elem is None or not printable(elem) or t == STR:
        raise ctx.error(f"a row is a list or tuple of values, not {t}", node)
    mark_tuple_iterable(node, t, elem)


def csv_writerow(ctx: CallContext) -> Type:
    ctx.arity(1)
    csv_row(ctx, ctx.args[0])
    return INT


def csv_writerows(ctx: CallContext) -> Type:
    ctx.arity(1)
    rows = ctx.checker.check_expr(ctx.args[0])
    row = element_type(rows)
    if row is None or not (isinstance(row, TupleType) or element_type(row) is not None) or row == STR:
        raise ctx.error(f"writerows() needs rows (lists or tuples of values), not {rows}", ctx.args[0])
    return NONE


def csv_dict_writerow(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if not (isinstance(t, DictType) and t.key == STR and printable(t.value)):
        raise ctx.error(f"DictWriter.writerow() needs a dict with str keys, not {t}", ctx.args[0])
    return INT


def csv_dict_writerows(ctx: CallContext) -> Type:
    ctx.arity(1)
    row = iterable_of(ctx, ctx.args[0])
    if not (isinstance(row, DictType) and row.key == STR):
        raise ctx.error(f"DictWriter.writerows() needs dicts with str keys, not {row}", ctx.args[0])
    return NONE


MODULES["csv"] = module_with_params(runtime_module(
    "csv", "modules/csv.hpp",
    reader=(csv_reader, None),
    writer=(csv_writer, None),
    DictReader=(csv_dict_reader, None),
    DictWriter=(csv_dict_writer, None),
    Error=exception_class("Error", "sd::csv::Error"),
    **{name: (INT, f"sd::csv::{name}") for name in ("QUOTE_MINIMAL", "QUOTE_ALL", "QUOTE_NONNUMERIC", "QUOTE_NONE",
                                                   "QUOTE_STRINGS", "QUOTE_NOTNULL")},
))
for _name, _t in (("writer", CSV_WRITER), ("DictReader", CSV_DICT_READER), ("DictWriter", CSV_DICT_WRITER)):
    MODULES["csv"].members[_name].as_type = _t
CSV_WRITER.methods.update({"writerow": csv_writerow, "writerows": csv_writerows})
CSV_DICT_WRITER.methods.update({"writerow": csv_dict_writerow, "writerows": csv_dict_writerows,
                                "writeheader": sync_method(INT)})
CSV_DICT_READER.attributes["fieldnames"] = lambda t: ListType(STR)
