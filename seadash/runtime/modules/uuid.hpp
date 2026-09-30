// The `uuid` module: UUID values (16 bytes, ordered and hashable like Python's), uuid1,
// uuid3, uuid4 and uuid5. Not supported yet: `u.int` (128 bits don't fit an int),
// getnode() (uuid1 uses a random node id, as Python does without a MAC address).
#pragma once

#include <openssl/evp.h>
#include <openssl/rand.h>

#include <chrono>

namespace sd::uuid {

class UUID {
    std::string b_;  // the 16 bytes, big-endian

    static std::int64_t at(const std::string& b, std::size_t from, std::size_t n) {
        std::int64_t v = 0;
        for (std::size_t k = 0; k < n; ++k) v = (v << 8) | static_cast<unsigned char>(b[from + k]);
        return v;
    }

public:
    UUID() : b_(16, '\0') {}
    explicit UUID(std::string raw) : b_(std::move(raw)) {}

    // UUID(hex, bytes=, version=): the hex may have braces, hyphens and a urn:uuid: prefix.
    static UUID make(std::optional<std::string> hex, std::optional<bytes> raw, std::optional<std::int64_t> version) {
        if (hex.has_value() == raw.has_value()) raise("TypeError", "one of the hex or bytes arguments must be given");
        std::string b;
        if (raw) {
            if (raw->size() != 16) raise("ValueError", "bytes is not a 16-char string");
            b = raw->data;
        } else {
            std::string h = *hex;
            if (h.starts_with("urn:uuid:")) h = h.substr(9);
            std::string digits;
            for (char c : h)
                if (c != '{' && c != '}' && c != '-') digits += c;
            if (digits.size() != 32) raise("ValueError", "badly formed hexadecimal UUID string");
            for (char c : digits)
                if (!std::isxdigit(static_cast<unsigned char>(c)))
                    raise("ValueError", "invalid literal for int() with base 16: " + repr_str(digits));
            for (std::size_t i = 0; i < 32; i += 2) b += static_cast<char>(std::stoi(digits.substr(i, 2), nullptr, 16));
        }
        if (version) {
            if (*version < 1 || *version > 8) raise("ValueError", "illegal version number");
            b[8] = static_cast<char>((static_cast<unsigned char>(b[8]) & 0x3F) | 0x80);  // the RFC 4122 variant
            b[6] = static_cast<char>((static_cast<unsigned char>(b[6]) & 0x0F) | (*version << 4));
        }
        return UUID(b);
    }

    std::string hex() const { return bytes_hex(bytes(b_)); }
    bytes get_bytes() const { return bytes(b_); }
    std::string variant() const {
        unsigned char c = static_cast<unsigned char>(b_[8]);
        if (!(c & 0x80)) return "reserved for NCS compatibility";
        if (!(c & 0x40)) return "specified in RFC 4122";
        if (!(c & 0x20)) return "reserved for Microsoft compatibility";
        return "reserved for future definition";
    }
    std::optional<std::int64_t> version() const {
        if (variant() != "specified in RFC 4122") return std::nullopt;
        return static_cast<unsigned char>(b_[6]) >> 4;
    }
    std::string urn() const { return "urn:uuid:" + sd_str(); }
    std::tuple<std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t> fields() const {
        return {at(b_, 0, 4), at(b_, 4, 2), at(b_, 6, 2), at(b_, 8, 1), at(b_, 9, 1), at(b_, 10, 6)};
    }
    std::int64_t time_low() const { return at(b_, 0, 4); }
    std::int64_t time_mid() const { return at(b_, 4, 2); }
    std::int64_t time_hi_version() const { return at(b_, 6, 2); }
    std::int64_t clock_seq_hi_variant() const { return at(b_, 8, 1); }
    std::int64_t clock_seq_low() const { return at(b_, 9, 1); }
    std::int64_t node() const { return at(b_, 10, 6); }
    std::int64_t clock_seq() const { return ((at(b_, 8, 1) & 0x3F) << 8) | at(b_, 9, 1); }
    std::int64_t time() const {
        return ((at(b_, 6, 2) & 0x0FFF) << 48) | (at(b_, 4, 2) << 32) | at(b_, 0, 4);
    }

    std::string sd_str() const {
        std::string h = hex();
        return h.substr(0, 8) + "-" + h.substr(8, 4) + "-" + h.substr(12, 4) + "-" + h.substr(16, 4) + "-" + h.substr(20);
    }
    std::string sd_repr() const { return "UUID('" + sd_str() + "')"; }
    const std::string& raw() const { return b_; }
    bool operator==(const UUID& o) const { return b_ == o.b_; }
    auto operator<=>(const UUID& o) const { return b_ <=> o.b_; }
};

inline std::string random_bytes(std::size_t n) {
    std::string out(n, '\0');
    if (RAND_bytes(reinterpret_cast<unsigned char*>(out.data()), static_cast<int>(n)) != 1) raise("OSError", "no random bytes available");
    return out;
}

inline UUID uuid4() { return UUID::make(std::nullopt, bytes(random_bytes(16)), 4); }

// uuid1(node=None, clock_seq=None): time-based. The node is a random 48-bit number with the
// multicast bit set (Python's fallback when it has no MAC address).
inline UUID uuid1(std::optional<std::int64_t> node = std::nullopt, std::optional<std::int64_t> clock_seq = std::nullopt) {
    static std::mutex mu;
    static std::int64_t last = 0, random_node = -1;
    using namespace std::chrono;
    std::lock_guard lock(mu);
    // 100-nanosecond intervals since the UUID epoch, 1582-10-15.
    std::int64_t now = duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count() / 100 + 0x01b21dd213814000LL;
    if (now <= last) now = last + 1;
    last = now;
    if (!clock_seq) {
        std::string r = random_bytes(2);
        clock_seq = static_cast<std::int64_t>(static_cast<unsigned char>(r[0]) << 8 | static_cast<unsigned char>(r[1])) & 0x3FFF;
    }
    if (!node) {
        if (random_node < 0) {
            std::string r = random_bytes(6);
            random_node = 0;
            for (unsigned char c : r) random_node = (random_node << 8) | c;
            random_node |= 0x010000000000LL;
        }
        node = random_node;
    }
    std::int64_t time_low = now & 0xFFFFFFFF, time_mid = (now >> 32) & 0xFFFF, time_hi = (now >> 48) & 0x0FFF;
    std::string b;
    auto put = [&](std::int64_t v, int n) {
        for (int k = n - 1; k >= 0; --k) b += static_cast<char>((v >> (8 * k)) & 0xFF);
    };
    put(time_low, 4);
    put(time_mid, 2);
    put(time_hi, 2);
    put(*clock_seq >> 8, 1);
    put(*clock_seq & 0xFF, 1);
    put(*node, 6);
    return UUID::make(std::nullopt, bytes(b), 1);
}

inline UUID named(const UUID& ns, const std::string& name, const EVP_MD* md, std::int64_t version) {
    unsigned char out[EVP_MAX_MD_SIZE];
    unsigned int n = 0;
    EVP_MD_CTX* c = EVP_MD_CTX_new();
    bool ok = c && EVP_DigestInit_ex(c, md, nullptr) && EVP_DigestUpdate(c, ns.raw().data(), 16) &&
              EVP_DigestUpdate(c, name.data(), name.size()) && EVP_DigestFinal_ex(c, out, &n);
    EVP_MD_CTX_free(c);
    if (!ok) raise("RuntimeError", "OpenSSL digest failed");
    return UUID::make(std::nullopt, bytes(std::string(reinterpret_cast<char*>(out), 16)), version);
}
inline UUID uuid3(const UUID& ns, const std::string& name) { return named(ns, name, EVP_md5(), 3); }
inline UUID uuid5(const UUID& ns, const std::string& name) { return named(ns, name, EVP_sha1(), 5); }

inline const UUID NAMESPACE_DNS = UUID::make("6ba7b810-9dad-11d1-80b4-00c04fd430c8", std::nullopt, std::nullopt);
inline const UUID NAMESPACE_URL = UUID::make("6ba7b811-9dad-11d1-80b4-00c04fd430c8", std::nullopt, std::nullopt);
inline const UUID NAMESPACE_OID = UUID::make("6ba7b812-9dad-11d1-80b4-00c04fd430c8", std::nullopt, std::nullopt);
inline const UUID NAMESPACE_X500 = UUID::make("6ba7b814-9dad-11d1-80b4-00c04fd430c8", std::nullopt, std::nullopt);

}  // namespace sd::uuid

template <>
struct std::hash<sd::uuid::UUID> {
    std::size_t operator()(const sd::uuid::UUID& u) const { return std::hash<std::string>{}(u.raw()); }
};
