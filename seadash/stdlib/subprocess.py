"""`subprocess`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    AttributeUnavailable, bind_args, CallContext, content, EXCEPTIONS, Function, Module, module_type, MODULES,
    NamedType, returns, signature, Value,
)
from ..errors import Loc
from ..types import (
    BOOL, BYTES, DictType, Field, FileType, FLOAT, INT, ListType, NONE, OptionalType, ProcessType, STR, StructType,
    TupleType, Type,
)


PROCESS_KEYWORDS = {
    "run": ("stdin", "stdout", "stderr", "input", "capture_output", "text", "universal_newlines", "encoding",
            "check", "shell", "cwd", "timeout", "env"),
    "check_output": ("stdin", "stderr", "input", "text", "universal_newlines", "encoding", "shell", "cwd",
                     "timeout", "env"),
    "call": ("stdin", "stdout", "stderr", "shell", "cwd", "timeout", "env"),
    "check_call": ("stdin", "stdout", "stderr", "shell", "cwd", "timeout", "env"),
    "Popen": ("stdin", "stdout", "stderr", "text", "universal_newlines", "encoding", "shell", "cwd", "env", "bufsize"),
}
STREAM_CONSTANTS = {"PIPE": -1, "STDOUT": -2, "DEVNULL": -3}


def stream_kind(ctx: CallContext, node: A.Expr | None, name: str) -> str:
    """'inherit', 'PIPE', 'STDOUT', 'DEVNULL' or 'file' for stdin=/stdout=/stderr=."""
    if node is None or isinstance(node, A.NoneLit):
        return "inherit"
    const = None
    if isinstance(node, A.Attribute) and isinstance(node.value, A.Name):
        if ctx.checker.modules.get(node.value.id) is MODULES["subprocess"]:
            const = node.attr
    elif isinstance(node, A.Name) and node.id in ctx.checker.imported:
        mod, member = ctx.checker.imported[node.id]
        if mod is MODULES["subprocess"]:
            const = member
    if const in STREAM_CONSTANTS:
        ctx.checker.check_expr(node)
        if const == "STDOUT" and name != "stderr":
            raise ctx.error("only stderr can be subprocess.STDOUT", node)
        return const
    t = ctx.checker.check_expr(node)
    if isinstance(t, FileType) and t.memory:
        raise ctx.error(f"{name}= can't be a {t}: the child process needs a real file (one with a descriptor); "
                        f"use {name}=subprocess.PIPE and write the result's {name} to it", node)
    if isinstance(t, FileType):
        return "file"
    raise ctx.error(
        f"{name}= must be subprocess.PIPE, subprocess.DEVNULL, an open file, or None (written out: "
        f"it decides what the result holds), not {t}", node,
    )


def literal_bool(ctx: CallContext, node: A.Expr | None, name: str) -> bool:
    if node is None:
        return False
    if not isinstance(node, A.BoolLit):
        raise ctx.error(f"{name}= must be True or False written out (it decides the result's type)", node)
    node.ty = BOOL
    return node.value


def process_call(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if len(ctx.args) != 1:
            raise ctx.error(f"{ctx.what} takes the command as its only positional argument; the rest are keywords")
        kw = {}
        for k in ctx.call.keywords:
            if k.name not in PROCESS_KEYWORDS[op]:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{k.name}'", k)
            kw[k.name] = k.value
        args_t = ctx.arg(0, ListType(STR))
        if args_t not in (STR, ListType(STR)):
            raise ctx.error(f"{ctx.what} command must be a list of strings (or one string), not {args_t}", ctx.args[0])
        text = literal_bool(ctx, kw.get("text"), "text") or literal_bool(ctx, kw.get("universal_newlines"), "universal_newlines")
        if "encoding" in kw:
            ctx.checker.expect_type(kw["encoding"], STR, "encoding")
            text = True  # decoded (as UTF-8)
        capture = literal_bool(ctx, kw.get("capture_output"), "capture_output")
        streams = {name: stream_kind(ctx, kw.get(name), name) for name in ("stdin", "stdout", "stderr")}
        if capture and (streams["stdout"] != "inherit" or streams["stderr"] != "inherit"):
            raise ctx.error("stdout and stderr can't be used with capture_output=True", ctx.call)
        if capture:
            streams["stdout"] = streams["stderr"] = "PIPE"
        if op == "check_output":
            streams["stdout"] = "PIPE"
        if "input" in kw:
            if streams["stdin"] != "inherit":
                raise ctx.error("stdin and input can't both be given", kw["input"])
            ctx.checker.expect_type(kw["input"], STR if text else BYTES, "input" + ("" if text else " (without text=True)"))
        for name, t in (("check", BOOL), ("shell", BOOL), ("timeout", FLOAT), ("env", DictType(STR, STR)),
                        ("bufsize", INT)):
            if name in kw:
                ctx.checker.expect_type(kw[name], t, name)
        if "cwd" in kw and not isinstance(kw["cwd"], A.NoneLit):
            ctx.checker.expect_type(kw["cwd"], STR, "cwd")
        ctx.call.notes["process"] = {"op": op, "kw": kw, "text": text, "streams": streams}
        content = STR if text else BYTES
        match op:
            case "run":
                return ProcessType("CompletedProcess", text, False, streams["stdout"] == "PIPE",
                                   streams["stderr"] == "PIPE", args_t)
            case "check_output":
                return content
            case "call" | "check_call":
                return INT
            case "Popen":
                return ProcessType("Popen", text, streams["stdin"] == "PIPE", streams["stdout"] == "PIPE",
                                   streams["stderr"] == "PIPE", args_t)
        raise AssertionError(op)

    return handler


def captured(stream: str) -> Callable[[ProcessType], Type]:
    def attribute(t: ProcessType) -> Type:
        if t.args is None:
            raise AttributeUnavailable(f"what {t}.{stream} holds isn't known here (only where the process was started)")
        if not getattr(t, stream):
            how = "capture_output=True" if t.kind == "CompletedProcess" else f"{stream}=subprocess.PIPE"
            raise AttributeUnavailable(f"{stream} wasn't captured, so it's None: pass {how} to read it")
        if t.kind == "Popen":
            return FileType(binary=not t.text)
        return STR if t.text else BYTES

    return attribute


COMPLETED_ATTRIBUTES = {
    "args": lambda t: t.args or ListType(STR), "returncode": lambda t: INT,
    "stdout": captured("stdout"), "stderr": captured("stderr"),
}
POPEN_ATTRIBUTES = {
    "args": lambda t: t.args or ListType(STR), "returncode": lambda t: OptionalType(INT), "pid": lambda t: INT,
    "stdin": captured("stdin"), "stdout": captured("stdout"), "stderr": captured("stderr"),
}


def popen_communicate(ctx: CallContext) -> Type:
    t: ProcessType = ctx.receiver
    args = bind_args(ctx, (("input", None, True), ("timeout", FLOAT, True)))
    content = STR if t.text else BYTES
    if "input" in args and not isinstance(args["input"], A.NoneLit):
        if not t.stdin:
            raise ctx.error("communicate(input) needs Popen(..., stdin=subprocess.PIPE)", args["input"])
        ctx.checker.expect_type(args["input"], content, "input")
    if "timeout" in args:
        ctx.checker.expect_type(args["timeout"], FLOAT, "timeout")
    ctx.call.notes["communicate_args"] = args  # (the bound arguments, for codegen)
    return TupleType(tuple(content if piped else OptionalType(content) for piped in (t.stdout, t.stderr)))


PROCESS_METHODS = {
    "CompletedProcess": {"check_returncode": returns(NONE)},
    "Popen": {
        "communicate": popen_communicate,
        "wait": signature(INT, ("timeout", FLOAT, "std::nullopt")),
        "poll": returns(OptionalType(INT)),
        "kill": returns(NONE),
        "terminate": returns(NONE),
        "send_signal": returns(NONE, args=(INT,)),
    },
}


def subprocess_exception(name: str, fields: dict[str, Type]) -> StructType:
    base = MODULES_EXCEPTION_BASE.get(name)
    st = StructType(name, "class", None, bases=[base] if base else [], builtin=True, cpp_name=f"sd::subprocess::{name}")
    for fname, ft in fields.items():
        st.fields[fname] = Field(fname, ft, None, Loc(0, 0))
    return st


MODULES_EXCEPTION_BASE: dict[str, StructType] = {"SubprocessError": EXCEPTIONS["Exception"]}
SUBPROCESS_ERROR = subprocess_exception("SubprocessError", {})
MODULES_EXCEPTION_BASE.update(CalledProcessError=SUBPROCESS_ERROR, TimeoutExpired=SUBPROCESS_ERROR)
STREAM_TEXT = OptionalType(STR)  # captured output, decoded (Python gives bytes unless text=True)
MODULES["subprocess"] = Module("subprocess", {
    **{op: Function(op, process_call(op)) for op in PROCESS_KEYWORDS},
    "getoutput": Function("getoutput", signature(STR, ("cmd", STR)), "sd::subprocess::getoutput"),
    "getstatusoutput": Function("getstatusoutput", signature(TupleType((INT, STR)), ("cmd", STR)),
                                "sd::subprocess::getstatusoutput"),
    **{name: Value(name, INT, f"std::int64_t{{{v}}}") for name, v in STREAM_CONSTANTS.items()},
    "SubprocessError": SUBPROCESS_ERROR,
    "CalledProcessError": subprocess_exception("CalledProcessError", {
        "returncode": INT, "cmd": ListType(STR), "output": STREAM_TEXT, "stdout": STREAM_TEXT, "stderr": STREAM_TEXT,
    }),
    "TimeoutExpired": subprocess_exception("TimeoutExpired", {
        "cmd": ListType(STR), "timeout": FLOAT, "output": STREAM_TEXT, "stdout": STREAM_TEXT, "stderr": STREAM_TEXT,
    }),
    "CompletedProcess": NamedType("CompletedProcess", ProcessType("CompletedProcess")),
    "Popen": Function("Popen", process_call("Popen"), as_type=ProcessType("Popen")),
}, "modules/subprocess.hpp")
for _name in ("getoutput", "getstatusoutput"):
    MODULES["subprocess"].members[_name].params = MODULES["subprocess"].members[_name].check.params
module_type(ProcessType, methods=lambda t: PROCESS_METHODS[t.kind],
            attributes=lambda t: COMPLETED_ATTRIBUTES if t.kind == "CompletedProcess" else POPEN_ATTRIBUTES)
