// The `bz2` module: compress and decompress in memory, and open() for .bz2 files, which
// gives the same file objects as the builtin open(). Not supported yet: the BZ2File class
// and the incremental BZ2Compressor/BZ2Decompressor.
#pragma once

#include <bzlib.h>
#include <fcntl.h>
#include <unistd.h>

#include "cookie_file.hpp"

namespace sd::bz2 {

inline constexpr std::size_t CHUNK = 64 * 1024;

inline void check_level(std::int64_t level) {
    if (level < 1 || level > 9) raise("ValueError", "compresslevel must be between 1 and 9");
}

inline bytes compress(const bytes& data, std::int64_t compresslevel = 9) {
    check_level(compresslevel);
    bz_stream zs{};
    if (BZ2_bzCompressInit(&zs, static_cast<int>(compresslevel), 0, 0) != BZ_OK) raise("MemoryError", "");
    zs.next_in = const_cast<char*>(data.data.data());
    zs.avail_in = static_cast<unsigned>(data.size());
    std::string out;
    char chunk[CHUNK];
    int rc;
    do {
        zs.next_out = chunk;
        zs.avail_out = sizeof chunk;
        rc = BZ2_bzCompress(&zs, BZ_FINISH);
        out.append(chunk, sizeof chunk - zs.avail_out);
    } while (rc == BZ_FINISH_OK);
    BZ2_bzCompressEnd(&zs);
    if (rc != BZ_STREAM_END) raise("OSError", "Invalid data stream");
    return bytes(out);
}

// Every stream in the data, one after the other; trailing data that isn't a stream is
// ignored, like Python.
inline bytes decompress(const bytes& data) {
    const std::string& in = data.data;
    std::string out;
    std::size_t pos = 0;
    bool any = false;
    while (pos < in.size()) {
        bz_stream zs{};
        if (BZ2_bzDecompressInit(&zs, 0, 0) != BZ_OK) raise("MemoryError", "");
        zs.next_in = const_cast<char*>(in.data() + pos);
        zs.avail_in = static_cast<unsigned>(in.size() - pos);
        std::string piece;
        char chunk[CHUNK];
        int rc;
        do {
            zs.next_out = chunk;
            zs.avail_out = sizeof chunk;
            rc = BZ2_bzDecompress(&zs);
            piece.append(chunk, sizeof chunk - zs.avail_out);
        } while (rc == BZ_OK && (zs.avail_in > 0 || zs.avail_out == 0));
        pos = in.size() - zs.avail_in;
        BZ2_bzDecompressEnd(&zs);
        if (rc == BZ_STREAM_END) {
            out += piece;
            any = true;
        } else if (rc == BZ_OK) {
            raise("ValueError", "Compressed data ended before the end-of-stream marker was reached");
        } else if (any) {
            break;
        } else {
            raise("OSError", "Invalid data stream");
        }
    }
    return bytes(out);
}

// ---- open(): a FILE* over a streaming (de)compressor ---------------------------------

struct Reader {
    int fd;
    bz_stream zs{};
    char input[CHUNK];
    bool open = false, done = false;  // done: the input ended (cleanly)
    ~Reader() {
        if (open) BZ2_bzDecompressEnd(&zs);
        ::close(fd);
    }
    std::int64_t read(char* buf, std::size_t n) {
        zs.next_out = buf;
        zs.avail_out = static_cast<unsigned>(n);
        while (zs.avail_out > 0 && !done) {
            if (!open) {
                if (BZ2_bzDecompressInit(&zs, 0, 0) != BZ_OK) raise("MemoryError", "");
                open = true;
            }
            if (zs.avail_in == 0) {
                ssize_t got = ::read(fd, input, sizeof input);
                if (got < 0) raise_os(errno, std::nullopt);
                if (got == 0) raise("EOFError", "Compressed file ended before the end-of-stream marker was reached");  // (even an empty file)
                zs.next_in = input;
                zs.avail_in = static_cast<unsigned>(got);
            }
            int rc = BZ2_bzDecompress(&zs);
            if (rc == BZ_STREAM_END) {  // another stream may follow
                char* next_in = zs.next_in;
                unsigned avail_in = zs.avail_in;
                char* next_out = zs.next_out;
                unsigned avail_out = zs.avail_out;
                BZ2_bzDecompressEnd(&zs);
                open = false;
                zs = bz_stream{};
                if (avail_in == 0) {
                    ssize_t got = ::read(fd, input, sizeof input);
                    if (got < 0) raise_os(errno, std::nullopt);
                    if (got == 0) done = true;
                    next_in = input;
                    avail_in = static_cast<unsigned>(std::max<ssize_t>(got, 0));
                }
                zs.next_in = next_in;
                zs.avail_in = avail_in;
                zs.next_out = next_out;
                zs.avail_out = avail_out;
            } else if (rc != BZ_OK) {
                raise("OSError", "Invalid data stream");
            }
        }
        return static_cast<std::int64_t>(n - zs.avail_out);
    }
};

struct Writer {
    int fd;
    bz_stream zs{};
    bool open = false;
    void pump(int action) {
        char chunk[CHUNK];
        int rc;
        do {
            zs.next_out = chunk;
            zs.avail_out = sizeof chunk;
            rc = BZ2_bzCompress(&zs, action);
            std::size_t n = sizeof chunk - zs.avail_out;
            for (std::size_t written = 0; written < n;) {
                ssize_t w = ::write(fd, chunk + written, n - written);
                if (w < 0) raise_os(errno, std::nullopt);
                written += static_cast<std::size_t>(w);
            }
        } while (rc == BZ_RUN_OK ? zs.avail_in > 0 : rc == BZ_FINISH_OK);
    }
    std::int64_t write(const char* buf, std::size_t n) {
        zs.next_in = const_cast<char*>(buf);
        zs.avail_in = static_cast<unsigned>(n);
        pump(BZ_RUN);
        return static_cast<std::int64_t>(n);
    }
    void finish() {
        if (open) {
            zs.next_in = nullptr;
            zs.avail_in = 0;
            pump(BZ_FINISH);
            BZ2_bzCompressEnd(&zs);
            open = false;
        }
    }
    ~Writer() { ::close(fd); }
};

inline std::FILE* open_bz(const std::string& path, const std::string& mode, std::int64_t compresslevel) {
    check_level(compresslevel);
    char kind = mode[mode.find_first_of("rwax")];
    int fd = open_fd_for(path, mode);
    Cookie cookie;
    if (kind == 'r') {
        cookie = {new Reader{fd, {}, {}},
                  [](void* s, char* buf, std::size_t n) { return guarded([&] { return static_cast<Reader*>(s)->read(buf, n); }); },
                  nullptr, [](void* s) -> int { delete static_cast<Reader*>(s); return 0; }};
    } else {
        auto* w = new Writer{fd};
        if (BZ2_bzCompressInit(&w->zs, static_cast<int>(compresslevel), 0, 0) != BZ_OK) {
            delete w;
            raise("MemoryError", "");
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

inline std::shared_ptr<TextFile> open_text(const std::string& path, const std::string& mode, std::int64_t compresslevel,
                                           const std::string& encoding = "utf-8", std::optional<std::string> newline = std::nullopt) {
    check_encoding(encoding);
    auto f = std::make_shared<TextFile>(open_bz(path, mode, compresslevel), path, mode);
    f->translate_newlines = !newline;
    return f;
}
inline std::shared_ptr<BinaryFile> open_binary(const std::string& path, const std::string& mode, std::int64_t compresslevel) {
    return std::make_shared<BinaryFile>(open_bz(path, mode, compresslevel), path, mode);
}

// ---- BZ2Compressor / BZ2Decompressor: incremental, sharing state between copies -------

class BZ2Compressor {
    struct State {
        bz_stream zs{};
        bool live = false, flushed = false;
        ~State() {
            if (live) BZ2_bzCompressEnd(&zs);
        }
    };
    std::shared_ptr<State> s_;

    bytes run(const std::string& in, int action) {
        bz_stream& zs = s_->zs;
        zs.next_in = const_cast<char*>(in.data());
        zs.avail_in = static_cast<unsigned>(in.size());
        std::string out;
        char chunk[CHUNK];
        int rc;
        do {
            zs.next_out = chunk;
            zs.avail_out = sizeof chunk;
            rc = BZ2_bzCompress(&zs, action);
            if (rc < 0) raise("OSError", "Invalid data stream");
            out.append(chunk, sizeof chunk - zs.avail_out);
        } while (action == BZ_FINISH ? rc != BZ_STREAM_END : zs.avail_in > 0);
        return bytes(out);
    }

public:
    explicit BZ2Compressor(std::int64_t compresslevel = 9) : s_(std::make_shared<State>()) {
        check_level(compresslevel);
        if (BZ2_bzCompressInit(&s_->zs, static_cast<int>(compresslevel), 0, 0) != BZ_OK) raise("MemoryError", "");
        s_->live = true;
    }
    bytes compress(const bytes& data) {
        if (s_->flushed) raise("ValueError", "Compressor has been flushed");
        return run(data.data, BZ_RUN);
    }
    bytes flush() {
        if (s_->flushed) raise("ValueError", "Repeated call to flush()");
        s_->flushed = true;
        return run("", BZ_FINISH);
    }
    std::string sd_repr() const { return "<bz2.BZ2Compressor object>"; }
};

class BZ2Decompressor {
    struct State {
        bz_stream zs{};
        bool live = false, eof = false, needs_input = true;
        std::string input, unused_data;  // input: what decompress() was given but hasn't used
        ~State() {
            if (live) BZ2_bzDecompressEnd(&zs);
        }
    };
    std::shared_ptr<State> s_;

public:
    BZ2Decompressor() : s_(std::make_shared<State>()) {
        if (BZ2_bzDecompressInit(&s_->zs, 0, 0) != BZ_OK) raise("MemoryError", "");
        s_->live = true;
    }
    // Up to max_length bytes (-1: no limit). Input isn't lost: what wasn't used yet is
    // kept for the next call, and what follows the end of the stream is unused_data.
    bytes decompress(const bytes& data, std::int64_t max_length = -1) {
        if (s_->eof) raise("EOFError", "End of stream already reached");
        bz_stream& zs = s_->zs;
        s_->input += data.data;
        zs.next_in = s_->input.data();
        zs.avail_in = static_cast<unsigned>(s_->input.size());
        std::string out;
        char chunk[CHUNK];
        zs.avail_out = 1;
        while (true) {
            std::size_t room = max_length < 0 ? sizeof chunk : std::min<std::size_t>(sizeof chunk, static_cast<std::size_t>(max_length) - out.size());
            if (room == 0) break;
            zs.next_out = chunk;
            zs.avail_out = static_cast<unsigned>(room);
            int rc = BZ2_bzDecompress(&zs);
            out.append(chunk, room - zs.avail_out);
            if (rc == BZ_STREAM_END) {
                s_->eof = true;
                break;
            }
            if (rc != BZ_OK) raise("OSError", "Invalid data stream");
            if (zs.avail_in == 0 && zs.avail_out > 0) break;  // (all input used, and nothing more to give)
        }
        std::string left(s_->input.data() + (s_->input.size() - zs.avail_in), zs.avail_in);
        if (s_->eof) {
            s_->needs_input = false;
            s_->unused_data = left;
            s_->input.clear();
        } else if (left.empty()) {
            s_->needs_input = zs.avail_out != 0;  // (a full output buffer may have more behind it)
            s_->input.clear();
        } else {
            s_->needs_input = false;
            s_->input = left;
        }
        return bytes(out);
    }
    bool eof() const { return s_->eof; }
    bool needs_input() const { return s_->needs_input; }
    bytes unused_data() const { return bytes(s_->unused_data); }
    std::string sd_repr() const { return "<bz2.BZ2Decompressor object>"; }
};

}  // namespace sd::bz2
