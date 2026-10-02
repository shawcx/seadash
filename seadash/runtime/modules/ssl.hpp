// The `ssl` module: SSLContext for clients and servers (check_hostname, verify_mode,
// load_verify_locations, load_default_certs, load_cert_chain, wrap_socket), SSLSocket,
// create_default_context(), _create_unverified_context(), and the errors. The TLS itself is
// OpenSSL's.
#pragma once

#include <openssl/err.h>
#include <openssl/pem.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include <cstring>
#include <mutex>

#include "pathlib.hpp"
#include "socket.hpp"

namespace sd::ssl {

inline constexpr std::int64_t CERT_NONE = 0, CERT_OPTIONAL = 1, CERT_REQUIRED = 2;
inline constexpr std::int64_t PROTOCOL_TLS_CLIENT = 16, PROTOCOL_TLS_SERVER = 17;
inline constexpr std::int64_t PURPOSE_SERVER_AUTH = 1, PURPOSE_CLIENT_AUTH = 2;  // ssl.Purpose

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
class SSLSocket;

struct SSLContext : std::enable_shared_from_this<SSLContext> {
    bool check_hostname = true;
    std::int64_t verify_mode = CERT_REQUIRED;
    bool server = false;  // (PROTOCOL_TLS_SERVER)

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

    // ctx.wrap_socket(sock, server_side=False, do_handshake_on_connect=True, suppress_ragged_eofs=True,
    // server_hostname=None) (below, after SSLSocket)
    SSLSocket wrap_socket(socket::Socket sock, bool server_side = false, bool do_handshake_on_connect = true,
                          bool suppress_ragged_eofs = true, std::optional<std::string> server_hostname = std::nullopt);

    // ctx.load_cert_chain(certfile, keyfile=None, password=None): this side's certificate
    void load_cert_chain(const pathlib::Path& certfile, const std::optional<pathlib::Path>& keyfile = std::nullopt,
                         const std::optional<std::string>& password = std::nullopt) {
        for (const std::string& f : {certfile.str(), keyfile ? keyfile->str() : certfile.str()}) {
            if (FILE* fp = std::fopen(f.c_str(), "r")) std::fclose(fp);
            else raise_os(errno, f);
        }
        certfile_ = certfile.str();
        keyfile_ = keyfile ? keyfile->str() : certfile_;
        password_ = password;
        changed();
        native();  // (a bad file is an error now, as in Python)
    }

    // The same settings, sharing nothing: what a connection sent to another thread takes, so
    // the sender changing its context can't race with the thread connecting.
    std::shared_ptr<SSLContext> clone() const {
        auto c = std::make_shared<SSLContext>();
        c->check_hostname = check_hostname;
        c->verify_mode = verify_mode;
        c->server = server;
        c->certfile_ = certfile_;
        c->keyfile_ = keyfile_;
        c->password_ = password_;
        c->default_paths_ = default_paths_;
        c->cafiles_ = cafiles_;
        c->capaths_ = capaths_;
        c->cadata_ = cadata_;
        return c;
    }

    // The OpenSSL context for the current settings.
    SSL_CTX* native() {
        if (ctx_ && built_mode_ == verify_mode && built_version_ == version_) return ctx_;
        if (ctx_) SSL_CTX_free(ctx_);
        ctx_ = SSL_CTX_new(server ? TLS_server_method() : TLS_client_method());
        SSL_CTX_set_min_proto_version(ctx_, TLS1_2_VERSION);
        if (!certfile_.empty()) {
            if (password_) {
                SSL_CTX_set_default_passwd_cb_userdata(ctx_, const_cast<char*>(password_->c_str()));
                SSL_CTX_set_default_passwd_cb(ctx_, [](char* buf, int size, int, void* pw) -> int {
                    auto n = static_cast<int>(std::min<std::size_t>(std::strlen(static_cast<char*>(pw)), static_cast<std::size_t>(size)));
                    std::memcpy(buf, pw, static_cast<std::size_t>(n));
                    return n;
                });
            }
            if (SSL_CTX_use_certificate_chain_file(ctx_, certfile_.c_str()) != 1) failed_reset("[SSL] PEM lib");
            if (SSL_CTX_use_PrivateKey_file(ctx_, keyfile_.c_str(), SSL_FILETYPE_PEM) != 1) failed_reset("[SSL] PEM lib");
            if (SSL_CTX_check_private_key(ctx_) != 1) failed_reset("[SSL: KEY_VALUES_MISMATCH] key values mismatch");
        }
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

    [[noreturn]] void failed_reset(const std::string& msg) {  // (the half-built context isn't kept)
        ERR_clear_error();
        SSL_CTX_free(std::exchange(ctx_, nullptr));
        raise<SSLError>(msg);
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
    std::string certfile_, keyfile_;
    std::optional<std::string> password_;
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
    if (protocol != PROTOCOL_TLS_CLIENT && protocol != PROTOCOL_TLS_SERVER) raise("ValueError", "invalid or unsupported protocol version " + std::to_string(protocol));
    auto c = std::make_shared<SSLContext>();
    if (protocol == PROTOCOL_TLS_SERVER) {  // (a server doesn't ask its clients for certificates)
        c->server = true;
        c->check_hostname = false;
        c->verify_mode = CERT_NONE;
    }
    return c;
}
// ssl.create_default_context(purpose=Purpose.SERVER_AUTH, cafile=None, capath=None, cadata=None):
// for a client, the system's trusted certificates (or those given), host names checked; for a
// server (Purpose.CLIENT_AUTH), a context to load its certificate into.
inline std::shared_ptr<SSLContext> create_default_context(std::int64_t purpose = PURPOSE_SERVER_AUTH,
                                                          std::optional<std::string> cafile = std::nullopt,
                                                          std::optional<std::string> capath = std::nullopt,
                                                          std::optional<std::string> cadata = std::nullopt) {
    auto c = SSLContext_new(purpose == PURPOSE_CLIENT_AUTH ? PROTOCOL_TLS_SERVER : PROTOCOL_TLS_CLIENT);
    if (cafile || capath || cadata) c->load_verify_locations(cafile, capath, cadata);
    else if (c->verify_mode != CERT_NONE) c->load_default_certs();
    return c;
}
// ssl._create_unverified_context(): encryption without checking who's at the other end.
inline std::shared_ptr<SSLContext> create_unverified_context() {
    auto c = create_default_context();
    c->check_hostname = false;
    c->verify_mode = CERT_NONE;
    return c;
}

// A client's TLS over a connected socket: SNI, the certificate checked (and its names, with
// check_hostname). The SSL is freed, and the error raised, if the handshake fails.
inline SSL* connect_tls(int fd, SSLContext& ctx, const std::optional<std::string>& host) {
    if (ctx.check_hostname && !host) raise("ValueError", "check_hostname requires server_hostname");
    SSL* ssl = SSL_new(ctx.native());
    SSL_set_fd(ssl, fd);
    if (host) {
        SSL_set_tlsext_host_name(ssl, host->c_str());  // SNI
        if (ctx.check_hostname) {
#if OPENSSL_VERSION_MAJOR >= 4  // (SSL_set1_host is deprecated there)
            in6_addr addr;
            bool ip = ::inet_pton(AF_INET, host->c_str(), &addr) == 1 || ::inet_pton(AF_INET6, host->c_str(), &addr) == 1;
            (void)(ip ? SSL_set1_ipaddr(ssl, host->c_str()) : SSL_set1_dnsname(ssl, host->c_str()));
#else
            (void)SSL_set1_host(ssl, host->c_str());
#endif
        }
    }
    if (SSL_connect(ssl) != 1) {
        long verify = SSL_get_verify_result(ssl);
        SSL_free(ssl);
        if (verify != X509_V_OK && ctx.verify_mode != CERT_NONE) {
            ERR_clear_error();
            std::string why = verify == X509_V_ERR_HOSTNAME_MISMATCH
                                  ? "Hostname mismatch, certificate is not valid for " + repr_str(host.value_or("")) + "."
                                  : X509_verify_cert_error_string(verify);
            raise<SSLCertVerificationError>("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: " + why);
        }
        SSLContext::failed("handshake failed");
    }
    return ssl;
}
// A server's TLS over an accepted connection.
inline SSL* accept_tls(int fd, SSLContext& ctx) {
    SSL* ssl = SSL_new(ctx.native());
    SSL_set_fd(ssl, fd);
    if (SSL_accept(ssl) != 1) {
        SSL_free(ssl);
        SSLContext::failed("handshake failed");
    }
    return ssl;
}

// ctx.wrap_socket(sock, server_side=False, server_hostname=None): a socket whose data goes
// through TLS. A listening socket's accept() gives each connection's, after the handshake; a
// connected one's handshake happens here (do_handshake_on_connect, as in Python).
class SSLSocket {
    struct State {
        socket::Socket sock;
        std::shared_ptr<SSLContext> ctx;
        SSL* ssl = nullptr;
        bool server_side = false;
        std::optional<std::string> server_hostname;
        std::mutex mu;  // (OpenSSL can't use one connection from two threads at once: they take turns)
        ~State() {
            if (ssl) {
                SSL_shutdown(ssl);
                SSL_free(ssl);
            }
        }
    };
    std::shared_ptr<State> s_;
    SSL* need_ssl() const {
        if (!s_->ssl) raise("OSError", "[Errno 57] Socket is not connected");
        return s_->ssl;
    }

public:
    SSLSocket() = default;
    SSLSocket(socket::Socket sock, std::shared_ptr<SSLContext> ctx, bool server_side, std::optional<std::string> host,
              bool handshake = true)
        : s_(std::make_shared<State>()) {
        s_->sock = std::move(sock);
        s_->ctx = std::move(ctx);
        s_->server_side = server_side;
        s_->server_hostname = std::move(host);
        if (!handshake) return;
        sockaddr_storage peer{};
        socklen_t len = sizeof peer;
        bool connected = ::getpeername(static_cast<int>(s_->sock.fileno()), reinterpret_cast<sockaddr*>(&peer), &len) == 0;
        if (!connected) return;  // (a listening socket, or one to connect later)
        int fd = static_cast<int>(s_->sock.fileno());
        s_->ssl = server_side ? accept_tls(fd, *s_->ctx) : connect_tls(fd, *s_->ctx, s_->server_hostname);
    }

    std::shared_ptr<SSLContext> context() const { return s_->ctx; }
    const socket::Socket& raw_socket() const { return s_->sock; }
    bool server_side() const { return s_->server_side; }
    std::optional<std::string> server_hostname() const { return s_->server_hostname; }

    std::tuple<SSLSocket, std::tuple<std::string, std::int64_t>> accept() const {
        auto [conn, addr] = s_->sock.accept();
        return {SSLSocket(std::move(conn), s_->ctx, true, std::nullopt), addr};
    }
    void connect(const std::tuple<std::string, std::int64_t>& address) const {
        std::lock_guard lk(s_->mu);
        s_->sock.connect(address);
        s_->ssl = connect_tls(static_cast<int>(s_->sock.fileno()), *s_->ctx, s_->server_hostname);
    }
    template <class B>
    std::int64_t send(const B& data) const {
        const std::string& d = raw(data);
        if (d.empty()) return 0;
        std::lock_guard lk(s_->mu);
        int n = SSL_write(need_ssl(), d.data(), static_cast<int>(std::min<std::size_t>(d.size(), 1u << 30)));
        if (n <= 0) SSLContext::failed("write failed");
        return n;
    }
    template <class B>
    void sendall(const B& data) const {
        const std::string& d = raw(data);
        for (std::size_t sent = 0; sent < d.size();) sent += static_cast<std::size_t>(send(bytes(d.substr(sent))));
    }
    bytes recv(std::int64_t bufsize) const {
        std::string buf(static_cast<std::size_t>(std::max<std::int64_t>(bufsize, 0)), '\0');
        if (buf.empty()) return bytes();
        std::lock_guard lk(s_->mu);
        int n = SSL_read(need_ssl(), buf.data(), static_cast<int>(buf.size()));
        if (n > 0) {
            buf.resize(static_cast<std::size_t>(n));
            return bytes(buf);
        }
        int err = SSL_get_error(s_->ssl, n);
        if (err == SSL_ERROR_ZERO_RETURN || (err == SSL_ERROR_SYSCALL && errno == 0)) return bytes();  // (the end)
        if (err == SSL_ERROR_SYSCALL && (errno == EAGAIN || errno == EWOULDBLOCK)) raise<TimeoutError>("The read operation timed out");
        SSLContext::failed("read failed");
    }
    std::optional<std::string> version() const {
        std::lock_guard lk(s_->mu);
        if (!s_->ssl) return std::nullopt;
        return std::string(SSL_get_version(s_->ssl));
    }
    std::optional<std::tuple<std::string, std::string, std::int64_t>> cipher() const {
        std::lock_guard lk(s_->mu);
        if (!s_->ssl) return std::nullopt;
        const SSL_CIPHER* c = SSL_get_current_cipher(s_->ssl);
        if (!c) return std::nullopt;
        return std::tuple<std::string, std::string, std::int64_t>{SSL_CIPHER_get_name(c), SSL_CIPHER_get_version(c),
                                                                  SSL_CIPHER_get_bits(c, nullptr)};
    }
    void close() const {
        std::lock_guard lk(s_->mu);
        if (s_->ssl) {
            SSL_shutdown(s_->ssl);
            SSL_free(std::exchange(s_->ssl, nullptr));
        }
        s_->sock.close();
    }
    std::int64_t fileno() const { return s_->sock.fileno(); }
    std::tuple<std::string, std::int64_t> getpeername() const { return s_->sock.getpeername(); }
    std::tuple<std::string, std::int64_t> getsockname() const { return s_->sock.getsockname(); }
    void settimeout(std::optional<double> t) const { s_->sock.settimeout(t); }
    std::string sd_repr() const {
        return "<ssl.SSLSocket fd=" + std::to_string(s_->sock.fileno()) + (s_->server_side ? " server_side" : "") + ">";
    }
};

inline SSLSocket SSLContext::wrap_socket(socket::Socket sock, bool server_side, bool do_handshake_on_connect,
                                         bool suppress_ragged_eofs, std::optional<std::string> server_hostname) {
    (void)suppress_ragged_eofs;
    auto ctx = shared_from_this();
    if (server_side && !ctx->server) raise("ValueError", "Cannot create a server socket with a PROTOCOL_TLS_CLIENT context");
    if (!server_side && ctx->server) raise("ValueError", "Cannot create a client socket with a PROTOCOL_TLS_SERVER context");
    if (server_side && server_hostname) raise("ValueError", "server_hostname can only be specified in client mode");
    if (!server_side && ctx->check_hostname && !server_hostname) raise("ValueError", "check_hostname requires server_hostname");
    return SSLSocket(std::move(sock), ctx, server_side, std::move(server_hostname), do_handshake_on_connect);
}

}  // namespace sd::ssl
