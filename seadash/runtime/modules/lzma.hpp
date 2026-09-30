// The `lzma` module: .xz and .lzma compression on liblzma, in memory and as open() for
// files (the same file objects as the builtin open()). Not supported yet: FORMAT_RAW and
// filter chains, the LZMAFile class, and the incremental LZMACompressor/LZMADecompressor.
#pragma once

#include <fcntl.h>
#include <lzma.h>
#include <unistd.h>

#include "cookie_file.hpp"

namespace sd::lzma {

struct LZMAError : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "lzma.LZMAError"; }
};

inline constexpr std::int64_t FORMAT_AUTO = 0, FORMAT_XZ = 1, FORMAT_ALONE = 2, FORMAT_RAW = 3;
inline constexpr std::int64_t CHECK_NONE = 0, CHECK_CRC32 = 1, CHECK_CRC64 = 4, CHECK_SHA256 = 10, CHECK_UNKNOWN = 16;
inline constexpr std::int64_t PRESET_DEFAULT = 6, PRESET_EXTREME = 0x80000000;
inline constexpr std::size_t CHUNK = 64 * 1024;

[[noreturn]] inline void fail(const std::string& msg) { throw Thrown{std::make_shared<LZMAError>(msg)}; }

// liblzma's return codes, with Python's words for them.
inline void check(lzma_ret rc) {
    switch (rc) {
        case LZMA_OK:
        case LZMA_STREAM_END:
        case LZMA_NO_CHECK:
        case LZMA_UNSUPPORTED_CHECK:
        case LZMA_GET_CHECK: return;
        case LZMA_MEM_ERROR: raise("MemoryError", "");
        case LZMA_MEMLIMIT_ERROR: fail("Memory usage limit exceeded");
        case LZMA_FORMAT_ERROR: fail("Input format not supported by decoder");
        case LZMA_OPTIONS_ERROR: fail("Invalid or unsupported options");
        case LZMA_DATA_ERROR: fail("Corrupt input data");
        case LZMA_BUF_ERROR: fail("Insufficient buffer space");
        case LZMA_PROG_ERROR: fail("Internal error");
        default: fail("Unrecognized error from liblzma: " + std::to_string(rc));
    }
}

inline bool is_check_supported(std::int64_t check_id) {
    return check_id >= 0 && check_id <= 15 && lzma_check_is_supported(static_cast<lzma_check>(check_id));
}

// An encoder for the format: xz (with an integrity check) or the old .lzma "alone" format.
inline void init_encoder(lzma_stream& zs, std::int64_t format, std::int64_t check_id, std::optional<std::int64_t> preset) {
    std::uint32_t p = static_cast<std::uint32_t>(preset.value_or(PRESET_DEFAULT));
    if (format == FORMAT_XZ) {
        lzma_check c = check_id == -1 ? LZMA_CHECK_CRC64 : static_cast<lzma_check>(check_id);
        check(lzma_easy_encoder(&zs, p, c));
    } else if (format == FORMAT_ALONE) {
        if (check_id != -1 && check_id != CHECK_NONE) raise("ValueError", "Integrity checks are only supported by FORMAT_XZ");
        lzma_options_lzma options;
        if (lzma_lzma_preset(&options, p)) fail("Invalid compression preset: " + std::to_string(p));
        check(lzma_alone_encoder(&zs, &options));
    } else if (format == FORMAT_RAW) {
        raise("ValueError", "FORMAT_RAW is not supported yet");
    } else {
        raise("ValueError", "Invalid container format: " + std::to_string(format));
    }
}

inline void init_decoder(lzma_stream& zs, std::int64_t format, std::uint32_t flags = 0) {
    std::uint64_t memlimit = UINT64_MAX;
    if (format == FORMAT_AUTO) check(lzma_auto_decoder(&zs, memlimit, flags));
    else if (format == FORMAT_XZ) check(lzma_stream_decoder(&zs, memlimit, flags));
    else if (format == FORMAT_ALONE) check(lzma_alone_decoder(&zs, memlimit));
    else if (format == FORMAT_RAW) raise("ValueError", "FORMAT_RAW is not supported yet");
    else raise("ValueError", "Invalid container format: " + std::to_string(format));
}

inline bytes compress(const bytes& data, std::int64_t format = FORMAT_XZ, std::int64_t check_id = -1,
                      std::optional<std::int64_t> preset = std::nullopt) {
    lzma_stream zs = LZMA_STREAM_INIT;
    init_encoder(zs, format, check_id, preset);
    zs.next_in = reinterpret_cast<const std::uint8_t*>(data.data.data());
    zs.avail_in = data.size();
    std::string out;
    std::uint8_t chunk[CHUNK];
    lzma_ret rc;
    do {
        zs.next_out = chunk;
        zs.avail_out = sizeof chunk;
        rc = lzma_code(&zs, LZMA_FINISH);
        out.append(reinterpret_cast<char*>(chunk), sizeof chunk - zs.avail_out);
    } while (rc == LZMA_OK);
    lzma_end(&zs);
    check(rc);
    return bytes(out);
}

// Every stream in the data, one after the other; trailing data that isn't a stream is
// ignored, like Python.
inline bytes decompress(const bytes& data, std::int64_t format = FORMAT_AUTO) {
    const std::string& in = data.data;
    if (in.empty()) fail("Compressed data ended before the end-of-stream marker was reached");
    std::string out;
    std::size_t pos = 0;
    bool any = false;
    while (pos < in.size()) {
        lzma_stream zs = LZMA_STREAM_INIT;
        init_decoder(zs, format);
        zs.next_in = reinterpret_cast<const std::uint8_t*>(in.data() + pos);
        zs.avail_in = in.size() - pos;
        std::string piece;
        std::uint8_t chunk[CHUNK];
        lzma_ret rc;
        do {
            zs.next_out = chunk;
            zs.avail_out = sizeof chunk;
            rc = lzma_code(&zs, LZMA_FINISH);
            piece.append(reinterpret_cast<char*>(chunk), sizeof chunk - zs.avail_out);
        } while (rc == LZMA_OK && (zs.avail_in > 0 || zs.avail_out == 0));
        pos = in.size() - zs.avail_in;
        lzma_end(&zs);
        if (rc == LZMA_STREAM_END) {
            out += piece;
            any = true;
        } else if (rc == LZMA_OK || rc == LZMA_BUF_ERROR) {
            fail("Compressed data ended before the end-of-stream marker was reached");
        } else if (any) {
            break;
        } else {
            check(rc);
        }
    }
    return bytes(out);
}

// ---- open(): a FILE* over a streaming (de)compressor ---------------------------------

struct Reader {
    int fd;
    std::int64_t format;
    lzma_stream zs = LZMA_STREAM_INIT;
    std::uint8_t input[CHUNK];
    bool open = false, done = false;
    ~Reader() {
        if (open) lzma_end(&zs);
        ::close(fd);
    }
    std::int64_t read(char* buf, std::size_t n) {
        zs.next_out = reinterpret_cast<std::uint8_t*>(buf);
        zs.avail_out = n;
        while (zs.avail_out > 0 && !done) {
            if (!open) {
                init_decoder(zs, format, format == FORMAT_ALONE ? 0 : LZMA_CONCATENATED);
                open = true;
            }
            lzma_action action = LZMA_RUN;
            if (zs.avail_in == 0) {
                ssize_t got = ::read(fd, input, sizeof input);
                if (got < 0) raise_os(errno, std::nullopt);
                if (got == 0) {
                    if (zs.total_in == 0) raise("EOFError", "Compressed file ended before the end-of-stream marker was reached");
                    action = LZMA_FINISH;
                }
                zs.next_in = input;
                zs.avail_in = static_cast<std::size_t>(got);
            }
            lzma_ret rc = lzma_code(&zs, action);
            if (rc == LZMA_STREAM_END) {
                done = true;
            } else if (rc == LZMA_BUF_ERROR || (action == LZMA_FINISH && rc == LZMA_OK && zs.avail_out > 0)) {
                raise("EOFError", "Compressed file ended before the end-of-stream marker was reached");
            } else {
                check(rc);
            }
        }
        return static_cast<std::int64_t>(n - zs.avail_out);
    }
};

struct Writer {
    int fd;
    lzma_stream zs = LZMA_STREAM_INIT;
    bool open = false;
    void pump(lzma_action action) {
        std::uint8_t chunk[CHUNK];
        lzma_ret rc;
        do {
            zs.next_out = chunk;
            zs.avail_out = sizeof chunk;
            rc = lzma_code(&zs, action);
            check(rc);
            std::size_t n = sizeof chunk - zs.avail_out;
            for (std::size_t written = 0; written < n;) {
                ssize_t w = ::write(fd, chunk + written, n - written);
                if (w < 0) raise_os(errno, std::nullopt);
                written += static_cast<std::size_t>(w);
            }
        } while (action == LZMA_FINISH ? rc != LZMA_STREAM_END : zs.avail_in > 0);
    }
    std::int64_t write(const char* buf, std::size_t n) {
        zs.next_in = reinterpret_cast<const std::uint8_t*>(buf);
        zs.avail_in = n;
        pump(LZMA_RUN);
        return static_cast<std::int64_t>(n);
    }
    void finish() {
        if (open) {
            zs.next_in = nullptr;
            zs.avail_in = 0;
            pump(LZMA_FINISH);
            lzma_end(&zs);
            open = false;
        }
    }
    ~Writer() { ::close(fd); }
};

inline std::FILE* open_xz(const std::string& path, const std::string& mode, std::optional<std::int64_t> format,
                          std::int64_t check_id, std::optional<std::int64_t> preset) {
    char kind = mode[mode.find_first_of("rwax")];
    Cookie cookie;
    if (kind == 'r') {
        if (check_id != -1 || preset) raise("ValueError", "Cannot specify an integrity check or a preset when opening a file for reading");
        int fd = open_fd_for(path, mode);
        cookie = {new Reader{fd, format.value_or(FORMAT_AUTO), LZMA_STREAM_INIT, {}},
                  [](void* s, char* buf, std::size_t n) { return guarded([&] { return static_cast<Reader*>(s)->read(buf, n); }); },
                  nullptr, [](void* s) -> int { delete static_cast<Reader*>(s); return 0; }};
    } else {
        std::int64_t f = format.value_or(FORMAT_XZ);
        if (f == FORMAT_AUTO) raise("ValueError", "Cannot specify FORMAT_AUTO when writing");
        auto* w = new Writer{open_fd_for(path, mode)};
        try {
            init_encoder(w->zs, f, check_id, preset);
        } catch (...) {
            delete w;
            throw;
        }
        w->open = true;
        cookie = {w, nullptr,
                  [](void* s, const char* buf, std::size_t n) { return guarded([&] { return static_cast<Writer*>(s)->write(buf, n); }); },
                  [](void* s) -> int {
                      auto* w = static_cast<Writer*>(s);
                      int rc = static_cast<int>(guarded([&] { w->finish(); return std::int64_t{0}; }));
                      delete w;
                      return rc;
                  }};
    }
    std::FILE* f = cookie_file(cookie, kind == 'r');
    if (!f) {
        cookie.close(cookie.state);
        raise_os(errno, path);
    }
    return f;
}

inline std::shared_ptr<TextFile> open_text(const std::string& path, const std::string& mode, std::optional<std::int64_t> format,
                                           std::int64_t check_id, std::optional<std::int64_t> preset,
                                           const std::string& encoding = "utf-8", std::optional<std::string> newline = std::nullopt) {
    check_encoding(encoding);
    auto f = std::make_shared<TextFile>(open_xz(path, mode, format, check_id, preset), path, mode);
    f->translate_newlines = !newline;
    return f;
}
inline std::shared_ptr<BinaryFile> open_binary(const std::string& path, const std::string& mode, std::optional<std::int64_t> format,
                                               std::int64_t check_id, std::optional<std::int64_t> preset) {
    return std::make_shared<BinaryFile>(open_xz(path, mode, format, check_id, preset), path, mode);
}

}  // namespace sd::lzma
