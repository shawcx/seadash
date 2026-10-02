// The `os` and `os.path` modules: a small, commonly used subset on top of
// std::filesystem, raising Python's OSError subclasses ([Errno 2] ...).
#pragma once

#include <dirent.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <filesystem>
#include <variant>

#include "pathlib.hpp"

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

// ---- processes ----------------------------------------------------------------

inline std::int64_t getpid() { return ::getpid(); }

// Set by the signal module (modules/signal.hpp): sends a signal to this process and waits
// until its handler has run, as Python's os.kill() does (it runs pending handlers before it
// returns).
inline void (*kill_self)(std::int64_t sig) = nullptr;

inline void kill(std::int64_t pid, std::int64_t sig) {
    if (kill_self != nullptr && pid == ::getpid()) {
        kill_self(sig);
        return;
    }
    if (::kill(static_cast<pid_t>(pid), static_cast<int>(sig)) != 0) raise_os(errno, std::nullopt);
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

inline bool islink(const std::string& p) {
    struct stat st;
    return ::lstat(p.c_str(), &st) == 0 && S_ISLNK(st.st_mode);
}
inline std::string normpath(const std::string& path) {  // posixpath.normpath
    if (path.empty()) return ".";
    std::size_t slashes = path.starts_with('/') ? (path.starts_with("//") && !path.starts_with("///") ? 2 : 1) : 0;
    std::vector<std::string> parts;
    for (std::size_t start = 0; start <= path.size();) {
        std::size_t end = std::min(path.find('/', start), path.size());
        std::string comp = path.substr(start, end - start);
        start = end + 1;
        if (comp.empty() || comp == ".") continue;
        if (comp != ".." || (!slashes && parts.empty()) || (!parts.empty() && parts.back() == "..")) parts.push_back(comp);
        else if (!parts.empty()) parts.pop_back();
    }
    std::string out(slashes, '/');
    for (std::size_t i = 0; i < parts.size(); ++i) out += (i ? "/" : "") + parts[i];
    return out.empty() ? "." : out;
}
// os.path.relpath(path, start=os.curdir)
inline std::string relpath(const std::string& p, const std::optional<std::string>& start = std::nullopt) {
    if (p.empty()) raise("ValueError", "no path specified");
    auto split = [](const std::string& x) {
        std::string a = normpath(x.starts_with('/') ? x : fs::current_path().string() + "/" + x);
        std::vector<std::string> parts;
        for (std::size_t s = 0; s <= a.size();) {
            std::size_t e = std::min(a.find('/', s), a.size());
            if (e > s) parts.push_back(a.substr(s, e - s));
            s = e + 1;
        }
        return parts;
    };
    auto from = split(start.value_or(".")), to = split(p);
    std::size_t i = 0;
    while (i < from.size() && i < to.size() && from[i] == to[i]) ++i;
    std::string out;
    for (std::size_t k = i; k < from.size(); ++k) out += (out.empty() ? "" : "/") + std::string("..");
    for (std::size_t k = i; k < to.size(); ++k) out += (out.empty() ? "" : "/") + to[k];
    return out.empty() ? "." : out;
}
// os.path.realpath(path, strict=False): absolute, with every symlink resolved
inline std::string realpath(const std::string& p, bool strict = false) {
    if (strict) {
        struct stat st;
        if (::stat(p.c_str(), &st) != 0) raise_os(errno, p);
    }
    return pathlib::Path(p).resolve().str();
}

}  // namespace path

inline std::string getcwd() { return fs::current_path().string(); }

// os.stat(path, follow_symlinks=True), os.lstat(path), os.stat(fd)
inline pathlib::StatResult stat(const pathlib::Path& p, bool follow_symlinks = true) {
    struct stat st;
    if ((follow_symlinks ? ::stat(p.str().c_str(), &st) : ::lstat(p.str().c_str(), &st)) != 0) raise_os(errno, p.str());
    return pathlib::stat_result(st);
}
inline pathlib::StatResult stat(std::int64_t fd, bool = true) {
    struct stat st;
    if (::fstat(static_cast<int>(fd), &st) != 0) raise_os(errno, std::nullopt);
    return pathlib::stat_result(st);
}
inline pathlib::StatResult lstat(const pathlib::Path& p) { return stat(p, false); }

// os.readlink(path)
inline std::string readlink(const pathlib::Path& p) { return pathlib::Path(p).readlink().str(); }

// os.utime(path, times=None, ns=None, follow_symlinks=True): times as (atime, mtime) seconds,
// or ns as nanoseconds; neither is now
inline void utime(const pathlib::Path& p, const std::optional<std::tuple<double, double>>& times = std::nullopt,
                  const std::optional<std::tuple<std::int64_t, std::int64_t>>& ns = std::nullopt, bool follow_symlinks = true) {
    if (times && ns) raise("ValueError", "utime: you may specify either 'times' or 'ns' but not both");
    timespec ts[2];
    auto from_seconds = [](double t) {
        double sec = std::floor(t);
        timespec out{static_cast<time_t>(sec), static_cast<long>(std::round((t - sec) * 1e9))};
        if (out.tv_nsec >= 1000000000) {
            ++out.tv_sec;
            out.tv_nsec -= 1000000000;
        }
        return out;
    };
    auto from_ns = [](std::int64_t n) {
        std::int64_t sec = n >= 0 ? n / 1000000000 : -((-n + 999999999) / 1000000000);
        return timespec{static_cast<time_t>(sec), static_cast<long>(n - sec * 1000000000)};
    };
    timespec* which = ts;
    if (times) {
        ts[0] = from_seconds(std::get<0>(*times));
        ts[1] = from_seconds(std::get<1>(*times));
    } else if (ns) {
        ts[0] = from_ns(std::get<0>(*ns));
        ts[1] = from_ns(std::get<1>(*ns));
    } else {
        which = nullptr;  // (now)
    }
    if (::utimensat(AT_FDCWD, p.str().c_str(), which, follow_symlinks ? 0 : AT_SYMLINK_NOFOLLOW) != 0) raise_os(errno, p.str());
}

// os.walk(top, topdown=True, onerror=None, followlinks=False): (dirpath, dirnames, filenames)
// for each directory, in the order the system lists them (as Python's scandir does). Changing
// dirnames in place (top-down) decides which directories it goes into, as in Python.
using WalkEntry = std::tuple<std::string, list<std::string>, list<std::string>>;
template <class OnError = std::nullopt_t>
Generator<WalkEntry> walk(std::string top, bool topdown = true, OnError onerror = std::nullopt, bool followlinks = false) {
    std::vector<std::variant<std::string, WalkEntry>> stack{top};
    auto join = [](const std::string& a, const std::string& b) { return a.empty() || a.ends_with('/') ? a + b : a + "/" + b; };
    while (!stack.empty()) {
        auto item = std::move(stack.back());
        stack.pop_back();
        if (auto* done = std::get_if<WalkEntry>(&item)) {
            co_yield std::move(*done);
            continue;
        }
        std::string dir = std::get<std::string>(item);
        list<std::string> dirs, nondirs;
        std::vector<std::string> walk_dirs;
        DIR* d = ::opendir(dir.c_str());
        if (!d) {
            if constexpr (!std::is_same_v<OnError, std::nullopt_t>) {
                try {
                    raise_os(errno, dir);
                } catch (const Thrown& t) {
                    onerror(std::dynamic_pointer_cast<OSError>(t.exc));
                }
            }
            continue;
        }
        while (dirent* e = ::readdir(d)) {
            std::string name = e->d_name;
            if (name == "." || name == "..") continue;
            std::string path = join(dir, name);
            struct stat st;
            bool is_dir = e->d_type == DT_DIR ||
                          ((e->d_type == DT_LNK || e->d_type == DT_UNKNOWN) && ::stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode));
            (is_dir ? dirs : nondirs).push_back(name);
            if (!topdown && is_dir) {
                bool symlink = e->d_type == DT_LNK || (e->d_type == DT_UNKNOWN && path::islink(path));
                if (followlinks || !symlink) walk_dirs.push_back(path);
            }
        }
        ::closedir(d);
        if (topdown) {
            co_yield WalkEntry{dir, dirs, nondirs};
            for (std::size_t i = dirs.size(); i-- > 0;) {  // (the names as the loop left them)
                std::string next = join(dir, dirs[i]);
                if (followlinks || !path::islink(next)) stack.push_back(next);
            }
        } else {
            stack.push_back(WalkEntry{dir, dirs, nondirs});
            for (std::size_t i = walk_dirs.size(); i-- > 0;) stack.push_back(walk_dirs[i]);
        }
    }
}

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
