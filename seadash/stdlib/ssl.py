"""`ssl`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, NamedType, exception_class, module_with_params, runtime_module, signature, sync_method
from ..errors import Loc
from ..types import BOOL, BYTES, FLOAT, Field, INT, NONE, OptionalType, SSL_SOCKET, STR, StructType, TupleType


SSL_CONTEXT = StructType("SSLContext", "class", None, builtin=True, cpp_name="sd::ssl::SSLContext")
SSL_CONTEXT.fields["check_hostname"] = Field("check_hostname", BOOL, None, Loc(0, 0))
SSL_CONTEXT.fields["verify_mode"] = Field("verify_mode", INT, None, Loc(0, 0))
SSL_ERROR = exception_class("SSLError", "sd::ssl::SSLError", "OSError")
SSL_CERT_ERROR = StructType("SSLCertVerificationError", "class", None, base=SSL_ERROR, builtin=True,
                            cpp_name="sd::ssl::SSLCertVerificationError")
MODULES["ssl"] = module_with_params(runtime_module(
    "ssl", "modules/ssl.hpp", ("ssl", "crypto"),
    SSLContext=(signature(SSL_CONTEXT, ("protocol", INT, "sd::ssl::PROTOCOL_TLS_CLIENT")), "sd::ssl::SSLContext_new"),
    create_default_context=(signature(SSL_CONTEXT, ("purpose", INT, "sd::ssl::PURPOSE_SERVER_AUTH"),
                                      ("cafile", OptionalType(STR), "std::nullopt"), ("capath", OptionalType(STR), "std::nullopt"),
                                      ("cadata", OptionalType(STR), "std::nullopt")), "sd::ssl::create_default_context"),
    SSLSocket=NamedType("SSLSocket", SSL_SOCKET),
    _create_unverified_context=(signature(SSL_CONTEXT), "sd::ssl::create_unverified_context"),
    SSLError=SSL_ERROR,
    SSLCertVerificationError=SSL_CERT_ERROR,
    **{c: (INT, f"sd::ssl::{c}") for c in ("CERT_NONE", "CERT_OPTIONAL", "CERT_REQUIRED", "PROTOCOL_TLS_CLIENT",
                                            "PROTOCOL_TLS_SERVER")},
))
MODULES["ssl"].members["SSLContext"].as_type = SSL_CONTEXT
ADDRESS = TupleType((STR, INT))
SSL_SOCKET.methods.update({
    "send": sync_method(INT, ("data", BYTES)),
    "sendall": sync_method(NONE, ("data", BYTES)),
    "recv": sync_method(BYTES, ("bufsize", INT)),
    "accept": sync_method(TupleType((SSL_SOCKET, ADDRESS))),
    "connect": sync_method(NONE, ("address", ADDRESS)),
    "close": sync_method(NONE),
    "fileno": sync_method(INT),
    "getpeername": sync_method(ADDRESS),
    "getsockname": sync_method(ADDRESS),
    "settimeout": sync_method(NONE, ("value", OptionalType(FLOAT))),
    "version": sync_method(OptionalType(STR)),
    "cipher": sync_method(OptionalType(TupleType((STR, STR, INT)))),
})
SSL_SOCKET.attributes.update({"server_side": lambda t: BOOL, "server_hostname": lambda t: OptionalType(STR),
                              "context": lambda t: SSL_CONTEXT})
MODULES["ssl"].members["Purpose"] = runtime_module(  # ssl.Purpose.SERVER_AUTH / CLIENT_AUTH
    "ssl.Purpose", "modules/ssl.hpp", ("ssl", "crypto"),
    SERVER_AUTH=(INT, "sd::ssl::PURPOSE_SERVER_AUTH"), CLIENT_AUTH=(INT, "sd::ssl::PURPOSE_CLIENT_AUTH"))
