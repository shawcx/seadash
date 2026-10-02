"""`concurrent.futures`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    CallContext, cpp_string_literal, exception_class, EXCEPTIONS, iterable_of, MANY, Module, module_type,
    module_with_params, MODULES, NamedType, record_spawn, runtime_module, signature, sync_method, work_function,
)
from ..types import (
    BOOL, EXECUTOR, FLOAT, FuncType, FutureType, GeneratorType, INT, NONE, OptionalType, SetType, STR, TupleType,
    Type,
)


def executor_submit(ctx: CallContext) -> Type:
    if not ctx.args:
        raise ctx.error("submit() needs the function to run")
    if ctx.call.keywords:
        raise ctx.error("submit() passes positional arguments only (wrap keyword arguments in a lambda)", ctx.call.keywords[0])
    arg_types = tuple(ctx.checker.check_expr(a) for a in ctx.args[1:])
    result = work_function(ctx, ctx.args[0], arg_types)
    record_spawn(ctx, ctx.args[0], ctx.args[1:], arg_types, result)
    ctx.call.notes["work_types"] = (arg_types, result)
    return FutureType(result)


def executor_map(ctx: CallContext) -> Type:
    ctx.arity(2, MANY, keywords=("timeout", "chunksize"))
    elems = tuple(iterable_of(ctx, a) for a in ctx.args[1:])
    result = work_function(ctx, ctx.args[0], elems)
    if result == NONE:
        raise ctx.error("map()'s function must return a value (use submit() for work without a result)", ctx.args[0])
    ctx.keyword("timeout", OptionalType(FLOAT))
    ctx.keyword("chunksize", INT)
    record_spawn(ctx, ctx.args[0], ctx.args[1:], elems, result)
    ctx.call.notes["work_types"] = (elems, result)
    return GeneratorType(result)


def future_callback(ctx: CallContext) -> Type:
    """The callback may run on the worker thread, so it's checked like work given to a thread."""
    ctx.arity(1)
    ctx.checker.expect_type(ctx.args[0], FuncType((ctx.receiver,), NONE), "add_done_callback() callback")
    record_spawn(ctx, ctx.args[0], [], (), None)
    return NONE


def futures_of(ctx: CallContext) -> FutureType:
    elem = iterable_of(ctx, ctx.args[0])
    if not isinstance(elem, FutureType):
        raise ctx.error(f"{ctx.what} needs futures, not {elem}", ctx.args[0])
    return elem


def futures_as_completed(ctx: CallContext) -> Type:
    ctx.arity(1, 2, keywords=("timeout",))
    f = futures_of(ctx)
    if len(ctx.args) == 2:
        ctx.expect(1, OptionalType(FLOAT))
    ctx.keyword("timeout", OptionalType(FLOAT))
    return GeneratorType(f)


def futures_wait(ctx: CallContext) -> Type:
    ctx.arity(1, 3, keywords=("timeout", "return_when"))
    f = futures_of(ctx)
    if len(ctx.args) > 1:
        ctx.expect(1, OptionalType(FLOAT))
    if len(ctx.args) > 2:
        ctx.expect(2, STR)
    ctx.keyword("timeout", OptionalType(FLOAT))
    ctx.keyword("return_when", STR)
    return TupleType((SetType(f), SetType(f)))


EXECUTOR.methods.update({
    "submit": executor_submit,
    "map": executor_map,
    "shutdown": sync_method(NONE, ("wait", BOOL, "true"), ("cancel_futures", BOOL, "false")),
})
FUTURE_METHODS = {
    "result": sync_method(lambda f: f.elem, ("timeout", OptionalType(FLOAT), "std::nullopt")),
    "exception": sync_method(OptionalType(EXCEPTIONS["Exception"]), ("timeout", OptionalType(FLOAT), "std::nullopt")),
    "done": sync_method(BOOL),
    "running": sync_method(BOOL),
    "cancelled": sync_method(BOOL),
    "cancel": sync_method(BOOL),
    "add_done_callback": future_callback,
}
FUTURES = module_with_params(runtime_module(
    "concurrent.futures", "modules/futures.hpp", ("pthread",),
    ThreadPoolExecutor=(signature(EXECUTOR, ("max_workers", OptionalType(INT), "std::nullopt"),
                                  ("thread_name_prefix", STR, '""s')), "sd::futures::ThreadPoolExecutor"),
    as_completed=(futures_as_completed, None),
    wait=(futures_wait, None),
    CancelledError=exception_class("CancelledError", "sd::futures::CancelledError"),
    TimeoutError=EXCEPTIONS["TimeoutError"],
    **{name: (STR, cpp_string_literal(name)) for name in ("FIRST_COMPLETED", "FIRST_EXCEPTION", "ALL_COMPLETED")},
))
FUTURES.members["ThreadPoolExecutor"].as_type = EXECUTOR
FUTURE_MARKER = NamedType("Future", FutureType(NONE))  # Future[T] in annotations (see checker.resolve_type_name)
FUTURES.members["Future"] = FUTURE_MARKER
MODULES["concurrent"] = Module("concurrent", {"futures": FUTURES})
module_type(FutureType, methods=FUTURE_METHODS)
