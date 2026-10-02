"""`contextlib`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import CallContext, DecoratorName, Function, MODULES, Module, TypeAlias, plural_args, sync_method
from ..types import (
    BuiltinClass, ClassRefType, ContextManagerType, EXIT_STACK, FileType, FuncType, NONE, StructType, Type,
    assignable,
)
from .heapq_bisect import given


def contextlib_nullcontext(ctx: CallContext) -> Type:
    """nullcontext(enter_result=None): does nothing; `with ... as x` gives enter_result."""
    n = ctx.arity(0, 1, keywords=("enter_result",))
    node = ctx.args[0] if n else ctx.keyword_arg("enter_result")
    ctx.call.notes["never_suppresses"] = True
    if node is None:
        return ContextManagerType(NONE)
    want = ctx.expected.elem if isinstance(ctx.expected, ContextManagerType) else None
    return ContextManagerType(ctx.checker.check_expr(node, want))


def has_close(t: Type) -> bool:
    match t:
        case FileType():
            return True
        case BuiltinClass():
            return "close" in t.methods
        case StructType() if not t.builtin:
            m = t.find_method("close")
            return m is not None and not m.params and m.kind == "method"
    return False


def contextlib_closing(ctx: CallContext) -> Type:
    """closing(thing): `with closing(thing) as x:` gives thing, and calls thing.close() at the end."""
    ctx.arity(1)
    t = ctx.arg(0)
    if not has_close(t):
        raise ctx.error(f"closing() needs something with a close() method (taking no arguments), not {t}", ctx.args[0])
    ctx.call.notes["never_suppresses"] = True
    return ContextManagerType(t)


def contextlib_suppress(ctx: CallContext) -> Type:
    """suppress(*exceptions): a with block that ends early, quietly, on those exceptions."""
    if ctx.call.keywords:
        raise ctx.error("suppress() takes exception classes, not keyword arguments", ctx.call.keywords[0])
    classes = []
    for a in ctx.args:
        st = ctx.checker.lookup_struct(a.id) if isinstance(a, A.Name) and a.id not in ctx.checker.state.names \
            else ctx.checker.module_struct(a)
        if st is None or not st.is_exception:
            raise ctx.error("suppress() takes exception classes, like suppress(FileNotFoundError, KeyError)", a)
        a.sym, a.ty = st, ClassRefType(st)
        classes.append(st)
    ctx.call.notes["suppressed"] = classes
    return ContextManagerType(NONE)


def contextlib_exitstack(ctx: CallContext) -> Type:
    ctx.arity(0)
    return EXIT_STACK


def exitstack_enter_context(ctx: CallContext) -> Type:
    """stack.enter_context(cm): enters cm (anything a with statement takes) and gives what
    `with cm as x` would; cm's exit runs when the stack is closed."""
    ctx.arity(1)
    t = ctx.arg(0)
    info = ctx.checker.context_manager(t, ctx.args[0])
    if info.kind in ("lock", "mutex", "rw_read", "rw_write"):
        raise ctx.error(f"enter_context() can't hold {t} (a lock is held by a with statement's block)", ctx.args[0])
    ctx.call.notes["with_info"] = info
    return info.enter_type


def exitstack_callback(ctx: CallContext) -> Type:
    """stack.callback(f, *args): calls f(*args) when the stack is closed; returns f."""
    if ctx.call.keywords:
        raise ctx.error("callback() keyword arguments aren't supported yet; use a lambda", ctx.call.keywords[0])
    if not ctx.args:
        raise ctx.error("callback() needs the function to call")
    given = tuple(ctx.checker.check_expr(a) for a in ctx.args[1:])
    t = ctx.checker.check_expr(ctx.args[0], FuncType(given, None))
    if not isinstance(t, FuncType):
        raise ctx.error(f"callback() needs a function to call, not {t}", ctx.args[0])
    if len(t.params) != len(given):
        raise ctx.error(f"callback(): the function takes {plural_args(len(t.params))}, but {len(given)} "
                        f"{'is' if len(given) == 1 else 'are'} given", ctx.call)
    for a, want, got in zip(ctx.args[1:], t.params, given):
        if not assignable(got, want):
            raise ctx.error(f"callback(): this argument must be {want}, not {got}", a)
    return t


EXIT_STACK.methods.update(
    enter_context=exitstack_enter_context,
    callback=exitstack_callback,
    close=sync_method(NONE),
    pop_all=sync_method(EXIT_STACK),
)
MODULES["contextlib"] = Module("contextlib", {
    "contextmanager": DecoratorName("contextmanager"),
    "nullcontext": Function("nullcontext", contextlib_nullcontext),
    "closing": Function("closing", contextlib_closing),
    "suppress": Function("suppress", contextlib_suppress),
    "ExitStack": Function("ExitStack", contextlib_exitstack, as_type=EXIT_STACK),
    "AbstractContextManager": TypeAlias("AbstractContextManager"),
}, "modules/contextlib.hpp")
