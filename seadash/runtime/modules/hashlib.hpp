// The `hashlib` and `hmac` modules, on OpenSSL's EVP interface. A hash object is a
// handle (copies share it, like Python's objects); copy() makes an independent one.
#pragma once

#include <openssl/core_names.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>

namespace sd::hashlib {

// Python's algorithm names -> OpenSSL's.
inline const char* openssl_name(const std::string& name) {
    static const std::pair<const char*, const char*> names[] = {
        {"md5", "MD5"}, {"sha1", "SHA1"}, {"sha224", "SHA224"}, {"sha256", "SHA256"}, {"sha384", "SHA384"},
        {"sha512", "SHA512"}, {"sha3_224", "SHA3-224"}, {"sha3_256", "SHA3-256"}, {"sha3_384", "SHA3-384"},
        {"sha3_512", "SHA3-512"}, {"blake2b", "BLAKE2B-512"}, {"blake2s", "BLAKE2S-256"}, {"shake_128", "SHAKE128"},
        {"shake_256", "SHAKE256"}, {"sha512_224", "SHA512-224"}, {"sha512_256", "SHA512-256"}, {"sm3", "SM3"},
    };
    std::string lower = str_lower(name);
    for (auto [py, ossl] : names)
        if (lower == py) return ossl;
    return nullptr;
}

inline std::set<std::string> algorithms_guaranteed() {
    return {"blake2b", "blake2s", "md5", "sha1", "sha224", "sha256", "sha384", "sha3_224", "sha3_256",
            "sha3_384", "sha3_512", "sha512", "shake_128", "shake_256"};
}
inline std::set<std::string> algorithms_available() {
    std::set<std::string> out = algorithms_guaranteed();
    for (const char* extra : {"sha512_224", "sha512_256", "sm3"}) {
        if (EVP_MD* md = EVP_MD_fetch(nullptr, openssl_name(extra), nullptr)) {
            out.insert(extra);
            EVP_MD_free(md);
        }
    }
    return out;
}

[[noreturn]] inline void openssl_failed() { raise("ValueError", "hashing failed (OpenSSL error)"); }

inline std::string to_hex(const std::string& raw) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    for (unsigned char c : raw) out += digits[c >> 4], out += digits[c & 15];
    return out;
}

class Hash {
    struct State {
        std::string name;
        EVP_MD* md = nullptr;
        EVP_MD_CTX* ctx = nullptr;
        ~State() {
            EVP_MD_CTX_free(ctx);
            EVP_MD_free(md);
        }
    };
    std::shared_ptr<State> s_;
    bool xof() const { return EVP_MD_get_flags(s_->md) & EVP_MD_FLAG_XOF; }
    std::string finish(std::size_t length) const {  // on a copy, so the hash can be updated further
        EVP_MD_CTX* c = EVP_MD_CTX_new();
        if (!c || !EVP_MD_CTX_copy_ex(c, s_->ctx)) openssl_failed();
        std::string out(xof() ? length : static_cast<std::size_t>(EVP_MD_get_size(s_->md)), '\0');
        unsigned int n = 0;
        bool ok = xof() ? EVP_DigestFinalXOF(c, reinterpret_cast<unsigned char*>(out.data()), out.size())
                        : EVP_DigestFinal_ex(c, reinterpret_cast<unsigned char*>(out.data()), &n);
        EVP_MD_CTX_free(c);
        if (!ok) openssl_failed();
        return out;
    }

public:
    Hash() = default;
    Hash(const std::string& name, const bytes& data = bytes()) : s_(std::make_shared<State>()) {
        const char* ossl = openssl_name(name);
        s_->name = str_lower(name);
        s_->md = ossl ? EVP_MD_fetch(nullptr, ossl, nullptr) : nullptr;
        if (!s_->md) raise("ValueError", "unsupported hash type " + name);
        s_->ctx = EVP_MD_CTX_new();
        if (!s_->ctx || !EVP_DigestInit_ex(s_->ctx, s_->md, nullptr)) openssl_failed();
        update(data);
    }
    void update(const bytes& data) {
        if (!EVP_DigestUpdate(s_->ctx, data.data.data(), data.data.size())) openssl_failed();
    }
    bytes digest(std::optional<std::int64_t> length = std::nullopt) const {
        if (xof() && !length) raise("TypeError", "digest() missing required argument 'length' (pos 1)");
        return bytes(finish(static_cast<std::size_t>(length.value_or(0))));
    }
    std::string hexdigest(std::optional<std::int64_t> length = std::nullopt) const {
        if (xof() && !length) raise("TypeError", "hexdigest() missing required argument 'length' (pos 1)");
        return to_hex(finish(static_cast<std::size_t>(length.value_or(0))));
    }
    Hash copy() const {
        Hash h;
        h.s_ = std::make_shared<State>();
        h.s_->name = s_->name;
        EVP_MD_up_ref(s_->md);
        h.s_->md = s_->md;
        h.s_->ctx = EVP_MD_CTX_new();
        if (!h.s_->ctx || !EVP_MD_CTX_copy_ex(h.s_->ctx, s_->ctx)) openssl_failed();
        return h;
    }
    std::string name() const { return s_->name; }
    std::int64_t digest_size() const { return xof() ? 0 : EVP_MD_get_size(s_->md); }
    std::int64_t block_size() const { return EVP_MD_get_block_size(s_->md); }
    std::string sd_repr() const { return "<" + s_->name + " _hashlib.HASH object>"; }
};

inline Hash new_(const std::string& name, const bytes& data = bytes()) { return Hash(name, data); }

inline bytes pbkdf2_hmac(const std::string& hash_name, const bytes& password, const bytes& salt, std::int64_t iterations,
                         std::optional<std::int64_t> dklen = std::nullopt) {
    const char* ossl = openssl_name(hash_name);
    EVP_MD* md = ossl ? EVP_MD_fetch(nullptr, ossl, nullptr) : nullptr;
    if (!md) raise("ValueError", "unsupported hash type " + hash_name);
    if (iterations < 1) {
        EVP_MD_free(md);
        raise("ValueError", "iteration value must be greater than 0.");
    }
    std::string out(dklen ? static_cast<std::size_t>(*dklen) : static_cast<std::size_t>(EVP_MD_get_size(md)), '\0');
    bool ok = PKCS5_PBKDF2_HMAC(password.data.data(), static_cast<int>(password.data.size()),
                                reinterpret_cast<const unsigned char*>(salt.data.data()), static_cast<int>(salt.data.size()),
                                static_cast<int>(iterations), md, static_cast<int>(out.size()),
                                reinterpret_cast<unsigned char*>(out.data()));
    EVP_MD_free(md);
    if (!ok) openssl_failed();
    return bytes(out);
}

inline Hash file_digest(const std::shared_ptr<BinaryFile>& file, const std::string& name) {
    Hash h(name);
    for (bytes chunk; !(chunk = file->read(1 << 16)).data.empty();) h.update(chunk);
    return h;
}

}  // namespace sd::hashlib

namespace sd::hmac {

class HMAC {
    struct State {
        std::string digest_name;
        EVP_MAC_CTX* ctx = nullptr;
        ~State() { EVP_MAC_CTX_free(ctx); }
    };
    std::shared_ptr<State> s_;

    static EVP_MAC* mac() {
        static EVP_MAC* m = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
        return m;
    }

public:
    HMAC() = default;
    HMAC(const bytes& key, const std::optional<bytes>& msg, const std::string& digestmod) : s_(std::make_shared<State>()) {
        const char* ossl = hashlib::openssl_name(digestmod);
        if (!ossl) raise("ValueError", "unsupported hash type " + digestmod);
        s_->digest_name = str_lower(digestmod);
        s_->ctx = EVP_MAC_CTX_new(mac());
        OSSL_PARAM params[] = {OSSL_PARAM_construct_utf8_string(OSSL_MAC_PARAM_DIGEST, const_cast<char*>(ossl), 0),
                               OSSL_PARAM_construct_end()};
        if (!s_->ctx || !EVP_MAC_init(s_->ctx, reinterpret_cast<const unsigned char*>(key.data.data()), key.data.size(), params))
            hashlib::openssl_failed();
        if (msg) update(*msg);
    }
    void update(const bytes& msg) {
        if (!EVP_MAC_update(s_->ctx, reinterpret_cast<const unsigned char*>(msg.data.data()), msg.data.size()))
            hashlib::openssl_failed();
    }
    bytes digest() const {
        EVP_MAC_CTX* c = EVP_MAC_CTX_dup(s_->ctx);  // the HMAC can still be updated afterwards
        std::string out(EVP_MAC_CTX_get_mac_size(c), '\0');
        std::size_t n = 0;
        bool ok = c && EVP_MAC_final(c, reinterpret_cast<unsigned char*>(out.data()), &n, out.size());
        EVP_MAC_CTX_free(c);
        if (!ok) hashlib::openssl_failed();
        out.resize(n);
        return bytes(out);
    }
    std::string hexdigest() const { return hashlib::to_hex(digest().data); }
    HMAC copy() const {
        HMAC h;
        h.s_ = std::make_shared<State>();
        h.s_->digest_name = s_->digest_name;
        h.s_->ctx = EVP_MAC_CTX_dup(s_->ctx);
        return h;
    }
    std::string name() const { return "hmac-" + s_->digest_name; }
    std::int64_t digest_size() const { return static_cast<std::int64_t>(EVP_MAC_CTX_get_mac_size(s_->ctx)); }
    std::int64_t block_size() const { return hashlib::Hash(s_->digest_name).block_size(); }
    std::string sd_repr() const { return "<hmac.HMAC object>"; }
};

inline HMAC new_(const bytes& key, const std::optional<bytes>& msg, const std::string& digestmod) {
    return HMAC(key, msg, digestmod);
}
inline bytes digest(const bytes& key, const bytes& msg, const std::string& digest) {
    return HMAC(key, msg, digest).digest();
}
// Constant time (for the same length), so comparing secrets doesn't leak them through timing.
inline bool compare_digest(const std::string& a, const std::string& b) {
    return a.size() == b.size() && CRYPTO_memcmp(a.data(), b.data(), a.size()) == 0;
}
inline bool compare_digest(const bytes& a, const bytes& b) { return compare_digest(a.data, b.data); }

}  // namespace sd::hmac
