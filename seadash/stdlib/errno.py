"""`errno`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, runtime_module
from ..types import DictType, INT, STR


ERRNO_NAMES = (
    "EPERM", "ENOENT", "ESRCH", "EINTR", "EIO", "ENXIO", "E2BIG", "ENOEXEC", "EBADF",
    "ECHILD", "EAGAIN", "ENOMEM", "EACCES", "EFAULT", "ENOTBLK", "EBUSY", "EEXIST", "EXDEV",
    "ENODEV", "ENOTDIR", "EISDIR", "EINVAL", "ENFILE", "EMFILE", "ENOTTY", "ETXTBSY", "EFBIG",
    "ENOSPC", "ESPIPE", "EROFS", "EMLINK", "EPIPE", "EDOM", "ERANGE", "EDEADLK", "ENAMETOOLONG",
    "ENOLCK", "ENOSYS", "ENOTEMPTY", "ELOOP", "EWOULDBLOCK", "ENOMSG", "EIDRM", "ENOSTR", "ENODATA",
    "ETIME", "ENOSR", "EREMOTE", "ENOLINK", "EPROTO", "EMULTIHOP", "EBADMSG", "EOVERFLOW", "EILSEQ",
    "EUSERS", "ENOTSOCK", "EDESTADDRREQ", "EMSGSIZE", "EPROTOTYPE", "ENOPROTOOPT", "EPROTONOSUPPORT", "ESOCKTNOSUPPORT", "EOPNOTSUPP",
    "ENOTSUP", "EPFNOSUPPORT", "EAFNOSUPPORT", "EADDRINUSE", "EADDRNOTAVAIL", "ENETDOWN", "ENETUNREACH", "ENETRESET", "ECONNABORTED",
    "ECONNRESET", "ENOBUFS", "EISCONN", "ENOTCONN", "ESHUTDOWN", "ETOOMANYREFS", "ETIMEDOUT", "ECONNREFUSED", "EHOSTDOWN",
    "EHOSTUNREACH", "EALREADY", "EINPROGRESS", "ESTALE", "EDQUOT", "ECANCELED", "EOWNERDEAD", "ENOTRECOVERABLE",
)
MODULES["errno"] = runtime_module(
    "errno", "modules/errno.hpp",
    **{name: (INT, f"static_cast<std::int64_t>({name})") for name in ERRNO_NAMES},
    errorcode=(DictType(INT, STR), "sd::errnomod::errorcode()"),
)
