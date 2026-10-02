"""`dataclasses`, `functools` and `seadash` (decorators and their helpers): how the checker types it, and what codegen calls."""

from __future__ import annotations

from dataclasses import dataclass

from .. import ast as A
from ..builtins import (
    CallContext, DecoratorName, FUNCTIONS, Function, MODULES, Module, SyncTypeDef, strip_optional_type,
)
from ..types import (
    BOOL, ClassRefType, CmpKeyType, FuncInfo, FuncType, Param, Signature, StructType, Type, assignable, is_numeric,
)
from .threading import SYNCHRONIZED
from .heapq_bisect import given


def dataclasses_replace(ctx: CallContext) -> Type:
    """dataclasses.replace(obj, field=value, ...): a copy of obj with those fields changed
    (the way to change a frozen dataclass)."""
    if len(ctx.args) != 1:
        raise ctx.error(f"replace() takes the object, then fields as keywords: replace(obj, x=1) ({len(ctx.args)} given)")
    t = ctx.arg(0)
    if not isinstance(t, StructType) or t.builtin or t.is_exception:
        raise ctx.error(f"replace() needs a dataclass or @value class instance, not {t}", ctx.args[0])
    if any(a.builtin and a.name == "Synchronized" for a in t.ancestors()):
        raise ctx.error(f"replace() can't copy {t.name}: a Synchronized object's lock can't be copied", ctx.args[0])
    if t.kind == "class" and any("__init__" in a.methods for a in t.ancestors() if not a.builtin):
        raise ctx.error(f"replace() rebuilds an object from its fields, but {t.name} has its own __init__", ctx.args[0])
    fields = t.all_fields()
    for kw in ctx.call.keywords:
        f = fields.get(kw.name)
        if f is None:
            raise ctx.error(f"{t.name} has no field '{kw.name}'", kw)
        vt = ctx.checker.check_expr(kw.value, f.type)
        if not assignable(vt, f.type):
            raise ctx.error(f"field '{kw.name}' is {f.type}, not {vt}", kw.value)
    return t


MODULES["dataclasses"] = Module("dataclasses", {
    "dataclass": DecoratorName("dataclass"),
    "field": DecoratorName("field"),
    "replace": Function("replace", dataclasses_replace),
})
# seadash's own: `@value class Point:` makes a value type (copied on assignment, like an int).
MODULES["seadash"] = Module("seadash", {
    "value": DecoratorName("value"),
    **{kind: SyncTypeDef(kind) for kind in ("Atomic", "Mutex", "RWMutex")},  # thread-safe sharing
    "Synchronized": SYNCHRONIZED,
})  # (no header of its own: its thread types need threading's, but @value needs nothing)


def functools_reduce(ctx: CallContext) -> Type:
    """reduce(f, items[, initial]): the running value starts as initial (or the first item)
    and becomes f(running, item) for each item."""
    if ctx.call.keywords:
        raise ctx.error("reduce() takes no keyword arguments", ctx.call.keywords[0])
    n = ctx.arity(2, 3)
    elem = ctx.iterable(1)
    acc = ctx.arg(2, ctx.expected) if n == 3 else elem
    result = ctx.function(ctx.args[0], (acc, elem), "function")
    if not assignable(result, acc):
        start = "an initial value" if n == 2 else "an initial value of that type"
        raise ctx.error(f"reduce()'s function returns {result}, but the running value is {acc}: give it {start} "
                        f"(e.g. reduce(f, items, 0.0))", ctx.args[0])
    return acc


def functools_cmp_to_key(ctx: CallContext) -> Type:
    """cmp_to_key(cmp): a key function for sorted/min/max/list.sort from an old-style
    comparison, cmp(a, b) < 0 when a comes first."""
    ctx.arity(1)
    expected = ctx.expected
    if isinstance(expected, FuncType) and len(expected.params) == 1:
        elem = expected.params[0]
    else:
        t = ctx.arg(0)
        if not (isinstance(t, FuncType) and len(t.params) == 2 and t.params[0] == t.params[1]):
            raise ctx.error(f"cmp_to_key() needs a function comparing two values of one type, like (int, int) -> int, "
                            f"not {t}", ctx.args[0])
        elem = t.params[0]
    result = ctx.function(ctx.args[0], (elem, elem), "function")
    if not is_numeric(result) and result != BOOL:
        raise ctx.error(f"cmp_to_key()'s function must return a number (negative, zero or positive), not {result}",
                        ctx.args[0])
    return FuncType((elem,), CmpKeyType(elem))


@dataclass
class PartialInfo:
    """What functools.partial(func, *args, **keywords) binds, for codegen and the thread checker."""

    target: object  # FuncInfo (a def or bound method), StructType (a class: its constructor), or None (a function value)
    params: list  # the callee's parameters (types.Param)
    bound: list  # per parameter: the expression bound to it, or None
    taken: list  # the parameters the result takes, in order (indexes into params)
    sent: bool = False  # it runs on another thread: what it holds is copied over (threads.Spawn)


def functools_partial(ctx: CallContext) -> Type:
    """partial(func, *args, **keywords): func with its first arguments, and any named ones,
    already given (evaluated now, as in Python). The result takes the rest, keeping their names
    and defaults (see FuncType.sig)."""
    if not ctx.args:
        raise ctx.error("partial() needs the function to call")
    func, given = ctx.args[0], ctx.args[1:]
    checker = ctx.checker
    target = None
    if isinstance(func, A.Name) and func.id not in checker.state.names and (st := checker.lookup_struct(func.id)) is not None:
        target, params = st, checker.constructor_params(st, func)  # partial(Point, y=0)
        func.sym, func.ty = st, ClassRefType(st)
        ret: Type = st
    else:
        if isinstance(func, A.Name) and func.id in FUNCTIONS and func.id not in checker.state.names or (
                isinstance(func, A.Attribute) and isinstance(func.value, A.Name) and func.value.id in checker.modules
                and isinstance(checker.modules[func.value.id].members.get(func.attr), Function)):
            raise ctx.error("partial() of a built-in or library function isn't supported yet; use a lambda, "
                            "e.g. `lambda x: print(x, end='')`", func)
        hint = None
        expected = strip_optional_type(ctx.expected) if ctx.expected is not None else None
        if isinstance(func, A.Lambda) and isinstance(expected, FuncType) and not ctx.call.keywords:
            # partial(lambda k, x: ..., 3) where a (int) -> ... is expected: the lambda takes (int, int)
            hint = FuncType(tuple(checker.check_expr(g) for g in given) + expected.params, None)
        t = checker.check_expr(func, hint)
        if not isinstance(t, FuncType):
            raise ctx.error(f"partial() needs a function or a class, not {t}", func)
        if isinstance(func.sym, FuncInfo):
            target, params, ret = func.sym, func.sym.params, func.sym.ret
        else:  # a function value: its parameters have no names or defaults
            params, ret = [Param(f"argument {i + 1}", p, None, func.loc) for i, p in enumerate(t.params)], t.ret
    if any(p.star or p.double_star for p in params):
        raise ctx.error("partial() of a function with *args or **kwargs isn't supported yet; use a lambda", func)
    what = f"partial({func.id if isinstance(func, A.Name) else 'f'})"
    if len(given) > len(params):
        raise ctx.error(f"{what} gives {len(given)} arguments, but the function takes {len(params)}", given[len(params)])
    bound: list = [*given, *([None] * (len(params) - len(given)))]
    names = {p.name: i for i, p in enumerate(params)}
    for kw in ctx.call.keywords:
        if target is None:
            raise ctx.error("partial() of a function value can't bind keywords (its parameters have no names here)", kw)
        if kw.name not in names:
            raise ctx.error(f"{what}: the function has no parameter '{kw.name}'", kw)
        if bound[names[kw.name]] is not None:
            raise ctx.error(f"{what} got multiple values for argument '{kw.name}'", kw)
        bound[names[kw.name]] = kw.value
    for p, arg in zip(params, bound):
        if arg is not None:
            actual = checker.check_expr(arg, p.type)
            if not assignable(actual, p.type):
                raise ctx.error(f"{what}: argument '{p.name}' must be {p.type}, not {actual}", arg)
    # The result takes the rest, with their names and defaults; as in Python, those after one bound by
    # keyword can then only be passed by keyword (a positional one would land on the bound parameter).
    first_keyword = min((names[kw.name] for kw in ctx.call.keywords), default=len(params))
    taken = [i for i, a in enumerate(bound) if a is None]
    ctx.call.notes["partial"] = PartialInfo(target, params, bound, taken)
    rest = [Param(params[i].name, params[i].type, params[i].default, params[i].loc, False,
                  "kwonly" if i > first_keyword else params[i].kind) for i in taken]
    name = func.id if isinstance(func, A.Name) else func.attr if isinstance(func, A.Attribute) else "partial"
    sig = Signature(f"{name}()", rest) if target is not None else None  # (a function value's have no names)
    return FuncType(tuple(p.type for p in rest), ret, sig)


MODULES["functools"] = Module("functools", {
    "partial": Function("partial", functools_partial),
    "cache": DecoratorName("cache"),
    "lru_cache": DecoratorName("lru_cache"),
    "total_ordering": DecoratorName("total_ordering"),
    "cached_property": DecoratorName("cached_property"),
    "wraps": DecoratorName("wraps"),
    "reduce": Function("reduce", functools_reduce),
    "cmp_to_key": Function("cmp_to_key", functools_cmp_to_key),
})
