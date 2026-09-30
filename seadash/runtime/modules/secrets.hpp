// The `secrets` module: tokens and choices from the system's cryptographic random source
// (OpenSSL's), and compare_digest.
#pragma once

#include <openssl/rand.h>

#include "base64.hpp"
#include "hashlib.hpp"
#include "random.hpp"

namespace sd::secrets {

inline constexpr std::int64_t DEFAULT_ENTROPY = 32;

inline bytes token_bytes(std::optional<std::int64_t> nbytes = std::nullopt) {
    std::int64_t n = nbytes.value_or(DEFAULT_ENTROPY);
    if (n < 0) raise("ValueError", "negative argument not allowed");
    std::string out(static_cast<std::size_t>(n), '\0');
    if (n > 0 && RAND_bytes(reinterpret_cast<unsigned char*>(out.data()), static_cast<int>(n)) != 1)
        raise("OSError", "no random bytes available");
    return bytes(out);
}
inline std::string token_hex(std::optional<std::int64_t> nbytes = std::nullopt) { return bytes_hex(token_bytes(nbytes)); }
inline std::string token_urlsafe(std::optional<std::int64_t> nbytes = std::nullopt) {
    std::string s = binascii::b64_encode(token_bytes(nbytes).data, base64::URLSAFE).data;
    while (!s.empty() && s.back() == '=') s.pop_back();
    return s;
}

// A uniform integer in [0, n), by rejection.
inline std::int64_t randbelow(std::int64_t n) {
    if (n <= 0) raise("ValueError", "Upper bound must be positive.");
    std::uint64_t limit = static_cast<std::uint64_t>(n);
    std::uint64_t mask = std::numeric_limits<std::uint64_t>::max() >> std::countl_zero(limit);
    while (true) {
        std::uint64_t r;
        if (RAND_bytes(reinterpret_cast<unsigned char*>(&r), sizeof r) != 1) raise("OSError", "no random bytes available");
        r &= mask;
        if (r < limit) return static_cast<std::int64_t>(r);
    }
}
inline std::int64_t randbits(std::int64_t k) {
    if (k < 0) raise("ValueError", "number of bits must be non-negative");
    if (k > 63) raise("ValueError", "randbits(k) gives an int, so k must be at most 63");
    if (k == 0) return 0;
    std::uint64_t r;
    if (RAND_bytes(reinterpret_cast<unsigned char*>(&r), sizeof r) != 1) raise("OSError", "no random bytes available");
    return static_cast<std::int64_t>(r >> (64 - k));
}

template <class Seq>
auto choice(const Seq& population) {
    auto&& seq = random::as_sequence(population);
    if (seq.empty()) raise("IndexError", "Cannot choose from an empty sequence");
    return random::pick(seq, randbelow(static_cast<std::int64_t>(seq.size())));
}

using hmac::compare_digest;

}  // namespace sd::secrets
