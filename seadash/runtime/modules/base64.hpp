// The `base64` module: RFC 4648 base64 and base16, matching Python's base64.
// Inputs may be bytes or str (a str is taken as its UTF-8 bytes).
#pragma once

namespace sd::base64 {

inline constexpr const char* STANDARD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
inline constexpr const char* URLSAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

inline bytes encode_with(const std::string& in, const char* alphabet) {
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
inline bytes decode_with(const std::string& in, const char* alphabet) {
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
        raise("ValueError", "Invalid base64-encoded string: number of data characters (" +
                                std::to_string(digits.size()) + ") cannot be 1 more than a multiple of 4");
    if (rem != 0 && rem + pads < 4) raise("ValueError", "Incorrect padding");

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

template <class B>
bytes b64encode(const B& data) {
    return encode_with(raw(data), STANDARD);
}
template <class B>
bytes b64decode(const B& data) {
    return decode_with(raw(data), STANDARD);
}
template <class B>
bytes urlsafe_b64encode(const B& data) {
    return encode_with(raw(data), URLSAFE);
}
template <class B>
bytes urlsafe_b64decode(const B& data) {
    return decode_with(raw(data), URLSAFE);
}

template <class B>
bytes b16encode(const B& data) {
    return bytes(str_upper(bytes_hex(bytes(raw(data)))));
}
template <class B>
bytes b16decode(const B& data) {
    const std::string& in = raw(data);
    if (in.size() % 2) raise("ValueError", "Odd-length string");
    auto digit = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        raise("ValueError", "Non-base16 digit found");
    };
    std::string out;
    for (std::size_t i = 0; i < in.size(); i += 2) out += static_cast<char>(digit(in[i]) * 16 + digit(in[i + 1]));
    return bytes(out);
}

}  // namespace sd::base64
