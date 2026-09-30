// The `gzip` module: compress and decompress in memory, and open() for .gz files, which
// gives the same file objects as the builtin open() (over a zlib gzFile).
// Not supported yet: the GzipFile class itself (open() covers its uses).
#pragma once

#include <fcntl.h>
#include <unistd.h>
#include <zlib.h>

#include <ctime>

#include "cookie_file.hpp"

namespace sd::gzip {

struct BadGzipFile : OSError {
    using OSError::OSError;
    std::string sd_type() const override { return "gzip.BadGzipFile"; }
};

[[noreturn]] inline void bad(const std::string& msg) { throw Thrown{std::make_shared<BadGzipFile>(msg)}; }
[[noreturn]] inline void zlib_error(int rc, const char* msg) {
    raise("RuntimeError", "Error " + std::to_string(rc) + " while compressing data" + (msg ? ": "s + msg : ""));
}

inline std::string not_gzipped(const std::string& head) {
    return "Not a gzipped file (" + repr_bytes(bytes(head.substr(0, 2))) + ")";
}

// A raw deflate stream (no zlib or gzip framing).
inline std::string deflate_raw(const std::string& in, int level) {
    z_stream zs{};
    int rc = deflateInit2(&zs, level, Z_DEFLATED, -15, 8, Z_DEFAULT_STRATEGY);
    if (rc != Z_OK) zlib_error(rc, zs.msg);
    zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
    zs.avail_in = static_cast<uInt>(in.size());
    std::string out(deflateBound(&zs, static_cast<uLong>(in.size())), '\0');
    zs.next_out = reinterpret_cast<Bytef*>(out.data());
    zs.avail_out = static_cast<uInt>(out.size());
    rc = deflate(&zs, Z_FINISH);
    if (rc != Z_STREAM_END) {
        deflateEnd(&zs);
        zlib_error(rc, zs.msg);
    }
    out.resize(zs.total_out);
    deflateEnd(&zs);
    return out;
}

inline bytes compress(const bytes& data, std::int64_t compresslevel = 9, std::optional<std::int64_t> mtime = std::nullopt) {
    if (compresslevel < -1 || compresslevel > 9) raise("ValueError", "Bad compression level");
    int level = static_cast<int>(compresslevel);
    // The header as Python writes it: no name, the mtime (now by default), an unknown OS.
    std::uint32_t when = static_cast<std::uint32_t>(mtime ? *mtime : static_cast<std::int64_t>(std::time(nullptr)));
    std::string out("\x1f\x8b\x08\x00", 4);  // magic, deflate, no flags
    for (int k = 0; k < 4; ++k) out += static_cast<char>((when >> (8 * k)) & 0xFF);
    out += static_cast<char>(level == 9 ? 2 : level == 1 ? 4 : 0);
    out += static_cast<char>(0xFF);
    out += deflate_raw(data.data, level);
    std::uint32_t crc = static_cast<std::uint32_t>(::crc32(0, reinterpret_cast<const Bytef*>(data.data.data()), static_cast<uInt>(data.size())));
    std::uint32_t size = static_cast<std::uint32_t>(data.size());
    for (int k = 0; k < 4; ++k) out += static_cast<char>((crc >> (8 * k)) & 0xFF);
    for (int k = 0; k < 4; ++k) out += static_cast<char>((size >> (8 * k)) & 0xFF);
    return bytes(out);
}

// Every member in the data, one after the other, like Python (trailing zero padding is fine).
inline bytes decompress(const bytes& data) {
    const std::string& in = data.data;
    std::string out;
    std::size_t pos = 0;
    while (pos < in.size()) {
        if (in.size() - pos < 2 || in[pos] != '\x1f' || in[pos + 1] != '\x8b') bad(not_gzipped(in.substr(pos)));
        z_stream zs{};
        if (inflateInit2(&zs, 31) != Z_OK) raise("RuntimeError", "Error while initializing decompression");
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data() + pos));
        zs.avail_in = static_cast<uInt>(in.size() - pos);
        char chunk[64 * 1024];
        int rc;
        do {
            zs.next_out = reinterpret_cast<Bytef*>(chunk);
            zs.avail_out = sizeof chunk;
            rc = inflate(&zs, Z_NO_FLUSH);
            if (rc != Z_OK && rc != Z_STREAM_END) {
                std::string why = zs.msg ? zs.msg : "";
                inflateEnd(&zs);
                if (rc == Z_BUF_ERROR || (rc == Z_DATA_ERROR && zs.avail_in == 0))
                    raise("EOFError", "Compressed file ended before the end-of-stream marker was reached");
                if (why == "incorrect data check") bad("CRC check failed");
                if (why == "incorrect length check") bad("Incorrect length of data produced");
                raise("OSError", "Error " + std::to_string(rc) + " while decompressing data: " + why);
            }
            out.append(chunk, sizeof chunk - zs.avail_out);
        } while (rc != Z_STREAM_END);
        pos = in.size() - zs.avail_in;
        inflateEnd(&zs);
        while (pos < in.size() && in[pos] == '\0') ++pos;
    }
    return bytes(out);
}

// ---- open(): a FILE* over a gzFile ------------------------------------------------------

inline std::FILE* open_gz(const std::string& path, const std::string& mode, std::int64_t compresslevel) {
    if (compresslevel < 0 || compresslevel > 9) raise("ValueError", "Bad compression level");
    char kind = mode[mode.find_first_of("rwax")];
    int fd = open_fd_for(path, mode);
    if (kind == 'r') {  // Python checks the magic on the first read; a non-empty file must be gzipped
        char head[2];
        ssize_t n = ::read(fd, head, 2);
        if (n == 2 && (head[0] != '\x1f' || head[1] != '\x8b')) {
            ::close(fd);
            bad(not_gzipped(std::string(head, 2)));
        }
        ::lseek(fd, 0, SEEK_SET);
    }
    std::string gz_mode = kind == 'r' ? "rb" : (kind == 'a' ? "ab" : "wb") + std::to_string(compresslevel);
    gzFile gz = gzdopen(fd, gz_mode.c_str());
    if (!gz) {
        ::close(fd);
        raise_os(ENOMEM, path);
    }
    Cookie cookie{
        gz,
        [](void* g, char* buf, std::size_t n) -> std::int64_t { return gzread(static_cast<gzFile>(g), buf, static_cast<unsigned>(n)); },
        [](void* g, const char* buf, std::size_t n) -> std::int64_t {
            int w = gzwrite(static_cast<gzFile>(g), buf, static_cast<unsigned>(n));
            return w == 0 && n > 0 ? -1 : w;
        },
        [](void* g) -> int { return gzclose(static_cast<gzFile>(g)) == Z_OK ? 0 : -1; },
    };
    std::FILE* f = cookie_file(cookie, kind == 'r');
    if (!f) {
        gzclose(gz);
        raise_os(errno, path);
    }
    return f;
}

inline std::shared_ptr<TextFile> open_text(const std::string& path, const std::string& mode, std::int64_t compresslevel,
                                           const std::string& encoding = "utf-8", std::optional<std::string> newline = std::nullopt) {
    check_encoding(encoding);
    auto f = std::make_shared<TextFile>(open_gz(path, mode, compresslevel), path, mode);
    f->translate_newlines = !newline;
    return f;
}
inline std::shared_ptr<BinaryFile> open_binary(const std::string& path, const std::string& mode, std::int64_t compresslevel) {
    return std::make_shared<BinaryFile>(open_gz(path, mode, compresslevel), path, mode);
}

}  // namespace sd::gzip
