// The `urllib` package: urllib.parse (ports of CPython's functions) and urllib.request,
// an HTTP/1.1 client with HTTPS (OpenSSL, certificates and host names verified), redirects,
// chunked bodies and urllib.error's exceptions.
#pragma once

#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include "socket.hpp"

namespace sd::urlparse {

inline std::string lower(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// urlparse()/urlsplit()'s result (ParseResult / SplitResult).
struct Parts {
    std::string scheme_, netloc_, path_, params_, query_, fragment_;
    bool has_params = true;  // ParseResult (vs SplitResult)

    std::string scheme() const { return scheme_; }
    std::string netloc() const { return netloc_; }
    std::string path() const { return path_; }
    std::string params() const { return params_; }
    std::string query() const { return query_; }
    std::string fragment() const { return fragment_; }

    std::string userinfo() const {
        auto at = netloc_.rfind('@');
        return at == std::string::npos ? "" : netloc_.substr(0, at);
    }
    std::string hostport() const {
        auto at = netloc_.rfind('@');
        return at == std::string::npos ? netloc_ : netloc_.substr(at + 1);
    }
    std::optional<std::string> username() const {
        if (netloc_.find('@') == std::string::npos) return std::nullopt;
        std::string u = userinfo();
        return u.substr(0, u.find(':'));
    }
    std::optional<std::string> password() const {
        std::string u = userinfo();
        auto colon = u.find(':');
        if (netloc_.find('@') == std::string::npos || colon == std::string::npos) return std::nullopt;
        return u.substr(colon + 1);
    }
    std::optional<std::string> hostname() const {
        std::string hp = hostport(), host;
        if (!hp.empty() && hp[0] == '[') {
            host = hp.substr(1, hp.find(']') - 1);
        } else {
            host = hp.substr(0, hp.find(':'));
        }
        if (host.empty()) return std::nullopt;
        return lower(host);
    }
    std::optional<std::int64_t> port() const {
        std::string hp = hostport();
        std::size_t start = !hp.empty() && hp[0] == '[' ? hp.find(']') : 0;
        auto colon = hp.find(':', start == std::string::npos ? 0 : start);
        if (colon == std::string::npos) return std::nullopt;
        std::string digits = hp.substr(colon + 1);
        if (digits.empty()) return std::nullopt;
        if (!std::all_of(digits.begin(), digits.end(), [](char c) { return std::isdigit(static_cast<unsigned char>(c)); }))
            raise("ValueError", "Port could not be cast to integer value as " + repr_str(digits));
        std::int64_t p = std::stoll(digits);
        if (p > 65535) raise("ValueError", "Port out of range 0-65535");
        return p;
    }
    std::string geturl() const {
        std::string url = path_;
        if (has_params && !params_.empty()) url += ";" + params_;
        static const std::set<std::string> netloc_schemes = {"", "ftp", "http", "gopher", "nntp", "telnet", "imap", "wais",
                                                             "file", "mms", "https", "shttp", "snews", "prospero", "rtsp",
                                                             "rtsps", "rtspu", "rsync", "svn", "svn+ssh", "sftp", "nfs",
                                                             "git", "git+ssh", "ws", "wss", "itms-services"};
        if (!netloc_.empty() || (!scheme_.empty() && netloc_schemes.count(scheme_) && url.rfind("//", 0) != 0)) {
            if (!url.empty() && url[0] != '/') url = "/" + url;
            url = "//" + netloc_ + url;
        }
        if (!scheme_.empty()) url = scheme_ + ":" + url;
        if (!query_.empty()) url += "?" + query_;
        if (!fragment_.empty()) url += "#" + fragment_;
        return url;
    }
    std::string sd_repr() const {
        std::string out = std::string(has_params ? "ParseResult" : "SplitResult") + "(scheme=" + repr_str(scheme_) +
                          ", netloc=" + repr_str(netloc_) + ", path=" + repr_str(path_);
        if (has_params) out += ", params=" + repr_str(params_);
        return out + ", query=" + repr_str(query_) + ", fragment=" + repr_str(fragment_) + ")";
    }
    bool operator==(const Parts&) const = default;
};

inline Parts urlsplit(std::string url, const std::string& scheme = "", bool allow_fragments = true) {
    Parts p;
    p.has_params = false;
    p.scheme_ = scheme;
    auto colon = url.find(':');
    if (colon != std::string::npos && colon > 0 && std::isalpha(static_cast<unsigned char>(url[0])) &&
        std::all_of(url.begin(), url.begin() + colon, [](char c) {
            return std::isalnum(static_cast<unsigned char>(c)) || c == '+' || c == '-' || c == '.';
        })) {
        p.scheme_ = lower(url.substr(0, colon));
        url = url.substr(colon + 1);
    }
    if (url.rfind("//", 0) == 0) {
        auto end = url.find_first_of("/?#", 2);
        p.netloc_ = url.substr(2, end == std::string::npos ? std::string::npos : end - 2);
        url = end == std::string::npos ? "" : url.substr(end);
    }
    if (allow_fragments) {
        if (auto hash = url.find('#'); hash != std::string::npos) {
            p.fragment_ = url.substr(hash + 1);
            url = url.substr(0, hash);
        }
    }
    if (auto q = url.find('?'); q != std::string::npos) {
        p.query_ = url.substr(q + 1);
        url = url.substr(0, q);
    }
    p.path_ = url;
    return p;
}

inline Parts urlparse(const std::string& url, const std::string& scheme = "", bool allow_fragments = true) {
    Parts p = urlsplit(url, scheme, allow_fragments);
    p.has_params = true;
    std::size_t slash = p.path_.rfind('/');
    std::size_t semi = p.path_.find(';', slash == std::string::npos ? 0 : slash);
    if (semi != std::string::npos) {
        p.params_ = p.path_.substr(semi + 1);
        p.path_ = p.path_.substr(0, semi);
    }
    return p;
}

inline std::string quote(const std::string& s, const std::string& safe = "/") {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : s) {
        if (std::isalnum(c) || std::string("_.-~").find(static_cast<char>(c)) != std::string::npos ||
            (c < 0x80 && safe.find(static_cast<char>(c)) != std::string::npos)) {
            out += static_cast<char>(c);
        } else {
            out += '%';
            out += hex[c >> 4];
            out += hex[c & 15];
        }
    }
    return out;
}
inline std::string quote_plus(const std::string& s, const std::string& safe = "") {
    if (s.find(' ') == std::string::npos) return quote(s, safe);
    std::string q = quote(s, safe + " ");
    std::replace(q.begin(), q.end(), ' ', '+');
    return q;
}
inline std::string unquote(const std::string& s) {
    std::string out;
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '%' && i + 2 < s.size() && std::isxdigit(static_cast<unsigned char>(s[i + 1])) &&
            std::isxdigit(static_cast<unsigned char>(s[i + 2]))) {
            out += static_cast<char>(std::stoi(s.substr(i + 1, 2), nullptr, 16));
            i += 2;
        } else {
            out += s[i];
        }
    }
    return out;
}
inline std::string unquote_plus(std::string s) {
    std::replace(s.begin(), s.end(), '+', ' ');
    return unquote(s);
}

// urlencode(): pairs from a dict or a list of (key, value); with doseq, list values
// become repeated keys.
template <class V>
void encode_value(std::string& out, const std::string& key, const V& value, bool doseq) {
    if constexpr (is_vector<V>::value) {
        if (doseq) {
            for (const auto& v : value) {
                out += (out.empty() ? "" : "&") + quote_plus(key) + "=" + quote_plus(str(v));
            }
            return;
        }
    }
    out += (out.empty() ? "" : "&") + quote_plus(key) + "=" + quote_plus(str(value));
}
template <class Q>
std::string urlencode(const Q& query, bool doseq = false) {
    std::string out;
    if constexpr (is_dict_like<Q>::value) {
        for (const auto& [k, v] : query) encode_value(out, str(k), v, doseq);
    } else {
        for (const auto& pair : query) encode_value(out, str(std::get<0>(pair)), std::get<1>(pair), doseq);
    }
    return out;
}

inline std::string urljoin(const std::string& base, const std::string& url, bool allow_fragments = true) {
    if (base.empty()) return url;
    if (url.empty()) return base;
    static const std::set<std::string> relative = {"", "ftp", "http", "gopher", "nntp", "imap", "wais", "file",
                                                   "https", "shttp", "mms", "prospero", "rtsp", "rtsps", "rtspu",
                                                   "sftp", "svn", "svn+ssh", "ws", "wss"};
    Parts b = urlparse(base, "", allow_fragments);
    Parts u = urlparse(url, b.scheme_, allow_fragments);
    if (u.scheme_ != b.scheme_ || !relative.count(u.scheme_)) return url;
    if (!u.netloc_.empty()) return u.geturl();
    u.netloc_ = b.netloc_;
    if (u.path_.empty() && u.params_.empty()) {
        u.path_ = b.path_;
        u.params_ = b.params_;
        if (u.query_.empty()) u.query_ = b.query_;
        return u.geturl();
    }
    auto split = [](const std::string& s) {
        list<std::string> parts;
        std::size_t start = 0;
        for (std::size_t i; (i = s.find('/', start)) != std::string::npos; start = i + 1) parts.push_back(s.substr(start, i - start));
        parts.push_back(s.substr(start));
        return parts;
    };
    list<std::string> base_parts = split(b.path_);
    if (!base_parts.back().empty()) base_parts.pop_back();
    list<std::string> segments;
    if (!u.path_.empty() && u.path_[0] == '/') {
        segments = split(u.path_);
    } else {
        segments = base_parts;
        for (auto& s : split(u.path_)) segments.push_back(s);
        if (segments.size() > 2) {  // drop empty segments between the first and last
            list<std::string> kept{segments.front()};
            for (std::size_t i = 1; i + 1 < segments.size(); ++i)
                if (!segments[i].empty()) kept.push_back(segments[i]);
            kept.push_back(segments.back());
            segments = kept;
        }
    }
    list<std::string> resolved;
    for (const auto& seg : segments) {
        if (seg == "..") {
            if (!resolved.empty()) resolved.pop_back();
        } else if (seg != ".") {
            resolved.push_back(seg);
        }
    }
    if (!segments.empty() && (segments.back() == "." || segments.back() == "..")) resolved.push_back("");
    std::string path;
    for (std::size_t i = 0; i < resolved.size(); ++i) path += (i ? "/" : "") + resolved[i];
    u.path_ = path.empty() ? "/" : path;
    return u.geturl();
}

inline list<std::tuple<std::string, std::string>> parse_qsl(const std::string& qs, bool keep_blank_values = false,
                                                            bool strict_parsing = false) {
    list<std::tuple<std::string, std::string>> out;
    std::size_t start = 0;
    while (start <= qs.size()) {
        std::size_t end = qs.find('&', start);
        std::string nv = qs.substr(start, end == std::string::npos ? std::string::npos : end - start);
        start = end == std::string::npos ? qs.size() + 1 : end + 1;
        if (nv.empty() && !strict_parsing) continue;
        auto eq = nv.find('=');
        std::string name = nv.substr(0, eq), value = eq == std::string::npos ? "" : nv.substr(eq + 1);
        if (eq == std::string::npos) {
            if (strict_parsing) raise("ValueError", "bad query field: " + repr_str(nv));
            if (!keep_blank_values) continue;
        }
        if (!value.empty() || keep_blank_values) out.emplace_back(unquote_plus(name), unquote_plus(value));
    }
    return out;
}
inline dict<std::string, list<std::string>> parse_qs(const std::string& qs, bool keep_blank_values = false,
                                                     bool strict_parsing = false) {
    dict<std::string, list<std::string>> out;
    for (auto& [k, v] : parse_qsl(qs, keep_blank_values, strict_parsing)) out[k].push_back(v);
    return out;
}

}  // namespace sd::urlparse

namespace sd::urlerror {

struct URLError : OSError {
    std::string reason;
    explicit URLError(std::string why) : OSError("<urlopen error " + why + ">"), reason(std::move(why)) {}
    std::string sd_type() const override { return "urllib.error.URLError"; }
    std::string sd_repr() const override { return "URLError(" + repr_str(reason) + ")"; }
};

}  // namespace sd::urlerror

namespace sd::urlrequest {

// A response's (or error's) headers: case-insensitive, in order, repeats kept.
class Headers {
    std::vector<std::pair<std::string, std::string>> items_;

public:
    void add(std::string k, std::string v) { items_.emplace_back(std::move(k), std::move(v)); }
    std::optional<std::string> get(const std::string& name, std::optional<std::string> fallback = std::nullopt) const {
        for (const auto& [k, v] : items_)
            if (urlparse::lower(k) == urlparse::lower(name)) return v;
        return fallback;
    }
    std::optional<list<std::string>> get_all(const std::string& name) const {
        list<std::string> out;
        for (const auto& [k, v] : items_)
            if (urlparse::lower(k) == urlparse::lower(name)) out.push_back(v);
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
        std::string t = urlparse::lower(get("Content-Type").value_or("text/plain"));
        return t.substr(0, t.find(';'));
    }
    std::optional<std::string> get_content_charset() const {
        std::string t = get("Content-Type").value_or("");
        auto at = urlparse::lower(t).find("charset=");
        if (at == std::string::npos) return std::nullopt;
        std::string cs = t.substr(at + 8);
        cs = cs.substr(0, cs.find(';'));
        if (!cs.empty() && cs.front() == '"') cs = cs.substr(1, cs.size() - 2);
        return urlparse::lower(cs);
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

inline std::string capitalized(std::string k) {  // Python's Request normalizes header names: X-A -> X-a
    for (auto& c : k) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (!k.empty()) k[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(k[0])));
    return k;
}

class Request {
public:
    std::string full_url_;
    std::optional<bytes> data_;
    std::vector<std::pair<std::string, std::string>> headers_;
    std::optional<std::string> method_;

    Request() = default;
    template <class H>
    Request(std::string url, std::optional<bytes> data, const H& headers, std::optional<std::string> method)
        : full_url_(std::move(url)), data_(std::move(data)), method_(std::move(method)) {
        for (const auto& [k, v] : headers) add_header(k, v);
    }
    void add_header(const std::string& k, const std::string& v) {
        std::string key = capitalized(k);
        for (auto& [hk, hv] : headers_)
            if (hk == key) {
                hv = v;
                return;
            }
        headers_.emplace_back(key, v);
    }
    bool has_header(const std::string& k) const {
        for (const auto& [hk, hv] : headers_)
            if (hk == capitalized(k)) return true;
        return false;
    }
    std::optional<std::string> get_header(const std::string& k, std::optional<std::string> fallback = std::nullopt) const {
        for (const auto& [hk, hv] : headers_)
            if (hk == capitalized(k)) return hv;
        return fallback;
    }
    std::string get_method() const { return method_ ? *method_ : data_ ? "POST" : "GET"; }
    std::string full_url() const { return full_url_; }
    std::string get_full_url() const { return full_url_; }
    std::optional<bytes> data() const { return data_; }
    std::optional<std::string> method() const { return method_; }
    dict<std::string, std::string> headers() const {
        dict<std::string, std::string> out;
        for (const auto& [k, v] : headers_) out[k] = v;
        return out;
    }
    list<std::tuple<std::string, std::string>> header_items() const {
        list<std::tuple<std::string, std::string>> out;
        for (const auto& [k, v] : headers_) out.emplace_back(k, v);
        return out;
    }
    std::string sd_repr() const { return "<urllib.request.Request object>"; }
};

// ---- the connection: plain TCP, or TLS on top of it --------------------------------------

class Connection {
    socket::Socket sock_;
    SSL* ssl_ = nullptr;

    static SSL_CTX* tls_context() {
        static SSL_CTX* ctx = [] {
            SSL_CTX* c = SSL_CTX_new(TLS_client_method());
            SSL_CTX_set_default_verify_paths(c);
            SSL_CTX_set_verify(c, SSL_VERIFY_PEER, nullptr);
            SSL_CTX_set_min_proto_version(c, TLS1_2_VERSION);
            return c;
        }();
        return ctx;
    }
    [[noreturn]] static void tls_failed(const std::string& what) {
        unsigned long code = ERR_get_error();
        char buf[256] = "unknown error";
        if (code) ERR_error_string_n(code, buf, sizeof buf);
        throw Thrown{std::make_shared<urlerror::URLError>("[SSL] " + what + ": " + buf)};
    }

public:
    Connection(const std::string& host, std::int64_t port, bool tls, std::optional<double> timeout) {
        try {
            sock_ = socket::create_connection({host, port}, timeout);
        } catch (const Thrown& t) {
            std::string msg = t.exc->message;
            if (dynamic_cast<TimeoutError*>(t.exc.get())) msg = "timed out";
            throw Thrown{std::make_shared<urlerror::URLError>(msg)};
        }
        if (timeout) {  // (TLS reads the socket directly, so the timeout goes on the socket)
            timeval tv{static_cast<time_t>(*timeout), static_cast<suseconds_t>((*timeout - static_cast<std::int64_t>(*timeout)) * 1e6)};
            ::setsockopt(static_cast<int>(sock_.fileno()), SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
            ::setsockopt(static_cast<int>(sock_.fileno()), SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        }
        if (!tls) return;
        ssl_ = SSL_new(tls_context());
        SSL_set_fd(ssl_, static_cast<int>(sock_.fileno()));
        SSL_set_tlsext_host_name(ssl_, host.c_str());  // SNI
        SSL_set1_host(ssl_, host.c_str());             // the certificate must be for this host
        if (SSL_connect(ssl_) != 1) {
            long verify = SSL_get_verify_result(ssl_);
            if (verify != X509_V_OK)
                throw Thrown{std::make_shared<urlerror::URLError>(
                    std::string("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: ") +
                    X509_verify_cert_error_string(verify))};
            tls_failed("handshake failed");
        }
    }
    Connection(const Connection&) = delete;
    ~Connection() {
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
            if (n <= 0) tls_failed("write failed");
            sent += static_cast<std::size_t>(n);
        }
    }
    std::string recv_some() {  // "" at the end
        if (!ssl_) return sock_.recv(65536).data;
        char buf[65536];
        int n = SSL_read(ssl_, buf, sizeof buf);
        if (n > 0) return std::string(buf, static_cast<std::size_t>(n));
        int err = SSL_get_error(ssl_, n);
        if (err == SSL_ERROR_ZERO_RETURN || err == SSL_ERROR_SYSCALL) return "";
        if (errno == EAGAIN || errno == EWOULDBLOCK) throw Thrown{std::make_shared<TimeoutError>("The read operation timed out")};
        tls_failed("read failed");
    }
};

// ---- responses --------------------------------------------------------------------

class Response {
    struct State {
        std::int64_t status = 0;
        std::string reason, url, body;
        Headers headers;
        std::size_t pos = 0;
        bool closed = false;
    };
    std::shared_ptr<State> s_;

public:
    Response() : s_(std::make_shared<State>()) {}
    explicit Response(std::shared_ptr<State> s) : s_(std::move(s)) {}
    Response(std::int64_t status, std::string reason, Headers headers, std::string body, std::string url)
        : s_(std::make_shared<State>()) {
        s_->status = status, s_->reason = std::move(reason), s_->headers = std::move(headers);
        s_->body = std::move(body), s_->url = std::move(url);
    }
    bytes read(std::optional<std::int64_t> amt = std::nullopt) {
        std::size_t left = s_->body.size() - s_->pos;
        std::size_t n = amt && *amt >= 0 ? std::min<std::size_t>(left, static_cast<std::size_t>(*amt)) : left;
        std::string out = s_->body.substr(s_->pos, n);
        s_->pos += n;
        return bytes(out);
    }
    bytes readline() {
        std::size_t nl = s_->body.find('\n', s_->pos);
        std::size_t end = nl == std::string::npos ? s_->body.size() : nl + 1;
        std::string out = s_->body.substr(s_->pos, end - s_->pos);
        s_->pos = end;
        return bytes(out);
    }
    list<bytes> readlines() {
        list<bytes> out;
        for (bytes line; !(line = readline()).data.empty();) out.push_back(line);
        return out;
    }
    std::int64_t status() const { return s_->status; }
    std::int64_t code() const { return s_->status; }
    std::int64_t getcode() const { return s_->status; }
    std::string reason() const { return s_->reason; }
    std::string url() const { return s_->url; }
    std::string geturl() const { return s_->url; }
    Headers headers() const { return s_->headers; }
    Headers info() const { return s_->headers; }
    std::optional<std::string> getheader(const std::string& name, std::optional<std::string> fallback = std::nullopt) const {
        return s_->headers.get(name, fallback);
    }
    list<std::tuple<std::string, std::string>> getheaders() const { return s_->headers.items(); }
    void close() { s_->closed = true; }
    // `for line in response`: reads lines from where the response is, like a file.
    struct LineIterator {
        std::shared_ptr<State> s;
        bytes line;
        bool operator==(std::default_sentinel_t) const { return line.data.empty(); }
        const bytes& operator*() const { return line; }
        LineIterator& operator++() {
            line = Response(s).readline();
            return *this;
        }
    };
    LineIterator begin() const { return {s_, Response(s_).readline()}; }
    std::default_sentinel_t end() const { return {}; }
    std::string sd_repr() const { return "<http.client.HTTPResponse object>"; }
};

}  // namespace sd::urlrequest

namespace sd::urlerror {

struct HTTPError : URLError {
    std::int64_t code;
    std::string msg;
    urlrequest::Headers headers;
    urlrequest::Response response;
    std::string url;
    HTTPError(std::int64_t c, std::string reason_, urlrequest::Headers h, urlrequest::Response body, std::string u)
        : URLError(reason_), code(c), msg(reason_), headers(std::move(h)), response(std::move(body)), url(std::move(u)) {
        message = "HTTP Error " + std::to_string(code) + ": " + reason;
    }
    // An HTTPError is also the response (Python's error page body).
    bytes read(std::optional<std::int64_t> amt = std::nullopt) { return response.read(amt); }
    std::int64_t getcode() const { return code; }
    std::string geturl() const { return url; }
    urlrequest::Headers info() const { return headers; }
    std::string sd_type() const override { return "urllib.error.HTTPError"; }
    std::string sd_repr() const override { return "<HTTPError " + std::to_string(code) + ": " + repr_str(reason) + ">"; }
};

}  // namespace sd::urlerror

namespace sd::urlrequest {

// One request/response over a fresh connection ("Connection: close").
inline std::tuple<std::int64_t, std::string, Headers, std::string> exchange(const std::string& method, const std::string& url,
                                                                           const std::vector<std::pair<std::string, std::string>>& headers,
                                                                           const std::optional<bytes>& data,
                                                                           std::optional<double> timeout) {
    urlparse::Parts p = urlparse::urlsplit(url);
    if (p.scheme_ != "http" && p.scheme_ != "https")
        throw Thrown{std::make_shared<urlerror::URLError>("unknown url type: " + (p.scheme_.empty() ? repr_str(url) : p.scheme_))};
    auto host = p.hostname();
    if (!host) throw Thrown{std::make_shared<urlerror::URLError>("no host given")};
    bool tls = p.scheme_ == "https";
    std::int64_t port = p.port().value_or(tls ? 443 : 80);
    std::string target = (p.path_.empty() ? "/" : p.path_) + (p.query_.empty() ? "" : "?" + p.query_);
    std::string host_header = p.hostport();
    std::string req = method + " " + target + " HTTP/1.1\r\n";
    auto has = [&](const std::string& k) {
        for (const auto& [hk, hv] : headers)
            if (urlparse::lower(hk) == urlparse::lower(k)) return true;
        return false;
    };
    if (!has("Host")) req += "Host: " + host_header + "\r\n";
    if (!has("User-agent")) req += "User-Agent: Python-urllib/3.12\r\n";
    req += "Accept-Encoding: identity\r\n";
    if (data) {
        if (!has("Content-type")) req += "Content-Type: application/x-www-form-urlencoded\r\n";
        if (!has("Content-length")) req += "Content-Length: " + std::to_string(data->data.size()) + "\r\n";
    }
    for (const auto& [k, v] : headers) req += k + ": " + v + "\r\n";
    req += "Connection: close\r\n\r\n";
    if (data) req += data->data;

    Connection conn(*host, port, tls, timeout);
    conn.send_all(req);
    std::string in;
    std::size_t header_end;
    while ((header_end = in.find("\r\n\r\n")) == std::string::npos) {
        std::string more = conn.recv_some();
        if (more.empty()) throw Thrown{std::make_shared<urlerror::URLError>("Remote end closed connection without response")};
        in += more;
    }
    std::string head = in.substr(0, header_end);
    std::string rest = in.substr(header_end + 4);
    std::size_t line_end = head.find("\r\n");
    std::string status_line = head.substr(0, line_end);
    std::size_t sp1 = status_line.find(' '), sp2 = status_line.find(' ', sp1 + 1);
    std::int64_t status = std::stoll(status_line.substr(sp1 + 1, sp2 == std::string::npos ? std::string::npos : sp2 - sp1 - 1));
    std::string reason = sp2 == std::string::npos ? "" : status_line.substr(sp2 + 1);
    Headers h;
    for (std::size_t i = line_end == std::string::npos ? head.size() : line_end + 2; i < head.size();) {
        std::size_t e = head.find("\r\n", i);
        std::string line = head.substr(i, e == std::string::npos ? std::string::npos : e - i);
        auto colon = line.find(':');
        if (colon != std::string::npos) {
            std::string v = line.substr(colon + 1);
            v.erase(0, v.find_first_not_of(" \t"));
            h.add(line.substr(0, colon), v);
        }
        i = e == std::string::npos ? head.size() : e + 2;
    }
    auto read_more = [&]() {
        std::string more = conn.recv_some();
        rest += more;
        return !more.empty();
    };
    std::string body;
    bool no_body = method == "HEAD" || status == 204 || status == 304 || (status >= 100 && status < 200);
    if (no_body) {
    } else if (urlparse::lower(h.get("Transfer-Encoding").value_or("")).find("chunked") != std::string::npos) {
        std::size_t pos = 0;
        while (true) {
            std::size_t eol;
            while ((eol = rest.find("\r\n", pos)) == std::string::npos)
                if (!read_more()) throw Thrown{std::make_shared<urlerror::URLError>("incomplete chunked read")};
            std::size_t size = std::stoul(rest.substr(pos, eol - pos), nullptr, 16);
            pos = eol + 2;
            if (size == 0) break;
            while (rest.size() < pos + size + 2)
                if (!read_more()) throw Thrown{std::make_shared<urlerror::URLError>("incomplete chunked read")};
            body += rest.substr(pos, size);
            pos += size + 2;
        }
    } else if (auto length = h.get("Content-Length")) {
        std::size_t n = std::stoul(*length);
        while (rest.size() < n && read_more()) {
        }
        body = rest.substr(0, n);
    } else {
        while (read_more()) {
        }
        body = rest;
    }
    return {status, reason, h, body};
}

// urlopen: follows redirects like Python (301/302/303 become GET; 307/308 keep the method),
// and raises HTTPError for 4xx/5xx.
inline Response urlopen(const Request& request, std::optional<bytes> data = std::nullopt,
                        std::optional<double> timeout = std::nullopt) {
    std::string url = request.full_url_, method = request.get_method();
    std::optional<bytes> body = data ? data : request.data_;
    if (data && !request.method_) method = "POST";
    auto headers = request.headers_;
    for (int hops = 0;; ++hops) {
        auto [status, reason, h, content] = exchange(method, url, headers, body, timeout);
        auto location = h.get("Location");
        if ((status == 301 || status == 302 || status == 303 || status == 307 || status == 308) && location) {
            if (hops >= 10)
                throw Thrown{std::make_shared<urlerror::HTTPError>(
                    status, "The HTTP server returned a redirect error that would lead to an infinite loop.\n"
                            "The last 30x error message was:\n" + reason, h, Response(status, reason, h, content, url), url)};
            url = urlparse::urljoin(url, *location);
            if (status == 301 || status == 302 || status == 303) {
                if (method != "HEAD") method = "GET";
                body = std::nullopt;
                std::erase_if(headers, [](const auto& kv) {
                    return urlparse::lower(kv.first) == "content-type" || urlparse::lower(kv.first) == "content-length";
                });
            }
            continue;
        }
        Response r(status, reason, h, content, url);
        if (status >= 400) throw Thrown{std::make_shared<urlerror::HTTPError>(status, reason, h, r, url)};
        return r;
    }
}
inline Response urlopen(const std::string& url, std::optional<bytes> data = std::nullopt,
                        std::optional<double> timeout = std::nullopt) {
    return urlopen(Request(url, std::nullopt, dict<std::string, std::string>{}, std::nullopt), std::move(data), timeout);
}

}  // namespace sd::urlrequest

namespace sd {
// `"Content-Type" in response.headers`, `response.headers["Content-Type"]` (None if missing)
inline bool contains(const urlrequest::Headers& h, const std::string& name) { return h.has(name); }
inline std::optional<std::string> index(const urlrequest::Headers& h, const std::string& name) { return h.get(name); }
}  // namespace sd
