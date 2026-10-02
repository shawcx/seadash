"""`socket`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    BYTES_OR_STR, EXCEPTIONS, MODULES, OPT_FLOAT, exception_class, module_with_params, runtime_module, signature,
    sync_method,
)
from ..types import BOOL, BYTES, INT, NONE, OptionalType, SOCKET, STR, TupleType


ADDRESS = TupleType((STR, INT))
SOCKET.methods.update({
    "connect": sync_method(NONE, ("address", ADDRESS)),
    "bind": sync_method(NONE, ("address", ADDRESS)),
    "listen": sync_method(NONE, ("backlog", INT, "128_i")),
    "accept": sync_method(TupleType((SOCKET, ADDRESS))),
    "send": sync_method(INT, ("data", BYTES_OR_STR)),
    "sendall": sync_method(NONE, ("data", BYTES_OR_STR)),
    "recv": sync_method(BYTES, ("bufsize", INT)),
    "sendto": sync_method(INT, ("data", BYTES_OR_STR), ("address", ADDRESS)),
    "recvfrom": sync_method(TupleType((BYTES, ADDRESS)), ("bufsize", INT)),
    "settimeout": sync_method(NONE, ("value", OPT_FLOAT)),
    "gettimeout": sync_method(OPT_FLOAT),
    "setblocking": sync_method(NONE, ("flag", BOOL)),
    "getblocking": sync_method(BOOL),
    "setsockopt": sync_method(NONE, ("level", INT), ("optname", INT), ("value", INT)),
    "getsockname": sync_method(ADDRESS),
    "getpeername": sync_method(ADDRESS),
    "shutdown": sync_method(NONE, ("how", INT)),
    "close": sync_method(NONE),
    "fileno": sync_method(INT),
})
SOCKET_CONSTANTS = [
    "AF_INET", "AF_INET6", "AF_UNSPEC", "SOCK_STREAM", "SOCK_DGRAM", "SOL_SOCKET", "SO_REUSEADDR",
    "SO_REUSEPORT", "SO_KEEPALIVE", "SO_BROADCAST", "IPPROTO_TCP", "IPPROTO_UDP", "TCP_NODELAY",
    "SHUT_RD", "SHUT_WR", "SHUT_RDWR",
]
MODULES["socket"] = module_with_params(runtime_module(
    "socket", "modules/socket.hpp",
    socket=(signature(SOCKET, ("family", INT, "static_cast<std::int64_t>(AF_INET)"),
                      ("type", INT, "static_cast<std::int64_t>(SOCK_STREAM)"), ("proto", INT, "0_i"),
                      ("fileno", OptionalType(INT), "std::nullopt")), "sd::socket::Socket"),
    create_connection=(signature(SOCKET, ("address", ADDRESS), ("timeout", OPT_FLOAT, "std::nullopt")),
                       "sd::socket::create_connection"),
    create_server=(signature(SOCKET, ("address", ADDRESS), ("family", INT, "static_cast<std::int64_t>(AF_INET)"),
                             ("backlog", OptionalType(INT), "std::nullopt"), ("reuse_port", BOOL, "false")),
                   "sd::socket::create_server"),
    gethostname=(signature(STR), "sd::socket::gethostname"),
    gethostbyname=(signature(STR, ("hostname", STR)), "sd::socket::gethostbyname"),
    gaierror=exception_class("gaierror", "sd::socket::gaierror", "OSError"),
    **{c: (INT, f"static_cast<std::int64_t>({c})") for c in SOCKET_CONSTANTS},
))
MODULES["socket"].members["socket"].as_type = SOCKET
MODULES["socket"].members["timeout"] = EXCEPTIONS["TimeoutError"]  # socket.timeout is TimeoutError
MODULES["socket"].members["error"] = EXCEPTIONS["OSError"]  # socket.error is OSError
