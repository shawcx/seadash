"""`http.server`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import CallContext, Function, MODULES, Module, bind_args, record_spawn, sync_method
from ..errors import Loc
from ..types import (
    BINARY_FILE, BOOL, BYTES, ClassAttr, ClassRefType, FLOAT, Field, FuncInfo, HTTPServerType, HTTP_HEADERS, INT,
    NONE, OptionalType, Param, SOCKET, STR, StructType, TupleType, Type, Var, VarTupleType, assignable,
)


def runtime_method(owner: StructType, name: str, ret: Type, *params: tuple) -> FuncInfo:
    """A method of a runtime base class: params are (name, type[, default as a literal node])."""
    resolved = []
    for p in params:
        star = p[0].startswith("*")
        default = p[2] if len(p) > 2 else None
        resolved.append(Param(p[0].lstrip("*"), p[1], default, Loc(0, 0), star))
    # (a node like a parsed method's, self first, so the flow analyses can read it: an empty body keeps nothing)
    node = A.FunctionDef(name, [A.Param("self"), *(A.Param(p.name, None, p.default, p.star) for p in resolved)], None, [])
    node.params[0].sym = Var("self", "self", owner, "param", Loc(0, 0))
    for np, p in zip(node.params[1:], resolved):
        np.sym = Var(p.name, p.name, p.type, "param", Loc(0, 0))
    info = FuncInfo(name, resolved, ret, node, owner)
    node.sym = info
    owner.methods[name] = info
    return info


HANDLER = StructType("BaseHTTPRequestHandler", "class", None, builtin=True, runtime_fields=True,
                     cpp_name="sd::httpserver::BaseHTTPRequestHandler", module="http.server")
for _field, _t in (("command", STR), ("path", STR), ("request_version", STR), ("requestline", STR),
                   ("raw_requestline", BYTES), ("headers", HTTP_HEADERS), ("client_address", TupleType((STR, INT))),
                   ("rfile", BINARY_FILE), ("wfile", BINARY_FILE), ("close_connection", BOOL)):
    HANDLER.fields[_field] = Field(_field, _t, None, Loc(0, 0))
_NO_STR = A.NoneLit()
_OPT_STR = OptionalType(STR)
runtime_method(HANDLER, "send_response", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "send_response_only", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "send_header", NONE, ("keyword", STR), ("value", STR))
runtime_method(HANDLER, "end_headers", NONE)
runtime_method(HANDLER, "flush_headers", NONE)
runtime_method(HANDLER, "send_error", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR), ("explain", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "handle_expect_100", BOOL)
runtime_method(HANDLER, "log_request", NONE, ("code", STR, A.StrLit("-")), ("size", STR, A.StrLit("-")))
runtime_method(HANDLER, "log_error", NONE, ("format", STR), ("*args", VarTupleType(STR)))
runtime_method(HANDLER, "log_message", NONE, ("format", STR), ("*args", VarTupleType(STR)))
runtime_method(HANDLER, "version_string", STR)
runtime_method(HANDLER, "date_time_string", STR, ("timestamp", OptionalType(FLOAT), A.NoneLit()))
runtime_method(HANDLER, "log_date_time_string", STR)
runtime_method(HANDLER, "address_string", STR)
for _name, _value in (("server_version", "BaseHTTP/0.6"), ("sys_version", "seadash/0.0.1"), ("protocol_version", "HTTP/1.0"),
                      ("error_content_type", "text/html;charset=utf-8"), ("error_message_format", ""),
                      ("default_request_version", "HTTP/0.9")):
    HANDLER.class_attrs[_name] = ClassAttr(_name, STR, A.StrLit(_value), Loc(0, 0))
SIMPLE_HANDLER = StructType("SimpleHTTPRequestHandler", "class", None, base=HANDLER, builtin=True, runtime_fields=True,
                            cpp_name="sd::httpserver::SimpleHTTPRequestHandler", module="http.server")
SIMPLE_HANDLER.fields["directory"] = Field("directory", STR, None, Loc(0, 0))
runtime_method(SIMPLE_HANDLER, "do_GET", NONE)
runtime_method(SIMPLE_HANDLER, "do_HEAD", NONE)
runtime_method(SIMPLE_HANDLER, "send_head", OptionalType(BINARY_FILE))
runtime_method(SIMPLE_HANDLER, "list_directory", OptionalType(BINARY_FILE), ("path", STR))
runtime_method(SIMPLE_HANDLER, "translate_path", STR, ("path", STR))
runtime_method(SIMPLE_HANDLER, "copyfile", NONE, ("source", BINARY_FILE), ("outputfile", BINARY_FILE))
runtime_method(SIMPLE_HANDLER, "guess_type", STR, ("path", STR))
SIMPLE_HANDLER.class_attrs["server_version"] = ClassAttr("server_version", STR, A.StrLit("SimpleHTTP/0.6"), Loc(0, 0))
SIMPLE_HANDLER.class_attrs["index_pages"] = ClassAttr("index_pages", TupleType((STR, STR)),
                                                      A.TupleLit([A.StrLit("index.html"), A.StrLit("index.htm")]), Loc(0, 0))


def partial_of(ctx: CallContext, node: A.Expr) -> bool:
    """Is `node` a call of functools.partial?"""
    if not isinstance(node, A.Call):
        return False
    f, checker = node.func, ctx.checker
    member = None
    if isinstance(f, A.Name) and f.id in checker.imported and f.id not in checker.state.names:
        mod, name = checker.imported[f.id]
        member = mod.members.get(name)
    elif isinstance(f, A.Attribute) and isinstance(f.value, A.Name) and f.value.id in checker.modules:
        member = checker.modules[f.value.id].members.get(f.attr)
    return member is MODULES["functools"].members["partial"]


def handler_class(ctx: CallContext, node: A.Expr) -> StructType:
    """The RequestHandlerClass argument: a subclass of BaseHTTPRequestHandler, which the server
    makes one of for each connection (with no arguments), or partial(Handler, setting=value),
    whose settings (fields: directory=) are set on each one it makes."""
    settings: dict[str, A.Expr] = {}
    if partial_of(ctx, node):
        if len(node.args) != 1:
            raise ctx.error("partial() of a handler class gives it settings by keyword only, e.g. "
                            "partial(SimpleHTTPRequestHandler, directory='public')", node)
        for kw in node.keywords:
            settings[kw.name] = kw.value
        node.notes["compile_time"] = True  # (codegen builds the server's handler factory from it instead)
        node = node.args[0]
    st = ctx.checker.lookup_struct(node.id) if isinstance(node, A.Name) else None
    if st is None or not st.is_subclass_of(HANDLER) or st is HANDLER:
        raise ctx.error(f"{ctx.what} needs the handler class itself (a subclass of BaseHTTPRequestHandler with do_GET... "
                        f"methods), not an instance or anything else", node)
    if st.init is not None and st.init.owner is not HANDLER:
        raise ctx.error(f"{st.name} can't have an __init__: the server makes a new {st.name} for each connection, "
                        f"with no arguments of yours (keep shared state in a module-level Synchronized object)", node)
    for t in st.ancestors():
        for m in t.methods.values():
            if m.name.startswith("do_") and (m.params or m.ret != NONE):
                raise ctx.error(f"{t.name}.{m.name}() is called for each request with no arguments (besides self), "
                                f"and returns nothing", m.node)
    for f in st.all_fields().values():
        if f.default is None:
            raise ctx.error(f"{st.name}.{f.name} needs a default value: the server makes a new {st.name} for each "
                            f"connection, with no arguments", node)
    for name, value in settings.items():
        f = st.find_field(name)
        if f is None:
            known = ", ".join(sorted(fl for t in st.ancestors() for fl in t.fields)) or "none"
            raise ctx.error(f"{st.name} has no setting '{name}' (its fields: {known})", value)
        actual = ctx.checker.check_expr(value, f.type)
        if not assignable(actual, f.type):
            raise ctx.error(f"{st.name}.{name} is {f.type}, not {actual}", value)
        if reason := unsendable_reason(f.type):
            raise ctx.error(f"{st.name}.{name} can't be a setting: each connection's handler gets its own copy, and {reason}", value)
    ctx.call.notes["handler_settings"] = settings
    node.sym, node.ty = st, ClassRefType(st)
    return st


def unsendable_reason(t: Type) -> str | None:
    from ..threads import unsendable
    return unsendable(t)


def http_server_new(threading: bool):
    def check(ctx: CallContext) -> Type:
        args = bind_args(ctx, (("server_address", None), ("RequestHandlerClass", None), ("bind_and_activate", BOOL, True)))
        ctx.checker.expect_type(args["server_address"], TupleType((STR, INT)), "server_address")
        if "bind_and_activate" in args:
            ctx.checker.expect_type(args["bind_and_activate"], BOOL, "bind_and_activate")
        st = handler_class(ctx, args["RequestHandlerClass"])
        if partial_of(ctx, args["RequestHandlerClass"]):
            args["RequestHandlerClass"] = args["RequestHandlerClass"].args[0]  # (its settings are in handler_settings)
        ctx.call.notes["http_args"] = args  # (for codegen)
        t = HTTPServerType(st, threading)
        if threading:  # every request is handled on a thread of its own: the handler's code is thread code
            record_spawn(ctx, args["RequestHandlerClass"], [], (), None)
            ctx.call.notes["spawn_extra"]["handler"] = st
        return t
    return check


HTTP_SERVER_METHODS = {
    "serve_forever": sync_method(NONE, ("poll_interval", FLOAT, "0.5")),
    "shutdown": sync_method(NONE),
    "handle_request": sync_method(NONE),
    "server_close": sync_method(NONE),
    "server_bind": sync_method(NONE),
    "server_activate": sync_method(NONE),
    "fileno": sync_method(INT),
}
HTTP_SERVER_ATTRIBUTES = {"server_address": lambda t: TupleType((STR, INT)), "server_port": lambda t: INT,
                          "socket": lambda t: SOCKET}  # (set only to itself wrapped for TLS: Checker.assign)
HTTP_SERVER_MOD = Module("http.server", {
    "HTTPServer": Function("HTTPServer", http_server_new(False), as_type=HTTPServerType()),
    "ThreadingHTTPServer": Function("ThreadingHTTPServer", http_server_new(True), as_type=HTTPServerType(None, True)),
    "BaseHTTPRequestHandler": HANDLER,
    "SimpleHTTPRequestHandler": SIMPLE_HANDLER,
}, "modules/httpserver.hpp", ("ssl", "crypto", "pthread"))
MODULES["http"].members["server"] = HTTP_SERVER_MOD
