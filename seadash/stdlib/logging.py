"""`logging`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, MODULES, NamedType, module_with_params, printable, runtime_module, signature, sync_method,
)
from ..types import (
    BOOL, INT, LOGGER, LOG_FORMATTER, LOG_HANDLER, ListType, NONE, OptionalType, PATH, STR, TEXT_FILE, Type,
)


LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50, "fatal": 50,
              "exception": 40}


def log_call(level_name: str) -> Callable[[CallContext], Type]:
    """logging.info(msg, *args) / logger.info(...) / log(level, msg, *args)."""
    def handler(ctx: CallContext) -> Type:
        for kw in ctx.call.keywords:
            if kw.name not in ("exc_info", "stack_info", "stacklevel"):
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
        ctx.keyword("exc_info", BOOL)
        ctx.keyword("stack_info", BOOL)
        ctx.keyword("stacklevel", INT)
        first = 0
        if level_name == "log":
            if not ctx.args:
                raise ctx.error("log() needs a level and a message")
            ctx.expect(0, INT)
            first = 1
        if len(ctx.args) <= first:
            raise ctx.error(f"{ctx.what} needs a message")
        for i in range(first, len(ctx.args)):
            ctx.need(i, printable, "something printable")
        ctx.call.notes["log_call"] = {"level": level_name, "first": first}
        return NONE

    return handler


def level_arg(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node)
    if t not in (INT, STR):
        raise ctx.error(f"{what} must be a level: logging.INFO (an int) or 'INFO', not {t}", node)


def log_set_level(ctx: CallContext) -> Type:
    ctx.arity(1)
    level_arg(ctx, ctx.args[0], "setLevel()")
    return NONE


def log_basic_config(ctx: CallContext) -> Type:
    if ctx.args:
        raise ctx.error("basicConfig() takes keyword arguments only")
    types = {"format": STR, "datefmt": STR, "style": STR, "filemode": STR, "stream": TEXT_FILE,
             "handlers": ListType(LOG_HANDLER), "force": BOOL, "encoding": STR}
    for kw in ctx.call.keywords:
        if kw.name == "level":
            level_arg(ctx, kw.value, "basicConfig() level")
        elif kw.name == "filename":
            t = ctx.checker.check_expr(kw.value)
            if t not in (STR, PATH):
                raise ctx.error(f"basicConfig() filename must be a str or Path, not {t}", kw.value)
        elif kw.name in types:
            ctx.keyword(kw.name, types[kw.name])
        else:
            raise ctx.error(f"basicConfig() got an unexpected keyword argument '{kw.name}'", kw)
    return NONE


LOGGER.methods.update({
    **{name: log_call(name) for name in (*LOG_LEVELS, "log")},
    "setLevel": log_set_level,
    "addHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "removeHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "hasHandlers": sync_method(BOOL),
    "getEffectiveLevel": sync_method(INT),
    "isEnabledFor": sync_method(BOOL, ("level", INT)),
    "getChild": sync_method(LOGGER, ("suffix", STR)),
})
LOGGER.attributes.update({
    "name": lambda t: STR, "level": lambda t: INT, "propagate": lambda t: BOOL,
    "handlers": lambda t: ListType(LOG_HANDLER), "parent": lambda t: OptionalType(LOGGER),
})
LOG_HANDLER.methods.update({
    "setLevel": log_set_level,
    "setFormatter": sync_method(NONE, ("fmt", LOG_FORMATTER)),
    "flush": sync_method(NONE),
    "close": sync_method(NONE),
})
LOG = "sd::logging::"
MODULES["logging"] = module_with_params(runtime_module(
    "logging", "modules/logging.hpp",
    **{name: (log_call(name), None) for name in (*LOG_LEVELS, "log")},
    basicConfig=(log_basic_config, None),
    getLogger=(signature(LOGGER, ("name", OptionalType(STR), "std::nullopt")), LOG + "getLogger"),
    getLevelName=(signature(STR, ("level", INT)), LOG + "level_name"),
    disable=(signature(NONE, ("level", INT, LOG + "CRITICAL")), LOG + "disable"),
    StreamHandler=(signature(LOG_HANDLER, ("stream", OptionalType(TEXT_FILE), "std::nullopt")), LOG + "Handler::stream"),
    FileHandler=(signature(LOG_HANDLER, ("filename", STR), ("mode", STR, '"a"s'),
                           ("encoding", OptionalType(STR), "std::nullopt")), LOG + "Handler::file"),
    NullHandler=(signature(LOG_HANDLER), LOG + "Handler"),
    Formatter=(signature(LOG_FORMATTER, ("fmt", OptionalType(STR), "std::nullopt"),
                         ("datefmt", OptionalType(STR), "std::nullopt"), ("style", STR, '"%"s')), LOG + "Formatter"),
    **{name: (INT, LOG + name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET")},
    WARN=(INT, LOG + "WARNING"), FATAL=(INT, LOG + "CRITICAL"),
    BASIC_FORMAT=(STR, LOG + "BASIC_FORMAT"),
    root=(LOGGER, LOG + "Logger::root()"),
))
for _name, _t in (("StreamHandler", LOG_HANDLER), ("FileHandler", LOG_HANDLER),
                  ("NullHandler", LOG_HANDLER), ("Formatter", LOG_FORMATTER)):
    MODULES["logging"].members[_name].as_type = _t
MODULES["logging"].members["Logger"] = NamedType("Logger", LOGGER)
MODULES["logging"].members["Handler"] = NamedType("Handler", LOG_HANDLER)
