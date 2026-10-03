// The `zipfile` module: ZIP archives, read and written (stored, deflated, bzip2 and LZMA
// members, and ZIP64 for big ones), ported from CPython's zipfile.py so the archives and the
// errors are Python's. A ZipFile works over any binary file object: one it opens by name, an
// open file or an io.BytesIO; zf.open(name) gives a binary file object for a member.
// Encrypted members are read (legacy ZipCrypto, as in Python; there's no writing them).
// Not supported: Zstandard members, zipfile.Path, PyZipFile, ZipInfo.from_file and metadata_encoding=.
#pragma once

#include <bzlib.h>
#include <sys/stat.h>

#include <algorithm>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <format>
#include <memory>
#include <optional>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "lzma.hpp"
#include "pathlib.hpp"
#include "stat.hpp"
#include "zlib.hpp"

namespace sd::zipfile {

inline constexpr std::int64_t ZIP_STORED = 0, ZIP_DEFLATED = 8, ZIP_BZIP2 = 12, ZIP_LZMA = 14;
inline constexpr std::int64_t ZIP64_LIMIT = (std::int64_t{1} << 31) - 1, ZIP_FILECOUNT_LIMIT = (1 << 16) - 1,
                              ZIP_MAX_COMMENT = (1 << 16) - 1;
// Versions needed to extract (not LZMA_VERSION..., which liblzma defines)
inline constexpr std::int64_t VERSION_DEFAULT = 20, VERSION_ZIP64 = 45, VERSION_BZIP2 = 46, VERSION_LZMA = 63,
                              VERSION_MAX_EXTRACT = 63;
// General purpose flag bits
inline constexpr std::int64_t MASK_ENCRYPTED = 1 << 0, MASK_COMPRESS_OPTION_1 = 1 << 1, MASK_USE_DATA_DESCRIPTOR = 1 << 3,
                              MASK_COMPRESSED_PATCH = 1 << 5, MASK_STRONG_ENCRYPTION = 1 << 6, MASK_UTF_FILENAME = 1 << 11;
// Record sizes: end of central directory, central directory entry, local header, ZIP64 locator and end record
inline constexpr std::size_t SIZE_END_CENT_DIR = 22, SIZE_CENTRAL_DIR = 46, SIZE_FILE_HEADER = 30,
                             SIZE_END_CENT_DIR64_LOCATOR = 20, SIZE_END_CENT_DIR64 = 56;
inline constexpr std::size_t MIN_READ_SIZE = 4096;

struct BadZipFile : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "zipfile.BadZipFile"; }
};
struct LargeZipFile : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "zipfile.LargeZipFile"; }
};
[[noreturn]] inline void bad(const std::string& msg) { throw Thrown{std::make_shared<BadZipFile>(msg)}; }
[[noreturn]] inline void large(const std::string& msg) { throw Thrown{std::make_shared<LargeZipFile>(msg)}; }

// Python's warnings.warn(), without the file and line it would show.
inline void warn(const std::string& msg) {
    std::fflush(stdout);
    std::fprintf(stderr, "UserWarning: %s\n", msg.c_str());
}

inline void check_compression(std::int64_t compression) {
    if (compression != ZIP_STORED && compression != ZIP_DEFLATED && compression != ZIP_BZIP2 && compression != ZIP_LZMA)
        raise<NotImplementedError>("That compression method is not supported");
}

inline std::string compressor_name(std::int64_t type) {
    static const std::unordered_map<std::int64_t, std::string> names = {
        {0, "store"},   {1, "shrink"},     {2, "reduce"},  {3, "reduce"}, {4, "reduce"}, {5, "reduce"},
        {6, "implode"}, {7, "tokenize"},   {8, "deflate"}, {9, "deflate64"}, {10, "implode"}, {12, "bzip2"},
        {14, "lzma"},   {18, "terse"},     {19, "lz77"},   {93, "zstd"},  {97, "wavpack"}, {98, "ppmd"},
    };
    auto it = names.find(type);
    return it != names.end() ? it->second : std::to_string(type);
}

// ---- little-endian fields --------------------------------------------------------------

inline void put(std::string& out, std::uint64_t v, int n) {
    for (int k = 0; k < n; ++k) out += static_cast<char>((v >> (8 * k)) & 0xFF);
}
inline std::int64_t get(std::string_view s, std::size_t at, int n) {
    std::uint64_t v = 0;
    for (int k = n - 1; k >= 0; --k) v = (v << 8) | static_cast<unsigned char>(s[at + static_cast<std::size_t>(k)]);
    return static_cast<std::int64_t>(v);
}
inline std::uint32_t crc32_of(std::string_view data, std::uint32_t crc = 0) {
    while (!data.empty()) {  // (zlib takes at most 4 GiB at a time)
        auto n = static_cast<uInt>(std::min<std::size_t>(data.size(), 1u << 30));
        crc = static_cast<std::uint32_t>(::crc32(crc, reinterpret_cast<const Bytef*>(data.data()), n));
        data.remove_prefix(n);
    }
    return crc;
}

// ---- file names: UTF-8, or the historical code page 437 ---------------------------------

inline constexpr char32_t CP437_HIGH[128] = {
    0x00C7, 0x00FC, 0x00E9, 0x00E2, 0x00E4, 0x00E0, 0x00E5, 0x00E7, 0x00EA, 0x00EB, 0x00E8, 0x00EF, 0x00EE, 0x00EC, 0x00C4, 0x00C5,
    0x00C9, 0x00E6, 0x00C6, 0x00F4, 0x00F6, 0x00F2, 0x00FB, 0x00F9, 0x00FF, 0x00D6, 0x00DC, 0x00A2, 0x00A3, 0x00A5, 0x20A7, 0x0192,
    0x00E1, 0x00ED, 0x00F3, 0x00FA, 0x00F1, 0x00D1, 0x00AA, 0x00BA, 0x00BF, 0x2310, 0x00AC, 0x00BD, 0x00BC, 0x00A1, 0x00AB, 0x00BB,
    0x2591, 0x2592, 0x2593, 0x2502, 0x2524, 0x2561, 0x2562, 0x2556, 0x2555, 0x2563, 0x2551, 0x2557, 0x255D, 0x255C, 0x255B, 0x2510,
    0x2514, 0x2534, 0x252C, 0x251C, 0x2500, 0x253C, 0x255E, 0x255F, 0x255A, 0x2554, 0x2569, 0x2566, 0x2560, 0x2550, 0x256C, 0x2567,
    0x2568, 0x2564, 0x2565, 0x2559, 0x2558, 0x2552, 0x2553, 0x256B, 0x256A, 0x2518, 0x250C, 0x2588, 0x2584, 0x258C, 0x2590, 0x2580,
    0x03B1, 0x00DF, 0x0393, 0x03C0, 0x03A3, 0x03C3, 0x00B5, 0x03C4, 0x03A6, 0x0398, 0x03A9, 0x03B4, 0x221E, 0x03C6, 0x03B5, 0x2229,
    0x2261, 0x00B1, 0x2265, 0x2264, 0x2320, 0x2321, 0x00F7, 0x2248, 0x00B0, 0x2219, 0x00B7, 0x221A, 0x207F, 0x00B2, 0x25A0, 0x00A0,
};

inline void append_utf8(std::string& out, char32_t c) {
    if (c < 0x80) {
        out += static_cast<char>(c);
    } else if (c < 0x800) {
        out += static_cast<char>(0xC0 | (c >> 6));
        out += static_cast<char>(0x80 | (c & 0x3F));
    } else {
        out += static_cast<char>(0xE0 | (c >> 12));
        out += static_cast<char>(0x80 | ((c >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (c & 0x3F));
    }
}

inline std::string decode_cp437(std::string_view raw) {
    std::string out;
    for (unsigned char c : raw) append_utf8(out, c < 0x80 ? c : CP437_HIGH[c - 0x80]);
    return out;
}

// The name as stored, and the flag bits with or without the UTF-8 flag (Python's
// _encodeFilenameFlags): ASCII if it can be (or code page 437 without the flag), else UTF-8.
inline std::pair<std::string, std::int64_t> encode_name(const std::string& name, std::int64_t flag_bits) {
    bool ascii = std::all_of(name.begin(), name.end(), [](char c) { return static_cast<unsigned char>(c) < 0x80; });
    if (ascii) return {name, flag_bits & ~MASK_UTF_FILENAME};
    if (!(flag_bits & MASK_UTF_FILENAME)) {
        std::string out;
        bool ok = true;
        for (std::size_t i = 0; i < name.size() && ok;) {
            char32_t c = 0;
            std::size_t width = utf8_at(name, i, c);
            i += width ? width : 1;
            if (width == 0) {
                ok = false;
                break;
            }
            if (c < 0x80) {
                out += static_cast<char>(c);
                continue;
            }
            auto at = std::find(std::begin(CP437_HIGH), std::end(CP437_HIGH), c);
            ok = at != std::end(CP437_HIGH);
            if (ok) out += static_cast<char>(0x80 + (at - std::begin(CP437_HIGH)));
        }
        if (ok) return {out, flag_bits};
    }
    return {name, flag_bits | MASK_UTF_FILENAME};
}

// Python's _sanitize_filename: a name ends at a NUL (a trick of viruses).
inline std::string sanitize_filename(std::string name) {
    if (auto nul = name.find('\0'); nul != std::string::npos) name.resize(nul);
    return name;
}

// posixpath.normpath
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
        if (comp != ".." || (!slashes && parts.empty()) || (!parts.empty() && parts.back() == ".."))
            parts.push_back(comp);
        else if (!parts.empty())
            parts.pop_back();
    }
    std::string out(slashes, '/');
    for (std::size_t i = 0; i < parts.size(); ++i) out += (i ? "/" : "") + parts[i];
    return out.empty() ? "." : out;
}

// ---- ZipInfo ---------------------------------------------------------------------------

using DateTime = std::tuple<std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t>;

struct Info {
    std::string orig_filename, filename;
    DateTime date_time{1980, 1, 1, 0, 0, 0};
    std::int64_t compress_type = ZIP_STORED;
    std::optional<std::int64_t> compress_level;
    bytes comment, extra;
    std::int64_t create_system = 3, create_version = VERSION_DEFAULT, extract_version = VERSION_DEFAULT, reserved = 0,
                 flag_bits = 0, volume = 0, internal_attr = 0, external_attr = 0, header_offset = 0, CRC = 0,
                 compress_size = 0, file_size = 0;
    std::optional<std::int64_t> end_offset;  // where the next local header (or the central directory) starts
    std::int64_t raw_time = 0;               // the MS-DOS time as read (an encrypted member's check byte may come from it)
};

inline std::int64_t dos_date(const DateTime& dt) {
    return (std::get<0>(dt) - 1980) << 9 | std::get<1>(dt) << 5 | std::get<2>(dt);
}
inline std::int64_t dos_time(const DateTime& dt) {
    return std::get<3>(dt) << 11 | std::get<4>(dt) << 5 | std::get<5>(dt) / 2;
}

// A member's description: a handle, like Python's object (changing the ZipInfo from
// infolist() changes the archive's).
struct ZipInfo {
    std::shared_ptr<Info> d;
    ZipInfo() = default;  // (unset until it's assigned)
    explicit ZipInfo(std::shared_ptr<Info> info) : d(std::move(info)) {}

#define SD_ZIPINFO_FIELD(T, name)                     \
    T name() const { return d->name; }                \
    void set_##name(T value) { d->name = std::move(value); }
    SD_ZIPINFO_FIELD(std::string, orig_filename)
    SD_ZIPINFO_FIELD(std::string, filename)
    SD_ZIPINFO_FIELD(DateTime, date_time)
    SD_ZIPINFO_FIELD(std::int64_t, compress_type)
    SD_ZIPINFO_FIELD(std::optional<std::int64_t>, compress_level)
    SD_ZIPINFO_FIELD(bytes, comment)
    SD_ZIPINFO_FIELD(bytes, extra)
    SD_ZIPINFO_FIELD(std::int64_t, create_system)
    SD_ZIPINFO_FIELD(std::int64_t, create_version)
    SD_ZIPINFO_FIELD(std::int64_t, extract_version)
    SD_ZIPINFO_FIELD(std::int64_t, reserved)
    SD_ZIPINFO_FIELD(std::int64_t, flag_bits)
    SD_ZIPINFO_FIELD(std::int64_t, volume)
    SD_ZIPINFO_FIELD(std::int64_t, internal_attr)
    SD_ZIPINFO_FIELD(std::int64_t, external_attr)
    SD_ZIPINFO_FIELD(std::int64_t, header_offset)
    SD_ZIPINFO_FIELD(std::int64_t, CRC)
    SD_ZIPINFO_FIELD(std::int64_t, compress_size)
    SD_ZIPINFO_FIELD(std::int64_t, file_size)
#undef SD_ZIPINFO_FIELD

    bool is_dir() const { return d->filename.ends_with('/'); }

    std::string sd_repr() const {
        std::string out = "<ZipInfo filename=" + repr_str(d->filename);
        if (d->compress_type != ZIP_STORED) out += " compress_type=" + compressor_name(d->compress_type);
        std::int64_t hi = d->external_attr >> 16, lo = d->external_attr & 0xFFFF;
        if (hi) out += " filemode=" + repr_str(statmod::filemode(hi));
        if (lo) out += std::format(" external_attr={:#x}", lo);
        bool dir = is_dir();
        if (!dir || d->file_size) out += " file_size=" + std::to_string(d->file_size);
        if ((!dir || d->compress_size) && (d->compress_type != ZIP_STORED || d->file_size != d->compress_size))
            out += " compress_size=" + std::to_string(d->compress_size);
        return out + ">";
    }
    bool operator==(const ZipInfo& other) const { return d == other.d; }  // (identity, as in Python)
};

// zipfile.ZipInfo(filename="NoName", date_time=(1980, 1, 1, 0, 0, 0))
inline ZipInfo make_info(const std::string& filename = "NoName", const DateTime& date_time = {1980, 1, 1, 0, 0, 0}) {
    auto d = std::make_shared<Info>();
    d->orig_filename = filename;
    d->filename = sanitize_filename(filename);
    d->date_time = date_time;
    if (std::get<0>(date_time) < 1980) raise("ValueError", "ZIP does not support timestamps before 1980");
    return ZipInfo(std::move(d));
}

// The local file header (Python's ZipInfo.FileHeader).
inline std::string file_header(Info& z, bool zip64) {
    std::int64_t crc = z.CRC, compress_size = z.compress_size, file_size = z.file_size;
    if (z.flag_bits & MASK_USE_DATA_DESCRIPTOR) crc = compress_size = file_size = 0;  // (they follow the data)
    std::string extra = z.extra.data;
    std::int64_t min_version = 0;
    if (zip64) {
        put(extra, 1, 2);
        put(extra, 16, 2);
        put(extra, static_cast<std::uint64_t>(file_size), 8);
        put(extra, static_cast<std::uint64_t>(compress_size), 8);
        file_size = compress_size = 0xFFFFFFFF;
        min_version = VERSION_ZIP64;
    }
    if (z.compress_type == ZIP_BZIP2) min_version = std::max(VERSION_BZIP2, min_version);
    else if (z.compress_type == ZIP_LZMA) min_version = std::max(VERSION_LZMA, min_version);
    z.extract_version = std::max(min_version, z.extract_version);
    z.create_version = std::max(min_version, z.create_version);
    auto [name, flag_bits] = encode_name(z.filename, z.flag_bits);
    std::string h = "PK\x03\x04";
    put(h, static_cast<std::uint64_t>(z.extract_version), 1);
    put(h, static_cast<std::uint64_t>(z.reserved), 1);
    put(h, static_cast<std::uint64_t>(flag_bits), 2);
    put(h, static_cast<std::uint64_t>(z.compress_type), 2);
    put(h, static_cast<std::uint64_t>(dos_time(z.date_time)), 2);
    put(h, static_cast<std::uint64_t>(dos_date(z.date_time)), 2);
    put(h, static_cast<std::uint64_t>(crc), 4);
    put(h, static_cast<std::uint64_t>(compress_size), 4);
    put(h, static_cast<std::uint64_t>(file_size), 4);
    put(h, name.size(), 2);
    put(h, extra.size(), 2);
    return h + name + extra;
}

// The extra field's ZIP64 sizes and offset, and a Unicode path (Python's _decodeExtra).
inline void decode_extra(Info& z, std::uint32_t filename_crc) {
    std::string_view extra = z.extra.data;
    while (extra.size() >= 4) {
        std::int64_t tp = get(extra, 0, 2), ln = get(extra, 2, 2);
        if (static_cast<std::size_t>(ln) + 4 > extra.size()) bad(std::format("Corrupt extra field {:04x} (size={})", tp, ln));
        std::string_view data = extra.substr(4, static_cast<std::size_t>(ln));
        if (tp == 0x0001) {  // ZIP64: the sizes and offset too big for their fields
            auto take = [&](const char* field) {
                if (data.size() < 8) bad(std::string("Corrupt zip64 extra field. ") + field + " not found.");
                std::int64_t v = get(data, 0, 8);
                data.remove_prefix(8);
                return v;
            };
            if (z.file_size == 0xFFFFFFFF) z.file_size = take("File size");
            if (z.compress_size == 0xFFFFFFFF) z.compress_size = take("Compress size");
            if (z.header_offset == 0xFFFFFFFF) z.header_offset = take("Header offset");
        } else if (tp == 0x7075) {  // Unicode Path Extra Field
            if (data.size() < 5) bad("Corrupt unicode path extra field (0x7075)");
            if (static_cast<unsigned char>(data[0]) == 1 && static_cast<std::uint32_t>(get(data, 1, 4)) == filename_crc &&
                data.size() > 5)
                z.filename = sanitize_filename(std::string(data.substr(5)));
        }
        extra.remove_prefix(static_cast<std::size_t>(ln) + 4);
    }
}

// ---- compressors and decompressors for each method ---------------------------------------

struct Compressor {
    virtual ~Compressor() = default;
    virtual std::string compress(std::string_view data) = 0;
    virtual std::string flush() = 0;
};

struct Deflater final : Compressor {
    z_stream zs{};
    explicit Deflater(std::optional<std::int64_t> level) {
        int lvl = level ? static_cast<int>(*level) : Z_DEFAULT_COMPRESSION;
        if ((level && (*level < -1 || *level > 9)) || deflateInit2(&zs, lvl, Z_DEFLATED, -15, 8, Z_DEFAULT_STRATEGY) != Z_OK)
            raise("ValueError", "Invalid initialization option");
    }
    ~Deflater() override { deflateEnd(&zs); }
    std::string run(std::string_view data, int flush) {
        std::string out;
        char chunk[64 * 1024];
        zs.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(data.data()));
        zs.avail_in = static_cast<uInt>(data.size());
        int rc;
        do {
            zs.next_out = reinterpret_cast<Bytef*>(chunk);
            zs.avail_out = sizeof chunk;
            rc = deflate(&zs, flush);
            if (rc == Z_STREAM_ERROR) zlib::fail("Error -2 while compressing data");
            out.append(chunk, sizeof chunk - zs.avail_out);
        } while (zs.avail_out == 0 || (flush == Z_FINISH && rc != Z_STREAM_END));
        return out;
    }
    std::string compress(std::string_view data) override {
        std::string out;
        while (!data.empty()) {
            std::size_t n = std::min<std::size_t>(data.size(), 1u << 30);
            out += run(data.substr(0, n), Z_NO_FLUSH);
            data.remove_prefix(n);
        }
        return out;
    }
    std::string flush() override { return run({}, Z_FINISH); }
};

struct Bz2Compressor final : Compressor {
    bz_stream bs{};
    explicit Bz2Compressor(std::optional<std::int64_t> level) {
        int lvl = level ? static_cast<int>(*level) : 9;
        if (lvl < 1 || lvl > 9) raise("ValueError", "compresslevel must be between 1 and 9");
        if (BZ2_bzCompressInit(&bs, lvl, 0, 0) != BZ_OK) raise("MemoryError", "");
    }
    ~Bz2Compressor() override { BZ2_bzCompressEnd(&bs); }
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
            if (rc < 0) raise("OSError", "Invalid data stream");
            out.append(chunk, sizeof chunk - bs.avail_out);
        } while (action == BZ_FINISH ? rc != BZ_STREAM_END : bs.avail_in > 0);
        return out;
    }
    std::string compress(std::string_view data) override {
        std::string out;
        while (!data.empty()) {
            std::size_t n = std::min<std::size_t>(data.size(), 1u << 30);
            out += run(data.substr(0, n), BZ_RUN);
            data.remove_prefix(n);
        }
        return out;
    }
    std::string flush() override { return run({}, BZ_FINISH); }
};

// ZIP's LZMA: a header (the LZMA SDK version 9.4, the properties' size and the properties),
// then raw LZMA1 with an end-of-stream marker. The options are the default preset's.
struct LzmaCompressor final : Compressor {
    lzma_stream ls = LZMA_STREAM_INIT;
    lzma_options_lzma options{};
    std::string header;
    LzmaCompressor() {
        if (lzma_lzma_preset(&options, LZMA_PRESET_DEFAULT)) lzma::fail("Invalid or unsupported options");
        lzma_filter filters[] = {{LZMA_FILTER_LZMA1, &options}, {LZMA_VLI_UNKNOWN, nullptr}};
        std::uint32_t size = 0;
        lzma::check(lzma_properties_size(&size, filters));
        std::string props(size, '\0');
        lzma::check(lzma_properties_encode(filters, reinterpret_cast<std::uint8_t*>(props.data())));
        header = std::string("\x09\x04", 2);
        put(header, props.size(), 2);
        header += props;
        lzma::check(lzma_raw_encoder(&ls, filters));
    }
    ~LzmaCompressor() override { lzma_end(&ls); }
    std::string run(std::string_view data, lzma_action action) {
        std::string out = std::exchange(header, "");
        std::uint8_t chunk[64 * 1024];
        ls.next_in = reinterpret_cast<const std::uint8_t*>(data.data());
        ls.avail_in = data.size();
        lzma_ret rc;
        do {
            ls.next_out = chunk;
            ls.avail_out = sizeof chunk;
            rc = lzma_code(&ls, action);
            lzma::check(rc);
            out.append(reinterpret_cast<const char*>(chunk), sizeof chunk - ls.avail_out);
        } while (action == LZMA_FINISH ? rc != LZMA_STREAM_END : ls.avail_in > 0 || ls.avail_out == 0);
        return out;
    }
    std::string compress(std::string_view data) override { return run(data, LZMA_RUN); }
    std::string flush() override { return run({}, LZMA_FINISH); }
};

inline std::unique_ptr<Compressor> make_compressor(std::int64_t type, std::optional<std::int64_t> level) {
    if (type == ZIP_DEFLATED) return std::make_unique<Deflater>(level);
    if (type == ZIP_BZIP2) return std::make_unique<Bz2Compressor>(level);
    if (type == ZIP_LZMA) return std::make_unique<LzmaCompressor>();  // (the level is ignored, as in Python)
    return nullptr;
}

// Input is fed in; each call gives at most max_out bytes of output, keeping what it didn't use.
struct Decompressor {
    std::string pending;
    std::size_t used = 0;  // of pending
    bool eof = false;
    virtual ~Decompressor() = default;
    bool needs_input() const { return used >= pending.size(); }
    std::string decompress(std::string_view data, std::size_t max_out) {
        if (used > 0 && used == pending.size()) pending.clear(), used = 0;
        pending += data;
        std::string out;
        char chunk[64 * 1024];
        while (!eof && out.size() < max_out) {
            std::size_t want = std::min(sizeof chunk, max_out - out.size());
            std::size_t before = used;
            std::size_t got = step(chunk, want);
            out.append(chunk, got);
            if (got < want && used == before) break;  // (it needs more input)
            if (got == 0 && needs_input()) break;
        }
        return out;
    }
    // Decompresses from pending[used:] into out (at most n bytes), moving `used` on.
    virtual std::size_t step(char* out, std::size_t n) = 0;
};

struct Inflater final : Decompressor {
    z_stream zs{};
    Inflater() {
        if (inflateInit2(&zs, -15) != Z_OK) zlib::fail("Error while initializing decompression");
    }
    ~Inflater() override { inflateEnd(&zs); }
    std::size_t step(char* out, std::size_t n) override {
        zs.next_in = reinterpret_cast<Bytef*>(pending.data() + used);
        zs.avail_in = static_cast<uInt>(std::min<std::size_t>(pending.size() - used, 1u << 30));
        zs.next_out = reinterpret_cast<Bytef*>(out);
        zs.avail_out = static_cast<uInt>(n);
        uInt in_before = zs.avail_in;
        int rc = inflate(&zs, Z_SYNC_FLUSH);
        used += in_before - zs.avail_in;
        if (rc == Z_STREAM_END) eof = true;
        else if (rc != Z_OK && rc != Z_BUF_ERROR)
            zlib::fail("Error " + std::to_string(rc) + " while decompressing data: " + (zs.msg ? zs.msg : "invalid data"));
        return n - zs.avail_out;
    }
};

struct Bz2Decompressor final : Decompressor {
    bz_stream bs{};
    Bz2Decompressor() {
        if (BZ2_bzDecompressInit(&bs, 0, 0) != BZ_OK) raise("MemoryError", "");
    }
    ~Bz2Decompressor() override { BZ2_bzDecompressEnd(&bs); }
    std::size_t step(char* out, std::size_t n) override {
        bs.next_in = pending.data() + used;
        bs.avail_in = static_cast<unsigned>(std::min<std::size_t>(pending.size() - used, 1u << 30));
        bs.next_out = out;
        bs.avail_out = static_cast<unsigned>(n);
        unsigned in_before = bs.avail_in;
        int rc = BZ2_bzDecompress(&bs);
        used += in_before - bs.avail_in;
        if (rc == BZ_STREAM_END) eof = true;
        else if (rc != BZ_OK) raise("OSError", "Invalid data stream");
        return n - bs.avail_out;
    }
};

struct LzmaDecompressor final : Decompressor {
    lzma_stream ls = LZMA_STREAM_INIT;
    bool started = false;
    ~LzmaDecompressor() override { lzma_end(&ls); }
    std::size_t step(char* out, std::size_t n) override {
        if (!started) {  // the header first: 2 bytes of version, the properties' size, the properties
            std::string_view in(pending.data() + used, pending.size() - used);
            if (in.size() <= 4) return 0;
            auto psize = static_cast<std::size_t>(get(in, 2, 2));
            if (in.size() <= 4 + psize) return 0;
            lzma_filter filters[] = {{LZMA_FILTER_LZMA1, nullptr}, {LZMA_VLI_UNKNOWN, nullptr}};
            lzma::check(lzma_properties_decode(&filters[0], nullptr, reinterpret_cast<const std::uint8_t*>(in.data() + 4), psize));
            lzma_ret rc = lzma_raw_decoder(&ls, filters);
            std::free(filters[0].options);
            lzma::check(rc);
            used += 4 + psize;
            started = true;
        }
        ls.next_in = reinterpret_cast<const std::uint8_t*>(pending.data() + used);
        ls.avail_in = pending.size() - used;
        ls.next_out = reinterpret_cast<std::uint8_t*>(out);
        ls.avail_out = n;
        std::size_t in_before = ls.avail_in;
        lzma_ret rc = lzma_code(&ls, LZMA_RUN);
        used += in_before - ls.avail_in;
        if (rc == LZMA_STREAM_END) eof = true;
        else if (rc != LZMA_BUF_ERROR) lzma::check(rc);
        return n - ls.avail_out;
    }
};

inline std::unique_ptr<Decompressor> make_decompressor(std::int64_t type) {
    check_compression(type);
    if (type == ZIP_DEFLATED) return std::make_unique<Inflater>();
    if (type == ZIP_BZIP2) return std::make_unique<Bz2Decompressor>();
    if (type == ZIP_LZMA) return std::make_unique<LzmaDecompressor>();
    return nullptr;
}

// ---- the archive -----------------------------------------------------------------------

struct Archive {
    std::shared_ptr<BinaryFile> fp;  // none once it's closed
    bool file_passed = false;        // (then closing the archive leaves the file open)
    std::optional<std::string> filename;
    std::string mode;
    std::int64_t compression = ZIP_STORED;
    std::optional<std::int64_t> compresslevel;
    bool allow_zip64 = true, strict_timestamps = true, did_modify = false, seekable = true, writing = false;
    std::vector<ZipInfo> filelist;
    std::unordered_map<std::string, ZipInfo> name_to_info;
    bytes comment;
    std::int64_t start_dir = 0;
    std::int64_t written = 0;  // (the position, in a file that can't tell)
    std::optional<bytes> pwd;  // for encrypted members (setpassword)

    Archive() = default;
    Archive(const Archive&) = delete;
    Archive& operator=(const Archive&) = delete;
    ~Archive() {  // like Python's __del__: an archive nobody closed still gets its central directory
        try {
            close();
        } catch (...) {
        }
    }

    std::int64_t tell() { return seekable ? fp->tell() : written; }
    void write(const std::string& data) {
        fp->write_raw(data);
        written += static_cast<std::int64_t>(data.size());
    }
    std::string read_at(std::int64_t offset, std::size_t n) {
        fp->seek(offset, 0);
        return fp->read_raw(static_cast<std::int64_t>(n));
    }

    void close();
    void write_end_record();
    void real_get_contents();
    void writecheck(const ZipInfo& zinfo);
    void add(const ZipInfo& zinfo) {
        filelist.push_back(zinfo);
        name_to_info.insert_or_assign(zinfo.d->filename, zinfo);
    }
};

// The end of central directory record, as Python's _EndRecData gives it: the 8 fields, the
// comment, and where the record is.
struct EndRecord {
    std::int64_t disk_number, disk_start, entries_this_disk, entries_total, size, offset;
    bytes comment;
    std::int64_t location;
};

inline std::optional<EndRecord> end_rec_data64(Archive& a, std::int64_t offset, EndRecord rec) {
    offset -= SIZE_END_CENT_DIR64_LOCATOR;
    if (offset < 0) return rec;  // too small for a ZIP64 end record
    std::string data = a.read_at(offset, SIZE_END_CENT_DIR64_LOCATOR);
    if (data.size() != SIZE_END_CENT_DIR64_LOCATOR) raise("OSError", "Unknown I/O error");
    if (!data.starts_with("PK\x06\x07")) return rec;
    std::int64_t diskno = get(data, 4, 4), reloff = get(data, 8, 8), disks = get(data, 16, 4);
    if (diskno != 0 || disks > 1) bad("zipfiles that span multiple disks are not supported");
    offset -= SIZE_END_CENT_DIR64;
    if (reloff > offset) bad("Corrupt zip64 end of central directory locator");
    std::int64_t extrasz = offset - reloff;
    data = a.read_at(reloff, SIZE_END_CENT_DIR64);
    if (data.size() != SIZE_END_CENT_DIR64) raise("OSError", "Unknown I/O error");
    if (!data.starts_with("PK\x06\x06") && reloff != offset) {  // prepended data, perhaps
        extrasz = 0;
        data = a.read_at(offset, SIZE_END_CENT_DIR64);
        if (data.size() != SIZE_END_CENT_DIR64) raise("OSError", "Unknown I/O error");
    }
    if (!data.starts_with("PK\x06\x06")) bad("Zip64 end of central directory record not found");
    std::int64_t sz = get(data, 4, 8), dirsize = get(data, 40, 8), diroffset = get(data, 48, 8);
    if (diroffset + dirsize != reloff || sz + 12 != static_cast<std::int64_t>(SIZE_END_CENT_DIR64) + extrasz)
        bad("Corrupt zip64 end of central directory record");
    rec.disk_number = get(data, 16, 4);
    rec.disk_start = get(data, 20, 4);
    rec.entries_this_disk = get(data, 24, 8);
    rec.entries_total = get(data, 32, 8);
    rec.size = dirsize;
    rec.offset = diroffset;
    rec.location = offset - extrasz;
    return rec;
}

inline EndRecord unpack_end(std::string_view data, bytes comment, std::int64_t location) {
    return {get(data, 4, 2), get(data, 6, 2), get(data, 8, 2), get(data, 10, 2), get(data, 12, 4), get(data, 16, 4),
            std::move(comment), location};
}

inline std::optional<EndRecord> end_rec_data(Archive& a) {
    a.fp->seek(0, 2);
    std::int64_t filesize = a.fp->tell();
    if (filesize >= static_cast<std::int64_t>(SIZE_END_CENT_DIR)) {  // no archive comment: the record is last
        std::string data = a.read_at(filesize - static_cast<std::int64_t>(SIZE_END_CENT_DIR), SIZE_END_CENT_DIR);
        if (data.size() == SIZE_END_CENT_DIR && data.starts_with("PK\x05\x06") && data.ends_with(std::string(2, '\0')))
            return end_rec_data64(a, filesize - static_cast<std::int64_t>(SIZE_END_CENT_DIR),
                                  unpack_end(data, bytes(), filesize - static_cast<std::int64_t>(SIZE_END_CENT_DIR)));
    }
    // A comment (up to 64 KiB) follows the record: look for its signature.
    std::int64_t max_comment_start = std::max<std::int64_t>(filesize - ZIP_MAX_COMMENT - static_cast<std::int64_t>(SIZE_END_CENT_DIR), 0);
    std::string data = a.read_at(max_comment_start, ZIP_MAX_COMMENT + SIZE_END_CENT_DIR);
    std::size_t start = data.rfind("PK\x05\x06");
    if (start == std::string::npos) return std::nullopt;
    if (data.size() - start < SIZE_END_CENT_DIR) return std::nullopt;  // corrupt
    std::string_view rec(data.data() + start, SIZE_END_CENT_DIR);
    auto comment_size = static_cast<std::size_t>(get(rec, 20, 2));
    bytes comment(data.substr(start + SIZE_END_CENT_DIR, comment_size));
    auto location = max_comment_start + static_cast<std::int64_t>(start);
    return end_rec_data64(a, location, unpack_end(rec, std::move(comment), location));
}

// Python's _RealGetContents: the central directory.
inline void Archive::real_get_contents() {
    std::optional<EndRecord> end;
    try {
        end = end_rec_data(*this);
    } catch (const Thrown& t) {
        if (!isinstance<OSError>(t)) throw;
        bad("File is not a zip file");
    }
    if (!end) bad("File is not a zip file");
    comment = end->comment;
    std::int64_t concat = end->location - end->size - end->offset;  // (data before the archive)
    start_dir = end->offset + concat;
    if (start_dir < 0) bad("Bad offset for central directory");
    std::string data = read_at(start_dir, static_cast<std::size_t>(end->size));
    std::size_t pos = 0;
    while (static_cast<std::int64_t>(pos) < end->size) {
        if (data.size() - std::min(pos, data.size()) < SIZE_CENTRAL_DIR) bad("Truncated central directory");
        std::string_view cd(data.data() + pos, SIZE_CENTRAL_DIR);
        if (!cd.starts_with("PK\x01\x02")) bad("Bad magic number for central directory");
        auto name_len = static_cast<std::size_t>(get(cd, 28, 2)), extra_len = static_cast<std::size_t>(get(cd, 30, 2)),
             comment_len = static_cast<std::size_t>(get(cd, 32, 2));
        std::size_t at = pos + SIZE_CENTRAL_DIR;
        auto field = [&](std::size_t n) {
            std::string s = at < data.size() ? data.substr(at, n) : std::string();
            at += n;
            return s;
        };
        std::string raw_name = field(name_len);
        std::int64_t flags = get(cd, 8, 2);
        ZipInfo x = make_info(flags & MASK_UTF_FILENAME ? raw_name : decode_cp437(raw_name));
        Info& z = *x.d;
        z.extra = bytes(field(extra_len));
        z.comment = bytes(field(comment_len));
        z.create_version = get(cd, 4, 1);
        z.create_system = get(cd, 5, 1);
        z.extract_version = get(cd, 6, 1);
        z.reserved = get(cd, 7, 1);
        z.flag_bits = flags;
        z.compress_type = get(cd, 10, 2);
        std::int64_t t = get(cd, 12, 2), d = get(cd, 14, 2);
        z.raw_time = t;
        z.CRC = get(cd, 16, 4);
        z.compress_size = get(cd, 20, 4);
        z.file_size = get(cd, 24, 4);
        z.volume = get(cd, 34, 2);
        z.internal_attr = get(cd, 36, 2);
        z.external_attr = get(cd, 38, 4);
        z.header_offset = get(cd, 42, 4);
        if (z.extract_version > VERSION_MAX_EXTRACT)
            raise<NotImplementedError>(std::format("zip file version {:.1f}", static_cast<double>(z.extract_version) / 10));
        z.date_time = {(d >> 9) + 1980, (d >> 5) & 0xF, d & 0x1F, t >> 11, (t >> 5) & 0x3F, (t & 0x1F) * 2};
        decode_extra(z, crc32_of(raw_name));
        z.header_offset += concat;
        add(x);
        pos += SIZE_CENTRAL_DIR + name_len + extra_len + comment_len;
    }
    std::vector<ZipInfo> by_offset = filelist;  // each member ends where the next one starts
    std::stable_sort(by_offset.begin(), by_offset.end(), [](const ZipInfo& a, const ZipInfo& b) { return a.d->header_offset < b.d->header_offset; });
    std::int64_t end_offset = start_dir;
    for (auto it = by_offset.rbegin(); it != by_offset.rend(); ++it) {
        it->d->end_offset = end_offset;
        end_offset = it->d->header_offset;
    }
}

inline void Archive::writecheck(const ZipInfo& zinfo) {
    if (name_to_info.contains(zinfo.d->filename)) warn("Duplicate name: " + repr_str(zinfo.d->filename));
    if (mode != "w" && mode != "x" && mode != "a") raise("ValueError", "write() requires mode 'w', 'x', or 'a'");
    if (!fp) raise("ValueError", "Attempt to write ZIP archive that was already closed");
    check_compression(zinfo.d->compress_type);
    if (!allow_zip64) {
        const char* requires_zip64 = nullptr;
        if (static_cast<std::int64_t>(filelist.size()) >= ZIP_FILECOUNT_LIMIT) requires_zip64 = "Files count";
        else if (zinfo.d->file_size > ZIP64_LIMIT) requires_zip64 = "Filesize";
        else if (zinfo.d->header_offset > ZIP64_LIMIT) requires_zip64 = "Zipfile size";
        if (requires_zip64) large(std::string(requires_zip64) + " would require ZIP64 extensions");
    }
}

inline void Archive::write_end_record() {
    for (const ZipInfo& zi : filelist) {  // the central directory
        Info& z = *zi.d;
        std::vector<std::int64_t> extra;
        std::int64_t file_size = z.file_size, compress_size = z.compress_size, header_offset = z.header_offset;
        if (z.file_size > ZIP64_LIMIT || z.compress_size > ZIP64_LIMIT) {
            extra.push_back(z.file_size);
            extra.push_back(z.compress_size);
            file_size = compress_size = 0xFFFFFFFF;
        }
        if (z.header_offset > ZIP64_LIMIT) {
            extra.push_back(z.header_offset);
            header_offset = 0xFFFFFFFF;
        }
        std::string extra_data = z.extra.data;
        std::int64_t min_version = 0;
        if (!extra.empty()) {  // a ZIP64 field first, replacing any already there
            std::string kept;
            std::string_view rest = extra_data;
            while (rest.size() >= 4) {
                std::size_t n = std::min(rest.size(), 4 + static_cast<std::size_t>(get(rest, 2, 2)));
                if (get(rest, 0, 2) != 1) kept += rest.substr(0, n);
                rest.remove_prefix(n);
            }
            kept += rest;
            std::string field;
            put(field, 1, 2);
            put(field, 8 * extra.size(), 2);
            for (std::int64_t v : extra) put(field, static_cast<std::uint64_t>(v), 8);
            extra_data = field + kept;
            min_version = VERSION_ZIP64;
        }
        if (z.compress_type == ZIP_BZIP2) min_version = std::max(VERSION_BZIP2, min_version);
        else if (z.compress_type == ZIP_LZMA) min_version = std::max(VERSION_LZMA, min_version);
        auto [name, flag_bits] = encode_name(z.filename, z.flag_bits);
        std::string cd = "PK\x01\x02";
        put(cd, static_cast<std::uint64_t>(std::max(min_version, z.create_version)), 1);
        put(cd, static_cast<std::uint64_t>(z.create_system), 1);
        put(cd, static_cast<std::uint64_t>(std::max(min_version, z.extract_version)), 1);
        put(cd, static_cast<std::uint64_t>(z.reserved), 1);
        put(cd, static_cast<std::uint64_t>(flag_bits), 2);
        put(cd, static_cast<std::uint64_t>(z.compress_type), 2);
        put(cd, static_cast<std::uint64_t>(dos_time(z.date_time)), 2);
        put(cd, static_cast<std::uint64_t>(dos_date(z.date_time)), 2);
        put(cd, static_cast<std::uint64_t>(z.CRC), 4);
        put(cd, static_cast<std::uint64_t>(compress_size), 4);
        put(cd, static_cast<std::uint64_t>(file_size), 4);
        put(cd, name.size(), 2);
        put(cd, extra_data.size(), 2);
        put(cd, z.comment.size(), 2);
        put(cd, 0, 2);
        put(cd, static_cast<std::uint64_t>(z.internal_attr), 2);
        put(cd, static_cast<std::uint64_t>(z.external_attr), 4);
        put(cd, static_cast<std::uint64_t>(header_offset), 4);
        write(cd + name + extra_data + z.comment.data);
    }
    std::int64_t pos2 = tell();
    auto count = static_cast<std::int64_t>(filelist.size());
    std::int64_t size = pos2 - start_dir, offset = start_dir;
    const char* requires_zip64 = nullptr;
    if (count > ZIP_FILECOUNT_LIMIT) requires_zip64 = "Files count";
    else if (offset > ZIP64_LIMIT) requires_zip64 = "Central directory offset";
    else if (size > ZIP64_LIMIT) requires_zip64 = "Central directory size";
    if (requires_zip64) {
        if (!allow_zip64) large(std::string(requires_zip64) + " would require ZIP64 extensions");
        std::string rec = "PK\x06\x06";
        put(rec, SIZE_END_CENT_DIR64 - 12, 8);
        put(rec, 45, 2);
        put(rec, 45, 2);
        put(rec, 0, 4);
        put(rec, 0, 4);
        put(rec, static_cast<std::uint64_t>(count), 8);
        put(rec, static_cast<std::uint64_t>(count), 8);
        put(rec, static_cast<std::uint64_t>(size), 8);
        put(rec, static_cast<std::uint64_t>(offset), 8);
        rec += "PK\x06\x07";
        put(rec, 0, 4);
        put(rec, static_cast<std::uint64_t>(pos2), 8);
        put(rec, 1, 4);
        write(rec);
        count = std::min<std::int64_t>(count, 0xFFFF);
        size = std::min<std::int64_t>(size, 0xFFFFFFFF);
        offset = std::min<std::int64_t>(offset, 0xFFFFFFFF);
    }
    std::string rec = "PK\x05\x06";
    put(rec, 0, 2);
    put(rec, 0, 2);
    put(rec, static_cast<std::uint64_t>(count), 2);
    put(rec, static_cast<std::uint64_t>(count), 2);
    put(rec, static_cast<std::uint64_t>(size), 4);
    put(rec, static_cast<std::uint64_t>(offset), 4);
    put(rec, comment.size(), 2);
    write(rec + comment.data);
    if (mode == "a") fp->truncate();
    fp->flush();
}

inline void Archive::close() {
    if (!fp) return;
    if (writing)
        raise("ValueError", "Can't close the ZIP file while there is an open writing handle on it. "
                            "Close the writing handle before closing the zip.");
    auto release = [&] {
        auto file = std::exchange(fp, nullptr);
        if (!file_passed && file.use_count() == 1) file->close();  // (a member still being read keeps it open)
    };
    try {
        if ((mode == "w" || mode == "x" || mode == "a") && did_modify) {
            if (seekable) fp->seek(start_dir, 0);
            write_end_record();
        }
    } catch (...) {
        release();
        throw;
    }
    release();
}

// Legacy ZIP ("ZipCrypto") decryption: three keys stirred with CRC-32 steps (Python's _ZipDecrypter).
struct ZipDecrypter {
    std::uint32_t key0 = 305419896, key1 = 591751049, key2 = 878082192;
    explicit ZipDecrypter(std::string_view pwd) {
        for (char c : pwd) update_keys(static_cast<unsigned char>(c));
    }
    static std::uint32_t crc32_step(std::uint32_t crc, std::uint32_t c) {
        static const z_crc_t* table = ::get_crc_table();
        return (crc >> 8) ^ static_cast<std::uint32_t>(table[(crc ^ c) & 0xFF]);
    }
    void update_keys(std::uint32_t c) {
        key0 = crc32_step(key0, c);
        key1 = (key1 + (key0 & 0xFF)) * 134775813u + 1;
        key2 = crc32_step(key2, key1 >> 24);
    }
    void decrypt(std::string& data) {
        for (char& ch : data) {
            std::uint32_t k = key2 | 2;
            std::uint32_t c = static_cast<unsigned char>(ch) ^ (((k * (k ^ 1)) >> 8) & 0xFF);
            update_keys(c);
            ch = static_cast<char>(c);
        }
    }
};

// zf.open(name): reading a member, decrypting and decompressing as it goes (Python's ZipExtFile).
struct ExtFile final : BinaryFile {
    std::shared_ptr<BinaryFile> file;  // the archive's
    std::string name;
    std::int64_t compress_type, compress_size, file_size, data_start;
    std::uint32_t expected_crc;
    std::int64_t pos = 0, compress_left = 0, left = 0;  // in the archive; compressed and uncompressed bytes to come
    std::uint32_t running_crc = 0;
    std::unique_ptr<Decompressor> decompressor;
    std::optional<std::string> pwd;  // (an encrypted member's)
    std::optional<ZipDecrypter> decrypter;
    std::string buffer;  // decompressed, from `offset` not read yet
    std::size_t offset = 0;
    bool eof = false, open = true;

    ExtFile(std::shared_ptr<BinaryFile> f, const Info& z, std::int64_t start, std::optional<std::string> password = std::nullopt)
        : BinaryFile(nullptr, "", "rb"), file(std::move(f)), name(z.filename), compress_type(z.compress_type),
          compress_size(z.compress_size), file_size(z.file_size), data_start(start),
          expected_crc(static_cast<std::uint32_t>(z.CRC)), pwd(std::move(password)) {
        int check = rewind();
        if (pwd) {  // the encryption header's last byte: the CRC's top byte, or the time's if the CRC follows the data
            std::int64_t want = z.flag_bits & MASK_USE_DATA_DESCRIPTOR ? (z.raw_time >> 8) & 0xFF : (z.CRC >> 24) & 0xFF;
            if (check != want) raise("RuntimeError", "Bad password for file " + repr_str(z.orig_filename));
        }
    }
    int rewind() {  // (the encryption header's check byte, if it's encrypted)
        pos = data_start;
        compress_left = compress_size;
        left = file_size;
        running_crc = 0;
        decompressor = make_decompressor(compress_type);
        buffer.clear();
        offset = 0;
        eof = false;
        return pwd ? init_decrypter() : -1;
    }
    int init_decrypter() {  // (Python's _init_decrypter): the 12-byte encryption header starts the data
        decrypter.emplace(*pwd);
        file->seek(pos, 0);
        std::string header = file->read_raw(12);
        pos += static_cast<std::int64_t>(header.size());
        compress_left -= 12;
        decrypter->decrypt(header);
        return header.size() == 12 ? static_cast<unsigned char>(header[11]) : -1;
    }
    void need_open() const {
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }

    std::string read_compressed(std::size_t n) {  // (Python's _read2)
        if (compress_left <= 0) return "";
        n = std::min<std::size_t>(std::max(n, MIN_READ_SIZE), static_cast<std::size_t>(compress_left));
        file->seek(pos, 0);
        std::string data = file->read_raw(static_cast<std::int64_t>(n));
        pos += static_cast<std::int64_t>(data.size());
        compress_left -= static_cast<std::int64_t>(data.size());
        if (data.empty()) raise("EOFError", "");
        if (decrypter) decrypter->decrypt(data);
        return data;
    }
    std::string read_some(std::size_t n) {  // at most one read from the archive (Python's _read1)
        if (eof || n == 0) return "";
        std::string data;
        if (!decompressor || decompressor->needs_input()) data = read_compressed(n);
        if (!decompressor) {
            eof = compress_left <= 0;
        } else {
            data = decompressor->decompress(data, std::max(n, MIN_READ_SIZE));
            eof = decompressor->eof || (compress_left <= 0 && decompressor->needs_input());
        }
        if (static_cast<std::int64_t>(data.size()) > left) data.resize(static_cast<std::size_t>(left));
        left -= static_cast<std::int64_t>(data.size());
        if (left <= 0) eof = true;
        running_crc = crc32_of(data, running_crc);
        if (eof && running_crc != expected_crc) bad("Bad CRC-32 for file " + repr_str(name));
        return data;
    }

    std::string read_raw(std::int64_t n) override {
        need_open();
        if (n < 0) {
            std::string out = buffer.substr(offset);
            buffer.clear();
            offset = 0;
            while (!eof) out += read_some(std::size_t{1} << 20);
            return out;
        }
        auto want = static_cast<std::size_t>(n);
        if (offset + want < buffer.size()) {
            std::string out = buffer.substr(offset, want);
            offset += want;
            return out;
        }
        std::string out = buffer.substr(offset);
        want -= out.size();
        buffer.clear();
        offset = 0;
        while (want > 0 && !eof) {
            std::string data = read_some(want);
            if (want < data.size()) {
                out.append(data, 0, want);
                buffer = std::move(data);
                offset = want;
                break;
            }
            out += data;
            want -= data.size();
        }
        return out;
    }
    std::string readline_raw() override {
        need_open();
        for (;;) {
            std::size_t nl = buffer.find('\n', offset);
            if (nl != std::string::npos || eof) {
                std::size_t end = nl == std::string::npos ? buffer.size() : nl + 1;
                std::string out = buffer.substr(offset, end - offset);
                offset = end;
                return out;
            }
            buffer = buffer.substr(offset) + read_some(MIN_READ_SIZE);
            offset = 0;
        }
    }
    std::int64_t tell() override {
        need_open();
        return file_size - left - static_cast<std::int64_t>(buffer.size()) + static_cast<std::int64_t>(offset);
    }
    std::int64_t seek(std::int64_t to, std::int64_t whence = 0) override {
        need_open();
        std::int64_t current = tell(), target;
        if (whence == 0) target = to;
        else if (whence == 1) target = current + to;
        else if (whence == 2) target = file_size + to;
        else raise("ValueError", "whence must be os.SEEK_SET (0), os.SEEK_CUR (1), or os.SEEK_END (2)");
        target = std::clamp<std::int64_t>(target, 0, file_size);
        std::int64_t forward = target - current;
        std::int64_t in_buffer = forward + static_cast<std::int64_t>(offset);
        if (in_buffer >= 0 && in_buffer < static_cast<std::int64_t>(buffer.size())) {
            offset = static_cast<std::size_t>(in_buffer);
            return tell();
        }
        if (forward < 0) {  // back to the start, then forward
            rewind();
            forward = target;
        }
        while (forward > 0) forward -= static_cast<std::int64_t>(read_raw(std::min<std::int64_t>(forward, 1 << 24)).size());
        return tell();
    }
    std::int64_t write_raw(const std::string&) override { raise("OSError", "write"); }  // (io.UnsupportedOperation)
    void check_open() const override { need_open(); }
    void close() override {
        open = false;
        file = nullptr;
        decompressor = nullptr;
        decrypter.reset();
        buffer = std::string();
    }
    void flush() override { need_open(); }
    bool is_closed() const override { return !open; }
    bool readable() const override { return need_open(), true; }
    bool writable() const override { return need_open(), false; }
    bool seekable() const override { return need_open(), true; }
    bool isatty() const override { return need_open(), false; }
    std::string get_name() const override { return name; }
    std::string get_mode() const override { return "rb"; }
    std::int64_t fileno_() const override { raise("OSError", "fileno"); }
    std::int64_t truncate(std::optional<std::int64_t> = std::nullopt) override { raise("OSError", "truncate"); }
    std::string sd_repr() const override {
        if (!open) return "<zipfile.ZipExtFile [closed]>";
        std::string out = "<zipfile.ZipExtFile name=" + repr_str(name);
        if (compress_type != ZIP_STORED) out += " compress_type=" + compressor_name(compress_type);
        return out + ">";
    }
};

// zf.open(name, "w"): writing a member (Python's _ZipWriteFile). Closing it finishes the member.
struct WriteFile final : BinaryFile {
    std::shared_ptr<Archive> zf;
    ZipInfo zinfo;
    bool zip64;
    std::unique_ptr<Compressor> compressor;
    std::int64_t file_size = 0, compress_size = 0;
    std::uint32_t crc = 0;
    bool open = true;

    WriteFile(std::shared_ptr<Archive> archive, ZipInfo info, bool big, std::unique_ptr<Compressor> c)
        : BinaryFile(nullptr, "", "wb"), zf(std::move(archive)), zinfo(std::move(info)), zip64(big), compressor(std::move(c)) {}
    ~WriteFile() override {
        try {
            close();
        } catch (...) {
        }
    }
    void need_open() const {
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }
    std::int64_t write_raw(const std::string& data) override {
        need_open();
        file_size += static_cast<std::int64_t>(data.size());
        crc = crc32_of(data, crc);
        if (compressor) {
            std::string out = compressor->compress(data);
            compress_size += static_cast<std::int64_t>(out.size());
            zf->write(out);
        } else {
            zf->write(data);
        }
        return static_cast<std::int64_t>(data.size());
    }
    void close() override {
        if (!open) return;
        open = false;
        struct Done {  // (whatever happens, the archive can be used again)
            Archive& a;
            ~Done() { a.writing = false; }
        } done{*zf};
        Info& z = *zinfo.d;
        if (compressor) {
            std::string out = compressor->flush();
            compress_size += static_cast<std::int64_t>(out.size());
            zf->write(out);
            z.compress_size = compress_size;
        } else {
            z.compress_size = file_size;
        }
        z.CRC = crc;
        z.file_size = file_size;
        if (!zip64) {
            if (file_size > ZIP64_LIMIT) raise("RuntimeError", "File size too large, try using force_zip64");
            if (compress_size > ZIP64_LIMIT) raise("RuntimeError", "Compressed size too large, try using force_zip64");
        }
        if (z.flag_bits & MASK_USE_DATA_DESCRIPTOR) {  // the CRC and sizes after the data
            std::string dd;
            put(dd, 0x08074b50, 4);
            put(dd, static_cast<std::uint64_t>(z.CRC), 4);
            put(dd, static_cast<std::uint64_t>(z.compress_size), zip64 ? 8 : 4);
            put(dd, static_cast<std::uint64_t>(z.file_size), zip64 ? 8 : 4);
            zf->write(dd);
            zf->start_dir = zf->tell();
        } else {  // back to the header, now with the CRC and sizes
            zf->start_dir = zf->tell();
            zf->fp->seek(z.header_offset, 0);
            zf->fp->write_raw(file_header(z, zip64));
            zf->fp->seek(zf->start_dir, 0);
        }
        zf->add(zinfo);
    }
    void check_open() const override { need_open(); }
    std::string read_raw(std::int64_t) override { raise("OSError", "read"); }
    std::string readline_raw() override { raise("OSError", "readline"); }
    void flush() override { need_open(); }
    bool is_closed() const override { return !open; }
    bool readable() const override { return need_open(), false; }
    bool writable() const override { return need_open(), true; }
    bool seekable() const override { return need_open(), false; }
    bool isatty() const override { return need_open(), false; }
    std::int64_t tell() override { raise("OSError", "tell"); }
    std::int64_t seek(std::int64_t, std::int64_t = 0) override { raise("OSError", "seek"); }
    std::string get_name() const override { return zinfo.d->filename; }
    std::string get_mode() const override { return "wb"; }
    std::int64_t fileno_() const override { raise("OSError", "fileno"); }
    std::int64_t truncate(std::optional<std::int64_t> = std::nullopt) override { raise("OSError", "truncate"); }
    std::string sd_repr() const override { return "<zipfile._ZipWriteFile object>"; }
};

inline std::string utf8_width_pad(const std::string& s, std::size_t width) {  // %-46s
    std::size_t n = code_points(s);
    return n >= width ? s : s + std::string(width - n, ' ');
}

inline DateTime local_date_time(std::time_t when) {
    std::tm tm{};
    ::localtime_r(&when, &tm);
    return {tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec};
}

// The archive as the program sees it: a handle (copies are the same archive).
struct ZipFile {
    std::shared_ptr<Archive> a;
    ZipFile() = default;  // (unset until it's assigned)

    ZipFile(const pathlib::Path& file, const std::string& mode = "r", std::int64_t compression = ZIP_STORED, bool allowZip64 = true,
            std::optional<std::int64_t> compresslevel = std::nullopt, bool strict_timestamps = true) {
        init(mode, compression, allowZip64, compresslevel, strict_timestamps);
        a->filename = file.str();
        // Python's modes, each falling back to the next if the file can't be opened that way
        static const std::unordered_map<std::string, std::string> next = {
            {"r", "rb"}, {"w", "w+b"}, {"x", "x+b"}, {"a", "r+b"}, {"r+b", "w+b"}, {"w+b", "wb"}, {"x+b", "xb"}};
        std::string filemode = next.at(mode);
        for (;;) {
            try {
                a->fp = open_binary(file.str(), filemode);
                break;
            } catch (const Thrown& t) {
                if (!isinstance<OSError>(t) || !next.contains(filemode)) throw;
                filemode = next.at(filemode);
            }
        }
        start();
    }
    ZipFile(std::shared_ptr<BinaryFile> file, const std::string& mode = "r", std::int64_t compression = ZIP_STORED,
            bool allowZip64 = true, std::optional<std::int64_t> compresslevel = std::nullopt, bool strict_timestamps = true) {
        init(mode, compression, allowZip64, compresslevel, strict_timestamps);
        a->file_passed = true;
        try {
            a->filename = file->get_name();
        } catch (const Thrown&) {  // (an io.BytesIO has no name)
        }
        a->fp = std::move(file);
        start();
    }

    void init(const std::string& mode, std::int64_t compression, bool allowZip64, std::optional<std::int64_t> compresslevel,
              bool strict_timestamps) {
        if (mode != "r" && mode != "w" && mode != "x" && mode != "a") raise("ValueError", "ZipFile requires mode 'r', 'w', 'x', or 'a'");
        check_compression(compression);
        a = std::make_shared<Archive>();
        a->mode = mode;
        a->compression = compression;
        a->compresslevel = compresslevel;
        a->allow_zip64 = allowZip64;
        a->strict_timestamps = strict_timestamps;
    }
    void start() {
        try {
            if (a->mode == "r") {
                a->real_get_contents();
            } else if (a->mode == "w" || a->mode == "x") {
                a->did_modify = true;  // (so even an empty archive gets its central directory)
                try {
                    a->start_dir = a->fp->tell();
                    a->fp->seek(a->start_dir, 0);
                } catch (const Thrown& t) {
                    if (!isinstance<OSError>(t)) throw;
                    a->seekable = false;  // a pipe: each member's CRC and sizes follow its data
                    a->start_dir = 0;
                }
            } else {
                try {  // a zip file: replace its central directory
                    a->real_get_contents();
                    a->fp->seek(a->start_dir, 0);
                } catch (const Thrown& t) {
                    if (!isinstance<BadZipFile>(t)) throw;
                    a->fp->seek(0, 2);  // not a zip file: add one to its end
                    a->did_modify = true;
                    a->start_dir = a->fp->tell();
                }
            }
        } catch (...) {
            auto file = std::exchange(a->fp, nullptr);
            if (!a->file_passed) file->close();
            throw;
        }
    }

    Archive& open_archive(const char* what = "Attempt to use ZIP archive that was already closed") const {
        if (!a->fp) raise("ValueError", what);
        return *a;
    }
    ZipInfo info_for(const std::string& name) const { return getinfo(name); }
    ZipInfo info_for(const ZipInfo& info) const { return info; }

    void close() { a->close(); }
    list<std::string> namelist() const {
        list<std::string> out;
        for (const auto& z : a->filelist) out.push_back(z.d->filename);
        return out;
    }
    list<ZipInfo> infolist() const {
        list<ZipInfo> out;
        for (const auto& z : a->filelist) out.push_back(z);
        return out;
    }
    ZipInfo getinfo(const std::string& name) const {
        auto it = a->name_to_info.find(name);
        if (it == a->name_to_info.end()) raise<KeyError>("There is no item named " + repr_str(name) + " in the archive");
        return it->second;
    }
    void printdir(const std::optional<std::shared_ptr<TextFile>>& file = std::nullopt) const {
        auto line = [&](const std::string& s) {
            if (file) sd::print_to(*file, " ", "\n", s);
            else sd::print(" ", "\n", s);
        };
        line(std::format("{} {:>19} {:>12}", utf8_width_pad("File Name", 46), "Modified    ", "Size"));
        for (const auto& z : a->filelist) {
            const DateTime& dt = z.d->date_time;
            std::string date = std::format("{}-{:02}-{:02} {:02}:{:02}:{:02}", std::get<0>(dt), std::get<1>(dt), std::get<2>(dt),
                                           std::get<3>(dt), std::get<4>(dt), std::get<5>(dt));
            line(std::format("{} {} {:>12}", utf8_width_pad(z.d->filename, 46), date, z.d->file_size));
        }
    }
    std::optional<std::string> testzip() {
        for (const auto& z : a->filelist) {
            try {
                auto f = open(z.d->filename);  // (by name, as Python does)
                while (!f->read_raw(1 << 20).empty()) {
                }
            } catch (const Thrown& t) {
                if (!isinstance<BadZipFile>(t)) throw;
                return z.d->filename;
            }
        }
        return std::nullopt;
    }

    // zf.setpassword(pwd), zf.pwd: the default password for encrypted members
    void setpassword(const std::optional<bytes>& pwd) { a->pwd = pwd && !pwd->empty() ? pwd : std::nullopt; }
    std::optional<bytes> pwd() const { return a->pwd; }
    void set_pwd(std::optional<bytes> value) { a->pwd = std::move(value); }

    template <class Name>
    bytes read(const Name& name, const std::optional<bytes>& pwd = std::nullopt) {
        auto f = open(name, "r", pwd);
        return bytes(f->read_raw(-1));
    }

    template <class Name>
    std::shared_ptr<BinaryFile> open(const Name& name, const std::string& mode = "r", const std::optional<bytes>& pwd = std::nullopt,
                                     bool force_zip64 = false) {
        if (mode != "r" && mode != "w") raise("ValueError", "open() requires mode \"r\" or \"w\"");
        if (pwd && !pwd->empty() && mode == "w") raise("ValueError", "pwd is only supported for reading files");
        open_archive();
        ZipInfo zinfo;
        if constexpr (std::is_same_v<Name, ZipInfo>) {
            zinfo = name;
        } else if (mode == "w") {
            zinfo = make_info(name);
            zinfo.d->compress_type = a->compression;
            zinfo.d->compress_level = a->compresslevel;
        } else {
            zinfo = getinfo(name);
        }
        if (mode == "w") return open_to_write(zinfo, force_zip64);
        if (a->writing)
            raise("ValueError", "Can't read from the ZIP file while there is an open writing handle on it. "
                                "Close the writing handle before trying to read.");
        Info& z = *zinfo.d;
        std::string header = a->read_at(z.header_offset, SIZE_FILE_HEADER);
        if (header.size() != SIZE_FILE_HEADER) bad("Truncated file header");
        if (!header.starts_with("PK\x03\x04")) bad("Bad magic number for file header");
        std::string fname = a->fp->read_raw(get(header, 26, 2));
        std::int64_t data_start = z.header_offset + static_cast<std::int64_t>(SIZE_FILE_HEADER) + get(header, 26, 2) + get(header, 28, 2);
        if (z.flag_bits & MASK_COMPRESSED_PATCH) raise<NotImplementedError>("compressed patched data (flag bit 5)");
        if (z.flag_bits & MASK_STRONG_ENCRYPTION) raise<NotImplementedError>("strong encryption (flag bit 6)");
        std::string fname_str = get(header, 6, 2) & MASK_UTF_FILENAME ? fname : decode_cp437(fname);
        if (fname_str != z.orig_filename)
            bad("File name in directory " + repr_str(z.orig_filename) + " and header " + repr_bytes(bytes(fname)) + " differ.");
        if (z.end_offset && data_start + z.compress_size > *z.end_offset) {
            std::string what = "Overlapped entries: " + repr_str(z.orig_filename) + " (possible zip bomb)";
            if (*z.end_offset == z.header_offset) warn(what);
            else bad(what);
        }
        std::optional<std::string> password;
        if (z.flag_bits & MASK_ENCRYPTED) {
            if (pwd && !pwd->empty()) password = pwd->data;
            else if (a->pwd && !a->pwd->empty()) password = a->pwd->data;
            if (!password) {
                std::string shown;
                if constexpr (std::is_same_v<Name, ZipInfo>) shown = name.sd_repr();
                else shown = repr_str(name);
                raise("RuntimeError", "File " + shown + " is encrypted, password required for extraction");
            }
        }
        return std::make_shared<ExtFile>(a->fp, z, data_start, std::move(password));
    }

    std::shared_ptr<BinaryFile> open_to_write(const ZipInfo& zinfo, bool force_zip64) {
        Archive& ar = *a;
        if (force_zip64 && !ar.allow_zip64)
            raise("ValueError", "force_zip64 is True, but allowZip64 was False when opening the ZIP file.");
        if (ar.writing)
            raise("ValueError", "Can't write to the ZIP file while there is another write handle open on it. "
                                "Close the first handle before opening another.");
        Info& z = *zinfo.d;
        z.compress_size = 0;  // (filled in when it's closed)
        z.CRC = 0;
        z.flag_bits = MASK_UTF_FILENAME;
        if (z.compress_type == ZIP_LZMA) z.flag_bits |= MASK_COMPRESS_OPTION_1;  // (the data ends with a marker)
        if (!ar.seekable) z.flag_bits |= MASK_USE_DATA_DESCRIPTOR;
        if (!z.external_attr) z.external_attr = 0600 << 16;  // ?rw-------
        bool zip64 = force_zip64 || static_cast<double>(z.file_size) * 1.05 > static_cast<double>(ZIP64_LIMIT);
        if (!ar.allow_zip64 && zip64) large("Filesize would require ZIP64 extensions");
        if (ar.seekable) ar.fp->seek(ar.start_dir, 0);
        z.header_offset = ar.tell();
        ar.writecheck(zinfo);
        auto compressor = make_compressor(z.compress_type, z.compress_level);
        ar.did_modify = true;
        ar.write(file_header(z, zip64));
        ar.writing = true;
        return std::make_shared<WriteFile>(a, zinfo, zip64, std::move(compressor));
    }

    void need_writable(const char* closed, const char* busy) const {
        if (!a->fp) raise("ValueError", closed);
        if (a->writing) raise("ValueError", busy);
    }

    // zf.write(filename, arcname=None, compress_type=None, compresslevel=None)
    void write(const pathlib::Path& filename, const std::optional<pathlib::Path>& arcname = std::nullopt,
               std::optional<std::int64_t> compress_type = std::nullopt, std::optional<std::int64_t> compresslevel = std::nullopt) {
        need_writable("Attempt to write to ZIP archive that was already closed",
                      "Can't write to ZIP archive while an open writing handle exists");
        // Python's ZipInfo.from_file
        const std::string path = filename.str();
        struct stat st;
        if (::stat(path.c_str(), &st) != 0) raise_os(errno, path);
        bool dir = S_ISDIR(st.st_mode);
        DateTime date_time = local_date_time(st.st_mtime);
        if (!a->strict_timestamps && std::get<0>(date_time) < 1980) date_time = {1980, 1, 1, 0, 0, 0};
        else if (!a->strict_timestamps && std::get<0>(date_time) > 2107) date_time = {2107, 12, 31, 23, 59, 59};
        std::string name = normpath(arcname ? arcname->str() : path);
        while (name.starts_with('/')) name.erase(0, 1);
        if (dir) name += '/';
        ZipInfo zinfo = make_info(name, date_time);
        Info& z = *zinfo.d;
        z.external_attr = (static_cast<std::int64_t>(st.st_mode) & 0xFFFF) << 16;
        if (dir) {
            z.file_size = 0;
            z.external_attr |= 0x10;  // the MS-DOS directory flag
            z.compress_size = 0;
            z.CRC = 0;
            mkdir(zinfo);
            return;
        }
        z.file_size = st.st_size;
        z.compress_type = compress_type ? *compress_type : a->compression;
        z.compress_level = compresslevel ? compresslevel : a->compresslevel;
        auto src = open_binary(path, "rb");
        auto dest = open_to_write(zinfo, false);
        try {
            for (std::string chunk; !(chunk = src->read_raw(1024 * 8)).empty();) dest->write_raw(chunk);
        } catch (...) {
            try {
                dest->close();
            } catch (...) {
            }
            throw;
        }
        dest->close();
    }

    // zf.writestr(zinfo_or_arcname, data, compress_type=None, compresslevel=None)
    template <class Name, class Data>
    void writestr(const Name& zinfo_or_arcname, const Data& data, std::optional<std::int64_t> compress_type = std::nullopt,
                  std::optional<std::int64_t> compresslevel = std::nullopt) {
        ZipInfo zinfo;
        if constexpr (std::is_same_v<Name, ZipInfo>) {
            zinfo = zinfo_or_arcname;
        } else {  // Python's ZipInfo._for_archive
            zinfo = make_info(zinfo_or_arcname);
            Info& z = *zinfo.d;
            const char* epoch = std::getenv("SOURCE_DATE_EPOCH");
            if (epoch && *epoch) {
                std::time_t when = std::stoll(epoch);
                std::tm tm{};
                ::gmtime_r(&when, &tm);
                z.date_time = {tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec};
            } else {
                z.date_time = local_date_time(std::time(nullptr));
            }
            z.compress_type = a->compression;
            z.compress_level = a->compresslevel;
            z.external_attr = z.filename.ends_with('/') ? (040775 << 16) | 0x10 : 0600 << 16;
        }
        need_writable("Attempt to write to ZIP archive that was already closed",
                      "Can't write to ZIP archive while an open writing handle exists.");
        Info& z = *zinfo.d;
        if (compress_type) z.compress_type = *compress_type;
        if (compresslevel) z.compress_level = compresslevel;
        const std::string& raw = sd::raw(data);
        z.file_size = static_cast<std::int64_t>(raw.size());
        auto dest = open_to_write(zinfo, false);
        try {
            dest->write_raw(raw);
        } catch (...) {
            try {
                dest->close();
            } catch (...) {
            }
            throw;
        }
        dest->close();
    }

    // zf.mkdir(zinfo_or_directory_name, mode=511)
    template <class Name>
    void mkdir(const Name& zinfo_or_directory_name, std::int64_t mode = 0777) {
        ZipInfo zinfo;
        if constexpr (std::is_same_v<Name, ZipInfo>) {
            zinfo = zinfo_or_directory_name;
            if (!zinfo.is_dir()) raise("ValueError", "The given ZipInfo does not describe a directory");
        } else {
            std::string name = zinfo_or_directory_name;
            if (!name.ends_with('/')) name += '/';
            zinfo = make_info(name);
            zinfo.d->external_attr = (((040000 | mode) & 0xFFFF) << 16) | 0x10;
        }
        Archive& ar = *a;
        Info& z = *zinfo.d;
        if (ar.seekable) ar.fp->seek(ar.start_dir, 0);
        z.header_offset = ar.tell();
        if (z.compress_type == ZIP_LZMA) z.flag_bits |= MASK_COMPRESS_OPTION_1;
        ar.writecheck(zinfo);
        ar.did_modify = true;
        ar.add(zinfo);
        ar.write(file_header(z, false));
        ar.start_dir = ar.tell();
    }

    // zf.extract(member, path=None, pwd=None): where it went
    template <class Member>
    std::string extract(const Member& member, const std::optional<pathlib::Path>& path = std::nullopt,
                        const std::optional<bytes>& pwd = std::nullopt) {
        return extract_member(info_for(member), path ? path->str() : std::filesystem::current_path().string(), pwd);
    }
    // zf.extractall(path=None, members=None, pwd=None)
    template <class Members = std::nullopt_t>
    void extractall(const std::optional<pathlib::Path>& path = std::nullopt, const Members& members = std::nullopt,
                    const std::optional<bytes>& pwd = std::nullopt) {
        std::string target = path ? path->str() : std::filesystem::current_path().string();
        if constexpr (std::is_same_v<Members, std::nullopt_t>) {
            for (const auto& name : namelist()) extract_member(getinfo(name), target, pwd);
        } else if constexpr (requires { members.has_value(); }) {
            if (!members) return extractall(path, std::nullopt, pwd);
            for (const auto& m : *members) extract_member(info_for(m), target, pwd);
        } else {
            for (const auto& m : members) extract_member(info_for(m), target, pwd);
        }
    }
    // Python's _extract_member: the name made relative, without ".." or "." parts.
    std::string extract_member(const ZipInfo& member, const std::string& targetpath, const std::optional<bytes>& pwd) {
        std::string arcname = member.d->filename;
        std::size_t root = arcname.starts_with('/') ? (arcname.starts_with("//") && !arcname.starts_with("///") ? 2 : 1) : 0;
        std::string kept;
        std::size_t start = root;
        while (start <= arcname.size()) {
            std::size_t end = arcname.find('/', start);
            if (end == std::string::npos) end = arcname.size();
            std::string part = arcname.substr(start, end - start);
            start = end + 1;
            if (part.empty() || part == "." || part == "..") continue;
            kept += (kept.empty() ? "" : "/") + part;
        }
        if (kept.empty() && !member.is_dir()) raise("ValueError", "Empty filename.");
        std::string target = normpath(targetpath.empty() || targetpath.ends_with('/') ? targetpath + kept : targetpath + "/" + kept);
        namespace fs = std::filesystem;
        std::size_t slash = target.rfind('/');
        std::string upper = slash == std::string::npos ? "" : slash == 0 ? "/" : target.substr(0, slash);
        std::error_code ec;
        if (!upper.empty() && !fs::exists(upper, ec)) {
            fs::create_directories(upper, ec);
            if (ec && !fs::is_directory(upper)) raise_os(ec.value(), upper);
        }
        if (member.is_dir()) {
            if (!fs::is_directory(target, ec) && ::mkdir(target.c_str(), 0777) != 0 && !(errno == EEXIST && fs::is_directory(target, ec)))
                raise_os(errno, target);
            return target;
        }
        auto source = open(member, "r", pwd);
        auto out = open_binary(target, "wb");
        for (std::string chunk; !(chunk = source->read_raw(64 * 1024)).empty();) out->write_raw(chunk);
        out->close();
        return target;
    }

    // zf.comment, zf.filename, zf.mode, zf.compression, zf.compresslevel
    bytes comment() const { return a->comment; }
    void set_comment(bytes value) {
        if (static_cast<std::int64_t>(value.size()) > ZIP_MAX_COMMENT) {
            warn("Archive comment is too long; truncating to " + std::to_string(ZIP_MAX_COMMENT) + " bytes");
            value = bytes(value.data.substr(0, static_cast<std::size_t>(ZIP_MAX_COMMENT)));
        }
        a->comment = std::move(value);
        a->did_modify = true;
    }
    std::optional<std::string> filename() const { return a->filename; }
    std::string mode() const { return a->mode; }
    std::int64_t compression() const { return a->compression; }
    std::optional<std::int64_t> compresslevel() const { return a->compresslevel; }

    std::string sd_repr() const {
        if (!a->fp) return "<zipfile.ZipFile [closed]>";
        std::string out = "<zipfile.ZipFile";
        if (a->file_passed) out += " file=" + a->fp->sd_repr();
        else if (a->filename) out += " filename=" + repr_str(*a->filename);
        return out + " mode=" + repr_str(a->mode) + ">";
    }
};

// zipfile.is_zipfile(filename): a quick look for the end record (Python's _check_zipfile).
inline bool check_zipfile(Archive& a) {
    try {
        auto end = end_rec_data(a);
        if (!end) return false;
        if (end->entries_total == 0 && end->size == 0 && end->offset == 0) return true;  // an empty archive
        if (end->disk_number != end->disk_start) return false;
        std::int64_t concat = end->location - end->size - end->offset;
        if (end->size < static_cast<std::int64_t>(SIZE_CENTRAL_DIR)) return false;
        std::string data = a.read_at(end->offset + concat, SIZE_CENTRAL_DIR);
        return data.size() == SIZE_CENTRAL_DIR && data.starts_with("PK\x01\x02");
    } catch (const Thrown& t) {
        if (isinstance<OSError>(t) || isinstance<BadZipFile>(t)) return false;
        throw;
    }
}
inline bool is_zipfile(const pathlib::Path& filename) {
    Archive a;
    try {
        a.fp = open_binary(filename.str(), "rb");
    } catch (const Thrown& t) {
        if (isinstance<OSError>(t)) return false;
        throw;
    }
    bool result = check_zipfile(a);
    std::exchange(a.fp, nullptr)->close();
    return result;
}
inline bool is_zipfile(const std::shared_ptr<BinaryFile>& file) {
    Archive a;
    a.file_passed = true;
    a.fp = file;
    std::int64_t pos = file->tell();
    bool result = check_zipfile(a);
    file->seek(pos, 0);
    a.fp = nullptr;
    return result;
}

}  // namespace sd::zipfile
