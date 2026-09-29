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

}  // namespace sd::zlib
