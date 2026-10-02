"""`itertools`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import CallContext, Function, MANY, MODULES, Module, bind_args, iterable_of, spread_items
from ..types import (
    FLOAT, GeneratorType, INT, ListType, OptionalType, STR, TIMEDELTA, TupleType, Type, VarTupleType, assignable,
    element_type, is_numeric, join,
)


def it_bind(ctx: CallContext, params: tuple) -> dict:
    return bind_args(ctx, params)


def literal_int(node: A.Expr | None) -> int | None:
    if isinstance(node, A.IntLit):
        return node.value
    return None


def fixed_or_var_tuple(elem: Type, r: A.Expr | None) -> Type:
    n = literal_int(r)
    return TupleType((elem,) * n) if n is not None and n >= 0 else VarTupleType(elem)


def expect_opt_int(ctx: CallContext, node: A.Expr | None, what: str) -> None:
    if node is not None and not isinstance(node, A.NoneLit):
        ctx.checker.expect_type(node, INT, what)
    elif node is not None:
        ctx.checker.check_expr(node)


def itertools_function(name: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        info: dict = {}
        ctx.call.notes["itertools"] = info
        match name:
            case "count":
                args = it_bind(ctx, (("start", None, True), ("step", None, True)))
                types = [ctx.checker.check_expr(args[k]) for k in ("start", "step") if k in args]
                if any(not is_numeric(t) for t in types):
                    raise ctx.error("count() takes numbers")
                info["args"] = args
                return GeneratorType(FLOAT if FLOAT in types else INT)
            case "cycle" | "pairwise":
                ctx.arity(1)
                elem = iterable_of(ctx, ctx.args[0])
                return GeneratorType(TupleType((elem, elem)) if name == "pairwise" else elem)
            case "repeat":
                args = it_bind(ctx, (("object", None), ("times", None, True)))
                expect_opt_int(ctx, args.get("times"), "repeat() times")
                info["args"] = args
                return GeneratorType(ctx.checker.check_expr(args["object"]))
            case "accumulate":
                args = it_bind(ctx, (("iterable", None), ("func", None, True), ("initial", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                if "func" in args and not isinstance(args["func"], A.NoneLit):
                    result = ctx.function(args["func"], (elem, elem), "func")
                    if not assignable(result, elem):
                        raise ctx.error(f"accumulate() func must return {elem}, not {result}", args["func"])
                elif not (is_numeric(elem) or elem in (STR, TIMEDELTA) or isinstance(elem, ListType)):
                    raise ctx.error(f"accumulate() adds its items, but {elem} values can't be added (pass func=)")
                if "initial" in args and not isinstance(args["initial"], A.NoneLit):
                    ctx.checker.expect_type(args["initial"], elem, "accumulate() initial")
                info["args"] = args
                return GeneratorType(elem)
            case "chain" if (items := spread_items(ctx, "iterable")) is not None:  # chain(*lists)
                return GeneratorType(items)
            case "chain":
                ctx.arity(1, MANY)
                elem = None
                for a in ctx.args:
                    e = iterable_of(ctx, a)
                    elem = e if elem is None else join(elem, e)
                    if elem is None:
                        raise ctx.error("chain() needs iterables of the same kind of item", a)
                return GeneratorType(elem)
            case "chain.from_iterable":
                ctx.arity(1)
                inner = iterable_of(ctx, ctx.args[0])
                elem = element_type(inner)
                if elem is None:
                    raise ctx.error(f"chain.from_iterable() needs an iterable of iterables, not of {inner}", ctx.args[0])
                return GeneratorType(elem)
            case "compress":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[0])
                iterable_of(ctx, ctx.args[1], "selectors")
                return GeneratorType(elem)
            case "dropwhile" | "takewhile" | "filterfalse":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[1])
                result = ctx.function(ctx.args[0], (elem,), "predicate")
                ctx.checker.check_truthy(result, ctx.args[0])
                return GeneratorType(elem)
            case "groupby":
                args = it_bind(ctx, (("iterable", None), ("key", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                key = elem
                if "key" in args and not isinstance(args["key"], A.NoneLit):
                    key = ctx.function(args["key"], (elem,), "key")
                info["args"] = args
                return GeneratorType(TupleType((key, GeneratorType(elem))))
            case "islice":
                ctx.arity(2, 4)
                elem = iterable_of(ctx, ctx.args[0])
                for a in ctx.args[1:]:
                    expect_opt_int(ctx, a, "islice() index")
                return GeneratorType(elem)
            case "starmap":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[1])
                if not isinstance(elem, TupleType):
                    raise ctx.error(f"starmap() needs an iterable of tuples (the arguments), not of {elem}", ctx.args[1])
                result = ctx.function(ctx.args[0], elem.elts, "function")
                return GeneratorType(result)
            case "tee":
                ctx.arity(1, 2)
                elem = iterable_of(ctx, ctx.args[0])
                n = 2
                if len(ctx.args) == 2:
                    n = literal_int(ctx.args[1])
                    if n is None or n < 0:
                        raise ctx.error("tee()'s n must be a number written out (it decides how many you get)", ctx.args[1])
                    ctx.checker.check_expr(ctx.args[1])
                info["n"] = n
                return TupleType((GeneratorType(elem),) * n)
            case "zip_longest":
                ctx.arity(1, MANY, keywords=("fillvalue",))
                elems = [iterable_of(ctx, a) for a in ctx.args]
                fill = ctx.keyword_arg("fillvalue")
                if fill is None or isinstance(fill, A.NoneLit):
                    if fill is not None:
                        ctx.checker.check_expr(fill)
                    info["elems"] = elems
                    return GeneratorType(TupleType(tuple(e if isinstance(e, OptionalType) else OptionalType(e) for e in elems)))
                ft = ctx.checker.check_expr(fill)
                joined = []
                for e in elems:
                    j = join(e, ft)
                    if j is None:
                        raise ctx.error(f"zip_longest() fillvalue must fit every input's items: {e}, not {ft}", fill)
                    joined.append(j)
                info["elems"] = elems
                return GeneratorType(TupleType(tuple(joined)))
            case "product":
                spread = spread_items(ctx, "iterable")
                if spread is None:
                    ctx.arity(1, MANY, keywords=("repeat",))
                    elems = [iterable_of(ctx, a) for a in ctx.args]
                rep = ctx.keyword_arg("repeat")
                n = 1
                if rep is not None:
                    n = literal_int(rep)
                    if n is None or n < 0:
                        raise ctx.error("product()'s repeat must be a number written out (it decides the tuple size)", rep)
                    ctx.checker.check_expr(rep)
                info["repeat"] = n
                if spread is not None:  # product(*lists): tuples as long as lists (times repeat)
                    info["spread"] = True
                    return GeneratorType(VarTupleType(spread))
                return GeneratorType(TupleType(tuple(elems) * n))
            case "permutations":
                args = it_bind(ctx, (("iterable", None), ("r", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                expect_opt_int(ctx, args.get("r"), "permutations() r")
                info["args"] = args
                return GeneratorType(fixed_or_var_tuple(elem, args.get("r")))
            case "combinations" | "combinations_with_replacement":
                args = it_bind(ctx, (("iterable", None), ("r", None)))
                elem = iterable_of(ctx, args["iterable"])
                ctx.checker.expect_type(args["r"], INT, f"{name}() r")
                info["args"] = args
                return GeneratorType(fixed_or_var_tuple(elem, args["r"]))
            case "batched":
                args = it_bind(ctx, (("iterable", None), ("n", None)))
                elem = iterable_of(ctx, args["iterable"])
                ctx.checker.expect_type(args["n"], INT, "batched() n")
                info["args"] = args
                return GeneratorType(VarTupleType(elem))
        raise AssertionError(name)

    return handler


ITERTOOLS = ("count", "cycle", "repeat", "accumulate", "chain", "chain.from_iterable", "compress", "dropwhile",
             "takewhile", "filterfalse", "groupby", "islice", "starmap", "tee", "zip_longest", "product",
             "permutations", "combinations", "combinations_with_replacement", "pairwise", "batched")
MODULES["itertools"] = Module("itertools", {
    name: Function(name, itertools_function(name)) for name in ITERTOOLS
}, "modules/itertools.hpp")
