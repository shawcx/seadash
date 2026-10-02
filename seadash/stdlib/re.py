"""`re`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, Function, MANY, MODULES, Module, NamedType, Value, bind_args, exception_class, plural, returns,
)
from ..types import (
    DictType, FuncType, INT, ListType, MatchType, NONE, OptionalType, PatternType, RegexInfo, STR, TupleType, Type,
    assignable,
)


REGEX_OPS = {  # operation -> its parameters after the pattern: (name, type[, has default])
    "search": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "match": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "fullmatch": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "findall": (("string", STR),),
    "finditer": (("string", STR),),
    "sub": (("repl", None), ("string", STR), ("count", INT, True)),
    "subn": (("repl", None), ("string", STR), ("count", INT, True)),
    "split": (("string", STR), ("maxsplit", INT, True)),
}


def group_type(info: RegexInfo | None, group: int | None) -> Type:
    if group == 0:
        return STR
    if info is None or group is None or group in info.optional:
        return OptionalType(STR)
    return STR


def regex_op(ctx: CallContext, op: str, info: RegexInfo | None, args: dict[str, A.Expr]) -> Type:
    """Check the arguments of a pattern operation (all but the pattern) and give its result."""
    for name, t, *_ in REGEX_OPS[op]:
        if name in args and t is not None:
            actual = ctx.checker.check_expr(args[name], t)
            if not assignable(actual, t):
                raise ctx.error(f"{ctx.what} argument '{name}' must be {t}, not {actual}", args[name])
    match op:
        case "search" | "match" | "fullmatch":
            return OptionalType(MatchType(info))
        case "findall":
            if info is not None and info.groups > 1:
                return ListType(TupleType((STR,) * info.groups))
            return ListType(STR)
        case "finditer":
            return ListType(MatchType(info))
        case "sub" | "subn":
            repl = args["repl"]
            rt = ctx.checker.check_expr(repl, FuncType((MatchType(info),), STR))
            if isinstance(rt, FuncType):
                if rt.params != (MatchType(info),) or rt.ret != STR:
                    raise ctx.error(f"{ctx.what} replacement function must take a re.Match and return str, not {rt}", repl)
                ctx.call.notes["regex_repl_fn"] = True
            elif rt != STR:
                raise ctx.error(f"{ctx.what} replacement must be a str or a function, not {rt}", repl)
            return STR if op == "sub" else TupleType((STR, INT))
        case "split":
            if info is not None and info.optional:
                return ListType(OptionalType(STR))  # a group that didn't match is None
            return ListType(STR)
    raise AssertionError(op)


def constant_flags(ctx: CallContext, node: A.Expr | None) -> int | None:
    """re.I | re.M and friends, when they're written out; None if computed at run time."""
    from ..regex import FLAGS
    if node is None:
        return 0
    match node:
        case A.IntLit(v):
            return v
        case A.Attribute(A.Name(mod), attr) if ctx.checker.modules.get(mod) is MODULES["re"] and attr in FLAGS:
            return FLAGS[attr]
        case A.Name(name) if name in ctx.checker.imported and ctx.checker.imported[name][0] is MODULES["re"]:
            return FLAGS.get(ctx.checker.imported[name][1])
        case A.BinOp(left, "|", right):
            lv, rv = constant_flags(ctx, left), constant_flags(ctx, right)
            return lv | rv if lv is not None and rv is not None else None
    return None


def regex_pattern(ctx: CallContext, node: A.Expr, flags_node: A.Expr | None) -> RegexInfo | None:
    """Check the pattern (and flags) arguments; analyze the pattern if it's a literal."""
    import re as pyre
    from ..regex import analyze
    ctx.checker.expect_type(node, STR, f"{ctx.what} pattern")
    if flags_node is not None:
        ctx.checker.expect_type(flags_node, INT, f"{ctx.what} flags")
    flags = constant_flags(ctx, flags_node)
    if not isinstance(node, A.StrLit):
        return None
    try:
        info = analyze(node.value, flags or 0)
    except pyre.error as e:
        if flags is None:  # the flags (say, VERBOSE) aren't known until run time
            return None
        raise ctx.error(f"invalid regular expression: {e}", node)
    ctx.call.notes["regex_static"] = flags  # known flags: compile once, into a static
    return info


def re_function(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if op == "escape":
            ctx.arity(1)
            ctx.expect(0, STR)
            return STR
        if op == "purge":
            ctx.arity(0)
            return NONE
        rest = REGEX_OPS.get(op, ())
        # Python's module functions take the operation's arguments but not pos/endpos.
        rest = tuple(p for p in rest if p[0] not in ("pos", "endpos"))
        params = (("pattern", STR), *rest, ("flags", INT, True))
        args = bind_args(ctx, params)
        info = regex_pattern(ctx, args["pattern"], args.get("flags"))
        ctx.call.notes["regex_args"] = args
        if op == "compile":
            return PatternType(info)
        return regex_op(ctx, op, info, args)

    return handler


def pattern_method(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        args = bind_args(ctx, REGEX_OPS[op])
        ctx.call.notes["regex_args"] = args
        return regex_op(ctx, op, ctx.receiver.info, args)

    return handler


def match_group_arg(ctx: CallContext, node: A.Expr) -> Type:
    """One argument of m.group(...), m[...], m.start(...): a group number or name."""
    info: RegexInfo | None = ctx.receiver.info
    t = ctx.checker.check_expr(node)
    if t not in (INT, STR):
        raise ctx.error(f"a group is a number or a name, not {t}", node)
    group = None
    if isinstance(node, A.IntLit):
        group = node.value
    elif isinstance(node, A.StrLit) and info is not None:
        group = info.group_number(node.value)
        if group is None:
            raise ctx.error(f"the pattern has no group named '{node.value}'", node)
    if group is not None and info is not None and not 0 <= group <= info.groups:
        raise ctx.error(f"the pattern has no group {group} (it has {plural(info.groups, 'group')})", node)
    node.notes["regex_group"] = group  # codegen: a known group number
    return group_type(info, group)


def match_group(ctx: CallContext) -> Type:
    n = ctx.arity(0, MANY)
    if n == 0:
        return STR
    types = [match_group_arg(ctx, a) for a in ctx.args]
    return types[0] if n == 1 else TupleType(tuple(types))


def match_groups(ctx: CallContext) -> Type:
    ctx.arity(0)
    info: RegexInfo | None = ctx.receiver.info
    if info is None:
        return ListType(OptionalType(STR))  # unknown pattern: how many groups isn't known
    return TupleType(tuple(group_type(info, g) for g in range(1, info.groups + 1)))


def match_groupdict(ctx: CallContext) -> Type:
    ctx.arity(0)
    info: RegexInfo | None = ctx.receiver.info
    if info is not None and not any(i in info.optional for _, i in info.names):
        return DictType(STR, STR)
    return DictType(STR, OptionalType(STR))


def match_position(result: Type) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if ctx.arity(0, 1):
            match_group_arg(ctx, ctx.args[0])
        return result

    return handler


PATTERN_METHODS = {op: pattern_method(op) for op in REGEX_OPS}
MATCH_METHODS = {
    "group": match_group,
    "groups": match_groups,
    "groupdict": match_groupdict,
    "start": match_position(INT),
    "end": match_position(INT),
    "span": match_position(TupleType((INT, INT))),
    "expand": returns(STR, args=(STR,)),
}
# Attributes of built-in types: name -> type (given the receiver's type).
PATTERN_ATTRIBUTES = {
    "pattern": lambda t: STR, "flags": lambda t: INT, "groups": lambda t: INT,
    "groupindex": lambda t: DictType(STR, INT),
}
MATCH_ATTRIBUTES = {
    "string": lambda t: STR, "pos": lambda t: INT, "endpos": lambda t: INT,
    "re": lambda t: PatternType(t.info), "lastindex": lambda t: OptionalType(INT),
}
MODULES["re"] = Module("re", {
    **{op: Function(op, re_function(op)) for op in (*REGEX_OPS, "compile", "escape", "purge")},
    **{name: Value(name, INT, f"std::int64_t{{{int(value)}}}") for name, value in __import__("re").RegexFlag.__members__.items()
       if name in ("ASCII", "A", "IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "VERBOSE", "X", "UNICODE", "U", "NOFLAG")},
    "error": exception_class("error", "sd::re::error"),
    "Pattern": NamedType("Pattern", PatternType()),
    "Match": NamedType("Match", MatchType()),
}, "modules/re.hpp", ("pcre2-8",))
