"""`signal` (and `types.FrameType`): how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    CallContext, MODULES, Module, NamedType, module_with_params, record_spawn, runtime_module, signature,
)
from ..errors import Loc
from ..types import (
    EnumInfo, EnumMember, FRAME, FuncType, INT, NONE, OptionalType, SIGNAL_HANDLER, STR, SetType, StructType, Type,
    fills_defaults,
)


SIGNAL_HANDLER_PARAMS = (INT, OptionalType(FRAME))
SIGNAL_HANDLER_HINT = ("a function like `def handler(signum: int, frame: FrameType | None) -> None:` "
                       "(from types import FrameType; the frame is always None in seadash)")
# The signals Linux and macOS both have, and the names Python's signal.Signals gives them.
SIGNAL_NAMES = ("SIGHUP SIGINT SIGQUIT SIGILL SIGTRAP SIGABRT SIGBUS SIGFPE SIGKILL SIGUSR1 SIGSEGV SIGUSR2 SIGPIPE "
                "SIGALRM SIGTERM SIGCHLD SIGCONT SIGSTOP SIGTSTP SIGTTIN SIGTTOU SIGURG SIGXCPU SIGXFSZ SIGVTALRM "
                "SIGPROF SIGWINCH SIGIO SIGSYS").split()
SIGNAL_ALIASES = {"SIGIOT": "SIGABRT"}


def make_signals() -> StructType:
    """signal.Signals, an IntEnum. Its members' numbers depend on the platform, so the C++ class
    (sd::signal::Signals) is written in the runtime, with its table taken from <signal.h>; the
    values here only tell members apart."""
    info = EnumInfo("IntEnum", INT)
    for i, name in enumerate(SIGNAL_NAMES):
        info.members[name] = EnumMember(name, i + 1, Loc(0, 0))
    for alias, name in SIGNAL_ALIASES.items():
        info.members[alias] = EnumMember(alias, info.members[name].value, Loc(0, 0), name)
    return StructType("Signals", "struct", None, builtin=True, cpp_name="sd::signal::Signals", module="signal",
                      frozen=True, enum=info)


SIGNALS = make_signals()


def signal_signal(ctx: CallContext) -> Type:
    """signal.signal(signum, handler): a handler function runs on the signal-handling thread, so
    it's checked like a thread's work."""
    ctx.arity(2)
    ctx.expect(0, INT)
    node = ctx.args[1]
    t = ctx.checker.check_expr(node, FuncType(SIGNAL_HANDLER_PARAMS, None))
    if t == SIGNAL_HANDLER:  # SIG_DFL, SIG_IGN, or what signal() / getsignal() gave
        return SIGNAL_HANDLER
    if not isinstance(t, FuncType):
        raise ctx.error(f"{ctx.what} needs SIG_DFL, SIG_IGN, a handler that signal.signal() or signal.getsignal() "
                        f"gave, or {SIGNAL_HANDLER_HINT}, not {t}", node)
    if t.params != SIGNAL_HANDLER_PARAMS:
        if not fills_defaults(t, FuncType(SIGNAL_HANDLER_PARAMS, t.ret)):
            raise ctx.error(f"a signal handler takes (int, FrameType | None), the signal's number and the frame: "
                            f"{SIGNAL_HANDLER_HINT}, not {t}", node)
        node.notes["fill_to"] = FuncType(SIGNAL_HANDLER_PARAMS, t.ret)  # (its other parameters have defaults)
    record_spawn(ctx, node, [], (), None)
    ctx.call.notes["spawn_extra"]["signal_handler"] = True  # (threads.py words its errors for a handler)
    return SIGNAL_HANDLER


MODULES["signal"] = module_with_params(runtime_module(
    "signal", "modules/signal.hpp",
    signal=(signal_signal, None),
    getsignal=(signature(SIGNAL_HANDLER, ("signalnum", INT)), "sd::signal::getsignal"),
    raise_signal=(signature(NONE, ("signalnum", INT)), "sd::signal::raise_signal"),
    alarm=(signature(INT, ("seconds", INT)), "sd::signal::alarm"),
    pause=(signature(NONE), "sd::signal::pause"),
    strsignal=(signature(OptionalType(STR), ("signalnum", INT)), "sd::signal::strsignal"),
    valid_signals=(signature(SetType(INT)), "sd::signal::valid_signals"),
    Signals=SIGNALS,
    SIG_DFL=(SIGNAL_HANDLER, "sd::signal::default_action()"),
    SIG_IGN=(SIGNAL_HANDLER, "sd::signal::ignore()"),
    default_int_handler=(SIGNAL_HANDLER, "sd::signal::default_int_handler()"),
    NSIG=(INT, "std::int64_t{NSIG}"),
    **{name: (SIGNALS, f"sd::signal::Signals::sd_at({i})") for i, name in enumerate(SIGNAL_NAMES)},
    **{alias: (SIGNALS, f"sd::signal::Signals::sd_at({SIGNAL_NAMES.index(name)})") for alias, name in SIGNAL_ALIASES.items()},
))
# `from types import FrameType`, for annotating a handler's frame parameter.
MODULES["types"] = Module("types", {"FrameType": NamedType("FrameType", FRAME)}, "modules/signal.hpp")
