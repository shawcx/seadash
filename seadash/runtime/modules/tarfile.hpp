// The `tarfile` module: tar archives, read and written (USTAR, GNU and PAX formats; gzip,
// bzip2 and xz compression; the streaming "r|gz" and "w|gz" modes), ported from CPython's
// tarfile.py so the archives, the extraction filters and the errors are Python's.
// Not supported: sparse files' contents (a sparse member reads as its stored data), encodings
// other than UTF-8, Zstandard, and subclassing TarFile or TarInfo.
#pragma once

#include <bzlib.h>
#include <grp.h>
#include <lzma.h>
#include <pwd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <zlib.h>

#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <format>
#include <map>
#include <memory>
#include <optional>
#include <string_view>
#include <vector>

#include "pathlib.hpp"
#include "stat.hpp"

namespace sd::tarfile {

inline constexpr std::size_t BLOCKSIZE = 512, RECORDSIZE = BLOCKSIZE * 20;
inline constexpr std::int64_t USTAR_FORMAT = 0, GNU_FORMAT = 1, PAX_FORMAT = 2, DEFAULT_FORMAT = PAX_FORMAT;
inline const std::string GNU_MAGIC("ustar  \0", 8), POSIX_MAGIC("ustar\x00" "00", 8);
inline constexpr std::size_t LENGTH_NAME = 100, LENGTH_LINK = 100, LENGTH_PREFIX = 155;
// Member types (TarInfo.type is one byte, as bytes)
inline const std::string REGTYPE = "0", AREGTYPE(1, '\0'), LNKTYPE = "1", SYMTYPE = "2", CHRTYPE = "3", BLKTYPE = "4",
                         DIRTYPE = "5", FIFOTYPE = "6", CONTTYPE = "7", GNUTYPE_LONGNAME = "L", GNUTYPE_LONGLINK = "K",
                         GNUTYPE_SPARSE = "S", XHDTYPE = "x", XGLTYPE = "g", SOLARIS_XHDTYPE = "X";
inline const std::string ENCODING = "utf-8";

inline bool in(const std::string& t, std::initializer_list<const std::string*> types) {
    return std::any_of(types.begin(), types.end(), [&](const std::string* x) { return *x == t; });
}
inline bool supported_type(const std::string& t) {
    return in(t, {&REGTYPE, &AREGTYPE, &LNKTYPE, &SYMTYPE, &DIRTYPE, &FIFOTYPE, &CONTTYPE, &CHRTYPE, &BLKTYPE,
                  &GNUTYPE_LONGNAME, &GNUTYPE_LONGLINK, &GNUTYPE_SPARSE});
}
inline bool regular_type(const std::string& t) { return in(t, {&REGTYPE, &AREGTYPE, &CONTTYPE, &GNUTYPE_SPARSE}); }
inline bool gnu_type(const std::string& t) { return in(t, {&GNUTYPE_LONGNAME, &GNUTYPE_LONGLINK, &GNUTYPE_SPARSE}); }

// ---- exceptions ------------------------------------------------------------------------

#define SD_TAR_ERROR(Name, Base)                                                   \
    struct Name : Base {                                                           \
        using Base::Base;                                                          \
        std::string sd_type() const override { return "tarfile." #Name; }          \
    };
SD_TAR_ERROR(TarError, Exception)
SD_TAR_ERROR(ExtractError, TarError)
SD_TAR_ERROR(ReadError, TarError)
SD_TAR_ERROR(CompressionError, TarError)
SD_TAR_ERROR(StreamError, TarError)
SD_TAR_ERROR(HeaderError, TarError)
SD_TAR_ERROR(EmptyHeaderError, HeaderError)
SD_TAR_ERROR(TruncatedHeaderError, HeaderError)
SD_TAR_ERROR(EOFHeaderError, HeaderError)
SD_TAR_ERROR(InvalidHeaderError, HeaderError)
SD_TAR_ERROR(SubsequentHeaderError, HeaderError)
SD_TAR_ERROR(FilterError, TarError)
SD_TAR_ERROR(AbsolutePathError, FilterError)
SD_TAR_ERROR(OutsideDestinationError, FilterError)
SD_TAR_ERROR(SpecialFileError, FilterError)
SD_TAR_ERROR(AbsoluteLinkError, FilterError)
SD_TAR_ERROR(LinkOutsideDestinationError, FilterError)
SD_TAR_ERROR(LinkFallbackError, FilterError)
#undef SD_TAR_ERROR

template <class E>
[[noreturn]] void fail(const std::string& msg) {
    throw Thrown{std::make_shared<E>(msg)};
}

// ---- header fields ---------------------------------------------------------------------

// stn: a string as a NUL-padded field
inline std::string stn(const std::string& s, std::size_t length) {
    std::string out = s.substr(0, length);
    out.resize(length, '\0');
    return out;
}
// nts: a NUL-terminated field as a string
inline std::string nts(std::string_view s) {
    auto p = s.find('\0');
    return std::string(p == std::string_view::npos ? s : s.substr(0, p));
}
// nti: a number field (octal digits, or GNU's base-256)
inline std::int64_t nti(std::string_view s) {
    auto first = static_cast<unsigned char>(s[0]);
    if (first == 0200 || first == 0377) {
        std::int64_t n = 0;
        for (std::size_t i = 1; i < s.size(); ++i) n = (n << 8) + static_cast<unsigned char>(s[i]);
        if (first == 0377) n -= static_cast<std::int64_t>(1) << (8 * (s.size() - 1));
        return n;
    }
    std::string digits = nts(s);
    auto b = digits.find_first_not_of(" \t\n\r\f\v"), e = digits.find_last_not_of(" \t\n\r\f\v");
    digits = b == std::string::npos ? "" : digits.substr(b, e - b + 1);
    if (digits.empty()) return 0;
    std::int64_t n = 0;
    for (char c : digits) {
        if (c < '0' || c > '7') fail<InvalidHeaderError>("invalid header");
        n = n * 8 + (c - '0');
    }
    return n;
}
// itn: a number as a field of `digits` bytes (octal, or GNU's base-256 in the GNU format)
inline std::string itn(std::int64_t n, std::size_t digits, std::int64_t format) {
    double limit = std::pow(8.0, static_cast<double>(digits - 1));
    if (n >= 0 && static_cast<double>(n) < limit) return std::format("{:0{}o}", n, digits - 1) + '\0';
    if (format == GNU_FORMAT) {  // (a 64-bit value always fits in 7 or 11 bytes; sign-extended)
        std::string s(digits, n < 0 ? '\377' : '\0');
        s[0] = static_cast<char>(n >= 0 ? 0200 : 0377);
        auto u = static_cast<std::uint64_t>(n);
        for (std::size_t i = digits - 1, k = 0; i >= 1 && k < 8; --i, ++k) {
            s[i] = static_cast<char>(u & 0377);
            u >>= 8;
        }
        return s;
    }
    raise("ValueError", "overflow in number field");
}
inline std::pair<std::int64_t, std::int64_t> calc_chksums(std::string_view buf) {
    std::int64_t u = 256, sgn = 256;
    for (std::size_t i = 0; i < BLOCKSIZE; ++i) {
        if (i >= 148 && i < 156) continue;
        u += static_cast<unsigned char>(buf[i]);
        sgn += static_cast<signed char>(buf[i]);
    }
    return {u, sgn};
}
inline std::int64_t block(std::int64_t count) {  // rounded up to whole blocks
    if (count < 0) fail<InvalidHeaderError>("invalid offset");
    return (count + static_cast<std::int64_t>(BLOCKSIZE) - 1) / static_cast<std::int64_t>(BLOCKSIZE) *
           static_cast<std::int64_t>(BLOCKSIZE);
}
inline std::string pad_payload(std::string payload) {
    if (auto r = payload.size() % BLOCKSIZE) payload.append(BLOCKSIZE - r, '\0');
    return payload;
}
// s.encode("ascii", "replace"): each other character a '?'
inline std::string ascii_replace(const std::string& s) {
    std::string out;
    for (std::size_t i = 0; i < s.size();) {
        char32_t c = 0;
        std::size_t width = utf8_at(s, i, c);
        out += width == 1 ? s[i] : '?';
        i += width ? width : 1;
    }
    return out;
}
inline bool is_ascii(const std::string& s) {
    return std::all_of(s.begin(), s.end(), [](char c) { return static_cast<unsigned char>(c) < 0x80; });
}

// ---- paths (POSIX) ----------------------------------------------------------------------

inline std::string normpath(const std::string& path) {
    if (path.empty()) return ".";
    std::size_t slashes = path.starts_with('/') ? (path.starts_with("//") && !path.starts_with("///") ? 2 : 1) : 0;
    std::vector<std::string> parts;
    std::size_t start = 0;
    while (start <= path.size()) {
        std::size_t end = path.find('/', start);
        if (end == std::string::npos) end = path.size();
        std::string comp = path.substr(start, end - start);
        start = end + 1;
        if (comp.empty() || comp == ".") continue;
        if (comp != ".." || (!slashes && parts.empty()) || (!parts.empty() && parts.back() == "..")) parts.push_back(comp);
        else if (!parts.empty()) parts.pop_back();
    }
    std::string out(slashes, '/');
    for (std::size_t i = 0; i < parts.size(); ++i) out += (i ? "/" : "") + parts[i];
    return out.empty() ? "." : out;
}
inline std::string join(const std::string& a, const std::string& b) {
    if (b.starts_with('/')) return b;
    if (a.empty() || a.ends_with('/')) return a + b;
    return a + "/" + b;
}
inline std::string dirname(const std::string& p) {
    auto i = p.rfind('/');
    if (i == std::string::npos) return "";
    std::string head = p.substr(0, i + 1);
    if (head.find_first_not_of('/') != std::string::npos)
        while (head.ends_with('/')) head.pop_back();
    return head;
}
inline std::string abspath(const std::string& p) {
    return normpath(p.starts_with('/') ? p : join(std::filesystem::current_path().string(), p));
}
// os.path.realpath(p, strict=ALLOW_MISSING): the symlinks of the part that exists resolved
inline std::string realpath(const std::string& p) {
    std::error_code ec;
    auto r = std::filesystem::weakly_canonical(abspath(p), ec);
    std::string s = ec ? abspath(p) : r.string();
    while (s.size() > 1 && s.ends_with('/')) s.pop_back();
    return s;
}
inline bool inside(const std::string& path, const std::string& dir) {  // commonpath([path, dir]) == dir
    if (dir == "/") return path.starts_with('/');
    return path == dir || path.starts_with(dir + "/");
}
inline bool lexists(const std::string& p) {
    struct stat st;
    return ::lstat(p.c_str(), &st) == 0;
}
inline bool isdir(const std::string& p) {
    struct stat st;
    return ::stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

// ---- TarInfo ---------------------------------------------------------------------------

struct Info {
    std::string name;
    std::optional<std::int64_t> mode = 0644;  // (a filter may make these None: left as they are)
    std::optional<std::int64_t> uid = 0, gid = 0;
    std::int64_t size = 0;
    double mtime = 0;
    bool mtime_float = false;  // (os.stat's float mtime: in a PAX header in full)
    std::int64_t chksum = 0;
    std::string type = REGTYPE, linkname;
    std::optional<std::string> uname = "", gname = "";
    std::int64_t devmajor = 0, devminor = 0, offset = 0, offset_data = 0;
    dict<std::string, std::string> pax_headers;
    bool sparse = false;
    std::optional<std::string> link_target;  // (for a hard link being extracted)
    // (sparse structs read from an old GNU sparse header)
    bool sparse_extended = false;
    std::int64_t sparse_origsize = 0;
};

struct TarInfo {
    std::shared_ptr<Info> d;
    TarInfo() = default;  // (unset until it's assigned)
    explicit TarInfo(std::shared_ptr<Info> info) : d(std::move(info)) {}

    std::string name() const { return d->name; }
    void set_name(std::string v) { d->name = std::move(v); }
    std::string path() const { return d->name; }
    void set_path(std::string v) { d->name = std::move(v); }
    std::int64_t mode() const { return d->mode.value_or(0); }
    void set_mode(std::int64_t v) { d->mode = v; }
    std::int64_t uid() const { return d->uid.value_or(0); }
    void set_uid(std::int64_t v) { d->uid = v; }
    std::int64_t gid() const { return d->gid.value_or(0); }
    void set_gid(std::int64_t v) { d->gid = v; }
    std::int64_t size() const { return d->size; }
    void set_size(std::int64_t v) { d->size = v; }
    double mtime() const { return d->mtime; }
    void set_mtime(double v) {
        d->mtime = v;
        d->mtime_float = v != std::floor(v);
    }
    std::int64_t chksum() const { return d->chksum; }
    void set_chksum(std::int64_t v) { d->chksum = v; }
    bytes type() const { return bytes(d->type); }
    void set_type(bytes v) { d->type = v.data; }
    std::string linkname() const { return d->linkname; }
    void set_linkname(std::string v) { d->linkname = std::move(v); }
    std::string linkpath() const { return d->linkname; }
    void set_linkpath(std::string v) { d->linkname = std::move(v); }
    std::string uname() const { return d->uname.value_or(""); }
    void set_uname(std::string v) { d->uname = std::move(v); }
    std::string gname() const { return d->gname.value_or(""); }
    void set_gname(std::string v) { d->gname = std::move(v); }
    std::int64_t devmajor() const { return d->devmajor; }
    void set_devmajor(std::int64_t v) { d->devmajor = v; }
    std::int64_t devminor() const { return d->devminor; }
    void set_devminor(std::int64_t v) { d->devminor = v; }
    std::int64_t offset() const { return d->offset; }
    void set_offset(std::int64_t v) { d->offset = v; }
    std::int64_t offset_data() const { return d->offset_data; }
    void set_offset_data(std::int64_t v) { d->offset_data = v; }
    dict<std::string, std::string> pax_headers() const { return d->pax_headers; }
    void set_pax_headers(dict<std::string, std::string> v) { d->pax_headers = std::move(v); }

    bool isreg() const { return regular_type(d->type); }
    bool isfile() const { return isreg(); }
    bool isdir() const { return d->type == DIRTYPE; }
    bool issym() const { return d->type == SYMTYPE; }
    bool islnk() const { return d->type == LNKTYPE; }
    bool ischr() const { return d->type == CHRTYPE; }
    bool isblk() const { return d->type == BLKTYPE; }
    bool isfifo() const { return d->type == FIFOTYPE; }
    bool issparse() const { return d->sparse; }
    bool isdev() const { return ischr() || isblk() || isfifo(); }

    TarInfo copy() const { return TarInfo(std::make_shared<Info>(*d)); }
    // ti.replace(name=..., mtime=..., ...): a copy with those changed
    TarInfo replace(std::optional<std::string> name = std::nullopt, std::optional<double> mtime = std::nullopt,
                    std::optional<std::int64_t> mode = std::nullopt, std::optional<std::string> linkname = std::nullopt,
                    std::optional<std::int64_t> uid = std::nullopt, std::optional<std::int64_t> gid = std::nullopt,
                    std::optional<std::string> uname = std::nullopt, std::optional<std::string> gname = std::nullopt,
                    bool deep = true) const {
        (void)deep;
        TarInfo out = copy();
        if (name) out.d->name = *name;
        if (mtime) out.set_mtime(*mtime);
        if (mode) out.d->mode = *mode;
        if (linkname) out.d->linkname = *linkname;
        if (uid) out.d->uid = *uid;
        if (gid) out.d->gid = *gid;
        if (uname) out.d->uname = *uname;
        if (gname) out.d->gname = *gname;
        return out;
    }

    std::string sd_repr() const {
        return std::format("<TarInfo {} at {:#x}>", repr_str(d->name), reinterpret_cast<std::uintptr_t>(d.get()));
    }
    bool operator==(const TarInfo& o) const { return d == o.d; }

    bytes tobuf(std::int64_t format = DEFAULT_FORMAT, const std::string& encoding = ENCODING,
                const std::string& errors = "surrogateescape") const;
};

// zipfile.TarInfo(name="")
inline TarInfo make_info(const std::string& name = "") {
    auto d = std::make_shared<Info>();
    d->name = name;
    return TarInfo(std::move(d));
}

// The header fields, as TarInfo.get_info gives them to the header builders.
struct HeaderInfo {
    std::string name, linkname, uname, gname, type, magic = POSIX_MAGIC, prefix;
    std::int64_t mode = 0, uid = 0, gid = 0, size = 0, mtime = 0, devmajor = 0, devminor = 0;
};

inline std::string create_header(const HeaderInfo& info, std::int64_t format) {
    bool device = info.type == CHRTYPE || info.type == BLKTYPE;
    std::string buf;
    buf += stn(info.name, 100);
    buf += itn(info.mode & 07777, 8, format);
    buf += itn(info.uid, 8, format);
    buf += itn(info.gid, 8, format);
    buf += itn(info.size, 12, format);
    buf += itn(info.mtime, 12, format);
    buf += "        ";  // the checksum, for now
    buf += info.type;
    buf += stn(info.linkname, 100);
    buf += info.magic;
    buf += stn(info.uname, 32);
    buf += stn(info.gname, 32);
    buf += device ? itn(info.devmajor, 8, format) : stn("", 8);
    buf += device ? itn(info.devminor, 8, format) : stn("", 8);
    buf += stn(info.prefix, 155);
    buf.resize(BLOCKSIZE, '\0');
    std::string chksum = std::format("{:06o}", calc_chksums(buf).first);
    buf.replace(148, 7, chksum + '\0');
    return buf;
}

inline std::string create_gnu_long_header(const std::string& name, const std::string& type) {
    std::string payload = name + '\0';
    HeaderInfo info;
    info.name = "././@LongLink";
    info.type = type;
    info.size = static_cast<std::int64_t>(payload.size());
    info.magic = GNU_MAGIC;
    return create_header(info, USTAR_FORMAT) + pad_payload(payload);
}

inline std::string create_pax_generic_header(const dict<std::string, std::string>& pax_headers, const std::string& type) {
    std::string records;
    for (const auto& [keyword, value] : pax_headers) {
        std::size_t l = keyword.size() + value.size() + 3;  // ' ' + '=' + '\n'
        std::size_t n = 0, p = 0;
        for (;;) {
            n = l + std::to_string(p).size();
            if (n == p) break;
            p = n;
        }
        records += std::to_string(p) + " " + keyword + "=" + value + "\n";
    }
    HeaderInfo info;
    info.name = "././@PaxHeader";
    info.type = type;
    info.size = static_cast<std::int64_t>(records.size());
    info.magic = POSIX_MAGIC;
    return create_header(info, USTAR_FORMAT) + pad_payload(records);
}

inline std::pair<std::string, std::string> posix_split_name(const std::string& name) {
    std::vector<std::string> components;
    std::size_t start = 0;
    for (;;) {
        auto slash = name.find('/', start);
        components.push_back(name.substr(start, slash == std::string::npos ? std::string::npos : slash - start));
        if (slash == std::string::npos) break;
        start = slash + 1;
    }
    for (std::size_t i = 1; i < components.size(); ++i) {
        std::string prefix, rest;
        for (std::size_t k = 0; k < components.size(); ++k) (k < i ? prefix : rest) += (k == 0 || k == i ? "" : "/") + components[k];
        if (prefix.size() <= LENGTH_PREFIX && rest.size() <= LENGTH_NAME) return {prefix, rest};
    }
    raise("ValueError", "name is too long");
}

inline bytes TarInfo::tobuf(std::int64_t format, const std::string& encoding, const std::string& errors) const {
    (void)encoding;
    (void)errors;
    const Info& z = *d;
    auto none = [](const char* field) { raise("ValueError", std::string(field) + " may not be None"); };
    if (!z.mode) none("mode");
    if (!z.uid) none("uid");
    if (!z.gid) none("gid");
    if (!z.uname) none("uname");
    if (!z.gname) none("gname");
    HeaderInfo info;
    info.name = z.name;
    info.mode = *z.mode & 07777;
    info.uid = *z.uid;
    info.gid = *z.gid;
    info.size = z.size;
    info.mtime = static_cast<std::int64_t>(z.mtime);  // (itn truncates, as Python's int() does)
    info.type = z.type;
    info.linkname = z.linkname;
    info.uname = *z.uname;
    info.gname = *z.gname;
    info.devmajor = z.devmajor;
    info.devminor = z.devminor;
    if (info.type == DIRTYPE && !info.name.ends_with('/')) info.name += '/';
    if (format == USTAR_FORMAT) {
        info.magic = POSIX_MAGIC;
        if (info.linkname.size() > LENGTH_LINK) raise("ValueError", "linkname is too long");
        if (info.name.size() > LENGTH_NAME) std::tie(info.prefix, info.name) = posix_split_name(info.name);
        return bytes(create_header(info, USTAR_FORMAT));
    }
    if (format == GNU_FORMAT) {
        info.magic = GNU_MAGIC;
        std::string buf;
        if (info.linkname.size() > LENGTH_LINK) buf += create_gnu_long_header(info.linkname, GNUTYPE_LONGLINK);
        if (info.name.size() > LENGTH_NAME) buf += create_gnu_long_header(info.name, GNUTYPE_LONGNAME);
        return bytes(buf + create_header(info, GNU_FORMAT));
    }
    if (format != PAX_FORMAT) raise("ValueError", "invalid format");
    // PAX: what the ustar header can't hold goes in an extended header before it
    info.magic = POSIX_MAGIC;
    dict<std::string, std::string> pax = z.pax_headers.copy();
    struct Text {
        std::string* field;
        const char* hname;
        std::size_t length;
    };
    for (auto [field, hname, length] : {Text{&info.name, "path", LENGTH_NAME}, Text{&info.linkname, "linkpath", LENGTH_LINK},
                                        Text{&info.uname, "uname", 32}, Text{&info.gname, "gname", 32}}) {
        if (pax.contains(hname)) continue;
        if (!is_ascii(*field) || code_points(*field) > length) pax[hname] = *field;
    }
    struct Number {
        std::int64_t* field;
        const char* name;
        std::size_t digits;
        bool is_float;
        double exact;
    };
    for (auto [field, name, digits, is_float, exact] :
         {Number{&info.uid, "uid", 8, false, 0}, Number{&info.gid, "gid", 8, false, 0},
          Number{&info.size, "size", 12, false, 0}, Number{&info.mtime, "mtime", 12, z.mtime_float, z.mtime}}) {
        bool needs_pax = false;
        std::int64_t val = is_float ? static_cast<std::int64_t>(std::nearbyint(exact)) : (std::string(name) == "mtime" ? static_cast<std::int64_t>(z.mtime) : *field);
        if (val < 0 || static_cast<double>(val) >= std::pow(8.0, static_cast<double>(digits - 1))) {
            *field = 0;
            needs_pax = true;
        } else if (is_float) {
            *field = val;
            needs_pax = true;
        }
        if (needs_pax && !pax.contains(name))
            pax[name] = is_float ? float_repr(exact) : std::to_string(std::string(name) == "mtime" ? static_cast<std::int64_t>(z.mtime) : val);
    }
    std::string buf = pax.empty() ? std::string() : create_pax_generic_header(pax, XHDTYPE);
    for (std::string* field : {&info.name, &info.linkname, &info.uname, &info.gname}) *field = ascii_replace(*field);
    return bytes(buf + create_header(info, USTAR_FORMAT));
}

// ---- compression: file objects that (de)compress as they go -------------------------------

// Reading: what's decompressed so far, more on demand; seeking back starts again from the
// start (unless it's a stream, "r|gz", which can't go back).
struct Decoder {
    virtual ~Decoder() = default;
    virtual void reset() = 0;
    // Decompresses from in (moving it on) into out; true at the end of a stream.
    virtual bool step(std::string_view& in, std::string& out) = 0;
    virtual bool framed() const { return true; }  // (ending mid-stream is an error)
};

struct GzipDecoder final : Decoder {  // gzip members, one after another (zlib reads the headers)
    z_stream zs{};
    bool started = false;
    ~GzipDecoder() override {
        if (started) inflateEnd(&zs);
    }
    void reset() override {
        if (started) inflateEnd(&zs);
        zs = z_stream{};
        if (inflateInit2(&zs, 16 + MAX_WBITS) != Z_OK) raise("MemoryError", "");
        started = true;
    }
    bool step(std::string_view& in, std::string& out) override {
        if (!started) reset();
        char chunk[64 * 1024];
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
        zs.avail_in = static_cast<uInt>(std::min<std::size_t>(in.size(), 1u << 30));
        zs.next_out = reinterpret_cast<Bytef*>(chunk);
        zs.avail_out = sizeof chunk;
        uInt before = zs.avail_in;
        int rc = inflate(&zs, Z_NO_FLUSH);
        in.remove_prefix(before - zs.avail_in);
        out.append(chunk, sizeof chunk - zs.avail_out);
        if (rc == Z_STREAM_END) {
            reset();
            return true;
        }
        if (rc != Z_OK && rc != Z_BUF_ERROR) {
            std::string why = zs.msg ? zs.msg : "invalid data";
            if (why == "incorrect header check") raise("OSError", "Not a gzipped file");
            raise("OSError", "Error " + std::to_string(rc) + " while decompressing data: " + why);
        }
        return false;
    }
};

struct Bz2Decoder final : Decoder {
    bz_stream bs{};
    bool started = false;
    ~Bz2Decoder() override {
        if (started) BZ2_bzDecompressEnd(&bs);
    }
    void reset() override {
        if (started) BZ2_bzDecompressEnd(&bs);
        bs = bz_stream{};
        if (BZ2_bzDecompressInit(&bs, 0, 0) != BZ_OK) raise("MemoryError", "");
        started = true;
    }
    bool step(std::string_view& in, std::string& out) override {
        if (!started) reset();
        char chunk[64 * 1024];
        bs.next_in = const_cast<char*>(in.data());
        bs.avail_in = static_cast<unsigned>(std::min<std::size_t>(in.size(), 1u << 30));
        bs.next_out = chunk;
        bs.avail_out = sizeof chunk;
        unsigned before = bs.avail_in;
        int rc = BZ2_bzDecompress(&bs);
        in.remove_prefix(before - bs.avail_in);
        out.append(chunk, sizeof chunk - bs.avail_out);
        if (rc == BZ_STREAM_END) {
            reset();
            return true;
        }
        if (rc != BZ_OK) raise("OSError", "Invalid data stream");
        return false;
    }
};

struct XzDecoder final : Decoder {
    lzma_stream ls = LZMA_STREAM_INIT;
    bool started = false;
    ~XzDecoder() override { lzma_end(&ls); }
    void reset() override {
        lzma_end(&ls);
        ls = LZMA_STREAM_INIT;
        if (lzma_auto_decoder(&ls, UINT64_MAX, 0) != LZMA_OK) raise("MemoryError", "");
        started = true;
    }
    bool step(std::string_view& in, std::string& out) override {
        if (!started) reset();
        std::uint8_t chunk[64 * 1024];
        ls.next_in = reinterpret_cast<const std::uint8_t*>(in.data());
        ls.avail_in = in.size();
        ls.next_out = chunk;
        ls.avail_out = sizeof chunk;
        std::size_t before = ls.avail_in;
        lzma_ret rc = lzma_code(&ls, LZMA_RUN);
        in.remove_prefix(before - ls.avail_in);
        out.append(reinterpret_cast<const char*>(chunk), sizeof chunk - ls.avail_out);
        if (rc == LZMA_STREAM_END) {
            reset();
            return true;
        }
        if (rc == LZMA_FORMAT_ERROR) raise("OSError", "Input format not supported by decoder");
        if (rc == LZMA_DATA_ERROR) raise("OSError", "Corrupt input data");
        if (rc != LZMA_OK && rc != LZMA_BUF_ERROR) raise("OSError", "Error " + std::to_string(rc) + " while decompressing data");
        return false;
    }
};

struct PlainDecoder final : Decoder {  // (no compression: what's read as it is)
    void reset() override {}
    bool framed() const override { return false; }
    bool step(std::string_view& in, std::string& out) override {
        out += in;
        in = {};
        return false;
    }
};

struct DecompressingFile final : BinaryFile {
    std::shared_ptr<BinaryFile> src;
    bool owns_src;  // (closed with it)
    std::unique_ptr<Decoder> dec;
    bool stream;    // (no seeking back)
    std::int64_t src_start, pos = 0;
    std::string pending, buf;  // compressed not yet decoded; decoded not yet read
    bool src_done = false, in_stream = false, open = true;

    DecompressingFile(std::shared_ptr<BinaryFile> s, bool owns, std::unique_ptr<Decoder> d, bool is_stream, std::string head = "")
        : BinaryFile(nullptr, "", "rb"), src(std::move(s)), owns_src(owns), dec(std::move(d)), stream(is_stream) {
        src_start = stream ? 0 : src->tell();
        pending = std::move(head);
    }
    ~DecompressingFile() override {
        if (owns_src && src) src->close();
    }
    void fill(std::size_t want) {
        while (buf.size() < want) {
            if (pending.empty()) {
                if (src_done) {
                    if (in_stream) raise("EOFError", "Compressed file ended before the end-of-stream marker was reached");
                    return;
                }
                pending = src->read_raw(64 * 1024);
                if (pending.empty()) {
                    src_done = true;
                    continue;
                }
            }
            std::string_view in(pending);
            in_stream = dec->framed();
            bool ended = dec->step(in, buf);
            if (ended) {
                in_stream = false;
                std::size_t k = 0;  // (padding after a member: zeros)
                while (k < in.size() && in[k] == '\0') ++k;
                in.remove_prefix(k);
            }
            pending = std::string(in);
        }
    }
    std::string read_raw(std::int64_t n) override {
        if (!open) raise("ValueError", "I/O operation on closed file.");
        if (n < 0) {
            fill(SIZE_MAX);
            std::string out = std::move(buf);
            buf.clear();
            pos += static_cast<std::int64_t>(out.size());
            return out;
        }
        fill(static_cast<std::size_t>(n));
        std::string out = buf.substr(0, static_cast<std::size_t>(n));
        buf.erase(0, out.size());
        pos += static_cast<std::int64_t>(out.size());
        return out;
    }
    std::int64_t tell() override { return pos; }
    std::int64_t seek(std::int64_t to, std::int64_t whence = 0) override {
        if (whence == 1) to += pos;
        else if (whence == 2) raise("OSError", "seek from the end is not supported");
        if (to < pos) {
            if (stream) fail<StreamError>("seeking backwards is not allowed");
            src->seek(src_start, 0);
            dec->reset();
            pending.clear();
            buf.clear();
            src_done = in_stream = false;
            pos = 0;
        }
        while (pos < to) {
            auto got = read_raw(std::min<std::int64_t>(to - pos, 1 << 20));
            if (got.empty()) break;
        }
        return pos;
    }
    std::int64_t write_raw(const std::string&) override { raise("OSError", "write"); }
    void close() override {
        open = false;
        if (owns_src && src) src->close();
        src = nullptr;
    }
    void check_open() const override {
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }
    void flush() override {}
    bool is_closed() const override { return !open; }
    bool readable() const override { return true; }
    bool writable() const override { return false; }
    bool seekable() const override { return !stream; }
    bool isatty() const override { return false; }
    std::int64_t fileno_() const override { return src->fileno_(); }
    std::int64_t truncate(std::optional<std::int64_t> = std::nullopt) override { raise("OSError", "truncate"); }
};

// Writing: compressed as it's written, finished (trailer and all) when it's closed.
struct Encoder {
    virtual ~Encoder() = default;
    virtual std::string compress(std::string_view data) = 0;
    virtual std::string finish() = 0;
};

struct DeflateEncoder final : Encoder {
    z_stream zs{};
    std::uint32_t crc = 0;
    std::uint64_t size = 0;
    std::string header;
    explicit DeflateEncoder(int level, std::string gzip_header) : header(std::move(gzip_header)) {
        if (deflateInit2(&zs, level, Z_DEFLATED, -MAX_WBITS, 8, 0) != Z_OK) raise("ValueError", "Invalid initialization option");
    }
    ~DeflateEncoder() override { deflateEnd(&zs); }
    std::string run(std::string_view data, int flush) {
        std::string out = std::exchange(header, "");
        char chunk[64 * 1024];
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(data.data()));
        zs.avail_in = static_cast<uInt>(data.size());
        int rc;
        do {
            zs.next_out = reinterpret_cast<Bytef*>(chunk);
            zs.avail_out = sizeof chunk;
            rc = deflate(&zs, flush);
            out.append(chunk, sizeof chunk - zs.avail_out);
        } while (zs.avail_out == 0 || (flush == Z_FINISH && rc != Z_STREAM_END));
        return out;
    }
    std::string compress(std::string_view data) override {
        crc = static_cast<std::uint32_t>(::crc32(crc, reinterpret_cast<const Bytef*>(data.data()), static_cast<uInt>(data.size())));
        size += data.size();
        return run(data, Z_NO_FLUSH);
    }
    std::string finish() override {
        std::string out = run({}, Z_FINISH);
        for (int k = 0; k < 4; ++k) out += static_cast<char>((crc >> (8 * k)) & 0xFF);
        for (int k = 0; k < 4; ++k) out += static_cast<char>((size >> (8 * k)) & 0xFF);
        return out;
    }
};

struct Bz2Encoder final : Encoder {
    bz_stream bs{};
    explicit Bz2Encoder(int level) {
        if (level < 1 || level > 9) raise("ValueError", "compresslevel must be between 1 and 9");
        if (BZ2_bzCompressInit(&bs, level, 0, 0) != BZ_OK) raise("MemoryError", "");
    }
    ~Bz2Encoder() override { BZ2_bzCompressEnd(&bs); }
    std::string run(std::string_view data, int action) {
        std::string out;
        char chunk[64 * 1024];
        bs.next_in = const_cast<char*>(data.data());
        bs.avail_in = static_cast<unsigned>(data.size());
        int rc;
        do {
            bs.next_out = chunk;
            bs.avail_out = sizeof chunk;
            rc = BZ2_bzCompress(&bs, action);
            out.append(chunk, sizeof chunk - bs.avail_out);
        } while (action == BZ_FINISH ? rc != BZ_STREAM_END : bs.avail_in > 0);
        return out;
    }
    std::string compress(std::string_view data) override { return run(data, BZ_RUN); }
    std::string finish() override { return run({}, BZ_FINISH); }
};

struct XzEncoder final : Encoder {
    lzma_stream ls = LZMA_STREAM_INIT;
    explicit XzEncoder(std::optional<std::int64_t> preset) {
        if (lzma_easy_encoder(&ls, static_cast<std::uint32_t>(preset.value_or(LZMA_PRESET_DEFAULT)), LZMA_CHECK_CRC64) != LZMA_OK)
            raise("ValueError", "Invalid or unsupported options");
    }
    ~XzEncoder() override { lzma_end(&ls); }
    std::string run(std::string_view data, lzma_action action) {
        std::string out;
        std::uint8_t chunk[64 * 1024];
        ls.next_in = reinterpret_cast<const std::uint8_t*>(data.data());
        ls.avail_in = data.size();
        lzma_ret rc;
        do {
            ls.next_out = chunk;
            ls.avail_out = sizeof chunk;
            rc = lzma_code(&ls, action);
            out.append(reinterpret_cast<const char*>(chunk), sizeof chunk - ls.avail_out);
        } while (action == LZMA_FINISH ? rc != LZMA_STREAM_END : ls.avail_in > 0 || ls.avail_out == 0);
        return out;
    }
    std::string compress(std::string_view data) override { return run(data, LZMA_RUN); }
    std::string finish() override { return run({}, LZMA_FINISH); }
};

struct CompressingFile final : BinaryFile {
    std::shared_ptr<BinaryFile> dst;
    bool owns_dst;
    std::unique_ptr<Encoder> enc;
    std::int64_t pos = 0;
    bool open = true;
    CompressingFile(std::shared_ptr<BinaryFile> d, bool owns, std::unique_ptr<Encoder> e)
        : BinaryFile(nullptr, "", "wb"), dst(std::move(d)), owns_dst(owns), enc(std::move(e)) {}
    ~CompressingFile() override {
        try {
            close();
        } catch (...) {
        }
    }
    std::int64_t write_raw(const std::string& s) override {
        if (!open) raise("ValueError", "I/O operation on closed file.");
        pos += static_cast<std::int64_t>(s.size());
        std::string out = enc->compress(s);
        if (!out.empty()) dst->write_raw(out);
        return static_cast<std::int64_t>(s.size());
    }
    void close() override {
        if (!open) return;
        open = false;
        dst->write_raw(enc->finish());
        if (owns_dst) dst->close();
        else dst->flush();
    }
    std::int64_t tell() override { return pos; }
    std::int64_t seek(std::int64_t, std::int64_t = 0) override { raise("OSError", "seek"); }
    std::string read_raw(std::int64_t) override { raise("OSError", "read"); }
    std::string readline_raw() override { raise("OSError", "readline"); }
    void check_open() const override {
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }
    void flush() override {}
    bool is_closed() const override { return !open; }
    bool readable() const override { return false; }
    bool writable() const override { return true; }
    bool seekable() const override { return false; }
    bool isatty() const override { return false; }
    std::int64_t fileno_() const override { return dst->fileno_(); }
    std::int64_t truncate(std::optional<std::int64_t> = std::nullopt) override { raise("OSError", "truncate"); }
};

// gzip's header as Python's GzipFile writes it (gzopen), or as tarfile's own streams do ("w|gz").
inline std::string gzip_header(const std::string& name, int level, bool stream_style) {
    std::string fname = name;
    if (auto slash = fname.rfind('/'); slash != std::string::npos) fname = fname.substr(slash + 1);
    if (fname.ends_with(".gz")) fname.resize(fname.size() - 3);
    auto now = static_cast<std::uint32_t>(std::time(nullptr));
    std::string h = "\x1f\x8b\x08";
    h += static_cast<char>(stream_style || !fname.empty() ? 8 : 0);  // FNAME
    for (int k = 0; k < 4; ++k) h += static_cast<char>((now >> (8 * k)) & 0xFF);
    h += static_cast<char>(stream_style ? 2 : level == 9 ? 2 : level == 1 ? 4 : 0);
    h += static_cast<char>(0xFF);
    if (stream_style || !fname.empty()) h += fname + '\0';
    return h;
}

// ---- the archive -----------------------------------------------------------------------

struct Archive {
    std::shared_ptr<BinaryFile> fileobj;
    bool extfileobj = false;
    std::string mode;  // r, a, w, x
    std::optional<std::string> name;
    std::int64_t format = DEFAULT_FORMAT;
    dict<std::string, std::string> pax_headers;  // (global ones)
    std::vector<TarInfo> members;
    bool loaded = false, closed = false, stream = false, ignore_zeros = false, dereference = false;
    std::int64_t offset = 0, errorlevel = 1;
    std::optional<TarInfo> firstmember;
    std::map<std::pair<std::int64_t, std::int64_t>, std::string> inodes;
    std::map<std::int64_t, std::string> unames, gnames;

    Archive() = default;
    Archive(const Archive&) = delete;
    ~Archive() {  // (closed if it wasn't, without the end blocks: like Python's after an error)
        if (!closed && !extfileobj && fileobj) {
            try {
                fileobj->close();
            } catch (...) {
            }
        }
    }

    void check(const char* modes = nullptr) const {
        if (closed) raise("OSError", "TarFile is closed");
        if (modes && std::string(modes).find(mode) == std::string::npos) raise("OSError", "bad operation for mode " + repr_str(mode));
    }
    std::string read_block() { return fileobj->read_raw(static_cast<std::int64_t>(BLOCKSIZE)); }

    TarInfo frombuf(const std::string& buf, bool dircheck);
    TarInfo fromtarfile(bool dircheck = true);
    TarInfo proc_member(TarInfo t);
    void apply_pax_info(TarInfo& t, const dict<std::string, std::string>& pax);
    std::optional<TarInfo> next();
    void load() {
        if (!stream) {
            while (next()) {
            }
            loaded = true;
        }
    }
    const std::vector<TarInfo>& getmembers() {
        check();
        if (!loaded) load();
        return members;
    }
    std::optional<TarInfo> getmember_named(const std::string& name, const std::optional<TarInfo>& limit = std::nullopt,
                                           bool normalize = false);
    void close();
};

inline TarInfo Archive::frombuf(const std::string& buf, bool dircheck) {
    if (buf.empty()) fail<EmptyHeaderError>("empty header");
    if (buf.size() != BLOCKSIZE) fail<TruncatedHeaderError>("truncated header");
    if (std::count(buf.begin(), buf.end(), '\0') == static_cast<std::ptrdiff_t>(BLOCKSIZE)) fail<EOFHeaderError>("end of file header");
    std::string_view b(buf);
    std::int64_t chksum = nti(b.substr(148, 8));
    auto [u, s] = calc_chksums(b);
    if (chksum != u && chksum != s) fail<InvalidHeaderError>("bad checksum");
    TarInfo t = make_info();
    Info& z = *t.d;
    z.name = nts(b.substr(0, 100));
    z.mode = nti(b.substr(100, 8));
    z.uid = nti(b.substr(108, 8));
    z.gid = nti(b.substr(116, 8));
    z.size = nti(b.substr(124, 12));
    z.mtime = static_cast<double>(nti(b.substr(136, 12)));
    z.chksum = chksum;
    z.type = std::string(b.substr(156, 1));
    z.linkname = nts(b.substr(157, 100));
    z.uname = nts(b.substr(265, 32));
    z.gname = nts(b.substr(297, 32));
    z.devmajor = nti(b.substr(329, 8));
    z.devminor = nti(b.substr(337, 8));
    std::string prefix = nts(b.substr(345, 155));
    if (dircheck && z.type == AREGTYPE && z.name.ends_with('/')) z.type = DIRTYPE;
    if (z.type == GNUTYPE_SPARSE) {  // (old GNU sparse: only the sizes, to find the next member)
        z.sparse = true;
        z.sparse_extended = b[482] != 0;
        z.sparse_origsize = nti(b.substr(483, 12));
    }
    if (t.isdir())
        while (z.name.ends_with('/')) z.name.pop_back();
    if (!prefix.empty() && !gnu_type(z.type)) z.name = prefix + "/" + z.name;
    return t;
}

inline TarInfo Archive::fromtarfile(bool dircheck) {
    std::string buf = read_block();
    TarInfo t = frombuf(buf, dircheck);
    t.d->offset = fileobj->tell() - static_cast<std::int64_t>(BLOCKSIZE);
    return proc_member(t);
}

inline void Archive::apply_pax_info(TarInfo& t, const dict<std::string, std::string>& pax) {
    Info& z = *t.d;
    auto number = [](const std::string& v) -> std::int64_t {
        try {
            return std::stoll(v);
        } catch (...) {
            return 0;
        }
    };
    for (const auto& [keyword, value] : pax) {
        if (keyword == "GNU.sparse.name") z.name = value;
        else if (keyword == "GNU.sparse.size" || keyword == "GNU.sparse.realsize") z.size = number(value);
        else if (keyword == "path") {
            std::string v = value;
            while (v.ends_with('/')) v.pop_back();
            z.name = v;
        } else if (keyword == "linkpath") z.linkname = value;
        else if (keyword == "size") z.size = number(value);
        else if (keyword == "uid") z.uid = number(value);
        else if (keyword == "gid") z.gid = number(value);
        else if (keyword == "uname") z.uname = value;
        else if (keyword == "gname") z.gname = value;
        else if (keyword == "mtime") {
            try {
                z.mtime = std::stod(value);
            } catch (...) {
                z.mtime = 0;
            }
            z.mtime_float = true;
        }
    }
    z.pax_headers = pax.copy();
}

inline TarInfo Archive::proc_member(TarInfo t) {
    Info& z = *t.d;
    if (z.type == GNUTYPE_LONGNAME || z.type == GNUTYPE_LONGLINK) {
        std::string buf = fileobj->read_raw(block(z.size));
        TarInfo next;
        try {
            next = fromtarfile(false);
        } catch (const Thrown& e) {
            if (isinstance<HeaderError>(e)) fail<SubsequentHeaderError>(e.exc->sd_str());
            throw;
        }
        next.d->offset = z.offset;
        if (z.type == GNUTYPE_LONGNAME) next.d->name = nts(buf);
        else next.d->linkname = nts(buf);
        if (next.isdir() && next.d->name.ends_with('/')) next.d->name.pop_back();
        return next;
    }
    if (z.type == GNUTYPE_SPARSE) {
        bool extended = z.sparse_extended;
        while (extended) extended = read_block()[504] != 0;
        z.offset_data = fileobj->tell();
        offset = z.offset_data + block(z.size);
        z.size = z.sparse_origsize;
        return t;
    }
    if (z.type == XHDTYPE || z.type == XGLTYPE || z.type == SOLARIS_XHDTYPE) {
        std::string buf = fileobj->read_raw(block(z.size));
        dict<std::string, std::string> pax = z.type == XGLTYPE ? pax_headers : pax_headers.copy();
        std::size_t pos = 0;
        while (pos < buf.size() && buf[pos] != '\0') {
            std::size_t sp = pos;
            while (sp < buf.size() && sp - pos < 21 && std::isdigit(static_cast<unsigned char>(buf[sp]))) ++sp;
            if (sp == pos || sp >= buf.size() || buf[sp] != ' ') fail<InvalidHeaderError>("invalid header");
            std::size_t length = std::stoull(buf.substr(pos, sp - pos));
            if (length < 5 || pos + length > buf.size()) fail<InvalidHeaderError>("invalid header");
            std::size_t end = pos + length - 1;
            std::string kv = buf.substr(sp + 1, end - sp - 1);
            auto eq = kv.find('=');
            if (eq == std::string::npos || eq == 0 || buf[end] != '\n') fail<InvalidHeaderError>("invalid header");
            pax[kv.substr(0, eq)] = kv.substr(eq + 1);
            pos += length;
        }
        if (z.type == XGLTYPE) pax_headers = pax;
        TarInfo next;
        try {
            next = fromtarfile(false);
        } catch (const Thrown& e) {
            if (isinstance<HeaderError>(e)) fail<SubsequentHeaderError>(e.exc->sd_str());
            throw;
        }
        auto value = [&](const char* k) { return pax.contains(k) ? pax.at(k) : std::string(); };
        if (pax.contains("GNU.sparse.map") || pax.contains("GNU.sparse.size") ||
            (value("GNU.sparse.major") == "1" && value("GNU.sparse.minor") == "0"))
            next.d->sparse = true;
        if (z.type == XHDTYPE || z.type == SOLARIS_XHDTYPE) {
            apply_pax_info(next, pax);
            if (pax.contains("size")) {
                std::int64_t at = next.d->offset + static_cast<std::int64_t>(BLOCKSIZE);
                if (next.isreg() || !supported_type(next.d->type)) at += block(next.d->size);
                offset = at;
            }
            next.d->offset = z.offset;
        }
        return next;
    }
    // a built-in type (or an unknown one, read as a regular file)
    z.offset_data = fileobj->tell();
    std::int64_t at = z.offset_data;
    if (t.isreg() || !supported_type(z.type)) at += block(z.size);
    offset = at;
    apply_pax_info(t, pax_headers);
    if (t.isdir())
        while (z.name.ends_with('/')) z.name.pop_back();
    return t;
}

inline std::optional<TarInfo> Archive::next() {
    check("ra");
    if (firstmember) return std::exchange(firstmember, std::nullopt);
    if (offset != fileobj->tell()) {
        if (offset == 0) return std::nullopt;
        fileobj->seek(offset - 1, 0);
        if (fileobj->read_raw(1).empty()) fail<ReadError>("unexpected end of data");
    }
    std::optional<TarInfo> t;
    for (;;) {
        try {
            t = fromtarfile();
        } catch (const Thrown& e) {
            std::string why = e.exc->sd_str();
            if (isinstance<EOFHeaderError>(e)) {
                if (ignore_zeros) {
                    offset += static_cast<std::int64_t>(BLOCKSIZE);
                    continue;
                }
            } else if (isinstance<InvalidHeaderError>(e)) {
                if (ignore_zeros) {
                    offset += static_cast<std::int64_t>(BLOCKSIZE);
                    continue;
                }
                if (offset == 0) fail<ReadError>(why);
            } else if (isinstance<EmptyHeaderError>(e)) {
                if (offset == 0) fail<ReadError>("empty file");
            } else if (isinstance<TruncatedHeaderError>(e)) {
                if (offset == 0) fail<ReadError>(why);
            } else if (isinstance<SubsequentHeaderError>(e)) {
                fail<ReadError>(why);
            } else {
                throw;
            }
        }
        break;
    }
    if (t) {
        if (!stream) members.push_back(*t);
    } else {
        loaded = true;
    }
    return t;
}

inline std::optional<TarInfo> Archive::getmember_named(const std::string& wanted, const std::optional<TarInfo>& limit, bool normalize) {
    const auto& all = getmembers();
    std::size_t end = all.size();
    bool skipping = false;
    if (limit) {
        auto it = std::find(all.begin(), all.end(), *limit);
        if (it == all.end()) skipping = true;
        else end = static_cast<std::size_t>(it - all.begin());
    }
    std::string name = normalize ? normpath(wanted) : wanted;
    for (std::size_t i = end; i-- > 0;) {
        const TarInfo& m = all[i];
        if (skipping) {
            if (limit->d->offset == m.d->offset) skipping = false;
            continue;
        }
        if (name == (normalize ? normpath(m.d->name) : m.d->name)) return m;
    }
    if (skipping) raise("ValueError", limit->sd_repr());
    return std::nullopt;
}

inline void Archive::close() {
    if (closed) return;
    closed = true;
    struct Release {
        Archive& a;
        ~Release() {
            if (!a.extfileobj && a.fileobj) a.fileobj->close();
        }
    } release{*this};
    if (mode == "a" || mode == "w" || mode == "x") {
        fileobj->write_raw(std::string(BLOCKSIZE * 2, '\0'));
        offset += static_cast<std::int64_t>(BLOCKSIZE * 2);
        if (auto r = offset % static_cast<std::int64_t>(RECORDSIZE)) fileobj->write_raw(std::string(RECORDSIZE - static_cast<std::size_t>(r), '\0'));
    }
}

// tf.extractfile(member): its data, as a binary file object (Python's ExFileObject)
struct MemberFile final : BinaryFile {
    std::shared_ptr<Archive> a;  // (keeps the archive's file open)
    std::string member;
    std::int64_t start, length, position = 0;
    bool open = true;
    MemberFile(std::shared_ptr<Archive> archive, const Info& z)
        : BinaryFile(nullptr, "", "rb"), a(std::move(archive)), member(z.name), start(z.offset_data), length(z.size) {}
    void need_open() const {
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }
    std::string read_raw(std::int64_t n) override {
        need_open();
        std::int64_t want = n < 0 ? length - position : std::min(n, length - position);
        if (want <= 0) return "";
        a->fileobj->seek(start + position, 0);
        std::string data = a->fileobj->read_raw(want);
        if (static_cast<std::int64_t>(data.size()) != want) fail<ReadError>("unexpected end of data");
        position += want;
        return data;
    }
    std::string readline_raw() override {
        need_open();
        std::string out;
        while (position < length) {
            std::string chunk = read_raw(std::min<std::int64_t>(4096, length - position));
            auto nl = chunk.find('\n');
            if (nl != std::string::npos) {
                out += chunk.substr(0, nl + 1);
                position -= static_cast<std::int64_t>(chunk.size() - nl - 1);
                break;
            }
            out += chunk;
        }
        return out;
    }
    std::int64_t tell() override {
        need_open();
        return position;
    }
    std::int64_t seek(std::int64_t to, std::int64_t whence = 0) override {
        need_open();
        if (whence == 0) position = std::clamp<std::int64_t>(to, 0, length);
        else if (whence == 1) position = std::clamp<std::int64_t>(position + to, 0, length);
        else if (whence == 2) position = std::clamp<std::int64_t>(length + to, 0, length);
        else raise("ValueError", "Invalid argument");
        return position;
    }
    std::int64_t write_raw(const std::string&) override { raise("OSError", "write"); }
    void check_open() const override { need_open(); }
    void close() override {
        open = false;
        a = nullptr;
    }
    void flush() override {}
    bool is_closed() const override { return !open; }
    bool readable() const override { return need_open(), true; }
    bool writable() const override { return need_open(), false; }
    bool seekable() const override { return need_open(), true; }
    bool isatty() const override { return need_open(), false; }
    std::string get_name() const override { return member; }
    std::string get_mode() const override { return "rb"; }
    std::int64_t fileno_() const override { raise("OSError", "fileno"); }
    std::int64_t truncate(std::optional<std::int64_t> = std::nullopt) override { raise("OSError", "truncate"); }
    std::string sd_repr() const override { return "<ExFileObject name=" + repr_str(member) + ">"; }
};

// ---- extraction filters (PEP 706) ------------------------------------------------------------

inline std::string filter_name_repr(const Info& z) { return repr_str(z.name); }

// Python's _get_filtered_attrs, applied to a copy: nullopt from the filter means "excluded".
inline TarInfo filtered_copy(const TarInfo& member, const std::string& dest, bool for_data) {
    TarInfo t = member;
    bool copied = false;
    auto edit = [&]() -> Info& {
        if (!copied) {
            t = member.copy();
            copied = true;
        }
        return *t.d;
    };
    std::string dest_path = realpath(dest);
    std::string name = member.d->name;
    if (name.starts_with('/')) {
        name.erase(0, name.find_first_not_of('/'));
        edit().name = name;
    }
    if (name.starts_with('/')) fail<AbsolutePathError>("member " + repr_str(member.d->name) + " has an absolute path");
    bool dots = false;
    for (std::size_t start = 0;;) {
        auto slash = name.find('/', start);
        if (name.substr(start, slash == std::string::npos ? std::string::npos : slash - start) == "..") dots = true;
        if (slash == std::string::npos) break;
        start = slash + 1;
    }
    if (dots) {
        std::string normalized = normpath(name);
        if (normalized != name) edit().name = name = normalized;
    }
    std::string target = realpath(join(dest_path, name));
    if (!inside(target, dest_path))
        fail<OutsideDestinationError>(repr_str(member.d->name) + " would be extracted to " + repr_str(target) +
                                      ", which is outside the destination");
    if (member.d->mode) {
        std::int64_t mode = *member.d->mode & 0755;
        std::optional<std::int64_t> result = mode;
        if (for_data) {
            if (member.isreg() || member.islnk()) {
                if (!(mode & 0100)) mode &= ~0111;
                result = mode | 0600;
            } else if (member.isdir() || member.issym()) {
                result = std::nullopt;
            } else {
                fail<SpecialFileError>(repr_str(member.d->name) + " is a special file");
            }
        }
        if (result != member.d->mode) edit().mode = result;
    }
    if (for_data) {
        if (member.d->uid) edit().uid = std::nullopt;
        if (member.d->gid) edit().gid = std::nullopt;
        if (member.d->uname) edit().uname = std::nullopt;
        if (member.d->gname) edit().gname = std::nullopt;
        if (member.islnk() || member.issym()) {
            if (member.d->linkname.starts_with('/'))
                fail<AbsoluteLinkError>(repr_str(member.d->name) + " is a link to an absolute path");
            if (target == dest_path)
                fail<OutsideDestinationError>(repr_str(member.d->name) + " would be extracted to " + repr_str(target) +
                                              ", which is outside the destination");
            std::string normalized = normpath(member.d->linkname);
            if (normalized != member.d->linkname) edit().linkname = normalized;
            std::string link_target;
            if (member.issym()) {
                std::string stripped = name;
                while (stripped.ends_with('/')) stripped.pop_back();
                link_target = join(join(dest_path, dirname(stripped)), normalized);
            } else {
                link_target = join(dest_path, normalized);
            }
            link_target = realpath(link_target);
            if (!inside(link_target, dest_path))
                fail<LinkOutsideDestinationError>(repr_str(member.d->name) + " would link to " + repr_str(link_target) +
                                                  ", which is outside the destination");
        }
    }
    return t;
}

// What filter= gives: "data", "tar", "fully_trusted", or a function (member, path) -> member or None.
using FilterFn = std::function<std::optional<TarInfo>(const TarInfo&, const std::string&)>;
inline FilterFn named_filter(const std::string& name) {
    if (name == "fully_trusted") return [](const TarInfo& m, const std::string&) -> std::optional<TarInfo> { return m; };
    if (name == "tar") return [](const TarInfo& m, const std::string& p) -> std::optional<TarInfo> { return filtered_copy(m, p, false); };
    if (name == "data") return [](const TarInfo& m, const std::string& p) -> std::optional<TarInfo> { return filtered_copy(m, p, true); };
    raise("ValueError", "filter " + repr_str(name) + " not found");
}
template <class F>
FilterFn as_filter(const F& f) {
    if constexpr (std::is_same_v<F, std::nullopt_t>) {
        return named_filter("data");  // (Python 3.14's default)
    } else if constexpr (std::is_convertible_v<F, std::string>) {
        return named_filter(f);
    } else if constexpr (is_optional<F>::value) {
        return f ? as_filter(*f) : named_filter("data");
    } else {
        return [f = F(f)](const TarInfo& m, const std::string& p) mutable -> std::optional<TarInfo> { return f(m, p); };
    }
}

// tarfile.data_filter(member, path) and friends, called directly
inline std::optional<TarInfo> data_filter(const TarInfo& m, const std::string& p) { return filtered_copy(m, p, true); }
inline std::optional<TarInfo> tar_filter(const TarInfo& m, const std::string& p) { return filtered_copy(m, p, false); }
inline std::optional<TarInfo> fully_trusted_filter(const TarInfo& m, const std::string&) { return m; }

// ---- TarFile ---------------------------------------------------------------------------------

inline void set_mtime(const std::string& path, double mtime) {
    timespec ts[2];
    double sec = std::floor(mtime);
    ts[0].tv_sec = ts[1].tv_sec = static_cast<time_t>(sec);
    ts[0].tv_nsec = ts[1].tv_nsec = static_cast<long>(std::round((mtime - sec) * 1e9));
    if (ts[0].tv_nsec >= 1000000000) {
        ts[0].tv_sec = ++ts[1].tv_sec;
        ts[0].tv_nsec = ts[1].tv_nsec = 0;
    }
    if (::utimensat(AT_FDCWD, path.c_str(), ts, 0) != 0) fail<ExtractError>("could not change modification time");
}

struct TarFile {
    std::shared_ptr<Archive> a;
    TarFile() = default;  // (unset until it's assigned)
    explicit TarFile(std::shared_ptr<Archive> archive) : a(std::move(archive)) {}

    // tarfile.TarFile(name, mode, fileobj): an uncompressed archive
    TarFile(std::optional<pathlib::Path> name, const std::string& mode = "r",
            std::optional<std::shared_ptr<BinaryFile>> fileobj = std::nullopt, std::optional<std::int64_t> format = std::nullopt) {
        init(name ? std::optional<std::string>(name->str()) : std::nullopt, mode, fileobj ? *fileobj : nullptr, format, false);
    }

    void init(std::optional<std::string> name, const std::string& mode, std::shared_ptr<BinaryFile> fileobj,
              std::optional<std::int64_t> format, bool stream, bool ignore_zeros = false, bool dereference = false,
              std::int64_t errorlevel = 1, std::optional<dict<std::string, std::string>> pax_headers = std::nullopt) {
        static const std::map<std::string, std::string> modes = {{"r", "rb"}, {"a", "r+b"}, {"w", "wb"}, {"x", "xb"}};
        if (!modes.contains(mode)) raise("ValueError", "mode must be 'r', 'a', 'w' or 'x'");
        a = std::make_shared<Archive>();
        Archive& ar = *a;
        ar.mode = mode;
        std::string filemode = modes.at(mode);
        if (!fileobj) {
            if (mode == "a" && !lexists(*name)) {  // (appending creates the file)
                ar.mode = "w";
                filemode = "wb";
            }
            fileobj = open_binary(*name, filemode);
            ar.extfileobj = false;
        } else {
            if (!name) {
                try {
                    name = fileobj->get_name();
                } catch (const Thrown&) {  // (an io.BytesIO has no name)
                }
                if (name && name->empty()) name = std::nullopt;
            }
            ar.extfileobj = true;
        }
        ar.name = name ? std::optional<std::string>(abspath(*name)) : std::nullopt;
        ar.fileobj = fileobj;
        ar.stream = stream;
        if (format) ar.format = *format;
        ar.ignore_zeros = ignore_zeros;
        ar.dereference = dereference;
        ar.errorlevel = errorlevel;
        if (pax_headers && ar.format == PAX_FORMAT) ar.pax_headers = *pax_headers;
        try {
            ar.offset = ar.fileobj->tell();
            if (ar.mode == "r") {
                ar.firstmember = std::nullopt;
                ar.firstmember = ar.next();
            }
            if (ar.mode == "a") {  // to the end of the archive, before its first empty block
                for (;;) {
                    ar.fileobj->seek(ar.offset, 0);
                    try {
                        ar.members.push_back(ar.fromtarfile());
                    } catch (const Thrown& e) {
                        if (isinstance<EOFHeaderError>(e)) {
                            ar.fileobj->seek(ar.offset, 0);
                            break;
                        }
                        if (isinstance<HeaderError>(e)) fail<ReadError>(e.exc->sd_str());
                        throw;
                    }
                }
            }
            if (ar.mode == "a" || ar.mode == "w" || ar.mode == "x") {
                ar.loaded = true;
                if (!ar.pax_headers.empty()) {
                    std::string buf = create_pax_generic_header(ar.pax_headers, XGLTYPE);
                    ar.fileobj->write_raw(buf);
                    ar.offset += static_cast<std::int64_t>(buf.size());
                }
            }
        } catch (...) {
            if (!ar.extfileobj) ar.fileobj->close();
            ar.closed = true;
            throw;
        }
    }

    // tf.getmember(name), tf.getmembers(), tf.getnames(), tf.next()
    TarInfo getmember(const std::string& name) const {
        std::string n = name;
        while (n.ends_with('/')) n.pop_back();
        auto t = a->getmember_named(n);
        if (!t) raise<KeyError>("filename " + repr_str(name) + " not found");
        return *t;
    }
    sd::list<TarInfo> getmembers() const {
        sd::list<TarInfo> out;
        for (const auto& m : a->getmembers()) out.push_back(m);
        return out;
    }
    sd::list<std::string> getnames() const {
        sd::list<std::string> out;
        for (const auto& m : a->getmembers()) out.push_back(m.d->name);
        return out;
    }
    std::optional<TarInfo> next() const { return a->next(); }

    // `for member in tf:` reads the members as it goes (Python's __iter__), so a stream's
    // member can be read before the next header
    struct MemberRange {
        std::shared_ptr<Archive> a;
        struct sentinel {};
        struct iterator {
            Archive* a = nullptr;
            bool was_loaded = false, first = true;
            std::size_t index = 0;
            std::optional<TarInfo> current;
            void advance() {
                current.reset();
                if (was_loaded) {
                    if (index < a->members.size()) current = a->members[index++];
                    return;
                }
                if (first && a->firstmember) {
                    first = false;
                    current = a->next();
                    ++index;
                    return;
                }
                first = false;
                if (index < a->members.size()) {
                    current = a->members[index];
                } else if (!a->loaded) {
                    current = a->next();
                    if (!current) a->loaded = true;
                }
                if (current) ++index;
            }
            const TarInfo& operator*() const { return *current; }
            iterator& operator++() {
                advance();
                return *this;
            }
            bool operator!=(sentinel) const { return current.has_value(); }
        };
        iterator begin() const {
            iterator it;
            it.a = a.get();
            it.was_loaded = a->loaded;
            it.advance();
            return it;
        }
        sentinel end() const { return {}; }
    };
    MemberRange sd_iter() const { return MemberRange{a}; }
    sd::list<TarInfo> all_members() const {
        sd::list<TarInfo> out;
        for (const auto& m : sd_iter()) out.push_back(m);
        return out;
    }

    TarInfo member_of(const std::string& name) const { return getmember(name); }
    TarInfo member_of(const TarInfo& t) const { return t; }

    // tf.extractfile(member): a file object for a file (or a link to one), else None
    template <class Member>
    std::optional<std::shared_ptr<BinaryFile>> extractfile(const Member& member) const {
        a->check("r");
        TarInfo t = member_of(member);
        if (t.isreg() || !supported_type(t.d->type)) return std::shared_ptr<BinaryFile>(std::make_shared<MemberFile>(a, *t.d));
        if (t.islnk() || t.issym()) {
            if (a->stream) fail<StreamError>("cannot extract (sym)link as file object");
            return extractfile(find_link_target(t));
        }
        return std::nullopt;
    }

    TarInfo find_link_target(const TarInfo& t) const {
        std::string linkname;
        std::optional<TarInfo> limit;
        if (t.issym()) {
            std::string dir = dirname(t.d->name);
            linkname = dir.empty() ? t.d->linkname : t.d->linkname.empty() ? dir : dir + "/" + t.d->linkname;
        } else {
            linkname = t.d->linkname;
            limit = t;
        }
        auto m = a->getmember_named(linkname, limit, true);
        if (!m) raise<KeyError>("linkname " + repr_str(linkname) + " not found");
        return *m;
    }

    // tf.extract(member, path="", set_attrs=True, numeric_owner=False, filter=None)
    template <class Member, class Filter = std::nullopt_t>
    void extract(const Member& member, const std::optional<pathlib::Path>& path = std::nullopt, bool set_attrs = true,
                 bool numeric_owner = false, const Filter& filter = std::nullopt) {
        FilterFn fn = as_filter(filter);
        std::string where = path ? path->str() : "";
        auto t = extract_tarinfo(member_of(member), fn, where);
        if (t) extract_one(*t, where, set_attrs, numeric_owner, fn);
    }
    // tf.extractall(path=".", members=None, numeric_owner=False, filter=None)
    template <class Members = std::nullopt_t, class Filter = std::nullopt_t>
    void extractall(const std::optional<pathlib::Path>& path = std::nullopt, const Members& members = std::nullopt,
                    bool numeric_owner = false, const Filter& filter = std::nullopt) {
        FilterFn fn = as_filter(filter);
        std::string where = path ? path->str() : ".";
        std::vector<TarInfo> directories;
        auto each = [&](const TarInfo& member) {  // (as they're read: a stream can't go back)
            auto t = extract_tarinfo(member, fn, where);
            if (!t) return;
            if (t->isdir()) directories.push_back(member);
            extract_one(*t, where, !t->isdir(), numeric_owner, fn);
        };
        if constexpr (std::is_same_v<Members, std::nullopt_t>) {
            for (const auto& m : sd_iter()) each(m);
        } else if constexpr (is_optional<Members>::value) {
            if (members) for (const auto& m : *members) each(m);
            else for (const auto& m : sd_iter()) each(m);
        } else {
            for (const auto& m : members) each(m);
        }
        std::stable_sort(directories.begin(), directories.end(), [](const TarInfo& x, const TarInfo& y) { return x.d->name > y.d->name; });
        for (const auto& unfiltered : directories) {  // the directories' owner, times and mode, last
            try {
                std::optional<TarInfo> t;
                try {
                    t = fn(unfiltered, where);
                } catch (const Thrown& e) {
                    if (isinstance<FilterError>(e) || isinstance<OSError>(e) || isinstance<ExtractError>(e)) continue;
                    throw;
                }
                if (!t) continue;
                std::string dirpath = join(where, t->d->name);
                struct stat st;
                if (::lstat(dirpath.c_str(), &st) != 0 || !S_ISDIR(st.st_mode)) continue;
                chown(*t, dirpath, numeric_owner);
                utime(*t, dirpath);
                chmod(*t, dirpath);
            } catch (const Thrown& e) {
                if (!isinstance<ExtractError>(e)) throw;
                if (a->errorlevel > 1) throw;
            }
        }
    }

    std::optional<TarInfo> extract_tarinfo(const TarInfo& unfiltered, const FilterFn& fn, const std::string& path) {
        std::optional<TarInfo> filtered;
        try {
            filtered = fn(unfiltered, path);
        } catch (const Thrown& e) {
            if (isinstance<ExtractError>(e)) {
                if (a->errorlevel > 1) throw;
            } else if (a->errorlevel > 0 || !(isinstance<OSError>(e) || isinstance<FilterError>(e))) {
                throw;
            }
        }
        if (!filtered) return std::nullopt;
        if (filtered->islnk()) {
            *filtered = filtered->copy();
            filtered->d->link_target = join(path, filtered->d->linkname);
        }
        return filtered;
    }

    void extract_one(const TarInfo& t, const std::string& path, bool set_attrs, bool numeric_owner, const FilterFn& fn) {
        a->check("r");
        try {
            extract_member(t, join(path, t.d->name), set_attrs, numeric_owner, &fn, path);
        } catch (const Thrown& e) {
            if (isinstance<ExtractError>(e)) {
                if (a->errorlevel > 1) throw;
            } else {
                throw;  // (errorlevel 0, which only logs OSError, isn't supported)
            }
        }
    }

    void extract_member(const TarInfo& t, std::string targetpath, bool set_attrs, bool numeric_owner, const FilterFn* fn,
                        const std::string& root) {
        while (targetpath.ends_with('/')) targetpath.pop_back();
        std::string upper = dirname(targetpath);
        if (!upper.empty() && !lexists(upper)) {
            std::error_code ec;
            std::filesystem::create_directories(upper, ec);
            if (ec && !isdir(upper)) raise_os(ec.value(), upper);
        }
        if (t.isreg()) makefile(t, targetpath);
        else if (t.isdir()) makedir(t, targetpath);
        else if (t.isfifo()) {
            if (::mkfifo(targetpath.c_str(), 0666) != 0) raise_os(errno, targetpath);
        } else if (t.ischr() || t.isblk()) {
            mode_t mode = static_cast<mode_t>(t.d->mode.value_or(0600)) | (t.isblk() ? S_IFBLK : S_IFCHR);
            if (::mknod(targetpath.c_str(), mode, makedev(static_cast<unsigned>(t.d->devmajor), static_cast<unsigned>(t.d->devminor))) != 0)
                raise_os(errno, targetpath);
        } else if (t.islnk() || t.issym()) makelink(t, targetpath, fn, root);
        else makefile(t, targetpath);
        if (set_attrs) {
            chown(t, targetpath, numeric_owner);
            if (!t.issym()) {
                chmod(t, targetpath);
                utime(t, targetpath);
            }
        }
    }

    void makedir(const TarInfo& t, const std::string& targetpath) {
        if (::mkdir(targetpath.c_str(), t.d->mode ? 0700 : 0777) != 0) {
            if (errno == EEXIST && isdir(targetpath)) return;
            raise_os(errno, targetpath);
        }
    }
    void makefile(const TarInfo& t, const std::string& targetpath) {
        a->fileobj->seek(t.d->offset_data, 0);
        auto out = open_binary(targetpath, "wb");
        std::int64_t left = t.d->size;
        while (left > 0) {
            std::string chunk = a->fileobj->read_raw(std::min<std::int64_t>(left, 16 * 1024));
            if (chunk.empty()) fail<ReadError>("unexpected end of data");
            out->write_raw(chunk);
            left -= static_cast<std::int64_t>(chunk.size());
        }
        out->close();
    }
    void makelink(const TarInfo& t, const std::string& targetpath, const FilterFn* fn, const std::string& root) {
        bool failed = false;
        if (t.issym()) {
            if (lexists(targetpath)) ::unlink(targetpath.c_str());
            if (::symlink(t.d->linkname.c_str(), targetpath.c_str()) == 0) return;
            failed = true;
        } else if (t.d->link_target && lexists(*t.d->link_target)) {
            if (lexists(targetpath)) ::unlink(targetpath.c_str());
            if (::link(realpath(*t.d->link_target).c_str(), targetpath.c_str()) == 0) return;
            failed = true;
        }
        TarInfo unfiltered;
        try {
            unfiltered = find_link_target(t);
        } catch (const Thrown& e) {
            if (failed && isinstance<KeyError>(e)) fail<ExtractError>("unable to resolve link inside archive");
            throw;
        }
        std::optional<TarInfo> filtered = unfiltered;
        if (fn) {
            try {
                (*fn)(unfiltered.replace(t.d->name), root);
                filtered = (*fn)(unfiltered, root);
            } catch (const Thrown& e) {
                if (isinstance<FilterError>(e) || isinstance<OSError>(e) || isinstance<ExtractError>(e))
                    fail<LinkFallbackError>("link " + repr_str(t.d->name) + " would be extracted as a copy of " +
                                            repr_str(unfiltered.d->name) + ", which was rejected");
                throw;
            }
        }
        if (filtered) extract_member(*filtered, targetpath, true, false, fn, root);
    }
    void chown(const TarInfo& t, const std::string& targetpath, bool numeric_owner) {
        if (::geteuid() != 0) return;  // (only root can)
        long g = t.d->gid ? static_cast<long>(*t.d->gid) : -1, u = t.d->uid ? static_cast<long>(*t.d->uid) : -1;
        if (!numeric_owner) {
            if (t.d->gname && !t.d->gname->empty())
                if (struct group* gr = ::getgrnam(t.d->gname->c_str())) g = gr->gr_gid;
            if (t.d->uname && !t.d->uname->empty())
                if (struct passwd* pw = ::getpwnam(t.d->uname->c_str())) u = pw->pw_uid;
        }
        int rc = t.issym() ? ::lchown(targetpath.c_str(), static_cast<uid_t>(u), static_cast<gid_t>(g))
                           : ::chown(targetpath.c_str(), static_cast<uid_t>(u), static_cast<gid_t>(g));
        if (rc != 0) fail<ExtractError>("could not change owner");
    }
    void chmod(const TarInfo& t, const std::string& targetpath) {
        if (!t.d->mode) return;
        if (::chmod(targetpath.c_str(), static_cast<mode_t>(*t.d->mode)) != 0) fail<ExtractError>("could not change mode");
    }
    void utime(const TarInfo& t, const std::string& targetpath) { set_mtime(targetpath, t.d->mtime); }

    // tf.gettarinfo(name=None, arcname=None, fileobj=None)
    std::optional<TarInfo> gettarinfo(const std::optional<pathlib::Path>& name_ = std::nullopt,
                                      const std::optional<pathlib::Path>& arcname_ = std::nullopt,
                                      const std::optional<std::shared_ptr<BinaryFile>>& fileobj = std::nullopt) {
        a->check("awx");
        std::string name = name_ ? name_->str() : "";
        if (fileobj) name = (*fileobj)->get_name();
        std::string arcname = arcname_ ? arcname_->str() : name;
        arcname.erase(0, std::min(arcname.find_first_not_of('/'), arcname.size()));
        struct stat st;
        int rc = fileobj ? ::fstat(static_cast<int>((*fileobj)->fileno_()), &st)
                         : a->dereference ? ::stat(name.c_str(), &st) : ::lstat(name.c_str(), &st);
        if (rc != 0) raise_os(errno, name);
        TarInfo t = make_info();
        Info& z = *t.d;
        std::string linkname;
        std::string type;
        if (S_ISREG(st.st_mode)) {
            std::pair<std::int64_t, std::int64_t> inode{static_cast<std::int64_t>(st.st_ino), static_cast<std::int64_t>(st.st_dev)};
            auto it = a->inodes.find(inode);
            if (!a->dereference && st.st_nlink > 1 && it != a->inodes.end() && arcname != it->second) {
                type = LNKTYPE;  // a hard link to a file already in the archive
                linkname = it->second;
            } else {
                type = REGTYPE;
                if (inode.first) a->inodes[inode] = arcname;
            }
        } else if (S_ISDIR(st.st_mode)) type = DIRTYPE;
        else if (S_ISFIFO(st.st_mode)) type = FIFOTYPE;
        else if (S_ISLNK(st.st_mode)) {
            type = SYMTYPE;
            char buf[4096];
            ssize_t n = ::readlink(name.c_str(), buf, sizeof buf);
            if (n < 0) raise_os(errno, name);
            linkname.assign(buf, static_cast<std::size_t>(n));
        } else if (S_ISCHR(st.st_mode)) type = CHRTYPE;
        else if (S_ISBLK(st.st_mode)) type = BLKTYPE;
        else return std::nullopt;
        z.name = arcname;
        z.mode = st.st_mode;
        z.uid = st.st_uid;
        z.gid = st.st_gid;
        z.size = type == REGTYPE ? st.st_size : 0;
#ifdef __APPLE__
        long nsec = st.st_mtimespec.tv_nsec;
#else
        long nsec = st.st_mtim.tv_nsec;
#endif
        z.mtime = static_cast<double>(st.st_mtime) + static_cast<double>(nsec) * 1e-9;  // (os.stat's float, as Python makes it)
        z.mtime_float = true;
        z.type = type;
        z.linkname = linkname;
        if (!a->unames.contains(*z.uid)) {
            struct passwd* pw = ::getpwuid(static_cast<uid_t>(*z.uid));
            a->unames[*z.uid] = pw ? pw->pw_name : "";
        }
        z.uname = a->unames[*z.uid];
        if (!a->gnames.contains(*z.gid)) {
            struct group* gr = ::getgrgid(static_cast<gid_t>(*z.gid));
            a->gnames[*z.gid] = gr ? gr->gr_name : "";
        }
        z.gname = a->gnames[*z.gid];
        if (type == CHRTYPE || type == BLKTYPE) {
            z.devmajor = major(st.st_rdev);
            z.devminor = minor(st.st_rdev);
        }
        return t;
    }

    // tf.add(name, arcname=None, recursive=True, filter=None)
    template <class Filter = std::nullopt_t>
    void add(const pathlib::Path& name_, const std::optional<pathlib::Path>& arcname_ = std::nullopt, bool recursive = true,
             const Filter& filter = std::nullopt) {
        a->check("awx");
        std::string name = name_.str();
        std::string arcname = arcname_ ? arcname_->str() : name;
        if (a->name && abspath(name) == *a->name) return;  // (not the archive itself)
        auto t = gettarinfo(name, arcname);
        if (!t) return;
        if constexpr (!std::is_same_v<Filter, std::nullopt_t>) {
            auto fn = filter;  // (a lambda is mutable: called on a copy)
            if constexpr (is_optional<Filter>::value) {
                if (fn) t = (*fn)(*t);
            } else {
                t = fn(*t);
            }
            if (!t) return;
        }
        if (t->isreg()) {
            auto f = open_binary(name, "rb");
            addfile(*t, f);
            f->close();
        } else if (t->isdir()) {
            addfile(*t);
            if (recursive) {
                std::vector<std::string> entries;
                for (const auto& e : std::filesystem::directory_iterator(name)) entries.push_back(e.path().filename().string());
                std::sort(entries.begin(), entries.end());
                for (const auto& f : entries) add(pathlib::Path(join(name, f)), pathlib::Path(join(arcname, f)), recursive, filter);
            }
        } else {
            addfile(*t);
        }
    }

    // tf.addfile(tarinfo, fileobj=None)
    void addfile(const TarInfo& tarinfo, const std::optional<std::shared_ptr<BinaryFile>>& fileobj = std::nullopt) {
        a->check("awx");
        if (!fileobj && tarinfo.isreg() && tarinfo.d->size != 0) raise("ValueError", "fileobj not provided for non zero-size regular file");
        TarInfo t = tarinfo.copy();
        std::string buf = t.tobuf(a->format).data;
        a->fileobj->write_raw(buf);
        a->offset += static_cast<std::int64_t>(buf.size());
        if (fileobj) {
            std::int64_t left = t.d->size;
            while (left > 0) {
                std::string chunk = (*fileobj)->read_raw(std::min<std::int64_t>(left, 16 * 1024));
                if (chunk.empty()) raise("OSError", "unexpected end of data");
                a->fileobj->write_raw(chunk);
                left -= static_cast<std::int64_t>(chunk.size());
            }
            std::int64_t blocks = t.d->size / static_cast<std::int64_t>(BLOCKSIZE);
            if (auto r = t.d->size % static_cast<std::int64_t>(BLOCKSIZE)) {
                a->fileobj->write_raw(std::string(BLOCKSIZE - static_cast<std::size_t>(r), '\0'));
                ++blocks;
            }
            a->offset += blocks * static_cast<std::int64_t>(BLOCKSIZE);
        }
        a->members.push_back(t);
    }

    // tf.list(verbose=True, members=None)
    void list(bool verbose = true, const std::optional<sd::list<TarInfo>>& members = std::nullopt) {
        a->check();
        auto out = [](const std::string& s) { sd::print(" ", " ", s); };
        sd::list<TarInfo> chosen = members ? *members : all_members();
        for (const auto& t : chosen) {
            const Info& z = *t.d;
            if (verbose) {
                std::int64_t modetype = z.type == REGTYPE ? S_IFREG : z.type == SYMTYPE ? S_IFLNK : z.type == FIFOTYPE ? S_IFIFO
                                      : z.type == CHRTYPE ? S_IFCHR : z.type == DIRTYPE ? S_IFDIR : z.type == BLKTYPE ? S_IFBLK : 0;
                out(z.mode ? statmod::filemode(modetype | *z.mode) : "??????????");
                std::string who = z.uname && !z.uname->empty() ? *z.uname : std::to_string(z.uid.value_or(0));
                std::string grp = z.gname && !z.gname->empty() ? *z.gname : std::to_string(z.gid.value_or(0));
                out(who + "/" + grp);
                if (t.ischr() || t.isblk()) out(std::format("{:>10}", std::to_string(z.devmajor) + "," + std::to_string(z.devminor)));
                else out(std::format("{:>10}", z.size));
                std::time_t when = static_cast<std::time_t>(std::floor(z.mtime));
                std::tm tm{};
                ::localtime_r(&when, &tm);
                out(std::format("{}-{:02}-{:02} {:02}:{:02}:{:02}", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec));
            }
            out(z.name + (t.isdir() ? "/" : ""));
            if (verbose) {
                if (t.issym()) out("-> " + z.linkname);
                if (t.islnk()) out("link to " + z.linkname);
            }
            sd::print(" ", "\n");
        }
    }

    void close() { a->close(); }
    // `with tarfile.open(...) as tf:`: closed at the end, but after an exception without
    // writing the end of the archive (as in Python)
    void sd_exit(bool failed) {
        if (!failed) {
            close();
            return;
        }
        if (!a->extfileobj && a->fileobj) a->fileobj->close();
        a->closed = true;
    }

    std::optional<std::string> name() const { return a->name; }
    std::string mode() const { return a->mode; }
    std::int64_t format() const { return a->format; }
    dict<std::string, std::string> pax_headers() const { return a->pax_headers; }
    bool closed() const { return a->closed; }
    std::string sd_repr() const { return std::format("<tarfile.TarFile object at {:#x}>", reinterpret_cast<std::uintptr_t>(a.get())); }
};

// tarfile.open(name=None, mode="r", fileobj=None, bufsize=RECORDSIZE, format=None, compresslevel=9, preset=None, ...)
inline TarFile open_(std::optional<pathlib::Path> name_ = std::nullopt, const std::string& mode = "r",
                     std::optional<std::shared_ptr<BinaryFile>> fileobj_ = std::nullopt, std::int64_t bufsize = RECORDSIZE,
                     std::optional<std::int64_t> format = std::nullopt, std::optional<std::int64_t> compresslevel = std::nullopt,
                     std::optional<std::int64_t> preset = std::nullopt, bool ignore_zeros = false, bool dereference = false,
                     std::int64_t errorlevel = 1, std::optional<dict<std::string, std::string>> pax_headers = std::nullopt) {
    (void)bufsize;
    std::optional<std::string> name = name_ ? std::optional<std::string>(name_->str()) : std::nullopt;
    std::shared_ptr<BinaryFile> fileobj = fileobj_ ? *fileobj_ : nullptr;
    if (!name && !fileobj) raise("ValueError", "nothing to open");
    int level = static_cast<int>(compresslevel.value_or(9));
    auto make = [&](const std::string& filemode, std::shared_ptr<BinaryFile> fo, bool stream) {
        TarFile t;
        t.init(name, filemode, std::move(fo), format, stream, ignore_zeros, dereference, errorlevel, pax_headers);
        return t;
    };
    // the file a compressed archive is read from or written to (opened here unless given)
    auto raw_file = [&](const std::string& filemode) {
        if (fileobj) return std::pair{fileobj, false};
        return std::pair{open_binary(*name, filemode == "r" ? "rb" : filemode + "b"), true};
    };
    auto compressed = [&](const std::string& filemode, const std::string& comptype) -> TarFile {
        if (filemode != "r" && filemode != "w" && filemode != "x") raise("ValueError", "mode must be 'r', 'w' or 'x'");
        auto [fo, owned] = raw_file(filemode);
        std::shared_ptr<BinaryFile> wrapped;
        std::string name_for_gzip = name ? *name : "";
        if (!name && fileobj) {
            try {
                name_for_gzip = fileobj->get_name();
            } catch (const Thrown&) {
            }
        }
        if (filemode == "r") {
            std::unique_ptr<Decoder> dec;
            if (comptype == "gz") dec = std::make_unique<GzipDecoder>();
            else if (comptype == "bz2") dec = std::make_unique<Bz2Decoder>();
            else dec = std::make_unique<XzDecoder>();
            wrapped = std::make_shared<DecompressingFile>(fo, owned, std::move(dec), false);
        } else {
            std::unique_ptr<Encoder> enc;
            if (comptype == "gz") enc = std::make_unique<DeflateEncoder>(level, gzip_header(name_for_gzip, level, false));
            else if (comptype == "bz2") enc = std::make_unique<Bz2Encoder>(level);
            else enc = std::make_unique<XzEncoder>(preset);
            wrapped = std::make_shared<CompressingFile>(fo, owned, std::move(enc));
        }
        try {
            TarFile t = make(filemode, wrapped, false);
            t.a->extfileobj = false;
            return t;
        } catch (const Thrown& e) {
            wrapped->close();
            bool reading_error = isinstance<OSError>(e) || isinstance<EOFError>(e);
            if (filemode == "r" && reading_error) {
                fail<ReadError>(comptype == "gz" ? "not a gzip file" : comptype == "bz2" ? "not a bzip2 file" : "not an lzma file");
            }
            throw;
        }
    };
    if (mode == "r" || mode == "r:*") {
        std::string errors;
        for (const char* comptype : {"gz", "bz2", "xz", "tar"}) {
            std::int64_t saved = fileobj ? fileobj->tell() : 0;
            try {
                if (std::string(comptype) == "tar") return make("r", fileobj, false);
                return compressed("r", comptype);
            } catch (const Thrown& e) {
                if (!isinstance<ReadError>(e) && !isinstance<CompressionError>(e)) throw;
                errors += std::string("\n- method ") + comptype + ": " + e.exc->sd_repr();
                if (fileobj) fileobj->seek(saved, 0);
            }
        }
        fail<ReadError>("file could not be opened successfully:" + errors);
    }
    if (auto colon = mode.find(':'); colon != std::string::npos) {
        std::string filemode = mode.substr(0, colon), comptype = mode.substr(colon + 1);
        if (filemode.empty()) filemode = "r";
        if (comptype.empty()) comptype = "tar";
        if (comptype == "tar") {
            if (filemode != "r" && filemode != "a" && filemode != "w" && filemode != "x")
                raise("ValueError", "mode must be 'r', 'a', 'w' or 'x'");
            return make(filemode, fileobj, false);
        }
        if (comptype != "gz" && comptype != "bz2" && comptype != "xz") fail<CompressionError>("unknown compression type " + repr_str(comptype));
        return compressed(filemode, comptype);
    }
    if (auto bar = mode.find('|'); bar != std::string::npos) {
        std::string filemode = mode.substr(0, bar), comptype = mode.substr(bar + 1);
        if (filemode.empty()) filemode = "r";
        if (comptype.empty()) comptype = "tar";
        if (filemode != "r" && filemode != "w") raise("ValueError", "mode must be 'r' or 'w'");
        bool owned = !fileobj;
        std::shared_ptr<BinaryFile> fo = fileobj ? fileobj : open_binary(*name, filemode + "b");
        std::string head;
        if (comptype == "*") {  // (the first block says which)
            head = fo->read_raw(static_cast<std::int64_t>(BLOCKSIZE));
            if (head.starts_with("\x1f\x8b\x08")) comptype = "gz";
            else if (head.substr(0, 3) == "BZh" && head.substr(4, 6) == "1AY&SY") comptype = "bz2";
            else if (head.starts_with(std::string("\x5d\x00\x00\x80", 4)) || head.starts_with("\xfd" "7zXZ")) comptype = "xz";
            else comptype = "tar";
        }
        std::shared_ptr<BinaryFile> stream;
        if (comptype == "tar") {
            // (in "r|*", the block already read goes first)
            stream = head.empty() ? fo : std::make_shared<DecompressingFile>(fo, owned, std::make_unique<PlainDecoder>(), true, head);
        } else if (comptype != "gz" && comptype != "bz2" && comptype != "xz") {
            if (owned) fo->close();
            fail<CompressionError>("unknown compression type " + repr_str(comptype));
        } else if (filemode == "r") {
            std::unique_ptr<Decoder> dec;
            if (comptype == "gz") dec = std::make_unique<GzipDecoder>();
            else if (comptype == "bz2") dec = std::make_unique<Bz2Decoder>();
            else dec = std::make_unique<XzDecoder>();
            stream = std::make_shared<DecompressingFile>(fo, owned, std::move(dec), true, head);
        } else {
            std::unique_ptr<Encoder> enc;
            if (comptype == "gz") enc = std::make_unique<DeflateEncoder>(level, gzip_header(name.value_or(""), level, true));
            else if (comptype == "bz2") enc = std::make_unique<Bz2Encoder>(level);
            else enc = std::make_unique<XzEncoder>(preset);
            stream = std::make_shared<CompressingFile>(fo, owned, std::move(enc));
        }
        TarFile t = make(filemode, stream, true);
        t.a->extfileobj = comptype == "tar" && !owned;
        return t;
    }
    if (mode == "a" || mode == "w" || mode == "x") return make(mode, fileobj, false);
    raise("ValueError", "undiscernible mode");
}

// tarfile.is_tarfile(name): can it be opened as a tar archive?
inline bool is_tarfile(const pathlib::Path& name) {
    try {
        open_(name).close();
        return true;
    } catch (const Thrown& e) {
        if (isinstance<TarError>(e)) return false;
        throw;
    }
}
inline bool is_tarfile(const std::shared_ptr<BinaryFile>& file) {
    std::int64_t pos = file->tell();
    try {
        open_(std::nullopt, "r", file).close();
        file->seek(pos, 0);
        return true;
    } catch (const Thrown& e) {
        if (isinstance<TarError>(e)) return false;
        throw;
    }
}

}  // namespace sd::tarfile
