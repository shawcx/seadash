"""`urllib.parse`, `urllib.request` and `urllib.error`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    CallContext, EXCEPTIONS, MODULES, Module, OneOf, bind_args, module_with_params, runtime_module, signature,
    sync_method,
)
from ..errors import Loc
from ..types import (
    BOOL, BYTEARRAY, BYTES, DictType, FLOAT, Field, HTTP_HEADERS, HTTP_RESPONSE, INT, ListType, NONE, OptionalType,
    PATH, SOCKET, SSL_SOCKET, STR, StructType, TupleType, Type, URL_PARTS, URL_REQUEST, element_type,
)
from .ssl import SSL_CONTEXT


OPT_STR = OptionalType(STR)
HTTP_RESPONSE.methods.update({
    "read": sync_method(BYTES, ("amt", OptionalType(INT), "std::nullopt")),
    "isclosed": sync_method(BOOL),
    "readline": sync_method(BYTES),
    "readlines": sync_method(ListType(BYTES)),
    "getheader": sync_method(OPT_STR, ("name", STR), ("default", OPT_STR, "std::nullopt")),
    "getheaders": sync_method(ListType(TupleType((STR, STR)))),
    "geturl": sync_method(STR),
    "getcode": sync_method(INT),
    "info": sync_method(HTTP_HEADERS),
    "close": sync_method(NONE),
})
HTTP_RESPONSE.attributes.update({
    "status": lambda t: INT, "code": lambda t: INT, "reason": lambda t: STR, "url": lambda t: STR,
    "headers": lambda t: HTTP_HEADERS, "msg": lambda t: HTTP_HEADERS, "version": lambda t: INT,
    "closed": lambda t: BOOL,
})


def headers_get(ctx: CallContext) -> Type:
    """headers.get(name, failobj=None): a str when the default is one (it can't be None then)."""
    base = HEADERS_GET(ctx)
    node = ctx.args[1] if len(ctx.args) > 1 else ctx.keyword_arg("failobj")
    return STR if node is not None and node.ty == STR else base


HEADERS_GET = sync_method(OPT_STR, ("name", STR), ("failobj", OneOf(STR, NONE, what="a str or None"), "std::nullopt"))
headers_get.params, headers_get.resolve = HEADERS_GET.params, HEADERS_GET.resolve
HTTP_HEADERS.methods.update({
    "get": headers_get,
    "get_all": sync_method(OptionalType(ListType(STR)), ("name", STR)),
    "items": sync_method(ListType(TupleType((STR, STR)))),
    "keys": sync_method(ListType(STR)),
    "values": sync_method(ListType(STR)),
    "get_content_type": sync_method(STR),
    "get_content_charset": sync_method(OPT_STR),
})
URL_REQUEST.methods.update({
    "add_header": sync_method(NONE, ("key", STR), ("val", STR)),
    "has_header": sync_method(BOOL, ("header_name", STR)),
    "get_header": sync_method(OPT_STR, ("header_name", STR), ("default", OPT_STR, "std::nullopt")),
    "get_method": sync_method(STR),
    "get_full_url": sync_method(STR),
    "header_items": sync_method(ListType(TupleType((STR, STR)))),
})
URL_REQUEST.attributes.update({
    "full_url": lambda t: STR, "data": lambda t: OptionalType(BYTES), "method": lambda t: OptionalType(STR),
    "headers": lambda t: DictType(STR, STR),
})
URL_PARTS.methods["geturl"] = sync_method(STR)
URL_PARTS.attributes.update({
    **{f: (lambda t: STR) for f in ("scheme", "netloc", "path", "params", "query", "fragment")},
    "hostname": lambda t: OptionalType(STR), "port": lambda t: OptionalType(INT),
    "username": lambda t: OptionalType(STR), "password": lambda t: OptionalType(STR),
})
EXCEPTION_METHODS = {  # methods of built-in classes (by C++ name): HTTPError is also a response
    "sd::ssl::SSLContext": {
        "load_default_certs": sync_method(NONE),
        "load_cert_chain": sync_method(NONE, ("certfile", OneOf(STR, PATH, what="a str or Path")),
                                       ("keyfile", OneOf(STR, PATH, NONE, what="a str, a Path or None"), "std::nullopt"),
                                       ("password", OptionalType(STR), "std::nullopt")),
        "wrap_socket": sync_method(SSL_SOCKET, ("sock", SOCKET), ("server_side", BOOL, "false"),
                                   ("do_handshake_on_connect", BOOL, "true"), ("suppress_ragged_eofs", BOOL, "true"),
                                   ("server_hostname", OPT_STR, "std::nullopt")),
        "load_verify_locations": sync_method(NONE, ("cafile", OPT_STR, "std::nullopt"), ("capath", OPT_STR, "std::nullopt"),
                                             ("cadata", OPT_STR, "std::nullopt")),
    },
    "sd::urlerror::HTTPError": {
        "read": sync_method(BYTES, ("amt", OptionalType(INT), "std::nullopt")),
        "getcode": sync_method(INT),
        "geturl": sync_method(STR),
        "info": sync_method(HTTP_HEADERS),
    },
}


def url_data(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node, BYTES)
    if t not in (BYTES, BYTEARRAY):
        hint = "; use s.encode() (or urlencode(fields).encode() for a form)" if t == STR else ""
        raise ctx.error(f"{what} must be bytes, not {t}{hint}", node)


def url_request(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("url", STR), ("data", None, True), ("headers", None, True), ("origin_req_host", None, True),
                           ("unverifiable", None, True), ("method", None, True)))
    ctx.checker.expect_type(args["url"], STR, "Request url")
    if "data" in args and not isinstance(args["data"], A.NoneLit):
        url_data(ctx, args["data"], "Request data")
    if "headers" in args:
        ctx.checker.expect_type(args["headers"], DictType(STR, STR), "Request headers")
    if "method" in args and not isinstance(args["method"], A.NoneLit):
        ctx.checker.expect_type(args["method"], STR, "Request method")
    for ignored in ("origin_req_host", "unverifiable"):
        if ignored in args:
            ctx.checker.check_expr(args[ignored])
    ctx.call.notes["url_args"] = args
    return URL_REQUEST


def url_open(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("url", None), ("data", None, True), ("timeout", None, True), ("context", None, True)))
    if "context" in args and not isinstance(args["context"], A.NoneLit):
        ctx.checker.expect_type(args["context"], SSL_CONTEXT, "urlopen() context")
    t = ctx.checker.check_expr(args["url"])
    if t not in (STR, URL_REQUEST):
        raise ctx.error(f"urlopen() needs a URL (str) or a Request, not {t}", args["url"])
    if "data" in args and not isinstance(args["data"], A.NoneLit):
        url_data(ctx, args["data"], "urlopen() data")
    if "timeout" in args and not isinstance(args["timeout"], A.NoneLit):
        ctx.checker.expect_type(args["timeout"], FLOAT, "urlopen() timeout")
    ctx.call.notes["url_args"] = args
    return HTTP_RESPONSE


def url_encode(ctx: CallContext) -> Type:
    ctx.arity(1, 2, keywords=("doseq",))
    t = ctx.arg(0)
    pairs = isinstance(t, DictType) or isinstance(element_type(t) or NONE, TupleType)
    if not pairs:
        raise ctx.error(f"urlencode() needs a dict or a list of (key, value) pairs, not {t}", ctx.args[0])
    if len(ctx.args) == 2:
        ctx.expect(1, BOOL)
    ctx.keyword("doseq", BOOL)
    return STR


URL_ERROR = StructType("URLError", "class", None, base=EXCEPTIONS["OSError"], builtin=True, cpp_name="sd::urlerror::URLError")
URL_ERROR.fields["reason"] = Field("reason", STR, None, Loc(0, 0))
HTTP_ERROR = StructType("HTTPError", "class", None, base=URL_ERROR, builtin=True, cpp_name="sd::urlerror::HTTPError")
for _field, _t in (("code", INT), ("msg", STR), ("headers", HTTP_HEADERS), ("url", STR)):
    HTTP_ERROR.fields[_field] = Field(_field, _t, None, Loc(0, 0))
URL_PARSE = module_with_params(runtime_module(
    "urllib.parse", "modules/urllib.hpp", ("ssl", "crypto"),
    quote=(signature(STR, ("string", STR), ("safe", STR, '"/"s')), "sd::urlparse::quote"),
    quote_plus=(signature(STR, ("string", STR), ("safe", STR, '""s')), "sd::urlparse::quote_plus"),
    unquote=(signature(STR, ("string", STR)), "sd::urlparse::unquote"),
    unquote_plus=(signature(STR, ("string", STR)), "sd::urlparse::unquote_plus"),
    urlencode=(url_encode, None),
    urlparse=(signature(URL_PARTS, ("urlstring", STR), ("scheme", STR, '""s'), ("allow_fragments", BOOL, "true")),
              "sd::urlparse::urlparse"),
    urlsplit=(signature(URL_PARTS, ("urlstring", STR), ("scheme", STR, '""s'), ("allow_fragments", BOOL, "true")),
              "sd::urlparse::urlsplit"),
    urljoin=(signature(STR, ("base", STR), ("url", STR), ("allow_fragments", BOOL, "true")), "sd::urlparse::urljoin"),
    parse_qs=(signature(DictType(STR, ListType(STR)), ("qs", STR), ("keep_blank_values", BOOL, "false"),
                        ("strict_parsing", BOOL, "false")), "sd::urlparse::parse_qs"),
    parse_qsl=(signature(ListType(TupleType((STR, STR))), ("qs", STR), ("keep_blank_values", BOOL, "false"),
                         ("strict_parsing", BOOL, "false")), "sd::urlparse::parse_qsl"),
))
URL_REQUEST_MOD = module_with_params(runtime_module(
    "urllib.request", "modules/urllib.hpp", ("ssl", "crypto"),
    urlopen=(url_open, None),
    Request=(url_request, None),
))
URL_REQUEST_MOD.members["Request"].as_type = URL_REQUEST
URL_ERROR_MOD = Module("urllib.error", {"URLError": URL_ERROR, "HTTPError": HTTP_ERROR}, "modules/urllib.hpp",
                       ("ssl", "crypto"))
MODULES["urllib"] = Module("urllib", {"parse": URL_PARSE, "request": URL_REQUEST_MOD, "error": URL_ERROR_MOD})
