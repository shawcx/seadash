// The `http.server` module: HTTPServer and ThreadingHTTPServer, and BaseHTTPRequestHandler,
// which a program subclasses with do_GET(), do_POST()... methods. A port of CPython's
// http/server.py (and the parts of socketserver it uses), so requests are parsed, answered
// and logged the way Python's are.
#pragma once

#include <poll.h>

#include <condition_variable>
#include <ctime>
#include <thread>

#include <sys/stat.h>

#include "cookie_file.hpp"
#include "emailutils.hpp"
#include "httpclient.hpp"
#include "mimetypes.hpp"
#include "os.hpp"
#include "urllib.hpp"
#include "socket.hpp"

namespace sd::httpserver {

// HTTPStatus: code -> (phrase, description), for status lines and error pages.
inline const dict<std::int64_t, std::tuple<std::string, std::string>>& responses() {
    static const dict<std::int64_t, std::tuple<std::string, std::string>> table = {
        {100, {"Continue", "Request received, please continue"}},
        {101, {"Switching Protocols", "Switching to new protocol; obey Upgrade header"}},
        {102, {"Processing", ""}},
        {103, {"Early Hints", ""}},
        {200, {"OK", "Request fulfilled, document follows"}},
        {201, {"Created", "Document created, URL follows"}},
        {202, {"Accepted", "Request accepted, processing continues off-line"}},
        {203, {"Non-Authoritative Information", "Request fulfilled from cache"}},
        {204, {"No Content", "Request fulfilled, nothing follows"}},
        {205, {"Reset Content", "Clear input form for further input"}},
        {206, {"Partial Content", "Partial content follows"}},
        {207, {"Multi-Status", ""}},
        {208, {"Already Reported", ""}},
        {226, {"IM Used", ""}},
        {300, {"Multiple Choices", "Object has several resources -- see URI list"}},
        {301, {"Moved Permanently", "Object moved permanently -- see URI list"}},
        {302, {"Found", "Object moved temporarily -- see URI list"}},
        {303, {"See Other", "Object moved -- see Method and URL list"}},
        {304, {"Not Modified", "Document has not changed since given time"}},
        {305, {"Use Proxy", "You must use proxy specified in Location to access this resource"}},
        {307, {"Temporary Redirect", "Object moved temporarily -- see URI list"}},
        {308, {"Permanent Redirect", "Object moved permanently -- see URI list"}},
        {400, {"Bad Request", "Bad request syntax or unsupported method"}},
        {401, {"Unauthorized", "No permission -- see authorization schemes"}},
        {402, {"Payment Required", "No payment -- see charging schemes"}},
        {403, {"Forbidden", "Request forbidden -- authorization will not help"}},
        {404, {"Not Found", "Nothing matches the given URI"}},
        {405, {"Method Not Allowed", "Specified method is invalid for this resource"}},
        {406, {"Not Acceptable", "URI not available in preferred format"}},
        {407, {"Proxy Authentication Required", "You must authenticate with this proxy before proceeding"}},
        {408, {"Request Timeout", "Request timed out; try again later"}},
        {409, {"Conflict", "Request conflict"}},
        {410, {"Gone", "URI no longer exists and has been permanently removed"}},
        {411, {"Length Required", "Client must specify Content-Length"}},
        {412, {"Precondition Failed", "Precondition in headers is false"}},
        {413, {"Request Entity Too Large", "Entity is too large"}},
        {414, {"Request-URI Too Long", "URI is too long"}},
        {415, {"Unsupported Media Type", "Entity body in unsupported format"}},
        {416, {"Requested Range Not Satisfiable", "Cannot satisfy request range"}},
        {417, {"Expectation Failed", "Expect condition could not be satisfied"}},
        {418, {"I'm a Teapot", "Server refuses to brew coffee because it is a teapot."}},
        {421, {"Misdirected Request", "Server is not able to produce a response"}},
        {422, {"Unprocessable Entity", ""}},
        {423, {"Locked", ""}},
        {424, {"Failed Dependency", ""}},
        {425, {"Too Early", ""}},
        {426, {"Upgrade Required", ""}},
        {428, {"Precondition Required", "The origin server requires the request to be conditional"}},
        {429, {"Too Many Requests", "The user has sent too many requests in a given amount of time (\"rate limiting\")"}},
        {431, {"Request Header Fields Too Large", "The server is unwilling to process the request because its header fields are too large"}},
        {451, {"Unavailable For Legal Reasons", "The server is denying access to the resource as a consequence of a legal demand"}},
        {500, {"Internal Server Error", "Server got itself in trouble"}},
        {501, {"Not Implemented", "Server does not support this operation"}},
        {502, {"Bad Gateway", "Invalid responses from another server/proxy"}},
        {503, {"Service Unavailable", "The server cannot process the request due to a high load"}},
        {504, {"Gateway Timeout", "The gateway server did not receive a timely response"}},
        {505, {"HTTP Version Not Supported", "Cannot fulfill request"}},
        {506, {"Variant Also Negotiates", ""}},
        {507, {"Insufficient Storage", ""}},
        {508, {"Loop Detected", ""}},
        {510, {"Not Extended", ""}},
        {511, {"Network Authentication Required", "The client needs to authenticate to gain network access"}},
    };
    return table;
}

inline const std::string& default_error_message() {
    static const std::string text =
        "<!DOCTYPE HTML>\n"
        "<html lang=\"en\">\n"
        "    <head>\n"
        "        <meta charset=\"utf-8\">\n"
        "        <title>Error response</title>\n"
        "    </head>\n"
        "    <body>\n"
        "        <h1>Error response</h1>\n"
        "        <p>Error code: %(code)d</p>\n"
        "        <p>Message: %(message)s.</p>\n"
        "        <p>Error code explanation: %(code)s - %(explain)s.</p>\n"
        "    </body>\n"
        "</html>\n";
    return text;
}

// format % args for the log formats ("%s", "%d", "%r", "%%"); the arguments arrive as text.
inline std::string percent_format(const std::string& format, const vtuple<std::string>& args) {
    std::string out;
    std::size_t next = 0;
    for (std::size_t i = 0; i < format.size(); ++i) {
        if (format[i] != '%' || i + 1 == format.size()) {
            out += format[i];
            continue;
        }
        char conv = format[++i];
        if (conv == '%') {
            out += '%';
            continue;
        }
        if (next >= args.items.size()) raise("TypeError", "not enough arguments for format string");
        const std::string& arg = args.items[next++];
        out += conv == 'r' ? repr_str(arg) : arg;
    }
    if (next < args.items.size()) raise("TypeError", "not all arguments converted during string formatting");
    return out;
}

// error_message_format % {"code": ..., "message": ..., "explain": ...}
inline std::string fill_error_page(const std::string& format, std::int64_t code, const std::string& message,
                                   const std::string& explain) {
    std::string out;
    for (std::size_t i = 0; i < format.size(); ++i) {
        if (format[i] == '%' && i + 1 < format.size() && format[i + 1] == '%') {
            out += '%';
            ++i;
        } else if (format[i] == '%' && i + 1 < format.size() && format[i + 1] == '(') {
            std::size_t close = format.find(')', i);
            if (close == std::string::npos || close + 1 >= format.size()) raise("ValueError", "incomplete format key");
            std::string key = format.substr(i + 2, close - i - 2);
            if (key == "code") out += std::to_string(code);
            else if (key == "message") out += message;
            else if (key == "explain") out += explain;
            else raise("KeyError", repr_str(key));
            i = close + 1;  // (the conversion: d or s)
        } else {
            out += format[i];
        }
    }
    return out;
}

// html.escape(text, quote=False)
inline std::string escape_html(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == '&') out += "&amp;";
        else if (c == '<') out += "&lt;";
        else if (c == '>') out += "&gt;";
        else out += c;
    }
    return out;
}

// Log lines show control characters as \xNN (and a backslash as \\), as Python's do.
inline std::string escape_controls(const std::string& s) {
    std::string out;
    char buf[8];
    for (std::size_t i = 0; i < s.size(); ++i) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c < 0x20 || c == 0x7f) {
            std::snprintf(buf, sizeof buf, "\\x%02x", c);
            out += buf;
        } else if (c == 0xC2 && i + 1 < s.size() && static_cast<unsigned char>(s[i + 1]) >= 0x80 &&
                   static_cast<unsigned char>(s[i + 1]) <= 0x9F) {  // U+0080..U+009F
            std::snprintf(buf, sizeof buf, "\\x%02x", static_cast<unsigned char>(s[++i]));
            out += buf;
        } else if (c == '\\') {
            out += "\\\\";
        } else {
            out += static_cast<char>(c);
        }
    }
    return out;
}

inline const char* const weekday_names[] = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};
inline const char* const month_names[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

// The client socket's writing side as a binary file: each write is sent at once, and a
// closed connection is an error (EPIPE), not a SIGPIPE that kills the program.
inline std::shared_ptr<BinaryFile> socket_writer(int fd) {
    Cookie cookie{reinterpret_cast<void*>(static_cast<std::intptr_t>(fd)), nullptr,
                  [](void* p, const char* buf, std::size_t n) -> std::int64_t {
                      int fd = static_cast<int>(reinterpret_cast<std::intptr_t>(p));
                      std::size_t sent = 0;
                      while (sent < n) {
#ifdef MSG_NOSIGNAL
                          ssize_t k = ::send(fd, buf + sent, n - sent, MSG_NOSIGNAL);
#else
                          ssize_t k = ::send(fd, buf + sent, n - sent, 0);
#endif
                          if (k < 0) {
                              if (errno == EINTR) continue;
                              return -1;
                          }
                          sent += static_cast<std::size_t>(k);
                      }
                      return static_cast<std::int64_t>(n);
                  },
                  [](void* p) -> int { return ::close(static_cast<int>(reinterpret_cast<std::intptr_t>(p))); }};
    std::FILE* f = cookie_file(cookie, false);
    if (!f) raise_os(errno, std::nullopt);
    std::setvbuf(f, nullptr, _IONBF, 0);
    return std::make_shared<BinaryFile>(f, "<socket>", "wb");
}

class HTTPServer;

// The base class of a program's request handlers. The server makes one per connection;
// handle() reads each request, and calls the do_<METHOD>() the subclass defines
// (sd_dispatch, which codegen generates), or answers 501.
struct BaseHTTPRequestHandler : virtual object {
    // What the handler sees (seadash fields of the base class).
    std::string command, path, request_version = "HTTP/0.9", requestline;
    bytes raw_requestline;
    httpclient::HTTPMessage headers;
    std::tuple<std::string, std::int64_t> client_address;
    std::shared_ptr<BinaryFile> rfile, wfile;
    bool close_connection = true;

    virtual ~BaseHTTPRequestHandler() = default;

    // Class attributes (a subclass may set its own: protocol_version = "HTTP/1.1").
    static std::string sd_class_server_version() { return "BaseHTTP/0.6"; }
    static std::string sd_class_sys_version() { return "seadash/0.1.0"; }
    static std::string sd_class_protocol_version() { return "HTTP/1.0"; }
    static std::string sd_class_error_content_type() { return "text/html;charset=utf-8"; }
    static std::string sd_class_error_message_format() { return default_error_message(); }
    static std::string sd_class_default_request_version() { return "HTTP/0.9"; }
    virtual std::string sd_attr_server_version() const { return sd_class_server_version(); }
    virtual std::string sd_attr_sys_version() const { return sd_class_sys_version(); }
    virtual std::string sd_attr_protocol_version() const { return sd_class_protocol_version(); }
    virtual std::string sd_attr_error_content_type() const { return sd_class_error_content_type(); }
    virtual std::string sd_attr_error_message_format() const { return sd_class_error_message_format(); }
    virtual std::string sd_attr_default_request_version() const { return sd_class_default_request_version(); }

    // Calls the do_<command>() method, if the handler class has one.
    virtual bool sd_dispatch(const std::string&) { return false; }

    // ---- what a handler calls ----

    virtual void send_response(std::int64_t code, const std::optional<std::string>& message = std::nullopt) {
        log_request(std::to_string(code));
        send_response_only(code, message);
        send_header("Server", version_string());
        send_header("Date", date_time_string());
    }
    virtual void send_response_only(std::int64_t code, const std::optional<std::string>& message = std::nullopt) {
        if (request_version == "HTTP/0.9") return;
        std::string text;
        if (message) {
            text = *message;
        } else if (const auto* entry = responses().find(code)) {
            text = std::get<0>(*entry);
        }
        headers_buffer_ += str_encode(sd_attr_protocol_version() + " " + std::to_string(code) + " " + text + "\r\n", "latin-1").data;
    }
    virtual void send_header(const std::string& keyword, const std::string& value) {
        if (request_version != "HTTP/0.9") headers_buffer_ += str_encode(keyword + ": " + value + "\r\n", "latin-1").data;
        if (httpclient::ascii_lower(keyword) == "connection") {
            std::string v = httpclient::ascii_lower(value);
            if (v == "close") close_connection = true;
            else if (v == "keep-alive") close_connection = false;
        }
    }
    virtual void end_headers() {
        if (request_version != "HTTP/0.9") {
            headers_buffer_ += "\r\n";
            flush_headers();
        }
    }
    virtual void flush_headers() {
        if (!headers_buffer_.empty()) wfile->write(bytes(std::exchange(headers_buffer_, std::string())));
    }
    virtual void send_error(std::int64_t code, const std::optional<std::string>& message = std::nullopt,
                            const std::optional<std::string>& explain = std::nullopt) {
        std::string shortmsg = "???", longmsg = "???";
        if (const auto* entry = responses().find(code)) std::tie(shortmsg, longmsg) = *entry;
        std::string msg = message.value_or(shortmsg), why = explain.value_or(longmsg);
        log_error("code %d, message %s", vtuple<std::string>{std::to_string(code), msg});
        send_response(code, msg);
        send_header("Connection", "close");
        std::string body;
        if (code >= 200 && code != 204 && code != 205 && code != 304) {
            body = fill_error_page(sd_attr_error_message_format(), code, escape_html(msg), escape_html(why));
            send_header("Content-Type", sd_attr_error_content_type());
            send_header("Content-Length", std::to_string(body.size()));
        }
        end_headers();
        if (command != "HEAD" && !body.empty()) wfile->write(bytes(body));
    }
    virtual bool handle_expect_100() {
        send_response_only(100);
        end_headers();
        return true;
    }

    // ---- logging (to stderr, like Python's) ----

    virtual void log_request(const std::string& code = "-", const std::string& size = "-") {
        log_message("\"%s\" %s %s", vtuple<std::string>{requestline, code, size});
    }
    virtual void log_error(const std::string& format, const vtuple<std::string>& args) { log_message(format, args); }
    virtual void log_message(const std::string& format, const vtuple<std::string>& args) {
        std::string line = address_string() + " - - [" + log_date_time_string() + "] " +
                           escape_controls(percent_format(format, args)) + "\n";
        std::fwrite(line.data(), 1, line.size(), stderr);
    }
    virtual std::string version_string() { return sd_attr_server_version() + " " + sd_attr_sys_version(); }
    virtual std::string date_time_string(std::optional<double> timestamp = std::nullopt) {
        std::time_t t = timestamp ? static_cast<std::time_t>(*timestamp) : std::time(nullptr);
        std::tm tm{};
        ::gmtime_r(&t, &tm);
        char buf[64];
        std::snprintf(buf, sizeof buf, "%s, %02d %s %04d %02d:%02d:%02d GMT", weekday_names[tm.tm_wday], tm.tm_mday,
                      month_names[tm.tm_mon], tm.tm_year + 1900, tm.tm_hour, tm.tm_min, tm.tm_sec);
        return buf;
    }
    virtual std::string log_date_time_string() {
        std::time_t t = std::time(nullptr);
        std::tm tm{};
        ::localtime_r(&t, &tm);
        char buf[64];
        std::snprintf(buf, sizeof buf, "%02d/%3s/%04d %02d:%02d:%02d", tm.tm_mday, month_names[tm.tm_mon], tm.tm_year + 1900,
                      tm.tm_hour, tm.tm_min, tm.tm_sec);
        return buf;
    }
    virtual std::string address_string() { return std::get<0>(client_address); }

    // How a class instance shows itself (overridden by the program's subclass).
    virtual std::string sd_repr() const { return "<BaseHTTPRequestHandler>"; }
    virtual std::string sd_class_name() const { return "BaseHTTPRequestHandler"; }
    virtual std::string sd_str() const { return sd_repr(); }
    virtual bool sd_truthy() const { return true; }

    // ---- one connection (the server calls these) ----

    void sd_setup(int fd, std::tuple<std::string, std::int64_t> address) {
        client_address = std::move(address);
        int reading = ::dup(fd), writing = ::dup(fd);
        std::FILE* in = reading >= 0 ? ::fdopen(reading, "rb") : nullptr;
        if (!in) {
            if (reading >= 0) ::close(reading);
            if (writing >= 0) ::close(writing);
            raise_os(errno, std::nullopt);
        }
        rfile = std::make_shared<BinaryFile>(in, "<socket>", "rb");
        if (writing < 0) raise_os(errno, std::nullopt);
        wfile = socket_writer(writing);
    }
    // The same over TLS: rfile and wfile read and write through the connection's SSL (which
    // the server frees after sd_finish).
    void sd_setup_tls(SSL* ssl, std::tuple<std::string, std::int64_t> address) {
        client_address = std::move(address);
        Cookie reader{ssl,
                      [](void* p, char* buf, std::size_t n) -> std::int64_t {
                          int k = SSL_read(static_cast<SSL*>(p), buf, static_cast<int>(std::min<std::size_t>(n, 1u << 30)));
                          if (k > 0) return k;
                          int err = SSL_get_error(static_cast<SSL*>(p), k);
                          if (err == SSL_ERROR_ZERO_RETURN || (err == SSL_ERROR_SYSCALL && errno == 0)) return 0;
                          errno = errno ? errno : EIO;
                          return -1;
                      },
                      nullptr, [](void*) { return 0; }};
        Cookie writer{ssl, nullptr,
                      [](void* p, const char* buf, std::size_t n) -> std::int64_t {
                          int k = SSL_write(static_cast<SSL*>(p), buf, static_cast<int>(std::min<std::size_t>(n, 1u << 30)));
                          if (k > 0) return k;
                          errno = errno ? errno : EPIPE;
                          return -1;
                      },
                      [](void*) { return 0; }};
        std::FILE* in = cookie_file(reader, true);
        std::FILE* out = cookie_file(writer, false);
        if (!in || !out) raise_os(errno, std::nullopt);
        std::setvbuf(out, nullptr, _IONBF, 0);  // (sent as it's written, as on a plain socket)
        rfile = std::make_shared<BinaryFile>(in, "<socket>", "rb");
        wfile = std::make_shared<BinaryFile>(out, "<socket>", "wb");
    }
    void sd_handle() {  // handle(): the connection's requests, until one closes it
        close_connection = true;
        handle_one_request();
        while (!close_connection) handle_one_request();
    }
    void sd_finish() {
        if (wfile && !wfile->is_closed()) {
            try {
                wfile->flush();
            } catch (const Thrown&) {
                // (the client went away: nothing to send it)
            }
            wfile->close();
        }
        if (rfile) rfile->close();
    }

private:
    std::string headers_buffer_;

    void handle_one_request() {
        raw_requestline = bytes(rfile->readline_raw());
        if (raw_requestline.size() > 65536) {
            requestline = request_version = command = "";
            send_error(414);
            return;
        }
        if (raw_requestline.empty()) {
            close_connection = true;
            return;
        }
        if (!parse_request()) return;
        if (!sd_dispatch(command)) {
            send_error(501, "Unsupported method (" + repr_str(command) + ")");
            return;
        }
        wfile->flush();
    }

    static bool all_digits(const std::string& s) {
        return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; });
    }

    bool parse_request() {
        command = "";
        std::string version = sd_attr_default_request_version();
        request_version = version;
        close_connection = true;
        std::string line = bytes_decode(raw_requestline, "latin-1");
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
        requestline = line;
        std::vector<std::string> words = str_split(line);
        if (words.empty()) return false;
        if (words.size() >= 3) {
            version = words[words.size() - 1];
            std::string number = version.size() > 5 ? version.substr(5) : "";
            std::size_t dot = number.find('.');
            std::string major = number.substr(0, dot), minor = dot == std::string::npos ? "" : number.substr(dot + 1);
            if (!version.starts_with("HTTP/") || dot == std::string::npos || minor.find('.') != std::string::npos ||
                !all_digits(major) || !all_digits(minor) || major.size() > 10 || minor.size() > 10) {
                send_error(400, "Bad request version (" + repr_str(version) + ")");
                return false;
            }
            std::pair<long long, long long> v{std::stoll(major), std::stoll(minor)};
            if (v >= std::pair<long long, long long>{1, 1} && sd_attr_protocol_version() >= "HTTP/1.1") close_connection = false;
            if (v >= std::pair<long long, long long>{2, 0}) {
                send_error(505, "Invalid HTTP version (" + number + ")");
                return false;
            }
            request_version = version;
        }
        if (words.size() < 2 || words.size() > 3) {
            send_error(400, "Bad request syntax (" + repr_str(line) + ")");
            return false;
        }
        std::string verb = words[0], target = words[1];
        if (words.size() == 2) {
            close_connection = true;
            if (verb != "GET") {
                send_error(400, "Bad HTTP/0.9 request type (" + repr_str(verb) + ")");
                return false;
            }
        }
        command = verb;
        path = target;
        if (path.starts_with("//")) path = "/" + path.substr(path.find_first_not_of('/') == std::string::npos ? path.size() : path.find_first_not_of('/'));
        if (!read_headers()) return false;
        std::string conntype = httpclient::ascii_lower(headers.get("Connection").value_or(""));
        if (conntype == "close") close_connection = true;
        else if (conntype == "keep-alive" && sd_attr_protocol_version() >= "HTTP/1.1") close_connection = false;
        std::string expect = httpclient::ascii_lower(headers.get("Expect").value_or(""));
        if (expect == "100-continue" && sd_attr_protocol_version() >= "HTTP/1.1" && request_version >= "HTTP/1.1")
            return handle_expect_100();
        return true;
    }

    // http.client.parse_headers: up to 100 lines, ended by a blank line. As in Python's email
    // parser, a line that isn't `Name: value` ends the headers (the rest would be a body).
    bool read_headers() {
        headers = httpclient::HTTPMessage();
        bool ended = false;
        for (int count = 0;; ++count) {
            std::string raw = rfile->readline_raw();
            if (raw.size() > 65536) {
                send_error(431, "Line too long", "header line");
                return false;
            }
            if (raw.empty() || raw == "\r\n" || raw == "\n") return true;
            if (count >= 100) {
                send_error(431, "Too many headers", "got more than 100 headers");
                return false;
            }
            std::string line = bytes_decode(bytes(raw), "latin-1");
            while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) line.pop_back();
            if (ended) continue;
            std::size_t colon = line.find(':');
            if (colon == std::string::npos || colon == 0 || line[0] == ' ' || line[0] == '\t') {
                ended = true;
                continue;
            }
            std::string value = line.substr(colon + 1);
            value.erase(0, value.find_first_not_of(" \t"));
            headers.add(line.substr(0, colon), value);
        }
    }
};

// posixpath.normpath
inline std::string normpath(const std::string& path) {
    if (path.empty()) return ".";
    std::size_t lead = path[0] == '/' ? (path.starts_with("//") && !path.starts_with("///") ? 2 : 1) : 0;
    std::vector<std::string> parts;
    std::size_t i = 0;
    while (i <= path.size()) {
        std::size_t j = path.find('/', i);
        if (j == std::string::npos) j = path.size();
        std::string comp = path.substr(i, j - i);
        if (comp.empty() || comp == ".") {
        } else if (comp != ".." || (!lead && parts.empty()) || (!parts.empty() && parts.back() == "..")) {
            parts.push_back(comp);
        } else if (!parts.empty()) {
            parts.pop_back();
        }
        i = j + 1;
    }
    std::string out(lead, '/');
    for (std::size_t k = 0; k < parts.size(); ++k) out += (k ? "/" : "") + parts[k];
    return out.empty() ? "." : out;
}

// urllib.parse.unquote(s) (errors='replace'): bytes that aren't UTF-8 become U+FFFD.
inline std::string unquote_text(const std::string& s) {
    std::string raw = urlparse::unquote(s), out;
    for (std::size_t i = 0; i < raw.size();) {
        unsigned char c = static_cast<unsigned char>(raw[i]);
        std::size_t width = c < 0x80 ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : (c >> 3) == 0x1E ? 4 : 0;
        bool ok = width > 0 && i + width <= raw.size();
        for (std::size_t k = 1; ok && k < width; ++k) ok = (static_cast<unsigned char>(raw[i + k]) >> 6) == 0x2;
        if (ok) {
            out += raw.substr(i, width);
            i += width;
        } else {
            out += "\xEF\xBF\xBD";
            ++i;
        }
    }
    return out;
}

// SimpleHTTPRequestHandler: serves the files under `directory` (the current directory unless
// the server was given partial(SimpleHTTPRequestHandler, directory=...)), with directory
// listings, index.html, and If-Modified-Since. A port of CPython's.
struct SimpleHTTPRequestHandler : BaseHTTPRequestHandler {
    std::string directory = os::getcwd();

    static std::string sd_class_server_version() { return "SimpleHTTP/0.6"; }
    std::string sd_attr_server_version() const override { return sd_class_server_version(); }
    static std::tuple<std::string, std::string> sd_class_index_pages() { return {"index.html", "index.htm"}; }
    virtual std::tuple<std::string, std::string> sd_attr_index_pages() const { return sd_class_index_pages(); }

    bool sd_dispatch(const std::string& c) override {
        if (c == "GET") do_GET();
        else if (c == "HEAD") do_HEAD();
        else return BaseHTTPRequestHandler::sd_dispatch(c);
        return true;
    }
    virtual void do_GET() {
        if (auto f = send_head()) {
            copyfile(*f, wfile);
            (*f)->close();
        }
    }
    virtual void do_HEAD() {
        if (auto f = send_head()) (*f)->close();
    }

    // The response's headers, and the file to send (none if the response is complete).
    virtual std::optional<std::shared_ptr<BinaryFile>> send_head() {
        std::string fspath = translate_path(path);
        if (os::path::isdir(fspath)) {
            std::size_t cut = path.find_first_of("?#");
            std::string before = path.substr(0, cut), after = cut == std::string::npos ? "" : path.substr(cut);
            if (!before.ends_with('/')) {
                send_response(301);
                send_header("Location", before + "/" + after);
                send_header("Content-Length", "0");
                end_headers();
                return std::nullopt;
            }
            auto [first, second] = sd_attr_index_pages();
            bool found = false;
            for (const std::string& index : {first, second}) {
                std::string candidate = os::path::join(fspath, index);
                if (os::path::isfile(candidate)) {
                    fspath = candidate;
                    found = true;
                    break;
                }
            }
            if (!found) return list_directory(fspath);
        }
        std::string ctype = guess_type(fspath);
        if (fspath.ends_with('/')) {
            send_error(404, "File not found");
            return std::nullopt;
        }
        std::FILE* fp = std::fopen(fspath.c_str(), "rb");
        struct stat info {};
        if (!fp || ::fstat(fileno(fp), &info) != 0 || S_ISDIR(info.st_mode)) {
            if (fp) std::fclose(fp);
            send_error(404, "File not found");
            return std::nullopt;
        }
        auto f = std::make_shared<BinaryFile>(fp, fspath, "rb");
        if (headers.has("If-Modified-Since") && !headers.has("If-None-Match")) {
            try {  // (a date that can't be read, or that isn't UTC, is ignored, as in Python)
                auto ims = emailutils::parsedate_to_datetime(*headers.get("If-Modified-Since"));
                auto offset = ims.utcoffset();
                if (!offset || offset->total_us() == 0) {
                    datetime::datetime utc(ims.year(), ims.month(), ims.day(), ims.hour(), ims.minute(), ims.second(), 0,
                                           datetime::timezone::utc());
                    if (static_cast<double>(info.st_mtime) <= utc.timestamp()) {
                        send_response(304);
                        end_headers();
                        f->close();
                        return std::nullopt;
                    }
                }
            } catch (const Thrown&) {
            }
        }
        send_response(200);
        send_header("Content-type", ctype);
        send_header("Content-Length", std::to_string(info.st_size));
        send_header("Last-Modified", date_time_string(static_cast<double>(info.st_mtime)));
        end_headers();
        return f;
    }

    virtual std::optional<std::shared_ptr<BinaryFile>> list_directory(const std::string& fspath) {
        std::vector<std::string> names;
        try {
            names = os::listdir(fspath);
        } catch (const Thrown&) {
            send_error(404, "No permission to list directory");
            return std::nullopt;
        }
        std::stable_sort(names.begin(), names.end(), [](const std::string& a, const std::string& b) {
            return str_lower(a) < str_lower(b);
        });
        std::string display = escape_html(unquote_text(path));
        std::string title = "Directory listing for " + display;
        std::string page = "<!DOCTYPE HTML>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<title>" + title +
                           "</title>\n</head>\n<body>\n<h1>" + title + "</h1>\n<hr>\n<ul>";
        for (const std::string& name : names) {
            std::string full = os::path::join(fspath, name), shown = name, link = name;
            struct stat info {};
            bool is_link = ::lstat(full.c_str(), &info) == 0 && S_ISLNK(info.st_mode);
            if (os::path::isdir(full)) shown = link = name + "/";
            if (is_link) shown = name + "@";
            page += "\n<li><a href=\"" + urlparse::quote(link) + "\">" + escape_html(shown) + "</a></li>";
        }
        page += "\n</ul>\n<hr>\n</body>\n</html>\n";
        send_response(200);
        send_header("Content-type", "text/html; charset=utf-8");
        send_header("Content-Length", std::to_string(page.size()));
        end_headers();
        std::FILE* mem = std::tmpfile();
        if (!mem) raise_os(errno, std::nullopt);
        std::fwrite(page.data(), 1, page.size(), mem);
        std::rewind(mem);
        return std::make_shared<BinaryFile>(mem, "<listing>", "rb");
    }

    // The request's path as a file under `directory`: no "..", no absolute parts.
    virtual std::string translate_path(const std::string& request_path) {
        std::string p = request_path.substr(0, request_path.find('?'));
        p = p.substr(0, p.find('#'));
        std::string trimmed = p.substr(0, p.find_last_not_of(" \t\n\r\f\v") + 1);
        bool trailing_slash = trimmed.ends_with('/');
        p = normpath(unquote_text(p));
        std::string out = directory;
        for (const std::string& word : str_split(p, "/")) {
            if (word.empty() || word == "." || word == "..") continue;  // (and "dir/word" can't happen: split on /)
            out = os::path::join(out, word);
        }
        if (trailing_slash) out += "/";
        return out;
    }

    virtual void copyfile(const std::shared_ptr<BinaryFile>& source, const std::shared_ptr<BinaryFile>& output) {
        for (bytes chunk; !(chunk = source->read(64 * 1024)).data.empty();) output->write(chunk);
    }

    // The Content-type: compressed files by their encoding, then mimetypes, then a default.
    virtual std::string guess_type(const std::string& fspath) {
        static const std::pair<const char*, const char*> encodings[] = {
            {".gz", "application/gzip"}, {".Z", "application/octet-stream"}, {".bz2", "application/x-bzip2"},
            {".xz", "application/x-xz"}};
        std::string base = fspath.substr(fspath.rfind('/') == std::string::npos ? 0 : fspath.rfind('/') + 1);
        std::size_t dot = base.rfind('.');
        std::size_t name_start = base.find_first_not_of('.');  // (leading dots aren't an extension: .bashrc)
        std::string ext = dot == std::string::npos || name_start == std::string::npos || dot < name_start ? "" : base.substr(dot);
        for (const std::string& e : {ext, str_lower(ext)})
            for (auto [suffix, type] : encodings)
                if (e == suffix) return type;
        auto [type, encoding] = mimetypes::guess_type(fspath);
        return type.value_or("application/octet-stream");
    }

    std::string sd_repr() const override { return "<SimpleHTTPRequestHandler>"; }
    std::string sd_class_name() const override { return "SimpleHTTPRequestHandler"; }
};

// HTTPServer / ThreadingHTTPServer (socketserver.TCPServer): accepts connections and hands
// each to a new handler, on this thread or (threading) a thread of its own.
class HTTPServer {
public:
    using Factory = std::function<std::shared_ptr<BaseHTTPRequestHandler>()>;

private:
    struct State {
        socket::Socket sock;
        std::shared_ptr<ssl::SSLContext> tls;  // (HTTPS: httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True))
        std::tuple<std::string, std::int64_t> address;
        Factory make;
        bool threading = false;
        std::mutex mu;
        std::condition_variable cv;
        // is_shut_down is Python's event of that name: unset until a serve_forever() has finished,
        // so a shutdown() made before serve_forever() starts still stops it.
        bool shutdown_request = false, is_shut_down = false, closed = false;
        std::vector<std::thread> workers;
        ~State() {
            for (auto& t : workers)
                if (t.joinable()) t.join();
        }
    };
    std::shared_ptr<State> s_;

    static void process(const std::shared_ptr<State>& s, socket::Socket conn, std::tuple<std::string, std::int64_t> addr,
                        SSL* ssl = nullptr) {
        struct FreeTls {  // (after the handler is done with it)
            SSL* ssl;
            ~FreeTls() {
                if (ssl) {
                    SSL_shutdown(ssl);
                    SSL_free(ssl);
                }
            }
        } free_tls{ssl};
        try {
            std::shared_ptr<BaseHTTPRequestHandler> h = s->make();
            if (ssl) h->sd_setup_tls(ssl, addr);
            else h->sd_setup(static_cast<int>(conn.fileno()), addr);
            try {
                h->sd_handle();
            } catch (...) {
                h->sd_finish();
                throw;
            }
            h->sd_finish();
        } catch (const Thrown& t) {
            handle_error(addr, t);
        }
        try {
            conn.shutdown(SHUT_WR);
        } catch (const Thrown&) {
        }
        conn.close();
    }

    // socketserver's handle_error: the request failed, the server goes on.
    static void handle_error(const std::tuple<std::string, std::int64_t>& addr, const Thrown& t) {
        std::string msg = t.exc->sd_str();
        std::string text = std::string(40, '-') + "\nException occurred during processing of request from " + repr(addr) +
                           "\n" + t.exc->sd_type() + (msg.empty() ? "" : ": " + msg) + "\n" + std::string(40, '-') + "\n";
        std::fwrite(text.data(), 1, text.size(), stderr);
    }

    void handle_one(bool block_until_ready) {
        auto& s = *s_;
        if (!block_until_ready) {
            pollfd p{static_cast<int>(s.sock.fileno()), POLLIN, 0};
            if (::poll(&p, 1, 0) <= 0) return;
        }
        auto [conn, addr] = s.sock.accept();
        SSL* ssl = nullptr;
        if (s.tls) {  // the handshake, as Python's SSLSocket.accept() does it; one that fails is dropped
            try {
                ssl = ssl::accept_tls(static_cast<int>(conn.fileno()), *s.tls);
            } catch (const Thrown& t) {
                if (!isinstance<OSError>(t)) throw;
                conn.close();
                return;
            }
        }
        if (!s.threading) {
            process(s_, std::move(conn), std::move(addr), ssl);
            return;
        }
        std::lock_guard lk(s.mu);
        s.workers.emplace_back([state = s_, conn = std::move(conn), addr = std::move(addr), ssl]() mutable {
            process(state, std::move(conn), std::move(addr), ssl);
        });
    }

public:
    HTTPServer() = default;
    HTTPServer(const std::tuple<std::string, std::int64_t>& server_address, Factory make, bool bind_and_activate = true,
               bool threading = false)
        : s_(std::make_shared<State>()) {
        s_->make = std::move(make);
        s_->threading = threading;
        s_->sock = socket::Socket(AF_INET, SOCK_STREAM);
        s_->sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1);  // (HTTPServer.allow_reuse_address)
        s_->address = server_address;
        if (bind_and_activate) {
            try {
                server_bind();
                server_activate();
            } catch (...) {
                server_close();
                throw;
            }
        }
    }
    // httpd.socket, and `httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)` for HTTPS
    socket::Socket get_socket() const { return s_->sock; }
    void set_socket(const ssl::SSLSocket& wrapped) {
        if (!wrapped.server_side() || wrapped.raw_socket().fileno() != s_->sock.fileno())
            raise("ValueError", "httpd.socket can only be set to the server's own socket, wrapped for TLS: "
                                "ctx.wrap_socket(httpd.socket, server_side=True)");
        s_->tls = wrapped.context();
    }
    void server_bind() {
        s_->sock.bind(s_->address);
        s_->address = s_->sock.getsockname();
    }
    // Python's backlog is 5 (request_queue_size). Its clients take turns under the GIL; ours run
    // in parallel, and on macOS connections beyond a full queue are reset, so take the system's.
    void server_activate() { s_->sock.listen(SOMAXCONN); }

    std::tuple<std::string, std::int64_t> server_address() const { return s_->address; }
    std::int64_t server_port() const { return std::get<1>(s_->address); }
    std::int64_t fileno() const { return s_->sock.fileno(); }

    // Handles requests until shutdown() (from another thread). poll_interval: how often it
    // checks for that.
    void serve_forever(double poll_interval = 0.5) {
        {
            std::lock_guard lk(s_->mu);
            s_->is_shut_down = false;
        }
        try {
            while (true) {
                {
                    std::lock_guard lk(s_->mu);
                    if (s_->shutdown_request) break;
                }
                pollfd p{static_cast<int>(s_->sock.fileno()), POLLIN, 0};
                int ready = ::poll(&p, 1, static_cast<int>(poll_interval * 1000));
                {
                    std::lock_guard lk(s_->mu);
                    if (s_->shutdown_request) break;
                }
                if (ready > 0) handle_one(false);
            }
        } catch (...) {
            std::lock_guard lk(s_->mu);
            s_->is_shut_down = true;
            s_->shutdown_request = false;
            s_->cv.notify_all();
            throw;
        }
        std::lock_guard lk(s_->mu);
        s_->is_shut_down = true;
        s_->shutdown_request = false;
        s_->cv.notify_all();
    }
    // Stops serve_forever() and waits until it has, as Python's does: called before
    // serve_forever() starts, it waits for it to start and stop. (Called from another thread.)
    void shutdown() {
        std::unique_lock lk(s_->mu);
        s_->shutdown_request = true;
        s_->cv.wait(lk, [&] { return s_->is_shut_down; });
    }
    void handle_request() { handle_one(true); }
    // Closes the listening socket, and waits for any requests still being handled.
    void server_close() {
        std::vector<std::thread> workers;
        {
            std::lock_guard lk(s_->mu);
            s_->closed = true;
            workers = std::move(s_->workers);
        }
        s_->sock.close();
        for (auto& t : workers)
            if (t.joinable()) t.join();
    }
    std::string sd_repr() const {
        return std::string("<http.server.") + (s_->threading ? "ThreadingHTTPServer" : "HTTPServer") + " object>";
    }
};

}  // namespace sd::httpserver
