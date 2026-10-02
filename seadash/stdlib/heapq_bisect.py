"""`heapq` and `bisect`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import CallContext, Function, MANY, MODULES, Module, bind_args, iterable_of, ordered
from ..types import BOOL, GeneratorType, INT, ListType, NONE, OptionalType, StructType, Type, assignable, join


def given(node: A.Expr | None) -> bool:
    return node is not None and not isinstance(node, A.NoneLit)


def list_arg(ctx: CallContext, node: A.Expr) -> Type:
    t = ctx.checker.check_expr(node)
    if not isinstance(t, ListType):
        raise ctx.error(f"{ctx.what} needs a list, not {t}", node)
    return t.elem


def compared_as(ctx: CallContext, key: A.Expr | None, elem: Type, takes_key: bool = True) -> Type:
    """What items are compared as: key(item) with key=, else the item itself."""
    if not given(key):
        if key is not None:
            ctx.checker.check_expr(key)
        if not ordered(elem):
            hints = [f"give {elem.name} a __lt__ method"] if isinstance(elem, StructType) else []
            hints += ["pass key="] if takes_key else []
            raise ctx.error(f"{ctx.what} can't compare {elem} values" + (f" ({', or '.join(hints)})" if hints else ""))
        return elem
    result = ctx.function(key, (elem,), "key")
    if not ordered(result):
        raise ctx.error(f"{ctx.what} key must return something comparable, not {result}", key)
    return result


def expect_item(ctx: CallContext, node: A.Expr, t: Type, what: str, keyed: bool = False) -> None:
    actual = ctx.checker.check_expr(node, t)
    if not assignable(actual, t):
        if keyed:
            raise ctx.error(f"{ctx.what} compares {what} with key(item), so it must be {t}, not {actual}", node)
        raise ctx.error(f"{ctx.what} {what} must be {t} (the list's item type), not {actual}", node)


def heapq_function(name: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        match name:
            case "heapify" | "heappop":
                ctx.arity(1)
                elem = list_arg(ctx, ctx.args[0])
                compared_as(ctx, None, elem, takes_key=False)
                return NONE if name == "heapify" else elem
            case "heappush" | "heappushpop" | "heapreplace":
                ctx.arity(2)
                elem = list_arg(ctx, ctx.args[0])
                compared_as(ctx, None, elem, takes_key=False)
                expect_item(ctx, ctx.args[1], elem, "item")
                return NONE if name == "heappush" else elem
            case "nlargest" | "nsmallest":
                if len(ctx.args) > 3:
                    raise ctx.error(f"{ctx.what} takes from 2 to 3 positional arguments but {len(ctx.args)} were given")
                args = bind_args(ctx, (("n", None), ("iterable", None), ("key", None, True)))
                ctx.checker.expect_type(args["n"], INT, f"{ctx.what} n")
                elem = iterable_of(ctx, args["iterable"])
                compared_as(ctx, args.get("key"), elem)
                ctx.call.notes["lib_args"] = args
                return ListType(elem)
            case "merge":
                if not ctx.args:
                    raise ctx.error(f"{ctx.what} needs at least one iterable to merge")
                ctx.arity(1, MANY, keywords=("key", "reverse"))
                elem = None
                for a in ctx.args:
                    e = iterable_of(ctx, a)
                    elem = e if elem is None else join(elem, e)
                    if elem is None:
                        raise ctx.error(f"{ctx.what} needs iterables of the same kind of item", a)
                compared_as(ctx, ctx.keyword_arg("key"), elem)
                ctx.keyword("reverse", BOOL)
                return GeneratorType(elem)
        raise AssertionError(name)

    return handler


HEAPQ = ("heappush", "heappop", "heapify", "heappushpop", "heapreplace", "nlargest", "nsmallest", "merge")
MODULES["heapq"] = Module("heapq", {name: Function(name, heapq_function(name)) for name in HEAPQ}, "modules/heapq.hpp")


def bisect_function(name: str) -> Callable[[CallContext], Type]:
    """bisect_left(a, x, lo=0, hi=None, *, key=None) and the others: with key=, bisect_* compare
    x with key(item) (x is a key), and insort_* insert the item x where key(x) goes."""
    def handler(ctx: CallContext) -> Type:
        if len(ctx.args) > 4:
            raise ctx.error(f"{ctx.what} takes at most 4 positional arguments ({len(ctx.args)} given)")
        args = bind_args(ctx, (("a", None), ("x", None), ("lo", None, True), ("hi", None, True), ("key", None, True)))
        elem = list_arg(ctx, args["a"])
        cmp = compared_as(ctx, args.get("key"), elem)
        if name.startswith("insort") or not given(args.get("key")):
            expect_item(ctx, args["x"], elem, "x")
        else:
            expect_item(ctx, args["x"], cmp, "x", keyed=True)
        if "lo" in args:
            ctx.checker.expect_type(args["lo"], INT, f"{ctx.what} lo")
        if "hi" in args:
            ctx.checker.expect_type(args["hi"], OptionalType(INT), f"{ctx.what} hi")
        ctx.call.notes["lib_args"] = args
        ctx.call.notes["compared_as"] = cmp
        return NONE if name.startswith("insort") else INT

    return handler


BISECT = ("bisect_left", "bisect_right", "bisect", "insort_left", "insort_right", "insort")
MODULES["bisect"] = Module("bisect", {name: Function(name, bisect_function(name)) for name in BISECT},
                           "modules/bisect.hpp")
for _changer in ("heappush", "heappop", "heapify", "heappushpop", "heapreplace"):
    MODULES["heapq"].members[_changer].mutates_first_arg = True  # (they change the list in place)
for _changer in ("insort_left", "insort_right", "insort"):
    MODULES["bisect"].members[_changer].mutates_first_arg = True
