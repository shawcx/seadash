// The `shutil` module: copying, moving and removing files and directory trees.
// Every path parameter takes a str or a Path.
#pragma once

#include <sys/statvfs.h>

#include "pathlib.hpp"

namespace sd::shutil {

using pathlib::Path;

struct Error : OSError {
    using OSError::OSError;
    std::string sd_type() const override { return "shutil.Error"; }
};
struct SameFileError : Error {
    using Error::Error;
    std::string sd_type() const override { return "shutil.SameFileError"; }
};

struct DiskUsage {
    std::int64_t total = 0, used = 0, free = 0;
    std::string sd_repr() const {
        return "usage(total=" + std::to_string(total) + ", used=" + std::to_string(used) + ", free=" +
               std::to_string(free) + ")";
    }
};

inline bool same_file(const Path& a, const Path& b) {
    auto sa = a.stat_raw(), sb = b.stat_raw();
    return sa && sb && sa->st_dev == sb->st_dev && sa->st_ino == sb->st_ino;
}

inline std::string copyfile(const Path& src, const Path& dst) {
    if (same_file(src, dst))
        throw Thrown{std::make_shared<SameFileError>(repr_str(src.str()) + " and " + repr_str(dst.str()) + " are the same file")};
    int in = ::open(src.str().c_str(), O_RDONLY | O_CLOEXEC);
    if (in < 0) raise_os(errno, src.str());
    int out = ::open(dst.str().c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0666);
    if (out < 0) {
        int e = errno;
        ::close(in);
        raise_os(e, dst.str());
    }
    char buf[1 << 16];
    ssize_t n;
    while ((n = ::read(in, buf, sizeof buf)) > 0) {
        for (ssize_t done = 0; done < n;) {
            ssize_t w = ::write(out, buf + done, static_cast<std::size_t>(n - done));
            if (w < 0) {
                int e = errno;
                ::close(in), ::close(out);
                raise_os(e, dst.str());
            }
            done += w;
        }
    }
    int e = n < 0 ? errno : 0;
    ::close(in), ::close(out);
    if (e) raise_os(e, src.str());
    return dst.str();
}

inline void copymode(const Path& src, const Path& dst) {
    auto st = src.stat_raw();
    if (!st) raise_os(errno, src.str());
    if (::chmod(dst.str().c_str(), st->st_mode & 07777) != 0) raise_os(errno, dst.str());
}

inline void copystat(const Path& src, const Path& dst) {
    copymode(src, dst);
    auto st = src.stat_raw();
    timespec times[2] = {st->st_atim, st->st_mtim};
    if (::utimensat(AT_FDCWD, dst.str().c_str(), times, 0) != 0) raise_os(errno, dst.str());
}

// copy()/copy2() into a directory keep the file's name, like `cp file dir/`.
inline Path target_in(const Path& src, const Path& dst) { return dst.is_dir() ? dst / src.name() : dst; }

inline std::string copy(const Path& src, const Path& dst) {
    Path target = target_in(src, dst);
    copyfile(src, target);
    copymode(src, target);
    return target.str();
}

inline std::string copy2(const Path& src, const Path& dst) {
    Path target = target_in(src, dst);
    copyfile(src, target);
    copystat(src, target);
    return target.str();
}

inline std::string copytree(const Path& src, const Path& dst, bool dirs_exist_ok = false) {
    if (!src.is_dir()) raise_os(src.exists() ? ENOTDIR : ENOENT, src.str());
    if (::mkdir(dst.str().c_str(), 0777) != 0 && !(errno == EEXIST && dirs_exist_ok && dst.is_dir())) {
        raise_os(errno, dst.str());
    }
    for (const Path& child : src.iterdir()) {
        Path to = dst / child.name();
        if (child.is_dir()) {
            copytree(child, to, dirs_exist_ok);
        } else {
            copy2(child, to);
        }
    }
    copystat(src, dst);
    return dst.str();
}

inline void remove_tree(const Path& p, bool ignore_errors) {
    auto st = p.stat_raw(false);  // a symlink is removed itself, never followed
    if (!st) {
        if (!ignore_errors) raise_os(errno, p.str());
        return;
    }
    if (S_ISDIR(st->st_mode)) {
        DIR* d = ::opendir(p.str().c_str());
        if (!d) {
            if (!ignore_errors) raise_os(errno, p.str());
            return;
        }
        std::vector<std::string> names;
        while (dirent* entry = ::readdir(d)) {
            std::string n = entry->d_name;
            if (n != "." && n != "..") names.push_back(n);
        }
        ::closedir(d);
        for (const auto& n : names) remove_tree(p / n, ignore_errors);
        if (::rmdir(p.str().c_str()) != 0 && !ignore_errors) raise_os(errno, p.str());
    } else if (::unlink(p.str().c_str()) != 0 && !ignore_errors) {
        raise_os(errno, p.str());
    }
}

inline void rmtree(const Path& path, bool ignore_errors = false) { remove_tree(path, ignore_errors); }

inline std::string move(const Path& src, const Path& dst) {
    Path target = dst.is_dir() ? dst / src.name() : dst;
    if (target.exists() && dst.is_dir())
        throw Thrown{std::make_shared<Error>("Destination path " + repr_str(target.str()) + " already exists")};
    if (::rename(src.str().c_str(), target.str().c_str()) == 0) return target.str();
    if (errno != EXDEV) raise_os(errno, src.str());
    if (src.is_dir()) {  // another file system: copy, then remove the original
        copytree(src, target);
        rmtree(src);
    } else {
        copy2(src, target);
        src.unlink();
    }
    return target.str();
}

inline std::optional<std::string> which(const std::string& cmd, std::optional<std::string> path = std::nullopt) {
    auto runnable = [](const std::string& p) {
        struct stat st;
        return ::stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode) && ::access(p.c_str(), X_OK) == 0;
    };
    if (cmd.find('/') != std::string::npos) return runnable(cmd) ? std::optional(cmd) : std::nullopt;
    const char* env = std::getenv("PATH");
    std::string dirs = path ? *path : (env ? env : "/usr/bin:/bin");
    std::size_t i = 0;
    while (i <= dirs.size()) {
        std::size_t j = dirs.find(':', i);
        if (j == std::string::npos) j = dirs.size();
        std::string dir = dirs.substr(i, j - i);
        std::string candidate = (dir.empty() ? "." : dir) + "/" + cmd;
        if (runnable(candidate)) return candidate;
        i = j + 1;
    }
    return std::nullopt;
}

inline DiskUsage disk_usage(const Path& path) {
    struct statvfs st;
    if (::statvfs(path.str().c_str(), &st) != 0) raise_os(errno, path.str());
    auto block = static_cast<std::int64_t>(st.f_frsize);
    std::int64_t total = static_cast<std::int64_t>(st.f_blocks) * block;
    std::int64_t free = static_cast<std::int64_t>(st.f_bavail) * block;
    std::int64_t used = (static_cast<std::int64_t>(st.f_blocks) - static_cast<std::int64_t>(st.f_bfree)) * block;
    return {total, used, free};
}

}  // namespace sd::shutil
