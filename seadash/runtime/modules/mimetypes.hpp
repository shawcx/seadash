// The `mimetypes` module: guess_type, guess_extension, guess_all_extensions, add_type and
// the module's tables. Unlike Python, the system's mime.types files aren't read: the
// tables are Python 3.12's built-in defaults (mimetypes._types_map_default and friends).
#pragma once

#include <arpa/inet.h>

#include <shared_mutex>

#include "pathlib.hpp"

namespace sd::mimetypes {

// Generated from Python 3.12's mimetypes._suffix_map_default, _encodings_map_default,
// _types_map_default and _common_types_default (in order) with:
//   for name in ("suffix_map", "encodings_map", "types_map", "common_types"):
//       print each (ext, value) pair of getattr(mimetypes, f"_{name}_default")
inline constexpr std::pair<std::string_view, std::string_view> default_suffix_map[] = {
    {".svgz", ".svg.gz"},
    {".tgz", ".tar.gz"},
    {".taz", ".tar.gz"},
    {".tz", ".tar.gz"},
    {".tbz2", ".tar.bz2"},
    {".txz", ".tar.xz"},
};
inline constexpr std::pair<std::string_view, std::string_view> default_encodings_map[] = {
    {".gz", "gzip"},
    {".Z", "compress"},
    {".bz2", "bzip2"},
    {".xz", "xz"},
    {".br", "br"},
};
inline constexpr std::pair<std::string_view, std::string_view> default_types_map[] = {
    {".js", "text/javascript"},
    {".mjs", "text/javascript"},
    {".json", "application/json"},
    {".webmanifest", "application/manifest+json"},
    {".doc", "application/msword"},
    {".dot", "application/msword"},
    {".wiz", "application/msword"},
    {".nq", "application/n-quads"},
    {".nt", "application/n-triples"},
    {".bin", "application/octet-stream"},
    {".a", "application/octet-stream"},
    {".dll", "application/octet-stream"},
    {".exe", "application/octet-stream"},
    {".o", "application/octet-stream"},
    {".obj", "application/octet-stream"},
    {".so", "application/octet-stream"},
    {".oda", "application/oda"},
    {".pdf", "application/pdf"},
    {".p7c", "application/pkcs7-mime"},
    {".ps", "application/postscript"},
    {".ai", "application/postscript"},
    {".eps", "application/postscript"},
    {".trig", "application/trig"},
    {".m3u", "application/vnd.apple.mpegurl"},
    {".m3u8", "application/vnd.apple.mpegurl"},
    {".xls", "application/vnd.ms-excel"},
    {".xlb", "application/vnd.ms-excel"},
    {".ppt", "application/vnd.ms-powerpoint"},
    {".pot", "application/vnd.ms-powerpoint"},
    {".ppa", "application/vnd.ms-powerpoint"},
    {".pps", "application/vnd.ms-powerpoint"},
    {".pwz", "application/vnd.ms-powerpoint"},
    {".wasm", "application/wasm"},
    {".bcpio", "application/x-bcpio"},
    {".cpio", "application/x-cpio"},
    {".csh", "application/x-csh"},
    {".dvi", "application/x-dvi"},
    {".gtar", "application/x-gtar"},
    {".hdf", "application/x-hdf"},
    {".h5", "application/x-hdf5"},
    {".latex", "application/x-latex"},
    {".mif", "application/x-mif"},
    {".cdf", "application/x-netcdf"},
    {".nc", "application/x-netcdf"},
    {".p12", "application/x-pkcs12"},
    {".pfx", "application/x-pkcs12"},
    {".ram", "application/x-pn-realaudio"},
    {".pyc", "application/x-python-code"},
    {".pyo", "application/x-python-code"},
    {".sh", "application/x-sh"},
    {".shar", "application/x-shar"},
    {".swf", "application/x-shockwave-flash"},
    {".sv4cpio", "application/x-sv4cpio"},
    {".sv4crc", "application/x-sv4crc"},
    {".tar", "application/x-tar"},
    {".tcl", "application/x-tcl"},
    {".tex", "application/x-tex"},
    {".texi", "application/x-texinfo"},
    {".texinfo", "application/x-texinfo"},
    {".roff", "application/x-troff"},
    {".t", "application/x-troff"},
    {".tr", "application/x-troff"},
    {".man", "application/x-troff-man"},
    {".me", "application/x-troff-me"},
    {".ms", "application/x-troff-ms"},
    {".ustar", "application/x-ustar"},
    {".src", "application/x-wais-source"},
    {".xsl", "application/xml"},
    {".rdf", "application/xml"},
    {".wsdl", "application/xml"},
    {".xpdl", "application/xml"},
    {".zip", "application/zip"},
    {".3gp", "audio/3gpp"},
    {".3gpp", "audio/3gpp"},
    {".3g2", "audio/3gpp2"},
    {".3gpp2", "audio/3gpp2"},
    {".aac", "audio/aac"},
    {".adts", "audio/aac"},
    {".loas", "audio/aac"},
    {".ass", "audio/aac"},
    {".au", "audio/basic"},
    {".snd", "audio/basic"},
    {".mp3", "audio/mpeg"},
    {".mp2", "audio/mpeg"},
    {".opus", "audio/opus"},
    {".aif", "audio/x-aiff"},
    {".aifc", "audio/x-aiff"},
    {".aiff", "audio/x-aiff"},
    {".ra", "audio/x-pn-realaudio"},
    {".wav", "audio/x-wav"},
    {".avif", "image/avif"},
    {".bmp", "image/bmp"},
    {".gif", "image/gif"},
    {".ief", "image/ief"},
    {".jpg", "image/jpeg"},
    {".jpe", "image/jpeg"},
    {".jpeg", "image/jpeg"},
    {".heic", "image/heic"},
    {".heif", "image/heif"},
    {".png", "image/png"},
    {".svg", "image/svg+xml"},
    {".tiff", "image/tiff"},
    {".tif", "image/tiff"},
    {".ico", "image/vnd.microsoft.icon"},
    {".ras", "image/x-cmu-raster"},
    {".pnm", "image/x-portable-anymap"},
    {".pbm", "image/x-portable-bitmap"},
    {".pgm", "image/x-portable-graymap"},
    {".ppm", "image/x-portable-pixmap"},
    {".rgb", "image/x-rgb"},
    {".xbm", "image/x-xbitmap"},
    {".xpm", "image/x-xpixmap"},
    {".xwd", "image/x-xwindowdump"},
    {".eml", "message/rfc822"},
    {".mht", "message/rfc822"},
    {".mhtml", "message/rfc822"},
    {".nws", "message/rfc822"},
    {".css", "text/css"},
    {".csv", "text/csv"},
    {".html", "text/html"},
    {".htm", "text/html"},
    {".n3", "text/n3"},
    {".txt", "text/plain"},
    {".bat", "text/plain"},
    {".c", "text/plain"},
    {".h", "text/plain"},
    {".ksh", "text/plain"},
    {".pl", "text/plain"},
    {".srt", "text/plain"},
    {".rtx", "text/richtext"},
    {".tsv", "text/tab-separated-values"},
    {".vtt", "text/vtt"},
    {".py", "text/x-python"},
    {".etx", "text/x-setext"},
    {".sgm", "text/x-sgml"},
    {".sgml", "text/x-sgml"},
    {".vcf", "text/x-vcard"},
    {".xml", "text/xml"},
    {".mp4", "video/mp4"},
    {".mpeg", "video/mpeg"},
    {".m1v", "video/mpeg"},
    {".mpa", "video/mpeg"},
    {".mpe", "video/mpeg"},
    {".mpg", "video/mpeg"},
    {".mov", "video/quicktime"},
    {".qt", "video/quicktime"},
    {".webm", "video/webm"},
    {".avi", "video/x-msvideo"},
    {".movie", "video/x-sgi-movie"},
};
inline constexpr std::pair<std::string_view, std::string_view> default_common_types[] = {
    {".rtf", "application/rtf"},
    {".midi", "audio/midi"},
    {".mid", "audio/midi"},
    {".jpg", "image/jpg"},
    {".pict", "image/pict"},
    {".pct", "image/pict"},
    {".pic", "image/pict"},
    {".webp", "image/webp"},
    {".xul", "text/xul"},
};

using Map = dict<std::string, std::string>;

// The database, like Python's module-level MimeTypes: types_map[strict] and its inverse
// (type -> extensions). The module's types_map/common_types/encodings_map/suffix_map are
// these same dicts, so changing one changes what guess_type() sees, as in Python. The
// functions lock it, so threads can guess while another adds a type.
struct Db {
    Map suffix_map, encodings_map, types[2];  // types[true] is types_map, types[false] common_types
    dict<std::string, list<std::string>> inverse[2];
    std::shared_mutex mutex;

    void add(const std::string& type, const std::string& ext, bool strict) {
        types[strict][ext] = type;
        list<std::string> exts = inverse[strict][type];  // (a handle: inserts an empty list if missing)
        if (!has(exts, ext)) exts.push_back(ext);
    }

    static bool has(const list<std::string>& xs, const std::string& x) {
        return std::find(xs.vec().begin(), xs.vec().end(), x) != xs.vec().end();
    }

    Db() {
        for (auto [k, v] : default_suffix_map) suffix_map[std::string(k)] = std::string(v);
        for (auto [k, v] : default_encodings_map) encodings_map[std::string(k)] = std::string(v);
        for (auto [k, v] : default_types_map) add(std::string(v), std::string(k), true);
        for (auto [k, v] : default_common_types) add(std::string(v), std::string(k), false);
    }
};

inline Db& db() {
    static Db instance;
    return instance;
}

inline Map& types_map() { return db().types[true]; }
inline Map& common_types() { return db().types[false]; }
inline Map& encodings_map() { return db().encodings_map; }
inline Map& suffix_map() { return db().suffix_map; }

inline std::string ascii_lower(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// posixpath.splitext: leading dots of the last component don't start an extension.
inline std::pair<std::string, std::string> splitext(const std::string& p) {
    auto slash = p.rfind('/');
    std::size_t start = slash == std::string::npos ? 0 : slash + 1;
    auto dot = p.rfind('.');
    if (dot == std::string::npos || dot < start) return {p, ""};
    for (std::size_t i = start; i < dot; ++i)
        if (p[i] != '.') return {p.substr(0, dot), p.substr(dot)};
    return {p, ""};
}

// urllib.parse's checks of a bracketed host ([::1]) in the netloc: guess_type raises
// the same ValueErrors as Python's urlparse.
inline void check_bracketed_host(const std::string& host) {
    if (!host.empty() && host[0] == 'v') {
        // \Av[a-fA-F0-9]+\..+\Z
        std::size_t i = 1;
        while (i < host.size() && std::isxdigit(static_cast<unsigned char>(host[i]))) ++i;
        if (i == 1 || i >= host.size() || host[i] != '.' || i + 1 >= host.size() ||
            host.find('\n', i + 1) != std::string::npos)
            raise("ValueError", "IPvFuture address is invalid");
        return;
    }
    unsigned char buf[16];
    if (inet_pton(AF_INET, host.c_str(), buf) == 1) raise("ValueError", "An IPv4 address cannot be in brackets");
    std::string addr = host;
    if (auto pct = host.find('%'); pct != std::string::npos) {  // a scope id: fe80::1%eth0
        std::string scope = host.substr(pct + 1);
        if (scope.empty() || scope.find('%') != std::string::npos || scope.find('/') != std::string::npos) addr = "";
        else addr = host.substr(0, pct);
    }
    if (addr.empty() || inet_pton(AF_INET6, addr.c_str(), buf) != 1)
        raise("ValueError", repr_str(host) + " does not appear to be an IPv4 or IPv6 address");
}

inline void check_netloc(const std::string& netloc) {
    bool open = netloc.find('[') != std::string::npos, close = netloc.find(']') != std::string::npos;
    if (open != close) raise("ValueError", "Invalid IPv6 URL");
    if (!open) return;
    auto at = netloc.rfind('@');
    std::string hostport = at == std::string::npos ? netloc : netloc.substr(at + 1);
    auto bracket = hostport.find('[');
    if (bracket == std::string::npos) {
        check_bracketed_host(hostport.substr(0, hostport.find(':')));
        return;
    }
    if (bracket > 0) raise("ValueError", "Invalid IPv6 URL");
    std::string bracketed = hostport.substr(bracket + 1);
    auto end = bracketed.find(']');
    std::string host = bracketed.substr(0, end);
    std::string port = end == std::string::npos ? "" : bracketed.substr(end + 1);
    if (!port.empty() && port[0] != ':') raise("ValueError", "Invalid IPv6 URL");
    check_bracketed_host(host);
}

// What guess_type needs of urllib.parse.urlparse(url): the scheme and the path (Python's
// urlparse, including its URL cleanup and netloc errors).
inline std::pair<std::string, std::string> scheme_and_path(std::string url) {
    std::size_t skip = 0;
    while (skip < url.size() && static_cast<unsigned char>(url[skip]) <= ' ') ++skip;
    url.erase(0, skip);
    std::erase_if(url, [](char c) { return c == '\t' || c == '\r' || c == '\n'; });
    std::string scheme;
    auto colon = url.find(':');
    if (colon != std::string::npos && colon > 0 && std::isalpha(static_cast<unsigned char>(url[0])) &&
        std::all_of(url.begin(), url.begin() + static_cast<std::ptrdiff_t>(colon), [](char c) {
            return std::isalnum(static_cast<unsigned char>(c)) || c == '+' || c == '-' || c == '.';
        })) {
        scheme = ascii_lower(url.substr(0, colon));
        url = url.substr(colon + 1);
    }
    if (url.starts_with("//")) {
        auto end = url.find_first_of("/?#", 2);
        check_netloc(url.substr(2, end == std::string::npos ? std::string::npos : end - 2));
        url = end == std::string::npos ? "" : url.substr(end);
    }
    url = url.substr(0, url.find('#'));
    url = url.substr(0, url.find('?'));
    static const std::set<std::string> uses_params = {"",     "ftp",  "hdl",   "prospero", "http", "imap",
                                                      "https", "shttp", "rtsp", "rtsps",    "rtspu", "sip",
                                                      "sips",  "mms",  "sftp", "tel"};
    if (uses_params.count(scheme)) {
        auto slash = url.rfind('/');
        auto semi = url.find(';', slash == std::string::npos ? 0 : slash);
        if (semi != std::string::npos) url = url.substr(0, semi);
    }
    return {scheme, url};
}

using Guess = std::tuple<std::optional<std::string>, std::optional<std::string>>;

inline Guess guess_type(const std::string& original, bool strict = true) {
    auto [scheme, path] = scheme_and_path(original);
    std::string url = scheme.size() > 1 ? path : original;
    if (scheme.size() > 1 && scheme == "data") {
        // data:[<mediatype>][;base64],<data>; the type defaults to text/plain
        auto comma = url.find(',');
        if (comma == std::string::npos) return {std::nullopt, std::nullopt};
        auto semi = url.substr(0, comma).find(';');
        std::string type = url.substr(0, semi == std::string::npos ? comma : semi);
        if (type.find('=') != std::string::npos || type.find('/') == std::string::npos) type = "text/plain";
        return {type, std::nullopt};
    }
    Db& d = db();
    std::shared_lock lock(d.mutex);
    auto [base, ext] = splitext(url);
    while (true) {
        const std::string* suffix = d.suffix_map.find(ascii_lower(ext));
        if (!suffix) break;
        std::tie(base, ext) = splitext(base + *suffix);
    }
    std::optional<std::string> encoding;
    if (const std::string* found = d.encodings_map.find(ext)) {  // case-sensitive
        encoding = *found;
        std::tie(base, ext) = splitext(base);
    }
    ext = ascii_lower(ext);
    if (const std::string* type = d.types[true].find(ext)) return {*type, encoding};
    if (strict) return {std::nullopt, encoding};
    if (const std::string* type = d.types[false].find(ext)) return {*type, encoding};
    return {std::nullopt, encoding};
}

inline Guess guess_type(const pathlib::Path& path, bool strict = true) { return guess_type(path.str(), strict); }

inline list<std::string> guess_all_extensions(const std::string& type, bool strict = true) {
    Db& d = db();
    std::shared_lock lock(d.mutex);
    std::string lowered = ascii_lower(type);
    list<std::string> out;
    if (const auto* exts = d.inverse[true].find(lowered)) out = exts->copy();
    if (!strict) {
        if (const auto* exts = d.inverse[false].find(lowered))
            for (const auto& ext : exts->vec())
                if (!Db::has(out, ext)) out.push_back(ext);
    }
    return out;
}

inline std::optional<std::string> guess_extension(const std::string& type, bool strict = true) {
    list<std::string> exts = guess_all_extensions(type, strict);
    if (exts.empty()) return std::nullopt;
    return exts[0];
}

inline void add_type(const std::string& type, const std::string& ext, bool strict = true) {
    Db& d = db();
    std::unique_lock lock(d.mutex);
    d.add(type, ext, strict);
}

}  // namespace sd::mimetypes
