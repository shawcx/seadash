// The `struct` module: pack and unpack binary data with Python's format strings. The
// format decides the types at compile time (it must be a literal); this runtime does the
// bytes, with Python's sizes, alignment, byte orders and errors.
#pragma once

#include <bit>

namespace sd::structmod {

struct error : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "struct.error"; }
};
[[noreturn]] inline void fail(const std::string& msg) { throw Thrown{std::make_shared<error>(msg)}; }

struct Item {
    char code;
    std::size_t count;  // repeat count, or the length for s / p
    std::size_t size;   // of one element
    std::size_t align;  // in native mode; 1 otherwise
};

struct Format {
    bool native = true, little = std::endian::native == std::endian::little;
    std::vector<Item> items;
    std::size_t size = 0;

    explicit Format(const std::string& fmt) {
        std::size_t i = 0;
        if (i < fmt.size() && std::strchr("@=<>!", fmt[i])) {
            char order = fmt[i++];
            native = order == '@';
            if (order == '<') little = true;
            if (order == '>' || order == '!') little = false;
        }
        while (i < fmt.size()) {
            if (std::isspace(static_cast<unsigned char>(fmt[i]))) {
                ++i;
                continue;
            }
            std::size_t count = 1;
            if (i < fmt.size() && std::isdigit(static_cast<unsigned char>(fmt[i]))) {
                count = 0;
                while (i < fmt.size() && std::isdigit(static_cast<unsigned char>(fmt[i]))) count = count * 10 + static_cast<std::size_t>(fmt[i++] - '0');
            }
            if (i >= fmt.size()) fail("repeat count given without format specifier");
            char c = fmt[i++];
            std::size_t sz = size_of(c);
            if (sz == 0 && c != 'x') fail("bad char in struct format");
            if (!native && (c == 'n' || c == 'N' || c == 'P')) fail("bad char in struct format");
            if (c == 's' || c == 'p') {
                items.push_back({c, count, count, 1});
                size += count;
                continue;
            }
            if (c == 'x') {
                items.push_back({c, count, 1, 1});
                size += count;
                continue;
            }
            std::size_t align = native ? sz : 1;
            if (align > 1 && size % align) size += align - size % align;
            items.push_back({c, count, sz, align});
            size += sz * count;
        }
    }
    std::size_t size_of(char c) const {
        switch (c) {
            case 'c': case 'b': case 'B': case '?': case 's': case 'p': return 1;
            case 'h': case 'H': case 'e': return 2;
            case 'i': case 'I': case 'f': return 4;
            case 'l': case 'L': return native ? sizeof(long) : 4;
            case 'q': case 'Q': case 'd': case 'n': case 'N': case 'P': return 8;
            default: return 0;
        }
    }
    // How many values pack() takes / unpack() gives.
    std::size_t values() const {
        std::size_t n = 0;
        for (const Item& it : items) n += it.code == 'x' ? 0 : (it.code == 's' || it.code == 'p') ? 1 : it.count;
        return n;
    }
};

inline const Format& parsed(const std::string& fmt) {
    static std::mutex mu;
    static std::unordered_map<std::string, Format> cache;
    std::lock_guard lock(mu);
    auto it = cache.find(fmt);
    if (it == cache.end()) it = cache.emplace(fmt, Format(fmt)).first;
    return it->second;
}

inline std::int64_t calcsize(const std::string& fmt) { return static_cast<std::int64_t>(parsed(fmt).size); }

// ---- half precision, with Python's rounding and errors --------------------------------

inline std::uint16_t to_half(double x) {
    std::uint64_t bits = std::bit_cast<std::uint64_t>(x);
    std::uint16_t sign = static_cast<std::uint16_t>((bits >> 63) << 15);
    if (std::isnan(x)) return sign | 0x7E00;
    if (std::isinf(x)) return sign | 0x7C00;
    double a = std::fabs(x);
    if (a == 0) return sign;
    int e;
    double m = std::frexp(a, &e);  // a = m * 2^e, 0.5 <= m < 1
    // Half: 10 mantissa bits, exponent -14..15 (normal), subnormals down to 2^-24.
    if (e - 1 > 15) raise("OverflowError", "float too large to pack with e format");
    double scaled;
    std::uint16_t exp_bits;
    if (e - 1 < -14) {  // subnormal: units of 2^-24
        scaled = std::ldexp(a, 24);
        exp_bits = 0;
    } else {
        scaled = std::ldexp(m, 11);  // 1024 <= scaled < 2048
        exp_bits = static_cast<std::uint16_t>(e - 1 + 15);
    }
    double rounded = std::nearbyint(scaled);  // round half to even
    std::uint64_t r = static_cast<std::uint64_t>(rounded);
    if (exp_bits == 0) {
        if (r >= 1024) return sign | (1u << 10) | static_cast<std::uint16_t>(r - 1024);  // rounded up into normals
        return sign | static_cast<std::uint16_t>(r);
    }
    if (r >= 2048) {  // rounded up into the next exponent
        r >>= 1;
        ++exp_bits;
        if (exp_bits >= 31) raise("OverflowError", "float too large to pack with e format");
    }
    return sign | static_cast<std::uint16_t>(exp_bits << 10) | static_cast<std::uint16_t>(r - 1024);
}

inline double from_half(std::uint16_t h) {
    int sign = h >> 15 ? -1 : 1;
    int exp = (h >> 10) & 0x1F;
    int frac = h & 0x3FF;
    if (exp == 0x1F) return frac ? std::numeric_limits<double>::quiet_NaN() : sign * std::numeric_limits<double>::infinity();
    if (exp == 0) return sign * std::ldexp(static_cast<double>(frac), -24);
    return sign * std::ldexp(static_cast<double>(frac + 1024), exp - 25);
}

// ---- packing ----------------------------------------------------------------------------

class Packer {
    const Format& f_;
    std::string out_;
    std::size_t item_ = 0, within_ = 0;  // the next item, and how many of it are done

    void pads() {  // 'x' items and alignment before the next value
        while (item_ < f_.items.size() && f_.items[item_].code == 'x') {
            out_.append(f_.items[item_].count, '\0');
            ++item_;
        }
        if (item_ < f_.items.size()) {
            std::size_t align = f_.items[item_].align;
            if (align > 1 && out_.size() % align) out_.append(align - out_.size() % align, '\0');
        }
    }
    const Item& next() {
        if (within_ == 0) pads();
        if (item_ >= f_.items.size()) fail("pack expected " + std::to_string(f_.values()) + " items for packing (got more)");
        const Item& it = f_.items[item_];
        if (it.code == 's' || it.code == 'p' || ++within_ >= it.count) {
            within_ = 0;
            ++item_;
        }
        return it;
    }
    void put_int(std::uint64_t v, std::size_t n) {
        for (std::size_t k = 0; k < n; ++k) {
            std::size_t shift = 8 * (f_.little ? k : n - 1 - k);
            out_ += static_cast<char>((v >> shift) & 0xFF);
        }
    }
    void range_error(char c, const char* lo, const char* hi) {
        fail("'"s + c + "' format requires " + lo + " <= number <= " + hi);
    }

public:
    explicit Packer(const Format& f) : f_(f) {}

    template <class T>
    void put(const T& v) {
        const Item& it = next();
        char c = it.code;
        if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, std::int64_t>) {
            if (c == '?') {
                out_ += static_cast<char>(v ? 1 : 0);
                return;
            }
            std::int64_t n = static_cast<std::int64_t>(v);
            switch (c) {
                case 'b': if (n < -128 || n > 127) range_error(c, "-128", "127"); break;
                case 'B': if (n < 0 || n > 255) range_error(c, "0", "255"); break;
                case 'h': if (n < -32768 || n > 32767) range_error(c, "-32768", "32767"); break;
                case 'H': if (n < 0 || n > 65535) range_error(c, "0", "65535"); break;
                case 'i': if (n < INT32_MIN || n > INT32_MAX) range_error(c, "-2147483648", "2147483647"); break;
                case 'I': if (n < 0 || n > UINT32_MAX) range_error(c, "0", "4294967295"); break;
                case 'l': case 'L':
                    if (it.size == 4) {
                        if (c == 'l' && (n < INT32_MIN || n > INT32_MAX)) range_error(c, "-2147483648", "2147483647");
                        if (c == 'L' && (n < 0 || n > UINT32_MAX)) range_error(c, "0", "4294967295");
                    } else if (c == 'L' && n < 0) {
                        range_error(c, "0", "18446744073709551615");
                    }
                    break;
                case 'Q': case 'N': case 'P': if (n < 0) range_error(c, "0", "18446744073709551615"); break;
                case 'q': case 'n': break;
                case 'e': case 'f': case 'd': return put(static_cast<double>(n));
                default: fail("required argument is not an integer");
            }
            put_int(static_cast<std::uint64_t>(n), it.size);
        } else if constexpr (std::is_same_v<T, double>) {
            if (c == 'e') {
                put_int(to_half(v), 2);
            } else if (c == 'f') {
                if (std::isfinite(v) && std::fabs(v) > static_cast<double>(std::numeric_limits<float>::max()))
                    raise("OverflowError", "float too large to pack with f format");
                put_int(std::bit_cast<std::uint32_t>(static_cast<float>(v)), 4);
            } else if (c == 'd') {
                put_int(std::bit_cast<std::uint64_t>(v), 8);
            } else {
                fail("required argument is not a float");
            }
        } else if constexpr (std::is_same_v<T, bytes>) {
            if (c == 'c') {
                if (v.size() != 1) fail("char format requires a bytes object of length 1");
                out_ += v.data;
            } else if (c == 's') {
                out_ += v.data.substr(0, it.count);
                if (v.size() < it.count) out_.append(it.count - v.size(), '\0');
            } else if (c == 'p') {
                std::size_t n = std::min(v.size(), it.count ? std::min<std::size_t>(it.count - 1, 255) : 0);
                if (it.count) out_ += static_cast<char>(n);
                out_ += v.data.substr(0, n);
                if (it.count > n + 1) out_.append(it.count - n - 1, '\0');
            } else {
                fail("argument for '"s + c + "' must be a bytes object");
            }
        } else {
            static_assert(std::is_same_v<T, bytes>, "struct packs ints, floats, bools and bytes");
        }
    }
    bytes finish() {
        if (within_ != 0 || item_ < f_.items.size()) {
            std::size_t done = 0;  // count the values given so far, for Python's message
            for (std::size_t k = 0; k < item_; ++k) done += f_.items[k].code == 'x' ? 0 : (f_.items[k].code == 's' || f_.items[k].code == 'p') ? 1 : f_.items[k].count;
            done += within_;
            bool only_pads = true;
            for (std::size_t k = item_; k < f_.items.size(); ++k) only_pads = only_pads && f_.items[k].code == 'x';
            if (!only_pads || within_) fail("pack expected " + std::to_string(f_.values()) + " items for packing (got " + std::to_string(done) + ")");
            pads();
        }
        return bytes(std::move(out_));
    }
};

template <class... Args>
bytes pack(const std::string& fmt, const Args&... args) {
    const Format& f = parsed(fmt);
    if (sizeof...(Args) != f.values()) fail("pack expected " + std::to_string(f.values()) + " items for packing (got " + std::to_string(sizeof...(Args)) + ")");
    Packer p(f);
    (p.put(args), ...);
    return p.finish();
}

// ---- unpacking --------------------------------------------------------------------------

class Unpacker {
    const Format& f_;
    const unsigned char* data_;
    std::size_t pos_ = 0, item_ = 0, within_ = 0;

    const Item& next() {
        if (within_ == 0) {
            while (f_.items[item_].code == 'x') pos_ += f_.items[item_++].count;
            std::size_t align = f_.items[item_].align;
            if (align > 1 && pos_ % align) pos_ += align - pos_ % align;
        }
        const Item& it = f_.items[item_];
        if (it.code == 's' || it.code == 'p' || ++within_ >= it.count) {
            within_ = 0;
            ++item_;
        }
        return it;
    }
    std::uint64_t get_int(std::size_t n) {
        std::uint64_t v = 0;
        for (std::size_t k = 0; k < n; ++k) {
            std::size_t shift = 8 * (f_.little ? k : n - 1 - k);
            v |= static_cast<std::uint64_t>(data_[pos_ + k]) << shift;
        }
        pos_ += n;
        return v;
    }
    static std::int64_t sign_extend(std::uint64_t v, std::size_t n) {
        if (n == 8) return static_cast<std::int64_t>(v);
        std::uint64_t sign = std::uint64_t{1} << (8 * n - 1);
        return static_cast<std::int64_t>((v ^ sign) - sign);
    }

public:
    Unpacker(const Format& f, const std::string& data, std::size_t offset) : f_(f), data_(reinterpret_cast<const unsigned char*>(data.data()) + offset) {}

    template <class T>
    T get() {
        const Item& it = next();
        char c = it.code;
        if constexpr (std::is_same_v<T, bool>) {
            return get_int(1) != 0;
        } else if constexpr (std::is_same_v<T, std::int64_t>) {
            std::uint64_t v = get_int(it.size);
            bool is_signed = c == 'b' || c == 'h' || c == 'i' || c == 'l' || c == 'q' || c == 'n';
            return is_signed ? sign_extend(v, it.size) : static_cast<std::int64_t>(v);
        } else if constexpr (std::is_same_v<T, double>) {
            if (c == 'e') return from_half(static_cast<std::uint16_t>(get_int(2)));
            if (c == 'f') return static_cast<double>(std::bit_cast<float>(static_cast<std::uint32_t>(get_int(4))));
            return std::bit_cast<double>(get_int(8));
        } else {
            static_assert(std::is_same_v<T, bytes>, "struct unpacks ints, floats, bools and bytes");
            if (c == 'p') {
                std::size_t n = it.count ? std::min<std::size_t>(data_[pos_], it.count - 1) : 0;
                bytes out(std::string(reinterpret_cast<const char*>(data_) + pos_ + 1, n));
                pos_ += it.count;
                return out;
            }
            std::size_t n = c == 'c' ? 1 : it.count;
            bytes out(std::string(reinterpret_cast<const char*>(data_) + pos_, n));
            pos_ += n;
            return out;
        }
    }
};

template <class Row, std::size_t... I>
Row unpack_at(const Format& f, const std::string& data, std::size_t offset, std::index_sequence<I...>) {
    Unpacker u(f, data, offset);
    Row row;
    ((std::get<I>(row) = u.template get<std::tuple_element_t<I, Row>>()), ...);
    return row;
}

template <class Row>
Row unpack(const std::string& fmt, const bytes& buffer) {
    const Format& f = parsed(fmt);
    if (buffer.size() != f.size) fail("unpack requires a buffer of " + std::to_string(f.size) + " bytes");
    return unpack_at<Row>(f, buffer.data, 0, std::make_index_sequence<std::tuple_size_v<Row>>());
}

template <class Row>
Row unpack_from(const std::string& fmt, const bytes& buffer, std::int64_t offset = 0) {
    const Format& f = parsed(fmt);
    std::int64_t n = static_cast<std::int64_t>(buffer.size());
    if (offset < 0) {
        if (offset + n < 0) fail("offset " + std::to_string(offset) + " out of range for " + std::to_string(n) + "-byte buffer");
        offset += n;
    }
    if (n - offset < static_cast<std::int64_t>(f.size))
        fail("unpack_from requires a buffer of at least " + std::to_string(f.size + static_cast<std::size_t>(offset)) + " bytes for unpacking " +
             std::to_string(f.size) + " bytes at offset " + std::to_string(offset) + " (actual buffer size is " + std::to_string(n) + ")");
    return unpack_at<Row>(f, buffer.data, static_cast<std::size_t>(offset), std::make_index_sequence<std::tuple_size_v<Row>>());
}

template <class Row>
Generator<Row> iter_unpack(std::string fmt, bytes buffer) {
    const Format& f = parsed(fmt);
    if (f.size == 0) fail("cannot iteratively unpack with a struct of length 0");
    if (buffer.size() % f.size) fail("iterative unpacking requires a buffer of a multiple of " + std::to_string(f.size) + " bytes");
    for (std::size_t offset = 0; offset < buffer.size(); offset += f.size)
        co_yield unpack_at<Row>(f, buffer.data, offset, std::make_index_sequence<std::tuple_size_v<Row>>());
}

// struct.Struct(fmt): the same operations, with the format parsed once.
class Struct {
    std::string fmt_;

public:
    Struct() = default;
    explicit Struct(std::string fmt) : fmt_(std::move(fmt)) { parsed(fmt_); }
    template <class... Args>
    bytes pack(const Args&... args) const { return structmod::pack(fmt_, args...); }
    template <class Row>
    Row unpack(const bytes& buffer) const { return structmod::unpack<Row>(fmt_, buffer); }
    template <class Row>
    Row unpack_from(const bytes& buffer, std::int64_t offset = 0) const { return structmod::unpack_from<Row>(fmt_, buffer, offset); }
    template <class Row>
    Generator<Row> iter_unpack(const bytes& buffer) const { return structmod::iter_unpack<Row>(fmt_, buffer); }
    std::int64_t size() const { return calcsize(fmt_); }
    std::string format() const { return fmt_; }
    std::string sd_repr() const { return "Struct(" + repr_str(fmt_) + ")"; }
};

}  // namespace sd::structmod
