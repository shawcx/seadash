// The `urllib` package: urllib.parse (ports of CPython's functions), urllib.request (on
// http.client: HTTPS with certificates and host names verified, redirects) and
// urllib.error's exceptions.
#pragma once

#include "httpclient.hpp"

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

using Headers = httpclient::HTTPMessage;
using Response = httpclient::HTTPResponse;

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

// One request over a new connection ("Connection: close"). As in Python, errors while
// connecting and sending become URLError; errors reading the response don't.
inline Response open_once(const std::string& method, const std::string& url,
                          const std::vector<std::pair<std::string, std::string>>& headers,
                          const std::optional<bytes>& data, std::optional<double> timeout,
                          const std::shared_ptr<ssl::SSLContext>& context) {
    urlparse::Parts p = urlparse::urlsplit(url);
    if (p.scheme_ != "http" && p.scheme_ != "https")
        throw Thrown{std::make_shared<urlerror::URLError>("unknown url type: " + (p.scheme_.empty() ? repr_str(url) : p.scheme_))};
    auto host = p.hostname();
    if (!host) throw Thrown{std::make_shared<urlerror::URLError>("no host given")};
    bool tls = p.scheme_ == "https";
    std::string target = (p.path_.empty() ? "/" : p.path_) + (p.query_.empty() ? "" : "?" + p.query_);
    auto has = [&](const std::string& k) {
        for (const auto& [hk, hv] : headers)
            if (urlparse::lower(hk) == urlparse::lower(k)) return true;
        return false;
    };
    httpclient::HTTPConnection conn(*host, p.port().value_or(tls ? 443 : 80), timeout,
                                    tls ? (context ? context : ssl::create_default_context()) : nullptr);
    try {
        conn.putrequest(method, target, true, true);
        if (!has("Host")) conn.putheader("Host", p.hostport());
        if (!has("User-agent")) conn.putheader("User-Agent", "Python-urllib/3.12");
        conn.putheader("Accept-Encoding", "identity");
        if (data) {
            if (!has("Content-type")) conn.putheader("Content-Type", "application/x-www-form-urlencoded");
            if (!has("Content-length")) conn.putheader("Content-Length", std::to_string(data->data.size()));
        }
        for (const auto& [k, v] : headers) conn.putheader(k, v);
        conn.putheader("Connection", "close");
        conn.endheaders(data);
    } catch (const Thrown& t) {
        if (!dynamic_cast<OSError*>(t.exc.get())) throw;
        std::string why = dynamic_cast<TimeoutError*>(t.exc.get()) ? "timed out" : t.exc->message;
        throw Thrown{std::make_shared<urlerror::URLError>(why)};
    }
    Response r = conn.getresponse();
    r.state()->url = url;
    return r;
}

// urlopen: follows redirects like Python (301/302/303 become GET; 307/308 keep the method),
// and raises HTTPError for 4xx/5xx. The body is read as it's asked for.
inline Response urlopen(const Request& request, std::optional<bytes> data = std::nullopt,
                        std::optional<double> timeout = std::nullopt,
                        std::optional<std::shared_ptr<ssl::SSLContext>> context = std::nullopt) {
    std::string url = request.full_url_, method = request.get_method();
    std::optional<bytes> body = data ? data : request.data_;
    if (data && !request.method_) method = "POST";
    auto headers = request.headers_;
    std::shared_ptr<ssl::SSLContext> ctx = context ? *context : nullptr;
    for (int hops = 0;; ++hops) {
        Response r = open_once(method, url, headers, body, timeout, ctx);
        std::int64_t status = r.status();
        auto location = r.headers().get("Location");
        if ((status == 301 || status == 302 || status == 303 || status == 307 || status == 308) && location) {
            if (hops >= 10)
                throw Thrown{std::make_shared<urlerror::HTTPError>(
                    status, "The HTTP server returned a redirect error that would lead to an infinite loop.\n"
                            "The last 30x error message was:\n" + r.reason(), r.headers(), r, url)};
            r.read();  // (done with it)
            r.close();
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
        if (status >= 400) throw Thrown{std::make_shared<urlerror::HTTPError>(status, r.reason(), r.headers(), r, url)};
        return r;
    }
}
inline Response urlopen(const std::string& url, std::optional<bytes> data = std::nullopt,
                        std::optional<double> timeout = std::nullopt,
                        std::optional<std::shared_ptr<ssl::SSLContext>> context = std::nullopt) {
    return urlopen(Request(url, std::nullopt, dict<std::string, std::string>{}, std::nullopt), std::move(data), timeout,
                   std::move(context));
}

}  // namespace sd::urlrequest

