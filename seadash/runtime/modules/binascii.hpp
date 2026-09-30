// The `binascii` module: hex and base64 conversions and checksums (the functions
// base64 and zlib are built on). Not supported yet: uu and quoted-printable.
#pragma once

#include <zlib.h>

namespace sd::binascii {

struct Error : ValueError {
    using ValueError::ValueError;
    std::string sd_type() const override { return "binascii.Error"; }
};

[[noreturn]] inline void fail(const std::string& msg) { throw Thrown{std::make_shared<Error>(msg)}; }

// hexlify(data, sep, bytes_per_sep): "0102:0304" — groups count from the right, or from
// the left if bytes_per_sep is negative, like bytes.hex().
inline bytes hexlify(const bytes& data, std::optional<std::string> sep = std::nullopt, std::int64_t bytes_per_sep = 1) {
    static const char* digits = "0123456789abcdef";
    const std::string& in = data.data;
    std::string out;
    if (!sep || bytes_per_sep == 0 || in.empty()) {
        for (unsigned char c : in) {
            out += digits[c >> 4];
            out += digits[c & 15];
        }
        return bytes(out);
    }
    if (sep->size() != 1) fail(static_cast<unsigned char>((*sep)[0]) >= 0x80 ? "sep must be ASCII." : "sep must be length 1.");
    std::size_t group = static_cast<std::size_t>(bytes_per_sep < 0 ? -bytes_per_sep : bytes_per_sep);
    // The first group is the short one when counting from the right.
    std::size_t first = bytes_per_sep > 0 ? (in.size() % group ? in.size() % group : group) : group;
    for (std::size_t i = 0; i < in.size(); ++i) {
        if (i != 0 && (i < first ? false : (i - first) % group == 0)) out += *sep;
        unsigned char c = static_cast<unsigned char>(in[i]);
        out += digits[c >> 4];
        out += digits[c & 15];
    }
    return bytes(out);
}

inline int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    fail("Non-hexadecimal digit found");
}

inline void check_ascii(const std::string& s) {
    for (unsigned char c : s)
        if (c >= 0x80) raise("ValueError", "string argument should contain only ASCII characters");
}

template <class B>
bytes unhexlify(const B& data) {
    const std::string& in = raw(data);
    if constexpr (std::is_same_v<B, std::string>) check_ascii(in);
    if (in.size() % 2) fail("Odd-length string");
    std::string out;
    out.reserve(in.size() / 2);
    for (std::size_t i = 0; i < in.size(); i += 2) out += static_cast<char>(hex_digit(in[i]) * 16 + hex_digit(in[i + 1]));
    return bytes(out);
}

template <class B>
std::int64_t crc32(const B& data, std::int64_t value = 0) {
    const std::string& in = raw(data);
    return static_cast<std::int64_t>(::crc32(static_cast<uLong>(value & 0xFFFFFFFF),
                                             reinterpret_cast<const Bytef*>(in.data()), static_cast<uInt>(in.size())));
}

// CRC-CCITT (the one binhex used), 16 bits.
inline std::int64_t crc_hqx(const bytes& data, std::int64_t value) {
    std::uint32_t crc = static_cast<std::uint32_t>(value & 0xFFFF);
    for (unsigned char c : data.data) {
        crc ^= static_cast<std::uint32_t>(c) << 8;
        for (int k = 0; k < 8; ++k) crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) & 0xFFFF : (crc << 1) & 0xFFFF;
    }
    return crc;
}

inline constexpr const char* B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

inline bytes b64_encode(const std::string& in, const char* alphabet) {
    std::string out;
    out.reserve((in.size() + 2) / 3 * 4);
    std::size_t i = 0;
    auto byte = [&](std::size_t k) { return static_cast<unsigned char>(in[k]); };
    for (; i + 2 < in.size(); i += 3) {
        std::uint32_t n = (byte(i) << 16) | (byte(i + 1) << 8) | byte(i + 2);
        out += alphabet[(n >> 18) & 63];
        out += alphabet[(n >> 12) & 63];
        out += alphabet[(n >> 6) & 63];
        out += alphabet[n & 63];
    }
    if (std::size_t rest = in.size() - i) {
        std::uint32_t n = byte(i) << 16;
        if (rest == 2) n |= byte(i + 1) << 8;
        out += alphabet[(n >> 18) & 63];
        out += alphabet[(n >> 12) & 63];
        out += rest == 2 ? alphabet[(n >> 6) & 63] : '=';
        out += '=';
    }
    return bytes(out);
}

// Like Python's default (non-strict) decoding: characters outside the alphabet
// (newlines, spaces, ...) are skipped, and decoding stops at complete padding.
inline bytes b64_decode(const std::string& in, const char* alphabet) {
    int value[256];
    std::fill(std::begin(value), std::end(value), -1);
    for (int k = 0; k < 64; ++k) value[static_cast<unsigned char>(alphabet[k])] = k;

    std::vector<int> digits;
    int pads = 0;
    for (unsigned char c : in) {
        if (c == '=') {
            if (digits.size() % 4 >= 2 && ++pads + digits.size() % 4 == 4) break;
            continue;
        }
        if (value[c] >= 0 && pads == 0) digits.push_back(value[c]);
    }
    std::size_t rem = digits.size() % 4;
    if (rem == 1)
        fail("Invalid base64-encoded string: number of data characters (" + std::to_string(digits.size()) +
             ") cannot be 1 more than a multiple of 4");
    if (rem != 0 && rem + pads < 4) fail("Incorrect padding");

    std::string out;
    out.reserve(digits.size() * 3 / 4);
    for (std::size_t i = 0; i < digits.size(); i += 4) {
        std::size_t n = std::min<std::size_t>(4, digits.size() - i);
        std::uint32_t bits = 0;
        for (std::size_t k = 0; k < 4; ++k) bits = (bits << 6) | (k < n ? digits[i + k] : 0);
        out += static_cast<char>((bits >> 16) & 0xFF);
        if (n > 2) out += static_cast<char>((bits >> 8) & 0xFF);
        if (n > 3) out += static_cast<char>(bits & 0xFF);
    }
    return bytes(out);
}

inline bytes b2a_base64(const bytes& data, bool newline = true) {
    bytes out = b64_encode(data.data, B64_ALPHABET);
    if (newline) out.push_back('\n');
    return out;
}

template <class B>
bytes a2b_base64(const B& data) {
    const std::string& in = raw(data);
    if constexpr (std::is_same_v<B, std::string>) check_ascii(in);
    return b64_decode(in, B64_ALPHABET);
}

}  // namespace sd::binascii
