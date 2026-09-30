// The `errno` module: the error numbers, and errorcode (number -> name).
#pragma once

namespace sd::errnomod {

#define SD_ERRNO_NAMES(X) \
    X(EPERM) \
    X(ENOENT) \
    X(ESRCH) \
    X(EINTR) \
    X(EIO) \
    X(ENXIO) \
    X(E2BIG) \
    X(ENOEXEC) \
    X(EBADF) \
    X(ECHILD) \
    X(EAGAIN) \
    X(ENOMEM) \
    X(EACCES) \
    X(EFAULT) \
    X(ENOTBLK) \
    X(EBUSY) \
    X(EEXIST) \
    X(EXDEV) \
    X(ENODEV) \
    X(ENOTDIR) \
    X(EISDIR) \
    X(EINVAL) \
    X(ENFILE) \
    X(EMFILE) \
    X(ENOTTY) \
    X(ETXTBSY) \
    X(EFBIG) \
    X(ENOSPC) \
    X(ESPIPE) \
    X(EROFS) \
    X(EMLINK) \
    X(EPIPE) \
    X(EDOM) \
    X(ERANGE) \
    X(EDEADLK) \
    X(ENAMETOOLONG) \
    X(ENOLCK) \
    X(ENOSYS) \
    X(ENOTEMPTY) \
    X(ELOOP) \
    X(EWOULDBLOCK) \
    X(ENOMSG) \
    X(EIDRM) \
    X(ENOSTR) \
    X(ENODATA) \
    X(ETIME) \
    X(ENOSR) \
    X(EREMOTE) \
    X(ENOLINK) \
    X(EPROTO) \
    X(EMULTIHOP) \
    X(EBADMSG) \
    X(EOVERFLOW) \
    X(EILSEQ) \
    X(EUSERS) \
    X(ENOTSOCK) \
    X(EDESTADDRREQ) \
    X(EMSGSIZE) \
    X(EPROTOTYPE) \
    X(ENOPROTOOPT) \
    X(EPROTONOSUPPORT) \
    X(ESOCKTNOSUPPORT) \
    X(EOPNOTSUPP) \
    X(ENOTSUP) \
    X(EPFNOSUPPORT) \
    X(EAFNOSUPPORT) \
    X(EADDRINUSE) \
    X(EADDRNOTAVAIL) \
    X(ENETDOWN) \
    X(ENETUNREACH) \
    X(ENETRESET) \
    X(ECONNABORTED) \
    X(ECONNRESET) \
    X(ENOBUFS) \
    X(EISCONN) \
    X(ENOTCONN) \
    X(ESHUTDOWN) \
    X(ETOOMANYREFS) \
    X(ETIMEDOUT) \
    X(ECONNREFUSED) \
    X(EHOSTDOWN) \
    X(EHOSTUNREACH) \
    X(EALREADY) \
    X(EINPROGRESS) \
    X(ESTALE) \
    X(EDQUOT) \
    X(ECANCELED) \
    X(EOWNERDEAD) \
    X(ENOTRECOVERABLE)

inline const dict<std::int64_t, std::string>& errorcode() {
    static const dict<std::int64_t, std::string> codes = [] {
        dict<std::int64_t, std::string> out;
#define X(name) out[name] = #name;
        SD_ERRNO_NAMES(X)
#undef X
        return out;
    }();
    return codes;
}

}  // namespace sd::errnomod
