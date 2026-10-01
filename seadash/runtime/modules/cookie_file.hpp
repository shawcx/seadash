// A FILE* over read/write/close callbacks (funopen on macOS, fopencookie with glibc), so a
// compressed stream (gzip, bz2, lzma) can be an ordinary text or binary file object.
#pragma once

#include <fcntl.h>

namespace sd {

struct Cookie {
    void* state;
    std::int64_t (*read)(void*, char*, std::size_t);         // bytes read; 0 at the end; -1 on error (errno set)
    std::int64_t (*write)(void*, const char*, std::size_t);  // bytes written; -1 on error
    int (*close)(void*);                                     // 0, or -1 on error
};

inline std::FILE* cookie_file(Cookie cookie, bool reading) {
    auto* c = new Cookie(cookie);
#ifdef __APPLE__
    auto read = [](void* p, char* buf, int n) -> int {
        auto* k = static_cast<Cookie*>(p);
        return static_cast<int>(k->read(k->state, buf, static_cast<std::size_t>(n)));
    };
    auto write = [](void* p, const char* buf, int n) -> int {
        auto* k = static_cast<Cookie*>(p);
        return static_cast<int>(k->write(k->state, buf, static_cast<std::size_t>(n)));
    };
    auto close = [](void* p) -> int {
        auto* k = static_cast<Cookie*>(p);
        int rc = k->close(k->state);
        delete k;
        return rc;
    };
    std::FILE* f = funopen(c, reading ? read : nullptr, reading ? nullptr : write, nullptr, close);
#else
    cookie_io_functions_t io{};
    io.read = [](void* p, char* buf, size_t n) -> ssize_t {
        auto* k = static_cast<Cookie*>(p);
        return static_cast<ssize_t>(k->read(k->state, buf, n));
    };
    io.write = [](void* p, const char* buf, size_t n) -> ssize_t {
        auto* k = static_cast<Cookie*>(p);
        std::int64_t w = k->write(k->state, buf, n);
        return w < 0 ? 0 : static_cast<ssize_t>(w);  // (fopencookie takes 0 as the error)
    };
    io.close = [](void* p) -> int {
        auto* k = static_cast<Cookie*>(p);
        int rc = k->close(k->state);
        delete k;
        return rc;
    };
    std::FILE* f = fopencookie(c, reading ? "r" : "w", io);
#endif
    if (!f) delete c;
    return f;
}

// The callbacks can't throw through C stdio: an error is parked for the file object.
template <class F>
std::int64_t guarded(F&& f) {
    try {
        return f();
    } catch (...) {
        pending_file_error = std::current_exception();
        errno = EIO;
        return -1;
    }
}

// open() for a compressed file: the file descriptor, checked like the builtin open().
inline int open_fd_for(const std::string& path, const std::string& mode) {
    char kind = mode[mode.find_first_of("rwax")];
    int flags = kind == 'r' ? O_RDONLY : kind == 'w' ? O_WRONLY | O_CREAT | O_TRUNC : kind == 'a' ? O_WRONLY | O_CREAT | O_APPEND : O_WRONLY | O_CREAT | O_EXCL;
    int fd = ::open(path.c_str(), flags | O_CLOEXEC, 0666);
    if (fd < 0) raise_os(errno, path);
    struct stat info;
    if (fstat(fd, &info) == 0 && S_ISDIR(info.st_mode)) {
        ::close(fd);
        raise_os(EISDIR, path);
    }
    return fd;
}

}  // namespace sd
