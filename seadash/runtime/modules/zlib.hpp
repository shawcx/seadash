// The `zlib` module: a thin wrapper over the system zlib (programs link with -lz).
// Inputs may be bytes or str (a str is taken as its UTF-8 bytes).
#pragma once

#include <zlib.h>

namespace sd::zlib {

struct error : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "zlib.error"; }
};

[[noreturn]] inline void fail(const std::string& msg) { throw Thrown{std::make_shared<error>(msg)}; }

template <class B>
bytes compress(const B& data, std::int64_t level = -1) {
    const std::string& in = raw(data);
    if (level < -1 || level > 9) fail("Bad compression level");
    uLongf size = compressBound(static_cast<uLong>(in.size()));
    std::string out(size, '\0');
    int rc = compress2(reinterpret_cast<Bytef*>(out.data()), &size, reinterpret_cast<const Bytef*>(in.data()),
                       static_cast<uLong>(in.size()), static_cast<int>(level));
    if (rc != Z_OK) fail("Error " + std::to_string(rc) + " while compressing data");
    out.resize(size);
    return bytes(out);
}

template <class B>
bytes decompress(const B& data) {
    const std::string& in = raw(data);
    z_stream zs{};
    if (inflateInit(&zs) != Z_OK) fail("Error while initializing decompression");
    zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
    zs.avail_in = static_cast<uInt>(in.size());
    std::string out;
    char chunk[64 * 1024];
    int rc;
    do {
        zs.next_out = reinterpret_cast<Bytef*>(chunk);
        zs.avail_out = sizeof chunk;
        rc = inflate(&zs, Z_NO_FLUSH);
        if (rc != Z_OK && rc != Z_STREAM_END) {
            std::string why = zs.msg ? zs.msg : (rc == Z_BUF_ERROR ? "incomplete or truncated stream" : "invalid data");
            inflateEnd(&zs);
            fail("Error " + std::to_string(rc) + " while decompressing data: " + why);
        }
        out.append(chunk, sizeof chunk - zs.avail_out);
    } while (rc != Z_STREAM_END);
    inflateEnd(&zs);
    return bytes(out);
}

template <class B>
std::int64_t crc32(const B& data, std::int64_t value = 0) {
    const std::string& in = raw(data);
    return static_cast<std::int64_t>(::crc32(static_cast<uLong>(value & 0xFFFFFFFF),
                                             reinterpret_cast<const Bytef*>(in.data()), static_cast<uInt>(in.size())));
}

template <class B>
std::int64_t adler32(const B& data, std::int64_t value = 1) {
    const std::string& in = raw(data);
    return static_cast<std::int64_t>(::adler32(static_cast<uLong>(value & 0xFFFFFFFF),
                                               reinterpret_cast<const Bytef*>(in.data()), static_cast<uInt>(in.size())));
}

// ---- compressobj() / decompressobj(): incremental, sharing state between copies -------

inline std::string zlib_message(int rc, const char* msg, const char* what) {
    std::string why = msg ? msg : rc == Z_BUF_ERROR ? "incomplete or truncated stream" : rc == Z_MEM_ERROR ? "out of memory" : "";
    return "Error " + std::to_string(rc) + " while " + what + " data" + (why.empty() ? "" : ": " + why);
}

class Compress {
    struct State {
        z_stream zs{};
        bool live = false, finished = false;
        ~State() {
            if (live) deflateEnd(&zs);
        }
    };
    std::shared_ptr<State> s_;

    bytes run(const std::string& in, int mode) {
        z_stream& zs = s_->zs;
        if (s_->finished) fail("Error -2 while compressing data: inconsistent stream state");
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
        zs.avail_in = static_cast<uInt>(in.size());
        std::string out;
        char chunk[64 * 1024];
        int rc;
        do {
            zs.next_out = reinterpret_cast<Bytef*>(chunk);
            zs.avail_out = sizeof chunk;
            rc = deflate(&zs, mode);
            if (rc == Z_STREAM_ERROR) fail(zlib_message(rc, zs.msg, "compressing"));
            out.append(chunk, sizeof chunk - zs.avail_out);
        } while (zs.avail_out == 0 || (mode == Z_FINISH && rc != Z_STREAM_END));
        if (mode == Z_FINISH) s_->finished = true;
        return bytes(out);
    }

public:
    explicit Compress(std::int64_t level = -1, std::int64_t method = Z_DEFLATED, std::int64_t wbits = MAX_WBITS,
                      std::int64_t mem_level = 8, std::int64_t strategy = Z_DEFAULT_STRATEGY)
        : s_(std::make_shared<State>()) {
        int rc = deflateInit2(&s_->zs, static_cast<int>(level), static_cast<int>(method), static_cast<int>(wbits),
                              static_cast<int>(mem_level), static_cast<int>(strategy));
        if (rc == Z_STREAM_ERROR) raise("ValueError", "Invalid initialization option");
        if (rc != Z_OK) fail(zlib_message(rc, s_->zs.msg, "creating compression object"));
        s_->live = true;
    }
    bytes compress(const bytes& data) { return run(data.data, Z_NO_FLUSH); }
    bytes flush(std::int64_t mode = Z_FINISH) {
        if (mode == Z_NO_FLUSH) return bytes();
        return run("", static_cast<int>(mode));
    }
    Compress copy() const {
        Compress c(*this);
        c.s_ = std::make_shared<State>();
        if (deflateCopy(&c.s_->zs, &s_->zs) != Z_OK) fail("Error -2 while copying compression object: inconsistent stream state");
        c.s_->live = true;
        c.s_->finished = s_->finished;
        return c;
    }
    std::string sd_repr() const { return "<zlib.Compress object>"; }
};

class Decompress {
    struct State {
        z_stream zs{};
        bool live = false, eof = false;
        std::string unused_data, unconsumed_tail;
        ~State() {
            if (live) inflateEnd(&zs);
        }
    };
    std::shared_ptr<State> s_;

public:
    explicit Decompress(std::int64_t wbits = MAX_WBITS) : s_(std::make_shared<State>()) {
        int rc = inflateInit2(&s_->zs, static_cast<int>(wbits));
        if (rc == Z_STREAM_ERROR) raise("ValueError", "Invalid initialization option");
        if (rc != Z_OK) fail(zlib_message(rc, s_->zs.msg, "creating decompression object"));
        s_->live = true;
    }
    // Up to max_length bytes (0: no limit); input that couldn't be used yet goes to
    // unconsumed_tail, and input after the end of the stream to unused_data.
    bytes decompress(const bytes& data, std::int64_t max_length = 0) {
        if (max_length < 0) raise("ValueError", "max_length must be non-negative");
        z_stream& zs = s_->zs;
        const std::string& in = data.data;
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
        zs.avail_in = static_cast<uInt>(in.size());
        std::string out;
        char chunk[64 * 1024];
        while (!s_->eof && zs.avail_in > 0) {
            std::size_t room = max_length ? std::min<std::size_t>(sizeof chunk, static_cast<std::size_t>(max_length) - out.size()) : sizeof chunk;
            if (room == 0) break;
            zs.next_out = reinterpret_cast<Bytef*>(chunk);
            zs.avail_out = static_cast<uInt>(room);
            int rc = inflate(&zs, Z_SYNC_FLUSH);
            out.append(chunk, room - zs.avail_out);
            if (rc == Z_STREAM_END) {
                s_->eof = true;
            } else if (rc == Z_BUF_ERROR) {
                break;  // needs more input
            } else if (rc != Z_OK) {
                fail(zlib_message(rc, zs.msg, "decompressing"));
            }
        }
        std::string left(in.data() + (in.size() - zs.avail_in), zs.avail_in);
        if (s_->eof) s_->unused_data += left;
        s_->unconsumed_tail = left;
        return bytes(out);
    }
    bytes flush(std::int64_t length = 16384) {
        if (length <= 0) raise("ValueError", "length must be greater than zero");
        bytes out = decompress(bytes(s_->unconsumed_tail));
        return out;
    }
    bool eof() const { return s_->eof; }
    bytes unused_data() const { return bytes(s_->unused_data); }
    bytes unconsumed_tail() const { return bytes(s_->unconsumed_tail); }
    Decompress copy() const {
        Decompress c(*this);
        c.s_ = std::make_shared<State>();
        if (inflateCopy(&c.s_->zs, &s_->zs) != Z_OK) fail("Error -2 while copying decompression object: inconsistent stream state");
        c.s_->live = true;
        c.s_->eof = s_->eof;
        c.s_->unused_data = s_->unused_data;
        c.s_->unconsumed_tail = s_->unconsumed_tail;
        return c;
    }
    std::string sd_repr() const { return "<zlib.Decompress object>"; }
};

inline Compress compressobj(std::int64_t level = -1, std::int64_t method = Z_DEFLATED, std::int64_t wbits = MAX_WBITS,
                            std::int64_t mem_level = 8, std::int64_t strategy = Z_DEFAULT_STRATEGY) {
    return Compress(level, method, wbits, mem_level, strategy);
}
inline Decompress decompressobj(std::int64_t wbits = MAX_WBITS) { return Decompress(wbits); }

}  // namespace sd::zlib
