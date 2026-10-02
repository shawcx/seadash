"""`http.client` (and the `http` package): how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    bind_args, CallContext, exception_class, EXCEPTIONS, Module, module_with_params, MODULES, NamedType, OPT_FLOAT,
    runtime_module, signature, sync_method,
)
from ..errors import Loc
from ..types import (
    BOOL, BYTEARRAY, BYTES, DictType, Field, HTTP_CONNECTION, HTTP_HEADERS, HTTP_RESPONSE, INT, NONE, OptionalType,
    STR, StructType, Type,
)
from .ssl import SSL_CONTEXT


def http_request(ctx: CallContext) -> Type:
    """conn.request(method, url, body=None, headers={}): body is bytes, or str (sent as Latin-1)."""
    args = bind_args(ctx, (("method", STR), ("url", STR), ("body", None, True), ("headers", None, True),
                           ("encode_chunked", None, True)))
    ctx.checker.expect_type(args["method"], STR, "request() method")
    ctx.checker.expect_type(args["url"], STR, "request() url")
    if "body" in args and not isinstance(args["body"], A.NoneLit):
        t = ctx.checker.check_expr(args["body"])
        if t not in (BYTES, BYTEARRAY, STR):
            raise ctx.error(f"request() body must be bytes or str, not {t}", args["body"])
    if "headers" in args:
        ctx.checker.expect_type(args["headers"], DictType(STR, STR), "request() headers")
    if "encode_chunked" in args:
        raise ctx.error("request(encode_chunked=...) isn't supported yet", args["encode_chunked"])
    ctx.call.notes["http_args"] = args
    return NONE


HTTP_CONNECTION.methods.update({
    "request": http_request,
    "getresponse": sync_method(HTTP_RESPONSE),
    "connect": sync_method(NONE),
    "close": sync_method(NONE),
    "putrequest": sync_method(NONE, ("method", STR), ("url", STR), ("skip_host", BOOL, "false"),
                              ("skip_accept_encoding", BOOL, "false")),
    "putheader": sync_method(NONE, ("header", STR), ("value", STR)),
    "endheaders": sync_method(NONE, ("message_body", OptionalType(BYTES), "std::nullopt")),
    "send": sync_method(NONE, ("data", BYTES)),
    "set_debuglevel": sync_method(NONE, ("level", INT)),
})
HTTP_CONNECTION.attributes.update({"host": lambda t: STR, "port": lambda t: INT, "timeout": lambda t: OPT_FLOAT})


def http_exception(name: str, base: StructType) -> StructType:
    return exception_class(name, f"sd::httpclient::{name}", base)


HTTP_EXCEPTION = http_exception("HTTPException", EXCEPTIONS["Exception"])
IMPROPER_STATE = http_exception("ImproperConnectionState", HTTP_EXCEPTION)
INCOMPLETE_READ = http_exception("IncompleteRead", HTTP_EXCEPTION)
INCOMPLETE_READ.fields["partial"] = Field("partial", BYTES, None, Loc(0, 0))
INCOMPLETE_READ.fields["expected"] = Field("expected", OptionalType(INT), None, Loc(0, 0))
HTTP_CLIENT_MOD = module_with_params(runtime_module(
    "http.client", "modules/httpclient.hpp", ("ssl", "crypto"),
    HTTPConnection=(signature(HTTP_CONNECTION, ("host", STR), ("port", OptionalType(INT), "std::nullopt"),
                              ("timeout", OPT_FLOAT, "std::nullopt")), "sd::httpclient::HTTPConnection"),
    HTTPSConnection=(signature(HTTP_CONNECTION, ("host", STR), ("port", OptionalType(INT), "std::nullopt"),
                               ("timeout", OPT_FLOAT, "std::nullopt"), ("context", OptionalType(SSL_CONTEXT), "std::nullopt")),
                     "sd::httpclient::HTTPSConnection"),
    responses=(DictType(INT, STR), "sd::httpclient::responses()"),
    HTTPException=HTTP_EXCEPTION,
    NotConnected=http_exception("NotConnected", HTTP_EXCEPTION),
    InvalidURL=http_exception("InvalidURL", HTTP_EXCEPTION),
    ImproperConnectionState=IMPROPER_STATE,
    CannotSendRequest=http_exception("CannotSendRequest", IMPROPER_STATE),
    CannotSendHeader=http_exception("CannotSendHeader", IMPROPER_STATE),
    ResponseNotReady=http_exception("ResponseNotReady", IMPROPER_STATE),
    BadStatusLine=http_exception("BadStatusLine", HTTP_EXCEPTION),
    LineTooLong=http_exception("LineTooLong", HTTP_EXCEPTION),
    RemoteDisconnected=http_exception("RemoteDisconnected", EXCEPTIONS["ConnectionResetError"]),
    IncompleteRead=INCOMPLETE_READ,
    **{"ACCEPTED": (INT, "202_i"), "ALREADY_REPORTED": (INT, "208_i"), "BAD_GATEWAY": (INT, "502_i"), "BAD_REQUEST": (INT, "400_i"), "CONFLICT": (INT, "409_i"), "CONTINUE": (INT, "100_i"), "CREATED": (INT, "201_i"), "EARLY_HINTS": (INT, "103_i"), "EXPECTATION_FAILED": (INT, "417_i"), "FAILED_DEPENDENCY": (INT, "424_i"), "FORBIDDEN": (INT, "403_i"), "FOUND": (INT, "302_i"), "GATEWAY_TIMEOUT": (INT, "504_i"), "GONE": (INT, "410_i"), "HTTPS_PORT": (INT, "443_i"), "HTTP_PORT": (INT, "80_i"), "HTTP_VERSION_NOT_SUPPORTED": (INT, "505_i"), "IM_A_TEAPOT": (INT, "418_i"), "IM_USED": (INT, "226_i"), "INSUFFICIENT_STORAGE": (INT, "507_i"), "INTERNAL_SERVER_ERROR": (INT, "500_i"), "LENGTH_REQUIRED": (INT, "411_i"), "LOCKED": (INT, "423_i"), "LOOP_DETECTED": (INT, "508_i"), "METHOD_NOT_ALLOWED": (INT, "405_i"), "MISDIRECTED_REQUEST": (INT, "421_i"), "MOVED_PERMANENTLY": (INT, "301_i"), "MULTIPLE_CHOICES": (INT, "300_i"), "MULTI_STATUS": (INT, "207_i"), "NETWORK_AUTHENTICATION_REQUIRED": (INT, "511_i"), "NON_AUTHORITATIVE_INFORMATION": (INT, "203_i"), "NOT_ACCEPTABLE": (INT, "406_i"), "NOT_EXTENDED": (INT, "510_i"), "NOT_FOUND": (INT, "404_i"), "NOT_IMPLEMENTED": (INT, "501_i"), "NOT_MODIFIED": (INT, "304_i"), "NO_CONTENT": (INT, "204_i"), "OK": (INT, "200_i"), "PARTIAL_CONTENT": (INT, "206_i"), "PAYMENT_REQUIRED": (INT, "402_i"), "PERMANENT_REDIRECT": (INT, "308_i"), "PRECONDITION_FAILED": (INT, "412_i"), "PRECONDITION_REQUIRED": (INT, "428_i"), "PROCESSING": (INT, "102_i"), "PROXY_AUTHENTICATION_REQUIRED": (INT, "407_i"), "REQUESTED_RANGE_NOT_SATISFIABLE": (INT, "416_i"), "REQUEST_ENTITY_TOO_LARGE": (INT, "413_i"), "REQUEST_HEADER_FIELDS_TOO_LARGE": (INT, "431_i"), "REQUEST_TIMEOUT": (INT, "408_i"), "REQUEST_URI_TOO_LONG": (INT, "414_i"), "RESET_CONTENT": (INT, "205_i"), "SEE_OTHER": (INT, "303_i"), "SERVICE_UNAVAILABLE": (INT, "503_i"), "SWITCHING_PROTOCOLS": (INT, "101_i"), "TEMPORARY_REDIRECT": (INT, "307_i"), "TOO_EARLY": (INT, "425_i"), "TOO_MANY_REQUESTS": (INT, "429_i"), "UNAUTHORIZED": (INT, "401_i"), "UNAVAILABLE_FOR_LEGAL_REASONS": (INT, "451_i"), "UNPROCESSABLE_ENTITY": (INT, "422_i"), "UNSUPPORTED_MEDIA_TYPE": (INT, "415_i"), "UPGRADE_REQUIRED": (INT, "426_i"), "USE_PROXY": (INT, "305_i"), "VARIANT_ALSO_NEGOTIATES": (INT, "506_i"), "_MAXHEADERS": (INT, "100_i"), "_MAXLINE": (INT, "65536_i"), "_MIN_READ_BUF_SIZE": (INT, "1048576_i")},
))
for _name in ("HTTPConnection", "HTTPSConnection"):
    HTTP_CLIENT_MOD.members[_name].as_type = HTTP_CONNECTION
HTTP_CLIENT_MOD.members["HTTPResponse"] = NamedType("HTTPResponse", HTTP_RESPONSE)
HTTP_CLIENT_MOD.members["HTTPMessage"] = NamedType("HTTPMessage", HTTP_HEADERS)
MODULES["http"] = Module("http", {"client": HTTP_CLIENT_MOD})  # (http.server adds itself)
