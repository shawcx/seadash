// The `pathlib` module: Path, an immutable POSIX path value with Python's rules for
// joining (/), names and suffixes, plus the file system operations Path offers.
#pragma once

#include <dirent.h>
#include <fcntl.h>
#include <fnmatch.h>
#include <sys/stat.h>
#include <unistd.h>

#include <functional>

namespace sd::pathlib {

class Path;

struct StatResult {
    std::int64_t st_size = 0, st_mode = 0, st_uid = 0, st_gid = 0, st_nlink = 0, st_ino = 0;
    double st_mtime_ = 0, st_atime_ = 0, st_ctime_ = 0;  // st_mtime etc. are C macros
    std::string sd_repr() const {
        return "os.stat_result(st_mode=" + std::to_string(st_mode) + ", st_size=" + std::to_string(st_size) +
               ", st_mtime=" + float_repr(st_mtime_) + ")";
    }
};

class Path {
    std::string p_;  // normalized: no empty or "." parts, no trailing slash; "." for empty

    static std::string normalize(std::string_view s) {
        bool absolute = !s.empty() && s[0] == '/';
        std::string out = absolute ? "/" : "";
        std::size_t i = 0;
        while (i < s.size()) {
            std::size_t j = s.find('/', i);
            if (j == std::string_view::npos) j = s.size();
            std::string_view part = s.substr(i, j - i);
            if (!part.empty() && part != ".") {
                if (!out.empty() && out.back() != '/') out += '/';
                out += part;
            }
            i = j + 1;
        }
        return out.empty() ? "." : out;
    }
    struct Raw {};
    Path(Raw, std::string s) : p_(std::move(s)) {}

public:
    Path() : p_(".") {}
    Path(const std::string& s) : p_(normalize(s)) {}
    Path(const char* s) : p_(normalize(s)) {}
    template <class... More>
    Path(const Path& first, const More&... more) : p_(first.p_) {
        ((*this = *this / more), ...);
    }
    template <class... More>
    Path(const std::string& first, const More&... more) requires(sizeof...(More) > 0) : Path(Path(first), more...) {}

    static Path cwd() {
        char buf[4096];
        if (!::getcwd(buf, sizeof buf)) raise_os(errno, std::nullopt);
        return Path(std::string(buf));
    }
    static Path home() {
        const char* h = std::getenv("HOME");
        return Path(std::string(h ? h : "/"));
    }

    friend Path operator/(const Path& a, const Path& b) {
        if (b.is_absolute()) return b;
        if (a.p_ == ".") return b;
        if (b.p_ == ".") return a;
        return Path(Raw{}, a.p_ == "/" ? "/" + b.p_ : a.p_ + "/" + b.p_);
    }
    friend Path operator/(const Path& a, const std::string& b) { return a / Path(b); }
    friend Path operator/(const std::string& a, const Path& b) { return Path(a) / b; }

    const std::string& str() const { return p_; }
    std::string sd_str() const { return p_; }
    std::string sd_repr() const { return "PosixPath(" + repr_str(p_) + ")"; }
    std::string as_posix() const { return p_; }
    bool is_absolute() const { return !p_.empty() && p_[0] == '/'; }
    auto operator<=>(const Path&) const = default;
    bool operator==(const Path&) const = default;

    // ---- parts of the path -----------------------------------------------------------
    vtuple<std::string> parts() const {
        list<std::string> out;
        if (is_absolute()) out.push_back("/");
        std::size_t i = is_absolute() ? 1 : 0;
        while (i < p_.size() && p_ != ".") {
            std::size_t j = p_.find('/', i);
            if (j == std::string::npos) j = p_.size();
            out.push_back(p_.substr(i, j - i));
            i = j + 1;
        }
        return vtuple<std::string>(out);
    }
    std::string name() const {
        if (p_ == "/" || p_ == ".") return "";
        auto slash = p_.rfind('/');
        return slash == std::string::npos ? p_ : p_.substr(slash + 1);
    }
    std::string suffix() const {
        std::string n = name();
        auto dot = n.rfind('.');
        if (dot == std::string::npos || dot == 0 || dot == n.size() - 1) return "";
        return n.substr(dot);
    }
    list<std::string> suffixes() const {
        std::string n = name();
        list<std::string> out;
        if (n.empty() || n.back() == '.') return out;
        std::size_t start = n.find_first_not_of('.');  // leading dots belong to the name
        for (auto dot = n.find('.', start); dot != std::string::npos; dot = n.find('.', dot + 1)) {
            auto next = n.find('.', dot + 1);
            out.push_back(n.substr(dot, next == std::string::npos ? std::string::npos : next - dot));
        }
        return out;
    }
    std::string stem() const {
        std::string n = name(), s = suffix();
        return n.substr(0, n.size() - s.size());
    }
    Path parent() const {
        if (p_ == "/" || p_ == ".") return *this;
        auto slash = p_.rfind('/');
        if (slash == std::string::npos) return Path();
        return Path(Raw{}, slash == 0 ? "/" : p_.substr(0, slash));
    }
    list<Path> parents() const {
        list<Path> out;
        for (Path p = *this; p.parent() != p; p = p.parent()) out.push_back(p.parent());
        return out;
    }
    std::string anchor() const { return is_absolute() ? "/" : ""; }

    Path with_name(const std::string& n) const {
        if (name().empty()) raise("ValueError", sd_repr() + " has an empty name");
        if (n.empty() || n.find('/') != std::string::npos || n == ".") raise("ValueError", "Invalid name " + repr_str(n));
        return parent() / Path(n);
    }
    Path with_suffix(const std::string& s) const {
        if (!s.empty() && (s[0] != '.' || s == "." || s.find('/') != std::string::npos))
            raise("ValueError", "Invalid suffix " + repr_str(s));
        if (name().empty()) raise("ValueError", sd_repr() + " has an empty name");
        return with_name(stem() + s);
    }
    Path with_stem(const std::string& s) const { return with_name(s + suffix()); }
    Path relative_to(const Path& other) const {
        if (other.p_ == ".") return *this;
        if (p_ == other.p_) return Path();
        std::string prefix = other.p_ == "/" ? "/" : other.p_ + "/";
        if (p_.compare(0, prefix.size(), prefix) != 0 || is_absolute() != other.is_absolute())
            raise("ValueError", repr_str(p_) + " is not in the subpath of " + repr_str(other.p_));
        return Path(Raw{}, p_.substr(prefix.size()));
    }
    bool is_relative_to(const Path& other) const {
        std::string prefix = other.p_ == "/" ? "/" : other.p_ + "/";
        return p_ == other.p_ || other.p_ == "." || p_.compare(0, prefix.size(), prefix) == 0;
    }
    template <class... More>
    Path joinpath(const More&... more) const {
        Path out = *this;
        ((out = out / more), ...);
        return out;
    }
    // Glob-style match against the end of the path (the whole path if the pattern is absolute).
    bool match(const std::string& pattern) const {
        list<std::string> pat = Path(pattern).parts().items, mine = parts().items;
        if (pat.size() > mine.size() || (Path(pattern).is_absolute() && pat.size() != mine.size())) return false;
        for (std::size_t i = 0; i < pat.size(); ++i) {
            const std::string& part = mine[mine.size() - pat.size() + i];
            if (::fnmatch(pat[i].c_str(), part.c_str(), 0) != 0) return false;
        }
        return true;
    }

    // ---- the file system ---------------------------------------------------------------
    std::optional<struct stat> stat_raw(bool follow = true) const {
        struct stat st;
        if ((follow ? ::stat(p_.c_str(), &st) : ::lstat(p_.c_str(), &st)) != 0) return std::nullopt;
        return st;
    }
    bool exists() const { return stat_raw().has_value(); }
    bool is_file() const {
        auto st = stat_raw();
        return st && S_ISREG(st->st_mode);
    }
    bool is_dir() const {
        auto st = stat_raw();
        return st && S_ISDIR(st->st_mode);
    }
    bool is_symlink() const {
        auto st = stat_raw(false);
        return st && S_ISLNK(st->st_mode);
    }
    StatResult stat() const {
        auto st = stat_raw();
        if (!st) raise_os(errno, p_);
        auto secs = [](const timespec& t) { return static_cast<double>(t.tv_sec) + t.tv_nsec / 1e9; };
        return {static_cast<std::int64_t>(st->st_size), static_cast<std::int64_t>(st->st_mode),
                static_cast<std::int64_t>(st->st_uid), static_cast<std::int64_t>(st->st_gid),
                static_cast<std::int64_t>(st->st_nlink), static_cast<std::int64_t>(st->st_ino),
                secs(st->st_mtim), secs(st->st_atim), secs(st->st_ctim)};
    }
    Path absolute() const { return is_absolute() ? *this : cwd() / *this; }
    Path resolve() const {  // absolute, with symlinks and ".." resolved; the path needn't exist
        Path abs = absolute();
        Path out("/");
        for (const auto& part : abs.parts().items) {
            if (part == "/") continue;
            if (part == "..") {
                out = out.parent();
                continue;
            }
            out = out / Path(Raw{}, part);
            char buf[4096];
            ssize_t n = ::readlink(out.p_.c_str(), buf, sizeof buf);
            if (n > 0) {  // a symlink: follow it (relative links are relative to its directory)
                Path target(std::string(buf, static_cast<std::size_t>(n)));
                out = (target.is_absolute() ? target : out.parent() / target).resolve();
            }
        }
        return out;
    }
    Path expanduser() const {
        if (p_ == "~" || p_.rfind("~/", 0) == 0) return home() / Path(p_.substr(1 + (p_.size() > 1)));
        return *this;
    }

    std::string read_text(std::optional<std::string> = std::nullopt) const {
        auto f = open_text(p_, "r");
        return f->read();
    }
    bytes read_bytes() const { return open_binary(p_, "rb")->read(); }
    std::int64_t write_text(const std::string& data, std::optional<std::string> = std::nullopt) const {
        return open_text(p_, "w")->write(data);
    }
    std::int64_t write_bytes(const bytes& data) const { return open_binary(p_, "wb")->write(data); }

    void mkdir(std::int64_t mode = 0777, bool parents = false, bool exist_ok = false) const {
        if (::mkdir(p_.c_str(), static_cast<mode_t>(mode)) == 0) return;
        int e = errno;
        if (e == ENOENT && parents && parent() != *this) {
            parent().mkdir(mode, true, true);
            mkdir(mode, false, exist_ok);
            return;
        }
        if (e == EEXIST && exist_ok && is_dir()) return;
        raise_os(e, p_);
    }
    void rmdir() const {
        if (::rmdir(p_.c_str()) != 0) raise_os(errno, p_);
    }
    void unlink(bool missing_ok = false) const {
        if (::unlink(p_.c_str()) != 0 && !(missing_ok && errno == ENOENT)) raise_os(errno, p_);
    }
    Path rename(const Path& target) const {
        if (::rename(p_.c_str(), target.p_.c_str()) != 0) raise_os(errno, p_);
        return target;
    }
    Path replace(const Path& target) const { return rename(target); }
    void touch(std::int64_t mode = 0666, bool exist_ok = true) const {
        if (exists()) {
            if (!exist_ok) raise_os(EEXIST, p_);
            ::utimensat(AT_FDCWD, p_.c_str(), nullptr, 0);
            return;
        }
        int fd = ::open(p_.c_str(), O_WRONLY | O_CREAT, static_cast<mode_t>(mode));
        if (fd < 0) raise_os(errno, p_);
        ::close(fd);
    }

    list<Path> iterdir() const {
        DIR* d = ::opendir(p_.c_str());
        if (!d) raise_os(errno, p_);
        list<Path> out;
        while (dirent* entry = ::readdir(d)) {
            std::string n = entry->d_name;
            if (n != "." && n != "..") out.push_back(*this / Path(Raw{}, n));
        }
        ::closedir(d);
        return out;
    }
    // Python's pathlib globbing: `*` matches hidden files too; `**` matches any number
    // of directories (including none).
    list<Path> glob(const std::string& pattern) const {
        if (pattern.empty()) raise("ValueError", "Unacceptable pattern: ''");
        if (!pattern.empty() && pattern[0] == '/') raise("NotImplementedError", "Non-relative patterns are unsupported");
        list<Path> out;
        glob_parts(*this, Path(pattern).parts().items, 0, out);
        return out;
    }
    list<Path> rglob(const std::string& pattern) const { return glob("**/" + pattern); }

private:
    static void glob_parts(const Path& dir, const list<std::string>& parts, std::size_t i, list<Path>& out) {
        if (i == parts.size()) {
            out.push_back(dir);
            return;
        }
        const std::string& part = parts[i];
        if (part == "**") {
            glob_parts(dir, parts, i + 1, out);  // no directories
            if (!dir.is_dir()) return;
            for (const Path& child : safe_iterdir(dir)) {
                if (child.is_dir() && !child.is_symlink()) glob_parts(child, parts, i, out);
            }
            return;
        }
        bool wildcard = part.find_first_of("*?[") != std::string::npos;
        if (!wildcard) {
            Path next = dir / Path(Raw{}, part);
            if (i + 1 == parts.size() ? next.exists() : next.is_dir()) glob_parts(next, parts, i + 1, out);
            return;
        }
        for (const Path& child : safe_iterdir(dir)) {
            if (::fnmatch(part.c_str(), child.name().c_str(), 0) != 0) continue;
            if (i + 1 == parts.size() || child.is_dir()) glob_parts(child, parts, i + 1, out);
        }
    }
    static list<Path> safe_iterdir(const Path& dir) {  // unreadable directories are skipped, like Python
        if (!dir.is_dir()) return {};
        DIR* d = ::opendir(dir.p_.c_str());
        if (!d) return {};
        ::closedir(d);
        return dir.iterdir();
    }
};

}  // namespace sd::pathlib

template <>
struct std::hash<sd::pathlib::Path> {
    std::size_t operator()(const sd::pathlib::Path& p) const { return std::hash<std::string>()(p.str()); }
};
