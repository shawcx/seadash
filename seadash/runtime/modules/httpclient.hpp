// The `http.client` module: HTTPConnection / HTTPSConnection (HTTP/1.1, keep-alive, the
// connection reopened when the server closed it) and HTTPResponse, whose body is read as
// it's asked for (Content-Length, chunked, or up to the connection's end). urllib.request
// is built on it. TLS comes from the `ssl` module's contexts (OpenSSL).
#pragma once

#include "socket.hpp"
#include "ssl.hpp"

namespace sd::httpclient {

inline std::string ascii_lower(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// ---- errors ------------------------------------------------------------------------

struct HTTPException : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "http.client.HTTPException"; }
};
#define SD_HTTP_ERROR(Name, Base)                                                     \
    struct Name : Base {                                                                \
        using Base::Base;                                                               \
        std::string sd_type() const override { return "http.client." #Name; }           \
    };
SD_HTTP_ERROR(NotConnected, HTTPException)
SD_HTTP_ERROR(InvalidURL, HTTPException)
SD_HTTP_ERROR(ImproperConnectionState, HTTPException)
SD_HTTP_ERROR(CannotSendRequest, ImproperConnectionState)
SD_HTTP_ERROR(CannotSendHeader, ImproperConnectionState)
SD_HTTP_ERROR(ResponseNotReady, ImproperConnectionState)
SD_HTTP_ERROR(BadStatusLine, HTTPException)
SD_HTTP_ERROR(LineTooLong, HTTPException)
#undef SD_HTTP_ERROR
// (Python's RemoteDisconnected is also a BadStatusLine; here it's a ConnectionResetError.)
struct RemoteDisconnected : ConnectionResetError {
    using ConnectionResetError::ConnectionResetError;
    std::string sd_type() const override { return "http.client.RemoteDisconnected"; }
};
struct IncompleteRead : HTTPException {
    bytes partial;
    std::optional<std::int64_t> expected;
    IncompleteRead(bytes got, std::optional<std::int64_t> more)
        : HTTPException(describe(got, more)), partial(std::move(got)), expected(more) {}
    static std::string describe(const bytes& got, std::optional<std::int64_t> more) {
        std::string e = more ? ", " + std::to_string(*more) + " more expected" : "";
        return "IncompleteRead(" + std::to_string(got.size()) + " bytes read" + e + ")";
    }
    std::string sd_type() const override { return "http.client.IncompleteRead"; }
    std::string sd_repr() const override { return message; }
};

// ---- headers: case-insensitive, in order, repeats kept (email.message.Message) --------

class HTTPMessage {
    std::vector<std::pair<std::string, std::string>> items_;

public:
    void add(std::string k, std::string v) { items_.emplace_back(std::move(k), std::move(v)); }
    std::optional<std::string> get(const std::string& name, std::optional<std::string> fallback = std::nullopt) const {
        for (const auto& [k, v] : items_)
            if (ascii_lower(k) == ascii_lower(name)) return v;
        return fallback;
    }
    std::optional<list<std::string>> get_all(const std::string& name) const {
        list<std::string> out;
        for (const auto& [k, v] : items_)
            if (ascii_lower(k) == ascii_lower(name)) out.push_back(v);
        if (out.empty()) return std::nullopt;
        return out;
    }
    bool has(const std::string& name) const { return get(name).has_value(); }
    list<std::tuple<std::string, std::string>> items() const {
        list<std::tuple<std::string, std::string>> out;
        for (const auto& [k, v] : items_) out.emplace_back(k, v);
        return out;
    }
    list<std::string> keys() const {
        list<std::string> out;
        for (const auto& [k, v] : items_) out.push_back(k);
        return out;
    }
    list<std::string> values() const {
        list<std::string> out;
        for (const auto& [k, v] : items_) out.push_back(v);
        return out;
    }
    std::string get_content_type() const {
        std::string t = ascii_lower(get("Content-Type").value_or("text/plain"));
        return t.substr(0, t.find(';'));
    }
    std::optional<std::string> get_content_charset() const {
        std::string t = get("Content-Type").value_or("");
        auto at = ascii_lower(t).find("charset=");
        if (at == std::string::npos) return std::nullopt;
        std::string cs = t.substr(at + 8);
        cs = cs.substr(0, cs.find(';'));
        if (!cs.empty() && cs.front() == '"') cs = cs.substr(1, cs.size() - 2);
        return ascii_lower(cs);
    }
    std::size_t size() const { return items_.size(); }
    auto begin() const { return keys_cache().begin(); }
    auto end() const { return keys_cache().end(); }
    const list<std::string>& keys_cache() const {
        cache_ = keys();
        return cache_;
    }
    std::string sd_str() const {
        std::string out;
        for (const auto& [k, v] : items_) out += k + ": " + v + "\n";
        return out + "\n";
    }
    std::string sd_repr() const { return "<http.client.HTTPMessage object>"; }

private:
    mutable list<std::string> cache_;
};

// ---- the transport: TCP, or TLS on top of it, with a read buffer -----------------------

class Stream {
    socket::Socket sock_;
    SSL* ssl_ = nullptr;
    std::string buf_;
    std::size_t pos_ = 0;

    std::string recv_some() {  // "" at the end
        if (!ssl_) return sock_.recv(65536).data;
        char chunk[65536];
        int n = SSL_read(ssl_, chunk, sizeof chunk);
        if (n > 0) return std::string(chunk, static_cast<std::size_t>(n));
        int err = SSL_get_error(ssl_, n);
        if (err == SSL_ERROR_ZERO_RETURN || err == SSL_ERROR_SYSCALL) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) raise<TimeoutError>("The read operation timed out");
            return "";
        }
        ssl::SSLContext::failed("read failed");
    }
    bool fill() {
        std::string more = recv_some();
        if (more.empty()) return false;
        if (pos_ > 0) {
            buf_.erase(0, pos_);
            pos_ = 0;
        }
        buf_ += more;
        return true;
    }

public:
    Stream(const std::string& host, std::int64_t port, std::optional<double> timeout,
           const std::shared_ptr<ssl::SSLContext>& tls) {
        sock_ = socket::create_connection({host, port}, timeout);
        if (timeout) {  // (TLS reads the socket directly, so the timeout goes on the socket)
            timeval tv{static_cast<time_t>(*timeout),
                       static_cast<suseconds_t>((*timeout - static_cast<std::int64_t>(*timeout)) * 1e6)};
            ::setsockopt(static_cast<int>(sock_.fileno()), SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
            ::setsockopt(static_cast<int>(sock_.fileno()), SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        }
        if (!tls) return;
        ssl_ = SSL_new(tls->native());
        SSL_set_fd(ssl_, static_cast<int>(sock_.fileno()));
        SSL_set_tlsext_host_name(ssl_, host.c_str());  // SNI
        if (tls->check_hostname) SSL_set1_host(ssl_, host.c_str());  // the certificate must be for this host
        if (SSL_connect(ssl_) != 1) {  // (the destructor won't run: free what we have first)
            long verify = SSL_get_verify_result(ssl_);
            SSL_free(std::exchange(ssl_, nullptr));
            sock_.close();
            if (verify != X509_V_OK && tls->verify_mode != ssl::CERT_NONE) {
                ERR_clear_error();
                std::string why = verify == X509_V_ERR_HOSTNAME_MISMATCH
                                      ? "Hostname mismatch, certificate is not valid for " + repr_str(host) + "."
                                      : X509_verify_cert_error_string(verify);
                raise<ssl::SSLCertVerificationError>("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: " + why);
            }
            ssl::SSLContext::failed("handshake failed");
        }
    }
    Stream(const Stream&) = delete;
    ~Stream() {
        if (ssl_) {
            SSL_shutdown(ssl_);
            SSL_free(ssl_);
        }
        sock_.close();
    }
    void send_all(const std::string& data) {
        if (!ssl_) {
            sock_.sendall(bytes(data));
            return;
        }
        for (std::size_t sent = 0; sent < data.size();) {
            int n = SSL_write(ssl_, data.data() + sent, static_cast<int>(data.size() - sent));
            if (n <= 0) ssl::SSLContext::failed("write failed");
            sent += static_cast<std::size_t>(n);
        }
    }
    // A line, with its "\n" (without it at the end of the stream; "" at the end).
    std::string read_line(std::size_t limit = 65536) {
        while (true) {
            std::size_t nl = buf_.find('\n', pos_);
            if (nl != std::string::npos) {
                std::string line = buf_.substr(pos_, nl + 1 - pos_);
                pos_ = nl + 1;
                return line;
            }
            if (buf_.size() - pos_ > limit) raise<LineTooLong>("got more than " + std::to_string(limit) + " bytes when reading header line");
            if (!fill()) {
                std::string rest = buf_.substr(pos_);
                pos_ = buf_.size();
                return rest;
            }
        }
    }
    // Up to n bytes (at least one, unless the stream has ended).
    std::string read_upto(std::size_t n) {
        if (pos_ == buf_.size() && !fill()) return "";
        std::size_t k = std::min(n, buf_.size() - pos_);
        std::string out = buf_.substr(pos_, k);
        pos_ += k;
        return out;
    }
};

// ---- responses ---------------------------------------------------------------------

struct ResponseState {
    std::shared_ptr<Stream> stream;  // until the body has been read (or the response closed)
    std::int64_t status = 0, version = 11;
    std::string reason, url;
    HTTPMessage headers;
    enum class Body { none, length, chunked, until_close } body = Body::none;
    std::int64_t remaining = 0;   // (length: bytes left; chunked: bytes left in this chunk, -1 = none yet)
    std::string pending;          // read from the body but not yet handed out (readline)
    bool closed = false;          // close() was called
    bool done = false;            // the body has been read: nothing more will come
};

class HTTPResponse {
    std::shared_ptr<ResponseState> s_;

    void finish() {
        s_->done = true;
        s_->stream.reset();  // (the connection keeps it for the next request, unless it's closing)
    }
    // Some of the body (up to n bytes; "" at its end), following its framing.
    std::string body_some(std::size_t n) {
        ResponseState& s = *s_;
        if (!s.pending.empty()) {
            std::string out = s.pending.substr(0, n);
            s.pending.erase(0, out.size());
            return out;
        }
        if (s.done || !s.stream) return "";
        switch (s.body) {
            case ResponseState::Body::none:
                finish();
                return "";
            case ResponseState::Body::length: {
                if (s.remaining <= 0) {
                    finish();
                    return "";
                }
                std::string got = s.stream->read_upto(std::min<std::size_t>(n, static_cast<std::size_t>(s.remaining)));
                if (got.empty()) {
                    std::int64_t missing = s.remaining;
                    finish();
                    throw Thrown{std::make_shared<IncompleteRead>(bytes(), missing)};
                }
                s.remaining -= static_cast<std::int64_t>(got.size());
                if (s.remaining == 0) finish();
                return got;
            }
            case ResponseState::Body::chunked: {
                if (s.remaining <= 0) {
                    if (s.remaining == 0) s.stream->read_line();  // the CRLF after the previous chunk
                    std::string line = s.stream->read_line();
                    if (line.empty()) {
                        finish();
                        throw Thrown{std::make_shared<IncompleteRead>(bytes(), std::nullopt)};
                    }
                    std::size_t size = std::stoul(line.substr(0, line.find_first_of(";\r\n")), nullptr, 16);
                    if (size == 0) {
                        while (true) {  // trailers, up to the blank line
                            std::string t = s.stream->read_line();
                            if (t.empty() || t == "\r\n" || t == "\n") break;
                        }
                        finish();
                        return "";
                    }
                    s.remaining = static_cast<std::int64_t>(size);
                }
                std::string got = s.stream->read_upto(std::min<std::size_t>(n, static_cast<std::size_t>(s.remaining)));
                if (got.empty()) {
                    finish();
                    throw Thrown{std::make_shared<IncompleteRead>(bytes(), std::nullopt)};
                }
                s.remaining -= static_cast<std::int64_t>(got.size());
                return got;
            }
            case ResponseState::Body::until_close: {
                std::string got = s.stream->read_upto(n);
                if (got.empty()) finish();
                return got;
            }
        }
        return "";
    }

public:
    HTTPResponse() : s_(std::make_shared<ResponseState>()) {}
    explicit HTTPResponse(std::shared_ptr<ResponseState> s) : s_(std::move(s)) {}
    const std::shared_ptr<ResponseState>& state() const { return s_; }

    // read() or read(amt): the rest of the body, or up to amt bytes of it.
    bytes read(std::optional<std::int64_t> amt = std::nullopt) {
        std::string out;
        bool all = !amt || *amt < 0;
        std::size_t want = all ? SIZE_MAX : static_cast<std::size_t>(*amt);
        try {
            while (out.size() < want) {
                std::string got = body_some(std::min<std::size_t>(want - out.size(), 65536));
                if (got.empty()) break;
                out += got;
                if (!all && s_->body == ResponseState::Body::until_close) break;  // (as much as has arrived)
            }
        } catch (const Thrown& t) {
            if (auto* inc = dynamic_cast<IncompleteRead*>(t.exc.get())) {  // (what arrived, and what didn't)
                throw Thrown{std::make_shared<IncompleteRead>(bytes(out), inc->expected)};
            }
            throw;
        }
        return bytes(std::move(out));
    }
    bytes readline() {
        std::string line;
        while (true) {
            std::string got = body_some(8192);
            if (got.empty()) break;
            auto nl = got.find('\n');
            if (nl != std::string::npos) {
                line += got.substr(0, nl + 1);
                s_->pending = got.substr(nl + 1) + s_->pending;
                break;
            }
            line += got;
        }
        return bytes(std::move(line));
    }
    list<bytes> readlines() {
        list<bytes> out;
        for (bytes line; !(line = readline()).data.empty();) out.push_back(line);
        return out;
    }
    std::int64_t status() const { return s_->status; }
    std::int64_t code() const { return s_->status; }
    std::int64_t getcode() const { return s_->status; }
    std::int64_t version() const { return s_->version; }
    std::string reason() const { return s_->reason; }
    std::string url() const { return s_->url; }
    std::string geturl() const { return s_->url; }
    HTTPMessage headers() const { return s_->headers; }
    HTTPMessage msg() const { return s_->headers; }
    HTTPMessage info() const { return s_->headers; }
    bool closed() const { return s_->closed; }
    bool isclosed() const { return s_->done || !s_->stream; }
    // A header's value (several of the same name are joined with ", ", like Python).
    std::optional<std::string> getheader(const std::string& name, std::optional<std::string> fallback = std::nullopt) const {
        auto all = s_->headers.get_all(name);
        if (!all) return fallback;
        return str_join(", "s, *all);
    }
    list<std::tuple<std::string, std::string>> getheaders() const { return s_->headers.items(); }
    void close() {
        s_->closed = true;
        s_->pending.clear();
        s_->done = true;
        s_->stream.reset();
    }
    // `for line in response`: reads lines from where the response is, like a file.
    struct LineIterator {
        std::shared_ptr<ResponseState> s;
        bytes line;
        bool operator==(std::default_sentinel_t) const { return line.data.empty(); }
        const bytes& operator*() const { return line; }
        LineIterator& operator++() {
            line = HTTPResponse(s).readline();
            return *this;
        }
    };
    LineIterator begin() const { return {s_, HTTPResponse(s_).readline()}; }
    std::default_sentinel_t end() const { return {}; }
    std::string sd_repr() const { return "<http.client.HTTPResponse object>"; }
};

// ---- connections -------------------------------------------------------------------

inline const char* state_name(int state) {
    return state == 0 ? "Idle" : state == 1 ? "Request-started" : "Request-sent";
}

class HTTPConnection {
    struct State {
        std::string host;
        std::int64_t port = 80;
        std::optional<double> timeout;
        std::shared_ptr<ssl::SSLContext> tls;  // (HTTPS)
        std::shared_ptr<Stream> stream;
        std::shared_ptr<ResponseState> response;  // the last one, until it's been read
        int state = 0;                            // Idle, Request-started, Request-sent
        std::string method, buffer;
    };
    std::shared_ptr<State> s_;

    void forget_finished_response() {
        if (s_->response && (s_->response->done || !s_->response->stream)) s_->response.reset();
    }

public:
    HTTPConnection() = default;
    HTTPConnection(const std::string& host, std::optional<std::int64_t> port = std::nullopt,
                   std::optional<double> timeout = std::nullopt, std::shared_ptr<ssl::SSLContext> tls = nullptr)
        : s_(std::make_shared<State>()) {
        s_->tls = std::move(tls);
        std::int64_t default_port = s_->tls ? 443 : 80;
        std::string h = host;
        auto colon = h.rfind(':'), bracket = h.rfind(']');
        if (!port && colon != std::string::npos && (bracket == std::string::npos || colon > bracket)) {
            std::string p = h.substr(colon + 1);
            if (p.empty()) {
                port = default_port;
            } else {
                if (!std::all_of(p.begin(), p.end(), [](char c) { return std::isdigit(static_cast<unsigned char>(c)); }))
                    raise<InvalidURL>("nonnumeric port: '" + p + "'");
                port = std::stoll(p);
            }
            h = h.substr(0, colon);
        }
        if (h.size() > 1 && h.front() == '[' && h.back() == ']') h = h.substr(1, h.size() - 2);
        s_->host = h;
        s_->port = port.value_or(default_port);
        s_->timeout = timeout;
    }

    std::string host() const { return s_->host; }
    std::int64_t port() const { return s_->port; }
    std::optional<double> timeout() const { return s_->timeout; }
    void set_debuglevel(std::int64_t) {}

    void connect() {
        if (!s_->stream) s_->stream = std::make_shared<Stream>(s_->host, s_->port, s_->timeout, s_->tls);
    }
    void close() {
        s_->stream.reset();
        s_->response.reset();
        s_->state = 0;
    }

    // The low-level interface: putrequest, putheader..., endheaders(body).
    void putrequest(const std::string& method, const std::string& url, bool skip_host = false,
                    bool skip_accept_encoding = false) {
        forget_finished_response();
        if (s_->state != 0) raise<CannotSendRequest>(state_name(s_->state));
        for (char c : method)
            if (c <= ' ' || c == 0x7f) raise("ValueError", "method can't contain control characters. " + repr_str(method));
        for (char c : url)
            if (static_cast<unsigned char>(c) <= ' ' || c == 0x7f)
                raise<InvalidURL>("URL can't contain control characters. " + repr_str(url) + " (found at least " +
                                  repr_str(std::string(1, c)) + ")");
        s_->state = 1;
        s_->method = method;
        s_->buffer = method + " " + (url.empty() ? "/" : url) + " HTTP/1.1\r\n";
        if (!skip_host) {
            std::string h = s_->host.find(':') != std::string::npos ? "[" + s_->host + "]" : s_->host;
            bool default_port = s_->port == (s_->tls ? 443 : 80);
            s_->buffer += "Host: " + h + (default_port ? "" : ":" + std::to_string(s_->port)) + "\r\n";
        }
        if (!skip_accept_encoding) s_->buffer += "Accept-Encoding: identity\r\n";
    }
    void putheader(const std::string& header, const std::string& value) {
        if (s_->state != 1) raise<CannotSendHeader>("");
        s_->buffer += header + ": " + value + "\r\n";
    }
    void endheaders(std::optional<bytes> body = std::nullopt) {
        if (s_->state != 1) raise<CannotSendHeader>("");
        s_->buffer += "\r\n";
        if (body) s_->buffer += body->data;
        send(bytes(std::exchange(s_->buffer, std::string())));
        s_->state = 2;
    }
    void send(const bytes& data) {
        connect();
        s_->stream->send_all(data.data);
    }

    // request(method, url, body=None, headers={}): Content-Length is added (0 for a POST,
    // PUT or PATCH without a body), then the headers as given.
    void request(const std::string& method, const std::string& url, std::optional<bytes> body,
                 const dict<std::string, std::string>& headers) {
        bool has_host = false, has_encoding = false, has_length = false;
        for (const auto& [k, v] : headers) {
            std::string l = ascii_lower(k);
            has_host = has_host || l == "host";
            has_encoding = has_encoding || l == "accept-encoding";
            has_length = has_length || l == "content-length" || l == "transfer-encoding";
        }
        putrequest(method, url, has_host, has_encoding);
        if (!has_length) {
            if (body) {
                putheader("Content-Length", std::to_string(body->size()));
            } else {
                std::string m = str_upper(method);
                if (m == "POST" || m == "PUT" || m == "PATCH") putheader("Content-Length", "0");
            }
        }
        for (const auto& [k, v] : headers) putheader(k, v);
        endheaders(std::move(body));
    }

    HTTPResponse getresponse() {
        forget_finished_response();
        if (s_->state != 2 || s_->response) raise<ResponseNotReady>(state_name(s_->state));
        auto r = std::make_shared<ResponseState>();
        Stream& in = *s_->stream;
        std::string status_line;
        while (true) {  // (skipping "100 Continue" and other interim responses)
            status_line = in.read_line();
            if (status_line.empty()) {
                close();
                raise<RemoteDisconnected>("Remote end closed connection without response");
            }
            std::string line = status_line.substr(0, status_line.find_first_of("\r\n"));
            std::size_t sp1 = line.find(' ');
            if (sp1 == std::string::npos || !line.starts_with("HTTP/")) {
                close();
                raise<BadStatusLine>(repr_str(status_line));
            }
            std::string version = line.substr(0, sp1);
            std::size_t sp2 = line.find(' ', sp1 + 1);
            std::string code = line.substr(sp1 + 1, sp2 == std::string::npos ? std::string::npos : sp2 - sp1 - 1);
            if (code.size() != 3 || !std::all_of(code.begin(), code.end(), [](char c) { return std::isdigit(static_cast<unsigned char>(c)); })) {
                close();
                raise<BadStatusLine>(repr_str(status_line));
            }
            r->status = std::stoll(code);
            r->reason = sp2 == std::string::npos ? "" : line.substr(sp2 + 1);
            r->version = version == "HTTP/1.0" ? 10 : 11;
            HTTPMessage h;
            std::string last;
            while (true) {
                std::string hl = in.read_line();
                if (hl.empty() || hl == "\r\n" || hl == "\n") break;
                hl = hl.substr(0, hl.find_last_not_of("\r\n") + 1);
                auto colon = hl.find(':');
                if (colon != std::string::npos) {
                    std::string v = hl.substr(colon + 1);
                    v.erase(0, v.find_first_not_of(" \t"));
                    h.add(hl.substr(0, colon), v);
                }
            }
            r->headers = std::move(h);
            if (r->status >= 100 && r->status < 200 && r->status != 101) continue;  // an interim response
            break;
        }
        std::string conn_header = ascii_lower(r->headers.get("Connection").value_or(""));
        bool will_close = conn_header.find("close") != std::string::npos ||
                          (r->version == 10 && conn_header.find("keep-alive") == std::string::npos);
        std::string te = ascii_lower(r->headers.get("Transfer-Encoding").value_or(""));
        if (s_->method == "HEAD" || r->status == 204 || r->status == 304) {
            r->body = ResponseState::Body::none;
            r->done = true;
        } else if (te.find("chunked") != std::string::npos) {
            r->body = ResponseState::Body::chunked;
            r->remaining = -1;
        } else if (auto length = r->headers.get("Content-Length")) {
            r->body = ResponseState::Body::length;
            r->remaining = std::stoll(*length);
            if (r->remaining == 0) r->done = true;
        } else {
            r->body = ResponseState::Body::until_close;
            will_close = true;
        }
        s_->state = 0;
        if (!r->done) r->stream = s_->stream;
        if (will_close) {
            s_->stream.reset();  // (the response still reads from it; the next request reconnects)
        } else if (!r->done) {
            s_->response = r;
        }
        return HTTPResponse(r);
    }
    std::string sd_repr() const { return s_->tls ? "<http.client.HTTPSConnection object>" : "<http.client.HTTPConnection object>"; }
};

inline HTTPConnection HTTPSConnection(const std::string& host, std::optional<std::int64_t> port = std::nullopt,
                                      std::optional<double> timeout = std::nullopt,
                                      std::optional<std::shared_ptr<ssl::SSLContext>> context = std::nullopt) {
    return HTTPConnection(host, port, timeout, context && *context ? *context : ssl::create_default_context());
}

// A str body is sent as Latin-1, like Python's.
inline bytes latin1_body(const std::string& s) {
    std::string out;
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        std::size_t width = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        std::int64_t cp = utf8_code_points(s.substr(i, width))[0];
        if (cp > 0xFF) {
            std::string ch = s.substr(i, width);
            raise("UnicodeEncodeError",
                  "'latin-1' codec can't encode character " + ascii(repr_str(ch)) + " in position " +
                      std::to_string(code_points(s.substr(0, i))) + ": Body (" + repr_str(ch) +
                      ") is not valid Latin-1. Use body.encode('utf-8') if you want to send it encoded in UTF-8.");
        }
        out += static_cast<char>(cp);
        i += width;
    }
    return bytes(out);
}

// http.client.responses: status code -> reason phrase.
inline const dict<std::int64_t, std::string>& responses() {
    static const dict<std::int64_t, std::string> table = {
        {100, "Continue"}, {101, "Switching Protocols"}, {102, "Processing"}, {103, "Early Hints"}, {200, "OK"}, {201, "Created"}, {202, "Accepted"}, {203, "Non-Authoritative Information"}, {204, "No Content"}, {205, "Reset Content"}, {206, "Partial Content"}, {207, "Multi-Status"}, {208, "Already Reported"}, {226, "IM Used"}, {300, "Multiple Choices"}, {301, "Moved Permanently"}, {302, "Found"}, {303, "See Other"}, {304, "Not Modified"}, {305, "Use Proxy"}, {307, "Temporary Redirect"}, {308, "Permanent Redirect"}, {400, "Bad Request"}, {401, "Unauthorized"}, {402, "Payment Required"}, {403, "Forbidden"}, {404, "Not Found"}, {405, "Method Not Allowed"}, {406, "Not Acceptable"}, {407, "Proxy Authentication Required"}, {408, "Request Timeout"}, {409, "Conflict"}, {410, "Gone"}, {411, "Length Required"}, {412, "Precondition Failed"}, {413, "Request Entity Too Large"}, {414, "Request-URI Too Long"}, {415, "Unsupported Media Type"}, {416, "Requested Range Not Satisfiable"}, {417, "Expectation Failed"}, {418, "I'm a Teapot"}, {421, "Misdirected Request"}, {422, "Unprocessable Entity"}, {423, "Locked"}, {424, "Failed Dependency"}, {425, "Too Early"}, {426, "Upgrade Required"}, {428, "Precondition Required"}, {429, "Too Many Requests"}, {431, "Request Header Fields Too Large"}, {451, "Unavailable For Legal Reasons"}, {500, "Internal Server Error"}, {501, "Not Implemented"}, {502, "Bad Gateway"}, {503, "Service Unavailable"}, {504, "Gateway Timeout"}, {505, "HTTP Version Not Supported"}, {506, "Variant Also Negotiates"}, {507, "Insufficient Storage"}, {508, "Loop Detected"}, {510, "Not Extended"}, {511, "Network Authentication Required"},
    };
    return table;
}

}  // namespace sd::httpclient

namespace sd {
// `"Content-Type" in response.headers`, `response.headers["Content-Type"]` (None if missing)
inline bool contains(const httpclient::HTTPMessage& h, const std::string& name) { return h.has(name); }
inline std::optional<std::string> index(const httpclient::HTTPMessage& h, const std::string& name) { return h.get(name); }
}  // namespace sd
