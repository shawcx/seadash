// The `ssl` module, as far as HTTPS clients need it: SSLContext (check_hostname,
// verify_mode, load_verify_locations, load_default_certs), create_default_context(),
// _create_unverified_context(), and the errors. The TLS itself is OpenSSL's.
#pragma once

#include <openssl/err.h>
#include <openssl/pem.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

namespace sd::ssl {

inline constexpr std::int64_t CERT_NONE = 0, CERT_OPTIONAL = 1, CERT_REQUIRED = 2;
inline constexpr std::int64_t PROTOCOL_TLS_CLIENT = 16;

struct SSLError : OSError {
    using OSError::OSError;
    std::string sd_type() const override { return "ssl.SSLError"; }
};
struct SSLCertVerificationError : SSLError {
    using SSLError::SSLError;
    std::string sd_type() const override { return "ssl.SSLCertVerificationError"; }
};

// An SSLContext: the settings each TLS connection starts from (its OpenSSL context is
// rebuilt when they change).
struct SSLContext {
    bool check_hostname = true;
    std::int64_t verify_mode = CERT_REQUIRED;

    SSLContext() = default;
    SSLContext(const SSLContext&) = delete;
    ~SSLContext() {
        if (ctx_) SSL_CTX_free(ctx_);
    }

    void load_default_certs() {
        default_paths_ = true;
        changed();
    }
    void load_verify_locations(std::optional<std::string> cafile = std::nullopt,
                               std::optional<std::string> capath = std::nullopt,
                               std::optional<std::string> cadata = std::nullopt) {
        if (!cafile && !capath && !cadata) raise("TypeError", "cafile, capath and cadata cannot be all omitted");
        if (cafile) {
            if (FILE* f = std::fopen(cafile->c_str(), "r")) {
                std::fclose(f);
            } else {
                raise_os(errno, *cafile);
            }
            cafiles_.push_back(*cafile);
        }
        if (capath) capaths_.push_back(*capath);
        if (cadata) cadata_.push_back(*cadata);
        changed();
    }
    std::string sd_repr() const { return "<ssl.SSLContext object>"; }

    // The OpenSSL context for the current settings.
    SSL_CTX* native() {
        if (ctx_ && built_mode_ == verify_mode && built_version_ == version_) return ctx_;
        if (ctx_) SSL_CTX_free(ctx_);
        ctx_ = SSL_CTX_new(TLS_client_method());
        SSL_CTX_set_min_proto_version(ctx_, TLS1_2_VERSION);
        if (default_paths_) SSL_CTX_set_default_verify_paths(ctx_);
        for (const auto& f : cafiles_)
            if (SSL_CTX_load_verify_locations(ctx_, f.c_str(), nullptr) != 1) failed("load_verify_locations");
        for (const auto& p : capaths_) SSL_CTX_load_verify_locations(ctx_, nullptr, p.c_str());
        for (const auto& pem : cadata_) add_pem(pem);
        SSL_CTX_set_verify(ctx_, verify_mode == CERT_NONE ? SSL_VERIFY_NONE : SSL_VERIFY_PEER, nullptr);
        built_mode_ = verify_mode;
        built_version_ = version_;
        return ctx_;
    }

    [[noreturn]] static void failed(const std::string& what) {
        unsigned long code = ERR_get_error();
        char buf[256] = "unknown error";
        if (code) ERR_error_string_n(code, buf, sizeof buf);
        raise<SSLError>(what + ": " + buf);
    }

private:
    bool default_paths_ = false;
    std::vector<std::string> cafiles_, capaths_, cadata_;
    SSL_CTX* ctx_ = nullptr;
    std::int64_t built_mode_ = -1;
    int version_ = 0, built_version_ = -1;
    void changed() { ++version_; }
    void add_pem(const std::string& pem) {
        BIO* bio = BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size()));
        X509_STORE* store = SSL_CTX_get_cert_store(ctx_);
        int added = 0;
        while (X509* cert = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr)) {
            X509_STORE_add_cert(store, cert);
            X509_free(cert);
            ++added;
        }
        BIO_free(bio);
        ERR_clear_error();
        if (!added) raise<SSLError>("[X509: NO_CERTIFICATE_OR_CRL_FOUND] no certificate or crl found");
    }
};

inline std::shared_ptr<SSLContext> SSLContext_new(std::int64_t protocol = PROTOCOL_TLS_CLIENT) {
    (void)protocol;  // (TLS client is the only kind for now)
    return std::make_shared<SSLContext>();
}
// ssl.create_default_context(): the system's trusted certificates, host names checked.
inline std::shared_ptr<SSLContext> create_default_context() {
    auto c = std::make_shared<SSLContext>();
    c->load_default_certs();
    return c;
}
// ssl._create_unverified_context(): encryption without checking who's at the other end.
inline std::shared_ptr<SSLContext> create_unverified_context() {
    auto c = create_default_context();
    c->check_hostname = false;
    c->verify_mode = CERT_NONE;
    return c;
}

}  // namespace sd::ssl
