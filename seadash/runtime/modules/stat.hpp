// The `stat` module: the st_mode bits, the S_IS* tests, and filemode() ("-rw-r--r--").
// (The C names are macros, so the functions here have their own names.)
#pragma once

#include <sys/stat.h>

namespace sd::statmod {

inline bool is_dir(std::int64_t mode) { return S_ISDIR(static_cast<mode_t>(mode)); }
inline bool is_chr(std::int64_t mode) { return S_ISCHR(static_cast<mode_t>(mode)); }
inline bool is_blk(std::int64_t mode) { return S_ISBLK(static_cast<mode_t>(mode)); }
inline bool is_reg(std::int64_t mode) { return S_ISREG(static_cast<mode_t>(mode)); }
inline bool is_fifo(std::int64_t mode) { return S_ISFIFO(static_cast<mode_t>(mode)); }
inline bool is_lnk(std::int64_t mode) { return S_ISLNK(static_cast<mode_t>(mode)); }
inline bool is_sock(std::int64_t mode) { return S_ISSOCK(static_cast<mode_t>(mode)); }
inline std::int64_t imode(std::int64_t mode) { return mode & 07777; }
inline std::int64_t ifmt(std::int64_t mode) { return mode & S_IFMT; }

inline std::string filemode(std::int64_t mode) {
    std::string out;
    switch (mode & S_IFMT) {
        case S_IFLNK: out += 'l'; break;
        case S_IFSOCK: out += 's'; break;
        case S_IFREG: out += '-'; break;
        case S_IFBLK: out += 'b'; break;
        case S_IFDIR: out += 'd'; break;
        case S_IFCHR: out += 'c'; break;
        case S_IFIFO: out += 'p'; break;
        default: out += '?';
    }
    auto bit = [&](std::int64_t flag, char c) { out += mode & flag ? c : '-'; };
    auto exec = [&](std::int64_t flag, std::int64_t special, char set) {  // s/S, t/T when setuid/setgid/sticky
        out += mode & special ? (mode & flag ? set : static_cast<char>(std::toupper(set))) : (mode & flag ? 'x' : '-');
    };
    bit(S_IRUSR, 'r');
    bit(S_IWUSR, 'w');
    exec(S_IXUSR, S_ISUID, 's');
    bit(S_IRGRP, 'r');
    bit(S_IWGRP, 'w');
    exec(S_IXGRP, S_ISGID, 's');
    bit(S_IROTH, 'r');
    bit(S_IWOTH, 'w');
    exec(S_IXOTH, S_ISVTX, 't');
    return out;
}

}  // namespace sd::statmod
