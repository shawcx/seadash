// The `os` and `os.path` modules: a small, commonly used subset on top of
// std::filesystem, raising Python's OSError subclasses ([Errno 2] ...).
#pragma once

#include <filesystem>
#include <fcntl.h>
#include <unistd.h>

namespace sd::os {

// ---- file descriptors: the program does the bookkeeping (os.close what os.open gave) ----

inline std::int64_t open(const std::string& path, std::int64_t flags, std::int64_t mode = 0777) {
    int fd;
    do fd = ::open(path.c_str(), static_cast<int>(flags), static_cast<mode_t>(mode));
    while (fd < 0 && errno == EINTR);
    if (fd < 0) raise_os(errno, path);
    return fd;
}
inline void close(std::int64_t fd) {
    if (::close(static_cast<int>(fd)) != 0 && errno != EINTR) raise_os(errno, std::nullopt);
}
inline bytes read(std::int64_t fd, std::int64_t n) {
    if (n < 0) raise("ValueError", "negative buffersize in read");
    std::string buf(static_cast<std::size_t>(n), '\0');
    ssize_t k;
    do k = ::read(static_cast<int>(fd), buf.data(), buf.size());
    while (k < 0 && errno == EINTR);
    if (k < 0) raise_os(errno, std::nullopt);
    buf.resize(static_cast<std::size_t>(k));
    return bytes(std::move(buf));
}
inline std::int64_t write(std::int64_t fd, const bytes& data) {
    ssize_t k;
    do k = ::write(static_cast<int>(fd), data.data.data(), data.data.size());
    while (k < 0 && errno == EINTR);
    if (k < 0) raise_os(errno, std::nullopt);
    return k;
}
inline std::int64_t dup(std::int64_t fd) {
    int copy = ::dup(static_cast<int>(fd));
    if (copy < 0) raise_os(errno, std::nullopt);
    return copy;
}
inline std::tuple<std::int64_t, std::int64_t> pipe() {
    int fds[2];
    if (::pipe(fds) != 0) raise_os(errno, std::nullopt);
    return {fds[0], fds[1]};
}

namespace fs = std::filesystem;

namespace path {

inline std::string sep() { return std::string(1, fs::path::preferred_separator); }

inline bool exists(const std::string& p) {
    std::error_code ec;
    return fs::exists(p, ec);
}
inline bool isfile(const std::string& p) {
    std::error_code ec;
    return fs::is_regular_file(p, ec);
}
inline bool isdir(const std::string& p) {
    std::error_code ec;
    return fs::is_directory(p, ec);
}

// Like Python: an absolute component discards everything before it.
template <class... Parts>
std::string join(const std::string& first, const Parts&... rest) {
    fs::path out(first);
    ((out /= fs::path(rest)), ...);
    return out.string();
}

// os.path.join(*parts), or (a, *parts): the parts in a list.
inline std::string join_list(const list<std::string>& parts) {
    if (parts.empty()) raise("TypeError", "join() missing 1 required positional argument: 'a'");
    fs::path out(parts[0]);
    for (std::size_t i = 1; i < parts.size(); ++i) out /= fs::path(parts[i]);
    return out.string();
}

inline std::string basename(const std::string& p) {
    auto slash = p.find_last_of('/');
    return slash == std::string::npos ? p : p.substr(slash + 1);
}
inline std::string dirname(const std::string& p) {
    auto slash = p.find_last_of('/');
    if (slash == std::string::npos) return "";
    std::string head = p.substr(0, slash + 1);
    // strip trailing slashes, but keep a lone "/"
    while (head.size() > 1 && head.back() == '/') head.pop_back();
    return head;
}
inline std::tuple<std::string, std::string> splitext(const std::string& p) {
    std::string base = basename(p);
    auto first_real = base.find_first_not_of('.');  // leading dots don't start an extension (.bashrc)
    auto dot = base.find_last_of('.');
    if (dot == std::string::npos || first_real == std::string::npos || dot < first_real) return {p, ""};
    std::size_t cut = p.size() - (base.size() - dot);
    return {p.substr(0, cut), p.substr(cut)};
}
inline std::string abspath(const std::string& p) {
    std::string out = fs::absolute(fs::path(p)).lexically_normal().string();
    while (out.size() > 1 && out.back() == '/') out.pop_back();
    return out;
}
inline std::int64_t getsize(const std::string& p) {
    std::error_code ec;
    auto size = fs::file_size(p, ec);
    if (ec) raise_os(ec.value(), p);
    return static_cast<std::int64_t>(size);
}

}  // namespace path

inline std::string getcwd() { return fs::current_path().string(); }

inline std::vector<std::string> listdir(const std::string& p = ".") {
    std::error_code ec;
    fs::directory_iterator it(p, ec);
    if (ec) raise_os(ec.value(), p);
    std::vector<std::string> names;
    for (const auto& entry : it) names.push_back(entry.path().filename().string());
    return names;
}

inline void mkdir(const std::string& p) {
    if (::mkdir(p.c_str(), 0777) != 0) raise_os(errno, p);
}

inline void makedirs(const std::string& p, bool exist_ok = false) {
    std::error_code ec;
    if (fs::exists(p, ec)) {
        if (exist_ok && fs::is_directory(p, ec)) return;
        raise_os(EEXIST, p);
    }
    fs::create_directories(p, ec);
    if (ec) raise_os(ec.value(), p);
}

inline void remove(const std::string& p) {
    if (::unlink(p.c_str()) != 0) raise_os(errno, p);
}

inline void chmod(const std::string& p, std::int64_t mode) {
    if (::chmod(p.c_str(), static_cast<mode_t>(mode)) != 0) raise_os(errno, p);
}

inline void rmdir(const std::string& p) {
    if (::rmdir(p.c_str()) != 0) raise_os(errno, p);
}

inline void rename(const std::string& src, const std::string& dst) {
    if (std::rename(src.c_str(), dst.c_str()) != 0) raise_os(errno, src, dst);
}
inline void symlink(const std::string& src, const std::string& dst) {
    if (::symlink(src.c_str(), dst.c_str()) != 0) raise_os(errno, src, dst);
}
inline void chdir(const std::string& path) {
    if (::chdir(path.c_str()) != 0) raise_os(errno, path);
}

inline std::optional<std::string> getenv(const std::string& key) {
    const char* v = std::getenv(key.c_str());
    return v ? std::optional<std::string>(v) : std::nullopt;
}
inline std::string getenv(const std::string& key, const std::string& fallback) {
    const char* v = std::getenv(key.c_str());
    return v ? std::string(v) : fallback;
}

}  // namespace sd::os
