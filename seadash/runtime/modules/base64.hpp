// The `base64` module: RFC 4648 base64 and base16, matching Python's base64.
// Inputs may be bytes or str (a str is taken as its UTF-8 bytes).
#pragma once

#include "binascii.hpp"

namespace sd::base64 {

inline constexpr const char* STANDARD = binascii::B64_ALPHABET;
inline constexpr const char* URLSAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

template <class B>
bytes b64encode(const B& data) {
    return binascii::b64_encode(raw(data), STANDARD);
}
template <class B>
bytes b64decode(const B& data) {
    return binascii::b64_decode(raw(data), STANDARD);
}
template <class B>
bytes urlsafe_b64encode(const B& data) {
    return binascii::b64_encode(raw(data), URLSAFE);
}
template <class B>
bytes urlsafe_b64decode(const B& data) {
    return binascii::b64_decode(raw(data), URLSAFE);
}

template <class B>
bytes b16encode(const B& data) {
    return bytes(str_upper(bytes_hex(bytes(raw(data)))));
}
template <class B>
bytes b16decode(const B& data) {
    const std::string& in = raw(data);
    if (in.size() % 2) binascii::fail("Odd-length string");
    auto digit = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        binascii::fail("Non-base16 digit found");
    };
    std::string out;
    for (std::size_t i = 0; i < in.size(); i += 2) out += static_cast<char>(digit(in[i]) * 16 + digit(in[i + 1]));
    return bytes(out);
}

}  // namespace sd::base64
