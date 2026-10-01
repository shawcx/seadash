// The `glob` module: a port of Python's glob.py. Names are matched with fnmatch; `*`,
// `?` and `[...]` don't match a leading dot unless the pattern has one (or
// include_hidden=True); with recursive=True, `**` matches any files and zero or more
// directories. Results come in directory order (unsorted), as in Python. iglob is a
// generator: it reads directories as it goes. Unreadable directories are skipped.
#pragma once

#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "fnmatch.hpp"

namespace sd::glob {

namespace detail {

struct Options {
    int dir_fd = AT_FDCWD;  // relative paths are looked up from here
    bool recursive = false, include_hidden = false;
};

inline bool has_magic(const std::string& s) { return s.find_first_of("*?[") != std::string::npos; }
inline bool is_hidden(const std::string& name) { return !name.empty() && name[0] == '.'; }

inline std::string path_join(const std::string& a, const std::string& b) {  // os.path.join
    if (!b.empty() && b[0] == '/') return b;
    if (a.empty() || a.back() == '/') return a + b;
    return a + "/" + b;
}
inline std::string join(const std::string& a, const std::string& b) {  // glob._join
    if (a.empty() || b.empty()) return a.empty() ? b : a;
    return path_join(a, b);
}
inline std::pair<std::string, std::string> split(const std::string& p) {  // os.path.split
    std::size_t i = p.rfind('/') + 1;  // 0 if there's no slash
    std::string head = p.substr(0, i), tail = p.substr(i);
    if (!head.empty() && head.find_first_not_of('/') != std::string::npos) {
        while (head.back() == '/') head.pop_back();
    }
    return {head, tail};
}

inline bool lexists(const std::string& path, const Options& o) {
    struct stat st;
    return !path.empty() && ::fstatat(o.dir_fd, path.c_str(), &st, AT_SYMLINK_NOFOLLOW) == 0;
}
inline bool isdir(const std::string& path, const Options& o) {
    struct stat st;
    return !path.empty() && ::fstatat(o.dir_fd, path.c_str(), &st, 0) == 0 && S_ISDIR(st.st_mode);
}

// The names in a directory ("" is the current one), or only those of directories
// (following symlinks); none if it can't be read.
inline std::vector<std::string> listdir(const std::string& dirname, bool dironly, const Options& o) {
    std::vector<std::string> names;
    int fd = ::openat(o.dir_fd, dirname.empty() ? "." : dirname.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return names;
    DIR* dir = ::fdopendir(fd);
    if (!dir) {
        ::close(fd);
        return names;
    }
    while (dirent* e = ::readdir(dir)) {
        std::string name = e->d_name;
        if (name == "." || name == "..") continue;
        if (dironly) {
            bool is_dir = e->d_type == DT_DIR;
            if (e->d_type == DT_LNK || e->d_type == DT_UNKNOWN) {
                struct stat st;
                is_dir = ::fstatat(::dirfd(dir), e->d_name, &st, 0) == 0 && S_ISDIR(st.st_mode);
            }
            if (!is_dir) continue;
        }
        names.push_back(std::move(name));
    }
    ::closedir(dir);
    return names;
}

// Names in a literal directory matching a pattern without slashes.
inline Generator<std::string> glob1(std::string dirname, std::string pattern, bool dironly, Options o) {
    std::vector<std::string> names = listdir(dirname, dironly, o);
    re::Pattern p = fnmatch::detail::compiled(pattern);
    bool hidden_ok = o.include_hidden || is_hidden(pattern);
    for (auto& name : names) {
        if ((hidden_ok || !is_hidden(name)) && fnmatch::detail::matches(p, name)) co_yield name;
    }
}

// A literal name in a literal directory, if it exists ("" matches the directory itself).
inline Generator<std::string> glob0(std::string dirname, std::string basename, bool, Options o) {
    if (basename.empty() ? isdir(dirname, o) : lexists(join(dirname, basename), o)) co_yield basename;
}

// Every path under a literal directory, relative to it (directories are followed
// through symlinks, as in Python).
inline Generator<std::string> rlistdir(std::string dirname, bool dironly, Options o) {
    std::vector<std::string> names = listdir(dirname, dironly, o);
    for (auto& x : names) {
        if (!o.include_hidden && is_hidden(x)) continue;
        co_yield x;
        std::string path = dirname.empty() ? x : join(dirname, x);
        for (auto&& y : rlistdir(path, dironly, o)) co_yield join(x, y);
    }
}

// `**`: the directory itself (as "") and everything under it.
inline Generator<std::string> glob2(std::string dirname, std::string, bool dironly, Options o) {
    if (dirname.empty() || isdir(dirname, o)) co_yield std::string();
    for (auto&& x : rlistdir(dirname, dironly, o)) co_yield x;
}

inline Generator<std::string> iglob(std::string pathname, std::string root_dir, bool dironly, Options o) {
    auto [dirname, basename] = split(pathname);
    if (!has_magic(pathname)) {
        if (basename.empty() ? isdir(join(root_dir, dirname), o) : lexists(join(root_dir, pathname), o))
            co_yield pathname;  // a pattern ending with a slash matches only directories
        co_return;
    }
    bool recursive = o.recursive && basename == "**";
    if (dirname.empty()) {
        auto names = recursive ? glob2(root_dir, basename, dironly, o) : glob1(root_dir, basename, dironly, o);
        for (auto&& name : names) co_yield name;
        co_return;
    }
    auto glob_in_dir = !has_magic(basename) ? glob0 : recursive ? glob2 : glob1;
    if (dirname != pathname && has_magic(dirname)) {
        for (auto&& dir : iglob(dirname, root_dir, true, o))
            for (auto&& name : glob_in_dir(join(root_dir, dir), basename, dironly, o)) co_yield path_join(dir, name);
    } else {
        for (auto&& name : glob_in_dir(join(root_dir, dirname), basename, dironly, o)) co_yield path_join(dirname, name);
    }
}

// root_dir= may be None, a str or a Path.
inline std::string root(std::nullopt_t) { return ""; }
inline std::string root(const std::string& s) { return s; }
inline std::string root(const std::optional<std::string>& s) { return s.value_or(""); }
template <class P>
    requires requires(const P& p) { p.sd_str(); }
std::string root(const P& p) {
    return p.sd_str();
}

inline Generator<std::string> iglob_top(std::string pathname, std::string root_dir, Options o) {
    // `**` (or an empty pattern) starts with the root itself as "": not a result.
    bool skip_empty = pathname.empty() || (o.recursive && pathname.substr(0, 2) == "**");
    for (auto&& s : iglob(pathname, root_dir, false, o)) {
        if (skip_empty) {
            skip_empty = false;
            if (s.empty()) continue;
        }
        co_yield s;
    }
}

}  // namespace detail

template <class R>
Generator<std::string> iglob(const std::string& pathname, const R& root_dir, std::optional<std::int64_t> dir_fd,
                             bool recursive, bool include_hidden) {
    detail::Options o{dir_fd ? static_cast<int>(*dir_fd) : AT_FDCWD, recursive, include_hidden};
    return detail::iglob_top(pathname, detail::root(root_dir), o);
}

template <class R>
list<std::string> glob(const std::string& pathname, const R& root_dir, std::optional<std::int64_t> dir_fd,
                       bool recursive, bool include_hidden) {
    list<std::string> out;
    for (auto&& s : iglob(pathname, root_dir, dir_fd, recursive, include_hidden)) out.push_back(s);
    return out;
}

// Wraps each of `*?[` in brackets, so the pattern matches it literally.
inline std::string escape(const std::string& pathname) {
    std::string out;
    for (char c : pathname) {
        if (c == '*' || c == '?' || c == '[') {
            out += '[';
            out += c;
            out += ']';
        } else {
            out += c;
        }
    }
    return out;
}

// A port of Python's glob.translate (3.13): a regex for whole paths matching the pattern.
inline std::string translate(const std::string& pat, bool recursive, bool include_hidden,
                             const std::optional<std::string>& seps_arg) {
    std::string seps = seps_arg && !seps_arg->empty() ? *seps_arg : "/";
    std::string escaped_seps;
    for (char c : seps) escaped_seps += re::escape(std::string(1, c));
    std::string any_sep = seps.size() > 1 ? "[" + escaped_seps + "]" : escaped_seps;
    std::string not_sep = "[^" + escaped_seps + "]";
    std::string one_last_segment, one_segment, any_segments, any_last_segments;
    if (include_hidden) {
        one_last_segment = not_sep + "+";
        one_segment = one_last_segment + any_sep;
        any_segments = "(?:.+" + any_sep + ")?";
        any_last_segments = ".*";
    } else {
        one_last_segment = "[^" + escaped_seps + ".]" + not_sep + "*";
        one_segment = one_last_segment + any_sep;
        any_segments = "(?:" + one_segment + ")*";
        any_last_segments = any_segments + "(?:" + one_last_segment + ")?";
    }
    std::vector<std::string> parts;
    std::size_t start = 0;
    for (std::size_t i = 0; i <= pat.size(); ++i) {
        if (i == pat.size() || seps.find(pat[i]) != std::string::npos) {
            parts.push_back(pat.substr(start, i - start));
            start = i + 1;
        }
    }
    std::string res;
    std::size_t last = parts.size() - 1;
    for (std::size_t idx = 0; idx < parts.size(); ++idx) {
        const std::string& part = parts[idx];
        if (part == "*") {
            res += idx < last ? one_segment : one_last_segment;
        } else if (recursive && part == "**") {
            if (idx < last) {
                if (parts[idx + 1] != "**") res += any_segments;
            } else {
                res += any_last_segments;
            }
        } else {
            if (!part.empty()) {
                if (!include_hidden && (part[0] == '*' || part[0] == '?')) res += "(?!\\.)";
                for (auto& piece : fnmatch::detail::translate_parts(part, not_sep + "*", not_sep).parts) res += piece;
            }
            if (idx < last) res += any_sep;
        }
    }
    return "(?s:" + res + ")\\z";
}

}  // namespace sd::glob
