// seadash runtime: the small library every generated program includes.
//
// Python-flavoured behaviour lives here so the code generator can stay
// simple: printing and repr(), truthiness, Python-style // and %, negative
// indexing and slicing, str/list/dict/set methods, and Python-like errors.
#pragma once

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <format>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <memory>
#include <numbers>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

using namespace std::string_literals;
using namespace std::string_view_literals;

namespace sd {

// ============================================================================
// Exceptions
// ============================================================================
//
// Exceptions are classes (shared references) in seadash, so we throw a small
// wrapper holding a shared_ptr and match `except` clauses with dynamic_cast.
// The class tree mirrors Python's; keep it in sync with builtins.EXCEPTION_TREE.

struct BaseException : std::enable_shared_from_this<BaseException> {
    std::string message;
    BaseException() = default;
    explicit BaseException(std::string msg) : message(std::move(msg)) {}
    virtual ~BaseException() = default;
    virtual std::string sd_type() const { return "BaseException"; }
    virtual std::string sd_repr() const;  // e.g. ValueError('bad input')
};

#define SD_EXCEPTION(Name, Base)                                  \
    struct Name : Base {                                          \
        using Base::Base;                                         \
        std::string sd_type() const override { return #Name; }    \
    };

SD_EXCEPTION(Exception, BaseException)
SD_EXCEPTION(OSError, Exception)
SD_EXCEPTION(ArithmeticError, Exception)
SD_EXCEPTION(ZeroDivisionError, ArithmeticError)
SD_EXCEPTION(OverflowError, ArithmeticError)
SD_EXCEPTION(LookupError, Exception)
SD_EXCEPTION(IndexError, LookupError)

// A KeyError from a failed lookup holds repr(key) as its message, so its
// repr is KeyError('b') rather than KeyError("'b'"), matching Python.
struct KeyError : LookupError {
    using LookupError::LookupError;
    bool from_lookup = false;
    std::string sd_type() const override { return "KeyError"; }
    std::string sd_repr() const override;
};
SD_EXCEPTION(ValueError, Exception)
SD_EXCEPTION(TypeError, Exception)
SD_EXCEPTION(AttributeError, Exception)
SD_EXCEPTION(AssertionError, Exception)
SD_EXCEPTION(RuntimeError, Exception)
SD_EXCEPTION(NotImplementedError, RuntimeError)
SD_EXCEPTION(EOFError, Exception)
SD_EXCEPTION(FileNotFoundError, OSError)
SD_EXCEPTION(FileExistsError, OSError)
SD_EXCEPTION(PermissionError, OSError)
SD_EXCEPTION(IsADirectoryError, OSError)
SD_EXCEPTION(NotADirectoryError, OSError)
SD_EXCEPTION(TimeoutError, OSError)
SD_EXCEPTION(ConnectionError, OSError)
SD_EXCEPTION(BrokenPipeError, ConnectionError)
SD_EXCEPTION(ConnectionAbortedError, ConnectionError)
SD_EXCEPTION(ConnectionRefusedError, ConnectionError)
SD_EXCEPTION(ConnectionResetError, ConnectionError)
SD_EXCEPTION(UnicodeError, ValueError)
SD_EXCEPTION(UnicodeDecodeError, UnicodeError)
#undef SD_EXCEPTION

struct Thrown {
    std::shared_ptr<BaseException> exc;
};

template <class E>
bool isinstance(const Thrown& t) {
    return dynamic_cast<const E*>(t.exc.get()) != nullptr;
}

template <class E>
[[noreturn]] void raise(const std::string& msg) {
    throw Thrown{std::make_shared<E>(msg)};
}

// Used by the runtime itself, e.g. raise("IndexError", "list index out of range").
[[noreturn]] inline void raise(std::string_view kind, const std::string& msg) {
    if (kind == "IndexError") raise<IndexError>(msg);
    if (kind == "KeyError") {
        auto e = std::make_shared<KeyError>(msg);
        e->from_lookup = true;
        throw Thrown{e};
    }
    if (kind == "ValueError") raise<ValueError>(msg);
    if (kind == "TypeError") raise<TypeError>(msg);
    if (kind == "ZeroDivisionError") raise<ZeroDivisionError>(msg);
    if (kind == "AssertionError") raise<AssertionError>(msg);
    if (kind == "EOFError") raise<EOFError>(msg);
    if (kind == "LookupError") raise<LookupError>(msg);
    if (kind == "OverflowError") raise<OverflowError>(msg);
    if (kind == "OSError") raise<OSError>(msg);
    if (kind == "UnicodeDecodeError") raise<UnicodeDecodeError>(msg);
    if (kind == "UnicodeError") raise<UnicodeError>(msg);
    raise<RuntimeError>(msg);
}

// A hint to the CPU inside a spin-wait loop.
inline void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__)
    asm volatile("yield");
#endif
}

// A module used as a value, e.g. print(math): only its repr is needed.
struct ModuleRef {
    std::string text;
    std::string sd_repr() const { return text; }
};

// sys.exit(): not an exception seadash code can catch, but `finally` still runs.
struct Exit {
    int code;
};

// Runs a `finally` block when the scope is left normally (including return,
// break, continue). The exception path calls run_now() from a catch block,
// so an exception thrown by the finally block itself replaces the original,
// as in Python, instead of terminating the program.
template <class F>
struct Finally {
    F body;
    bool armed = true;
    explicit Finally(F f) : body(std::move(f)) {}
    Finally(const Finally&) = delete;
    Finally& operator=(const Finally&) = delete;
    void run_now() {
        armed = false;
        body();
    }
    void disarm() { armed = false; }
    ~Finally() noexcept(false) {
        if (armed) {
            armed = false;
            body();
        }
    }
};

// A narrowed attribute (`if self.head is not None: self.head.value`). The checker
// can't see every change (a method call may reset self.head), so this is checked:
// a stale narrowing raises instead of reading an empty optional.
template <class T>
T& unwrap(std::optional<T>& o, const char* what) {
    if (!o) raise<AttributeError>(std::string("'") + what + "' is None");
    return *o;
}
template <class T>
const T& unwrap(const std::optional<T>& o, const char* what) {
    if (!o) raise<AttributeError>(std::string("'") + what + "' is None");
    return *o;
}
template <class T>
T unwrap(std::optional<T>&& o, const char* what) {
    if (!o) raise<AttributeError>(std::string("'") + what + "' is None");
    return std::move(*o);
}

// isinstance(x, Dog) for classes (all seadash classes are polymorphic).
template <class C, class P>
bool isinstance_of(const std::shared_ptr<P>& p) {
    return dynamic_cast<const C*>(p.get()) != nullptr;
}
template <class C, class P>
bool isinstance_of(const std::optional<std::shared_ptr<P>>& p) {
    return p && isinstance_of<C>(*p);
}
// An attribute narrowed by isinstance(); checked, like unwrap().
template <class C, class P>
std::shared_ptr<C> downcast(const std::shared_ptr<P>& p, const char* what) {
    auto out = std::dynamic_pointer_cast<C>(p);
    if (!out) raise<TypeError>(std::string("'") + what + "' is no longer the narrowed class");
    return out;
}

// Tag for constructors generated from a user-written __init__.
struct init_t {};
inline constexpr init_t init{};

namespace literals {
constexpr std::int64_t operator""_i(unsigned long long v) { return static_cast<std::int64_t>(v); }
}  // namespace literals

// ============================================================================
// Type helpers
// ============================================================================

// std::vector<bool> is a bit-packed oddity whose elements aren't real bools
// (no bool& references), so list[bool] stores this instead.
struct Bool {
    bool v = false;
    Bool() = default;
    Bool(bool b) : v(b) {}
    operator bool() const { return v; }
    auto operator<=>(const Bool&) const = default;
};

template <class T>
using list = std::vector<std::conditional_t<std::is_same_v<T, bool>, Bool, T>>;

template <class K, class V>
class dict;

template <class T> struct is_vector : std::false_type {};
template <class T, class A> struct is_vector<std::vector<T, A>> : std::true_type {};
template <class T> struct is_set : std::false_type {};
template <class T, class C, class A> struct is_set<std::set<T, C, A>> : std::true_type {};
template <class T> struct is_dict : std::false_type {};
template <class K, class V> struct is_dict<dict<K, V>> : std::true_type {};
template <class T> struct is_optional : std::false_type {};
template <class T> struct is_optional<std::optional<T>> : std::true_type {};
template <class T> struct is_tuple : std::false_type {};
template <class... T> struct is_tuple<std::tuple<T...>> : std::true_type {};
template <class T> struct is_function : std::false_type {};
template <class R, class... A> struct is_function<std::function<R(A...)>> : std::true_type {};
template <class T> struct is_shared : std::false_type {};
template <class T> struct is_shared<std::shared_ptr<T>> : std::true_type {};

template <class T> inline constexpr bool always_false = false;

struct Hash {
    template <class T>
    std::size_t operator()(const T& x) const {
        if constexpr (is_tuple<T>::value) {
            std::size_t h = 0x345678;
            std::apply([&](const auto&... e) { ((h = (h * 1000003) ^ (*this)(e)), ...); }, x);
            return h;
        } else {
            return std::hash<T>{}(x);
        }
    }
};

template <class T>
std::string repr(const T& x);

template <class T>
std::string str(const T& x) {
    if constexpr (std::is_same_v<T, std::string>) {
        return x;
    } else if constexpr (is_shared<T>::value) {
        if constexpr (std::is_base_of_v<BaseException, typename T::element_type>) {
            return x ? x->message : "None";  // str(e) is the message, like Python
        } else if constexpr (requires { x->sd_str(); }) {
            return x ? x->sd_str() : "None";  // a class's __str__
        } else {
            return repr(x);
        }
    } else if constexpr (requires { x.sd_str(); }) {
        return x.sd_str();
    } else if constexpr (is_optional<T>::value) {
        return x ? str(*x) : "None";  // print(maybe_name) shows the text, not its repr
    } else {
        return repr(x);
    }
}

// ============================================================================
// dict: insertion-ordered hash map, laid out like CPython's
// ============================================================================
//
// Entries live in a vector in insertion order (with their hash cached); a
// separate open-addressing table maps hash -> entry index. Each key is stored
// once, lookups touch one small table, and iteration is a plain vector walk.
// Deleted entries become tombstones and are compacted away on the next resize.

template <class K, class V>
class dict {
    std::vector<std::pair<K, V>> items_;
    std::vector<std::size_t> hashes_;
    std::vector<char> alive_;
    std::vector<std::int32_t> table_;  // entry index, or EMPTY / DELETED
    std::size_t live_ = 0;

    static constexpr std::int32_t EMPTY = -1, DELETED = -2;

    static std::size_t hash_of(const K& k) {
        std::size_t h = Hash{}(k);
        h ^= h >> 33;  // mix: std::hash of an int is the int itself
        h *= 0xff51afd7ed558ccdULL;
        h ^= h >> 33;
        return h;
    }

    // Index of k in items_, or -1. `insert_at` gets the table slot where k would go.
    std::int64_t lookup(const K& k, std::size_t h, std::size_t* insert_at = nullptr) const {
        if (table_.empty()) return -1;
        std::size_t mask = table_.size() - 1;
        std::size_t first_deleted = SIZE_MAX;
        for (std::size_t i = h & mask;; i = (i + 1) & mask) {
            std::int32_t slot = table_[i];
            if (slot == EMPTY) {
                if (insert_at) *insert_at = first_deleted != SIZE_MAX ? first_deleted : i;
                return -1;
            }
            if (slot == DELETED) {
                if (first_deleted == SIZE_MAX) first_deleted = i;
            } else if (hashes_[slot] == h && items_[slot].first == k) {
                return slot;
            }
        }
    }

    void rebuild(std::size_t capacity) {
        if (live_ != items_.size()) {  // compact out deleted entries
            std::size_t j = 0;
            for (std::size_t i = 0; i < items_.size(); ++i) {
                if (!alive_[i]) continue;
                if (i != j) {
                    items_[j] = std::move(items_[i]);
                    hashes_[j] = hashes_[i];
                }
                ++j;
            }
            items_.resize(j);
            hashes_.resize(j);
            alive_.assign(j, 1);
        }
        std::size_t size = 8;
        while (size * 2 < capacity * 3) size *= 2;  // keep the load factor under 2/3
        table_.assign(size, EMPTY);
        std::size_t mask = size - 1;
        for (std::size_t n = 0; n < items_.size(); ++n) {
            std::size_t i = hashes_[n] & mask;
            while (table_[i] != EMPTY) i = (i + 1) & mask;
            table_[i] = static_cast<std::int32_t>(n);
        }
    }

    std::size_t insert_new(K key, V value, std::size_t h) {
        if ((items_.size() + 1) * 3 >= table_.size() * 2) rebuild(live_ + 1 > 4 ? (live_ + 1) * 2 : 8);
        std::size_t at = 0;
        lookup(key, h, &at);
        std::size_t n = items_.size();
        items_.emplace_back(std::move(key), std::move(value));
        hashes_.push_back(h);
        alive_.push_back(1);
        table_[at] = static_cast<std::int32_t>(n);
        ++live_;
        return n;
    }

public:
    dict() = default;
    dict(std::initializer_list<std::pair<K, V>> init) {
        for (const auto& [k, v] : init) (*this)[k] = v;
    }

    // d[k] = v : inserts if missing (assignment)
    V& operator[](const K& k) {
        std::size_t h = hash_of(k);
        std::int64_t i = lookup(k, h);
        if (i >= 0) return items_[i].second;
        return items_[insert_new(k, V{}, h)].second;
    }

    // d[k] as a value: KeyError if missing
    V& at(const K& k) {
        std::int64_t i = lookup(k, hash_of(k));
        if (i < 0) raise("KeyError", repr(k));
        return items_[i].second;
    }
    const V& at(const K& k) const { return const_cast<dict*>(this)->at(k); }

    const V* find(const K& k) const {
        std::int64_t i = lookup(k, hash_of(k));
        return i < 0 ? nullptr : &items_[i].second;
    }

    bool contains(const K& k) const { return lookup(k, hash_of(k)) >= 0; }
    std::size_t size() const { return live_; }
    bool empty() const { return live_ == 0; }
    void clear() {
        items_.clear();
        hashes_.clear();
        alive_.clear();
        table_.clear();
        live_ = 0;
    }

    bool erase(const K& k) {
        std::size_t h = hash_of(k);
        std::size_t mask = table_.empty() ? 0 : table_.size() - 1;
        if (table_.empty()) return false;
        for (std::size_t i = h & mask;; i = (i + 1) & mask) {
            std::int32_t slot = table_[i];
            if (slot == EMPTY) return false;
            if (slot >= 0 && hashes_[slot] == h && items_[slot].first == k) {
                table_[i] = DELETED;
                alive_[slot] = 0;
                items_[slot] = std::pair<K, V>{};  // release the memory now
                --live_;
                return true;
            }
        }
    }

    // Iteration skips deleted entries; yields (key, value) pairs in insertion order.
    struct const_iterator {
        const dict* d;
        std::size_t i;
        void skip() {
            while (i < d->items_.size() && !d->alive_[i]) ++i;
        }
        const std::pair<K, V>& operator*() const { return d->items_[i]; }
        const std::pair<K, V>* operator->() const { return &d->items_[i]; }
        const_iterator& operator++() {
            ++i;
            skip();
            return *this;
        }
        bool operator!=(const const_iterator& o) const { return i != o.i; }
        bool operator==(const const_iterator& o) const { return i == o.i; }
    };
    const_iterator begin() const {
        const_iterator it{this, 0};
        it.skip();
        return it;
    }
    const_iterator end() const { return {this, items_.size()}; }

    std::vector<K> keys() const {
        std::vector<K> out;
        out.reserve(live_);
        for (const auto& [k, v] : *this) out.push_back(k);
        return out;
    }
    list<V> values() const {
        list<V> out;
        out.reserve(live_);
        for (const auto& [k, v] : *this) out.push_back(v);
        return out;
    }
    std::vector<std::tuple<K, V>> items() const {
        std::vector<std::tuple<K, V>> out;
        out.reserve(live_);
        for (const auto& [k, v] : *this) out.emplace_back(k, v);
        return out;
    }

    bool operator==(const dict& other) const {
        if (size() != other.size()) return false;
        for (const auto& [k, v] : *this) {
            const V* o = other.find(k);
            if (!o || !(*o == v)) return false;
        }
        return true;
    }
};

// ============================================================================
// bytes: raw binary data (a std::string underneath, but a distinct type)
// ============================================================================

struct bytes {
    std::string data;
    bytes() = default;
    explicit bytes(std::string d) : data(std::move(d)) {}
    std::size_t size() const { return data.size(); }
    bool empty() const { return data.empty(); }
    char operator[](std::size_t i) const { return data[i]; }
    void push_back(char c) { data.push_back(c); }
    auto begin() const { return data.begin(); }
    auto end() const { return data.end(); }
    template <class It>
    void insert(std::string::const_iterator pos, It first, It last) {
        data.insert(pos, first, last);
    }
    friend bytes operator+(const bytes& a, const bytes& b) { return bytes(a.data + b.data); }
    bool operator==(const bytes&) const = default;
    auto operator<=>(const bytes&) const = default;
};

}  // namespace sd

template <>
struct std::hash<sd::bytes> {
    std::size_t operator()(const sd::bytes& b) const { return std::hash<std::string>{}(b.data); }
};

namespace sd {

inline std::string repr_bytes(const bytes& b) {
    bool has_single = b.data.find('\'') != std::string::npos;
    bool has_double = b.data.find('"') != std::string::npos;
    char quote = (has_single && !has_double) ? '"' : '\'';
    std::string out = "b";
    out += quote;
    for (unsigned char c : b.data) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c == quote) {
                    out += '\\';
                    out += static_cast<char>(c);
                } else if (c < 0x20 || c >= 0x7f) {
                    char buf[5];
                    std::snprintf(buf, sizeof buf, "\\x%02x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    out += quote;
    return out;
}

// ============================================================================
// repr / str / print
// ============================================================================

inline std::string repr_str(const std::string& s) {
    bool has_single = s.find('\'') != std::string::npos;
    bool has_double = s.find('"') != std::string::npos;
    char quote = (has_single && !has_double) ? '"' : '\'';
    std::string out(1, quote);
    for (unsigned char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c == quote) {
                    out += '\\';
                    out += static_cast<char>(c);
                } else if (c < 0x20 || c == 0x7f) {
                    char buf[5];
                    std::snprintf(buf, sizeof buf, "\\x%02x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    out += quote;
    return out;
}

// Python's float repr: shortest round-trip digits, fixed notation for
// exponents in [-4, 16), scientific otherwise, and always a '.0' or exponent.
inline std::string float_repr(double d) {
    if (std::isnan(d)) return "nan";
    if (std::isinf(d)) return d > 0 ? "inf" : "-inf";
    char buf[64];
    auto res = std::to_chars(buf, buf + sizeof buf, d, std::chars_format::scientific);
    std::string sci(buf, res.ptr);
    std::string sign;
    if (sci[0] == '-') {
        sign = "-";
        sci.erase(0, 1);
    }
    auto epos = sci.find('e');
    int exp = std::stoi(sci.substr(epos + 1));
    std::string digits = sci.substr(0, epos);
    digits.erase(std::remove(digits.begin(), digits.end(), '.'), digits.end());
    if (exp >= -4 && exp < 16) {
        if (exp >= 0) {
            std::string whole = digits.substr(0, std::min<std::size_t>(digits.size(), exp + 1));
            whole.append(exp + 1 - whole.size(), '0');
            std::string frac = digits.size() > static_cast<std::size_t>(exp + 1) ? digits.substr(exp + 1) : "0";
            return sign + whole + "." + frac;
        }
        return sign + "0." + std::string(-exp - 1, '0') + digits;
    }
    std::string mant = digits.substr(0, 1);
    if (digits.size() > 1) mant += "." + digits.substr(1);
    char ebuf[16];
    std::snprintf(ebuf, sizeof ebuf, "e%c%02d", exp < 0 ? '-' : '+', std::abs(exp));
    return sign + mant + ebuf;
}

template <class T>
std::string repr(const T& x) {
    if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, Bool>) {
        return x ? "True" : "False";
    } else if constexpr (std::is_same_v<T, std::string>) {
        return repr_str(x);
    } else if constexpr (std::is_same_v<T, bytes>) {
        return repr_bytes(x);
    } else if constexpr (std::is_integral_v<T>) {
        return std::to_string(x);
    } else if constexpr (std::is_floating_point_v<T>) {
        return float_repr(x);
    } else if constexpr (std::is_same_v<T, std::nullopt_t>) {
        return "None";
    } else if constexpr (is_optional<T>::value) {
        return x ? repr(*x) : "None";
    } else if constexpr (is_vector<T>::value || is_set<T>::value) {
        if constexpr (is_set<T>::value) {
            if (x.empty()) return "set()";
        }
        std::string out = is_set<T>::value ? "{" : "[";
        bool first = true;
        for (const auto& e : x) {
            if (!first) out += ", ";
            first = false;
            out += repr(e);
        }
        return out + (is_set<T>::value ? "}" : "]");
    } else if constexpr (is_dict<T>::value) {
        std::string out = "{";
        bool first = true;
        for (const auto& [k, v] : x) {
            if (!first) out += ", ";
            first = false;
            out += repr(k) + ": " + repr(v);
        }
        return out + "}";
    } else if constexpr (is_tuple<T>::value) {
        std::string out = "(";
        std::size_t i = 0;
        std::apply([&](const auto&... e) { ((out += (i++ ? ", " : "") + repr(e)), ...); }, x);
        return out + (std::tuple_size_v<T> == 1 ? ",)" : ")");
    } else if constexpr (is_function<T>::value) {
        return "<function>";
    } else if constexpr (is_shared<T>::value) {
        return x ? x->sd_repr() : "None";
    } else if constexpr (requires { x.sd_repr(); }) {
        return x.sd_repr();
    } else {
        static_assert(always_false<T>, "no repr for this type");
    }
}

inline std::string BaseException::sd_repr() const {
    return sd_type() + "(" + (message.empty() ? "" : repr_str(message)) + ")";
}
inline std::string KeyError::sd_repr() const {
    return from_lookup ? "KeyError(" + message + ")" : LookupError::sd_repr();
}

// f-strings: append every piece into one string (no chain of temporaries).
inline void fstr_piece(std::string& out, std::string_view s) { out += s; }
inline void fstr_piece(std::string& out, const std::string& s) { out += s; }
inline void fstr_piece(std::string& out, std::int64_t n) {
    char buf[24];
    auto res = std::to_chars(buf, buf + sizeof buf, n);
    out.append(buf, res.ptr);
}
template <class T>
void fstr_piece(std::string& out, const T& x) {
    out += str(x);
}
template <class... Ts>
std::string fstr(const Ts&... parts) {
    std::string out;
    (fstr_piece(out, parts), ...);
    return out;
}

template <class... Ts>
std::string print_line(std::string_view sep, std::string_view end, const Ts&... xs) {
    std::string out;
    bool first = true;
    auto add = [&](const auto& x) {
        if (!first) out += sep;
        first = false;
        out += str(x);
    };
    (add(xs), ...);
    (void)add;  // print() with no arguments
    out += end;
    return out;
}

template <class... Ts>
void print(std::string_view sep, std::string_view end, const Ts&... xs) {
    std::string out = print_line(sep, end, xs...);
    std::fwrite(out.data(), 1, out.size(), stdout);
}

// ============================================================================
// Truthiness, len, conversions
// ============================================================================

template <class T>
bool truthy(const T& x) {
    if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, Bool>) {
        return x;
    } else if constexpr (std::is_arithmetic_v<T>) {
        return x != 0;
    } else if constexpr (is_optional<T>::value) {
        return x.has_value() && truthy(*x);
    } else if constexpr (is_tuple<T>::value) {
        return std::tuple_size_v<T> != 0;
    } else if constexpr (is_shared<T>::value) {
        if constexpr (requires { x->sd_truthy(); }) {
            return x && x->sd_truthy();  // a class's __bool__ / __len__
        } else {
            return static_cast<bool>(x);
        }
    } else if constexpr (is_function<T>::value) {
        return static_cast<bool>(x);
    } else if constexpr (requires { x.sd_truthy(); }) {
        return x.sd_truthy();
    } else if constexpr (requires { x.empty(); }) {
        return !x.empty();
    } else {
        return true;  // struct values are always truthy
    }
}

template <class T>
std::int64_t len(const T& x) {
    if constexpr (is_tuple<T>::value) {
        return std::tuple_size_v<T>;
    } else {
        return static_cast<std::int64_t>(x.size());
    }
}

inline std::string_view strip_view(std::string_view s) {
    const char* ws = " \t\n\r\f\v";
    auto b = s.find_first_not_of(ws);
    if (b == std::string_view::npos) return {};
    return s.substr(b, s.find_last_not_of(ws) - b + 1);
}

inline std::int64_t to_int(std::int64_t x) { return x; }
inline std::int64_t to_int(bool x) { return x ? 1 : 0; }
inline std::int64_t to_int(double x) {
    if (std::isnan(x) || std::isinf(x)) raise("ValueError", "cannot convert float " + float_repr(x) + " to integer");
    return static_cast<std::int64_t>(x);
}
inline std::int64_t to_int(const std::string& s) {
    std::string cleaned;
    for (char c : strip_view(s))
        if (c != '_') cleaned += c;
    std::string_view v = cleaned;
    if (!v.empty() && v[0] == '+') v.remove_prefix(1);
    std::int64_t out = 0;
    auto res = std::from_chars(v.data(), v.data() + v.size(), out);
    if (v.empty() || res.ec != std::errc{} || res.ptr != v.data() + v.size())
        raise("ValueError", "invalid literal for int() with base 10: " + repr_str(s));
    return out;
}
inline std::int64_t to_int() { return 0; }

inline double to_float(double x) { return x; }
inline double to_float(std::int64_t x) { return static_cast<double>(x); }
inline double to_float(bool x) { return x ? 1.0 : 0.0; }
inline double to_float(const std::string& s) {
    std::string_view v = strip_view(s);
    if (!v.empty() && v[0] == '+') v.remove_prefix(1);
    double out = 0;
    auto res = std::from_chars(v.data(), v.data() + v.size(), out);
    if (v.empty() || res.ec != std::errc{} || res.ptr != v.data() + v.size())
        raise("ValueError", "could not convert string to float: " + repr_str(s));
    return out;
}
inline double to_float() { return 0.0; }

// ============================================================================
// Arithmetic with Python semantics
// ============================================================================

inline std::int64_t floordiv(std::int64_t a, std::int64_t b) {
    if (b == 0) raise("ZeroDivisionError", "integer division or modulo by zero");
    std::int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}
inline double floordiv(double a, double b) {
    if (b == 0) raise("ZeroDivisionError", "float floor division by zero");
    return std::floor(a / b);
}
inline std::int64_t mod(std::int64_t a, std::int64_t b) {
    if (b == 0) raise("ZeroDivisionError", "integer division or modulo by zero");
    std::int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}
inline double mod(double a, double b) {
    if (b == 0) raise("ZeroDivisionError", "float modulo");
    double r = std::fmod(a, b);
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}
inline double truediv(double a, double b) {
    if (b == 0) raise("ZeroDivisionError", "division by zero");
    return a / b;
}
inline std::int64_t pow(std::int64_t base, std::int64_t exp) {
    if (exp < 0) raise("ValueError", "negative exponent in int ** int; use a float (e.g. 2.0 ** -1)");
    std::uint64_t result = 1, b = static_cast<std::uint64_t>(base);
    for (auto e = static_cast<std::uint64_t>(exp); e; e >>= 1) {
        if (e & 1) result *= b;
        b *= b;
    }
    return static_cast<std::int64_t>(result);
}
inline double pow(double base, double exp) { return std::pow(base, exp); }

inline std::int64_t abs(std::int64_t x) { return x < 0 ? -x : x; }
inline double abs(double x) { return std::fabs(x); }

inline std::int64_t round(double x) { return static_cast<std::int64_t>(std::nearbyint(x)); }  // half to even
inline std::int64_t round(std::int64_t x) { return x; }
inline double round(double x, std::int64_t digits) {
    if (digits >= 0 && digits < 300 && std::isfinite(x)) {
        // printf rounds the exact binary value (2.675 is really 2.67499...), like Python.
        char buf[400];
        std::snprintf(buf, sizeof buf, "%.*f", static_cast<int>(digits), x);
        return std::strtod(buf, nullptr);
    }
    double scale = std::pow(10.0, static_cast<double>(-digits));
    return std::nearbyint(x / scale) * scale;
}
inline double round(std::int64_t x, std::int64_t digits) { return round(static_cast<double>(x), digits); }

// ============================================================================
// Indexing and slicing
// ============================================================================

inline std::size_t norm_index(std::int64_t i, std::size_t n, const char* what) {
    if (i < 0) i += static_cast<std::int64_t>(n);
    if (i < 0 || i >= static_cast<std::int64_t>(n)) raise("IndexError", std::string(what) + " index out of range");
    return static_cast<std::size_t>(i);
}

template <class T>
T& index(std::vector<T>& v, std::int64_t i) {
    return v[norm_index(i, v.size(), "list")];
}
template <class T>
const T& index(const std::vector<T>& v, std::int64_t i) {
    return v[norm_index(i, v.size(), "list")];
}
inline std::string index(const std::string& s, std::int64_t i) {
    return std::string(1, s[norm_index(i, s.size(), "string")]);
}
inline std::int64_t index(const bytes& b, std::int64_t i) {
    return static_cast<unsigned char>(b.data[norm_index(i, b.size(), "index")]);
}
template <class K, class V>
V& index(dict<K, V>& d, const std::type_identity_t<K>& k) {
    return d.at(k);
}
template <class K, class V>
const V& index(const dict<K, V>& d, const std::type_identity_t<K>& k) {
    return d.at(k);
}
template <class T, class... Ts>
T tuple_index(const std::tuple<T, Ts...>& t, std::int64_t i) {
    auto arr = std::apply([](const auto&... e) { return std::vector<T>{e...}; }, t);
    return arr[norm_index(i, arr.size(), "tuple")];
}

using opt_int = std::optional<std::int64_t>;

template <class Seq>
Seq slice(const Seq& s, opt_int lo, opt_int hi, opt_int step_) {
    std::int64_t n = static_cast<std::int64_t>(s.size());
    std::int64_t step = step_.value_or(1);
    if (step == 0) raise("ValueError", "slice step cannot be zero");
    auto adjust = [&](opt_int v, std::int64_t dflt) {
        if (!v) return dflt;
        std::int64_t i = *v;
        if (i < 0) {
            i += n;
            if (i < 0) i = step < 0 ? -1 : 0;
        } else if (i >= n) {
            i = step < 0 ? n - 1 : n;
        }
        return i;
    };
    std::int64_t start = adjust(lo, step < 0 ? n - 1 : 0);
    std::int64_t stop = adjust(hi, step < 0 ? -1 : n);
    Seq out;
    for (std::int64_t i = start; step > 0 ? i < stop : i > stop; i += step) out.push_back(s[static_cast<std::size_t>(i)]);
    return out;
}

// ============================================================================
// Iteration: range, iter(), enumerate, zip, ...
// ============================================================================

struct range {
    std::int64_t start_, stop_, step_;
    explicit range(std::int64_t stop) : range(0, stop, 1) {}
    range(std::int64_t start, std::int64_t stop, std::int64_t step = 1) : start_(start), stop_(stop), step_(step) {
        if (step == 0) raise("ValueError", "range() arg 3 must not be zero");
    }
    struct sentinel {};
    struct iterator {
        std::int64_t cur, stop, step;
        std::int64_t operator*() const { return cur; }
        iterator& operator++() {
            cur += step;
            return *this;
        }
        bool operator!=(sentinel) const { return step > 0 ? cur < stop : cur > stop; }
    };
    iterator begin() const { return {start_, stop_, step_}; }
    sentinel end() const { return {}; }
    bool contains(std::int64_t x) const {
        if (step_ > 0 ? (x < start_ || x >= stop_) : (x > start_ || x <= stop_)) return false;
        return (x - start_) % step_ == 0;
    }
};

inline std::vector<std::string> chars(const std::string& s) {
    std::vector<std::string> out;
    for (char c : s) out.emplace_back(1, c);
    return out;
}

// What `for x in value` iterates: strings give 1-char strings, dicts give keys.
// Temporaries are returned by value so range-for doesn't dangle.
template <class T>
decltype(auto) iter(T&& x) {
    using U = std::remove_cvref_t<T>;
    if constexpr (std::is_same_v<U, std::string>) {
        return chars(x);
    } else if constexpr (std::is_same_v<U, bytes>) {
        std::vector<std::int64_t> out;  // looping over bytes gives ints, like Python
        for (unsigned char c : x.data) out.push_back(c);
        return out;
    } else if constexpr (is_dict<U>::value) {
        return x.keys();
    } else if constexpr (requires { file_lines(x); }) {
        return file_lines(x);
    } else if constexpr (requires { x.sd_iter(); }) {
        return x.sd_iter();  // a struct's __iter__
    } else if constexpr (requires { x->sd_iter(); }) {
        return x->sd_iter();  // a class's __iter__
    } else if constexpr (std::is_lvalue_reference_v<T>) {
        return (x);
    } else {
        return U(std::move(x));
    }
}

template <class It>
using elem_t = std::remove_cvref_t<decltype(*std::begin(std::declval<It&>()))>;

template <class It>
auto to_list(It&& it) {
    auto&& src = iter(std::forward<It>(it));
    std::vector<elem_t<decltype(src)>> out;
    if constexpr (requires { src.size(); }) out.reserve(src.size());
    for (auto&& v : src) out.push_back(v);
    return out;
}

// Values where "equal" means "identical", so a stable sort can't be told apart from
// std::sort (which is faster). Not floats: 0.0 == -0.0, but they print differently.
template <class T>
struct plain_order : std::bool_constant<std::is_integral_v<T> || std::is_same_v<T, std::string> ||
                                        std::is_same_v<T, Bool>> {};
template <class... Ts>
struct plain_order<std::tuple<Ts...>> : std::bool_constant<(plain_order<Ts>::value && ...)> {};

template <class T, class Less>
void sort_values(std::vector<T>& v, Less less) {
    if constexpr (plain_order<T>::value) {
        std::sort(v.begin(), v.end(), less);
    } else {
        std::stable_sort(v.begin(), v.end(), less);
    }
}

template <class It>
auto to_set(It&& it) {
    auto&& src = iter(std::forward<It>(it));
    std::set<elem_t<decltype(src)>> out;
    for (auto&& v : src) out.insert(v);
    return out;
}

template <class It>
auto enumerate(It&& it, std::int64_t start = 0) {
    auto&& src = iter(std::forward<It>(it));
    std::vector<std::tuple<std::int64_t, elem_t<decltype(src)>>> out;
    for (auto&& v : src) out.emplace_back(start++, v);
    return out;
}

template <class... Its>
auto zip(Its&&... its) {
    auto lists = std::make_tuple(to_list(std::forward<Its>(its))...);
    std::size_t n = std::apply([](const auto&... l) { return std::min({l.size()...}); }, lists);
    std::vector<std::tuple<typename std::remove_cvref_t<decltype(to_list(its))>::value_type...>> out;
    for (std::size_t i = 0; i < n; ++i)
        out.push_back(std::apply([&](const auto&... l) { return std::make_tuple(l[i]...); }, lists));
    return out;
}

template <class It>
auto reversed(It&& it) {
    auto out = to_list(std::forward<It>(it));
    std::reverse(out.begin(), out.end());
    return out;
}

template <class It>
auto sorted(It&& it, bool reverse = false) {
    auto out = to_list(std::forward<It>(it));
    if (reverse)
        sort_values(out, [](const auto& a, const auto& b) { return b < a; });
    else
        sort_values(out, std::less<>{});
    return out;
}

// Sorting by key computes each key once, like Python (decorate-sort-undecorate).
template <class T, class F>
void sort_by_key(std::vector<T>& v, F&& key, bool reverse) {
    using K = std::remove_cvref_t<decltype(key(v[0]))>;
    std::vector<K> keys;
    keys.reserve(v.size());
    for (const auto& x : v) keys.push_back(key(x));
    std::vector<std::size_t> order(v.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](std::size_t a, std::size_t b) {
        return reverse ? keys[b] < keys[a] : keys[a] < keys[b];
    });
    std::vector<T> sorted;
    sorted.reserve(v.size());
    for (std::size_t i : order) sorted.push_back(std::move(v[i]));
    v = std::move(sorted);
}

template <class It, class F>
auto sorted_by(It&& it, F&& key, bool reverse = false) {
    auto out = to_list(std::forward<It>(it));
    sort_by_key(out, key, reverse);
    return out;
}

template <class T, class F>
void list_sort_by(std::vector<T>& v, F&& key, bool reverse = false) {
    sort_by_key(v, key, reverse);
}

template <class It, class F>
auto extreme_by(It&& it, F&& key, bool want_max, const char* name) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", std::string(name) + "() arg is an empty sequence");
    std::size_t best = 0;
    auto best_key = key(values[0]);
    for (std::size_t i = 1; i < values.size(); ++i) {
        auto k = key(values[i]);
        if (want_max ? best_key < k : k < best_key) {  // first of equal keys wins, like Python
            best = i;
            best_key = std::move(k);
        }
    }
    return values[best];
}
template <class It, class F>
auto min_by(It&& it, F&& key) {
    return extreme_by(std::forward<It>(it), key, false, "min");
}
template <class It, class F>
auto max_by(It&& it, F&& key) {
    return extreme_by(std::forward<It>(it), key, true, "max");
}

template <class F, class It>
auto map(F&& f, It&& it) {
    auto&& src = iter(std::forward<It>(it));
    std::vector<std::remove_cvref_t<decltype(f(std::declval<elem_t<decltype(src)>>()))>> out;
    for (auto&& v : src) out.push_back(f(v));
    return out;
}

template <class F, class It>
auto filter(F&& f, It&& it) {
    auto&& src = iter(std::forward<It>(it));
    std::vector<elem_t<decltype(src)>> out;
    for (auto&& v : src)
        if (truthy(f(v))) out.push_back(v);
    return out;
}

template <class It>
auto sum(It&& it) {
    auto&& src = iter(std::forward<It>(it));
    elem_t<decltype(src)> total{};
    for (auto&& v : src) total += v;
    return total;
}

template <class It>
auto min_of(It&& it) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", "min() arg is an empty sequence");
    return *std::min_element(values.begin(), values.end());
}
template <class It>
auto max_of(It&& it) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", "max() arg is an empty sequence");
    return *std::max_element(values.begin(), values.end());
}

template <class It>
bool any(It&& it) {
    for (auto&& v : iter(std::forward<It>(it)))
        if (truthy(v)) return true;
    return false;
}
template <class It>
bool all(It&& it) {
    for (auto&& v : iter(std::forward<It>(it)))
        if (!truthy(v)) return false;
    return true;
}

// ============================================================================
// Containers: +, *, in, set operators
// ============================================================================

template <class T>
std::vector<T> concat(const std::vector<T>& a, const std::vector<T>& b) {
    std::vector<T> out = a;
    out.insert(out.end(), b.begin(), b.end());
    return out;
}

template <class Seq>
Seq repeat(const Seq& s, std::int64_t n) {
    Seq out;
    for (std::int64_t i = 0; i < n; ++i) out.insert(out.end(), s.begin(), s.end());
    return out;
}

template <class T, class X>
bool contains(const std::vector<T>& v, const X& x) {
    return std::find(v.begin(), v.end(), x) != v.end();
}
template <class T, class X>
bool contains(const std::set<T>& s, const X& x) {
    return s.count(x) != 0;
}
template <class K, class V>
bool contains(const dict<K, V>& d, const std::type_identity_t<K>& k) {
    return d.contains(k);
}
inline bool contains(const std::string& s, const std::string& sub) { return s.find(sub) != std::string::npos; }
inline bool contains(const bytes& b, const bytes& sub) { return b.data.find(sub.data) != std::string::npos; }
inline bool contains(const bytes& b, std::int64_t byte) {
    if (byte < 0 || byte > 255) raise("ValueError", "byte must be in range(0, 256)");
    return b.data.find(static_cast<char>(byte)) != std::string::npos;
}
inline bool contains(const range& r, std::int64_t x) { return r.contains(x); }
template <class... Ts, class X>
bool contains(const std::tuple<Ts...>& t, const X& x) {
    return std::apply([&](const auto&... e) { return ((e == x) || ...); }, t);
}

template <class T>
std::set<T> set_or(const std::set<T>& a, const std::set<T>& b) {
    std::set<T> out = a;
    out.insert(b.begin(), b.end());
    return out;
}
template <class T>
std::set<T> set_and(const std::set<T>& a, const std::set<T>& b) {
    std::set<T> out;
    std::set_intersection(a.begin(), a.end(), b.begin(), b.end(), std::inserter(out, out.end()));
    return out;
}
template <class T>
std::set<T> set_sub(const std::set<T>& a, const std::set<T>& b) {
    std::set<T> out;
    std::set_difference(a.begin(), a.end(), b.begin(), b.end(), std::inserter(out, out.end()));
    return out;
}
template <class T>
std::set<T> set_xor(const std::set<T>& a, const std::set<T>& b) {
    std::set<T> out;
    std::set_symmetric_difference(a.begin(), a.end(), b.begin(), b.end(), std::inserter(out, out.end()));
    return out;
}

// ============================================================================
// str methods (ASCII case rules; strings are UTF-8 bytes)
// ============================================================================

inline std::string str_upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}
inline std::string str_lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}
inline std::string str_capitalize(const std::string& s) {
    if (s.empty()) return s;
    return str_upper(s.substr(0, 1)) + str_lower(s.substr(1));
}
inline std::string str_title(std::string s) {
    bool start = true;
    for (char& c : s) {
        unsigned char u = static_cast<unsigned char>(c);
        c = static_cast<char>(start ? std::toupper(u) : std::tolower(u));
        start = !std::isalpha(u);
    }
    return s;
}

inline const char* WHITESPACE = " \t\n\r\f\v";

inline std::string str_strip(const std::string& s, const std::string& chars = WHITESPACE) {
    auto b = s.find_first_not_of(chars);
    if (b == std::string::npos) return "";
    return s.substr(b, s.find_last_not_of(chars) - b + 1);
}
inline std::string str_lstrip(const std::string& s, const std::string& chars = WHITESPACE) {
    auto b = s.find_first_not_of(chars);
    return b == std::string::npos ? "" : s.substr(b);
}
inline std::string str_rstrip(const std::string& s, const std::string& chars = WHITESPACE) {
    auto e = s.find_last_not_of(chars);
    return e == std::string::npos ? "" : s.substr(0, e + 1);
}

inline std::vector<std::string> str_split(const std::string& s) {
    std::vector<std::string> out;
    std::size_t i = 0;
    while (true) {
        i = s.find_first_not_of(WHITESPACE, i);
        if (i == std::string::npos) break;
        std::size_t j = s.find_first_of(WHITESPACE, i);
        out.push_back(s.substr(i, j == std::string::npos ? std::string::npos : j - i));
        if (j == std::string::npos) break;
        i = j;
    }
    return out;
}
inline std::vector<std::string> str_split(const std::string& s, const std::string& sep) {
    if (sep.empty()) raise("ValueError", "empty separator");
    std::vector<std::string> out;
    std::size_t start = 0, pos;
    while ((pos = s.find(sep, start)) != std::string::npos) {
        out.push_back(s.substr(start, pos - start));
        start = pos + sep.size();
    }
    out.push_back(s.substr(start));
    return out;
}
inline std::vector<std::string> str_splitlines(const std::string& s) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (start < s.size()) {
        std::size_t pos = s.find_first_of("\r\n", start);
        if (pos == std::string::npos) {
            out.push_back(s.substr(start));
            break;
        }
        out.push_back(s.substr(start, pos - start));
        start = pos + ((s[pos] == '\r' && pos + 1 < s.size() && s[pos + 1] == '\n') ? 2 : 1);
    }
    return out;
}
template <class It>
std::string str_join(const std::string& sep, It&& it) {
    std::string out;
    bool first = true;
    for (auto&& part : iter(std::forward<It>(it))) {
        if (!first) out += sep;
        first = false;
        out += part;
    }
    return out;
}
inline bool str_startswith(const std::string& s, const std::string& p) { return s.starts_with(p); }
inline bool str_endswith(const std::string& s, const std::string& p) { return s.ends_with(p); }
inline std::int64_t str_find(const std::string& s, const std::string& sub) {
    auto pos = s.find(sub);
    return pos == std::string::npos ? -1 : static_cast<std::int64_t>(pos);
}
inline std::int64_t str_count(const std::string& s, const std::string& sub) {
    if (sub.empty()) return static_cast<std::int64_t>(s.size()) + 1;
    std::int64_t n = 0;
    for (std::size_t pos = s.find(sub); pos != std::string::npos; pos = s.find(sub, pos + sub.size())) ++n;
    return n;
}
inline std::string str_replace(const std::string& s, const std::string& from, const std::string& to) {
    if (from.empty()) return s;
    std::string out;
    std::size_t start = 0, pos;
    while ((pos = s.find(from, start)) != std::string::npos) {
        out += s.substr(start, pos - start) + to;
        start = pos + from.size();
    }
    return out + s.substr(start);
}

template <class Pred>
bool str_all(const std::string& s, Pred pred) {
    return !s.empty() && std::all_of(s.begin(), s.end(), [&](char c) { return pred(static_cast<unsigned char>(c)); });
}
inline bool str_isdigit(const std::string& s) { return str_all(s, [](int c) { return std::isdigit(c); }); }
inline bool str_isalpha(const std::string& s) { return str_all(s, [](int c) { return std::isalpha(c); }); }
inline bool str_isalnum(const std::string& s) { return str_all(s, [](int c) { return std::isalnum(c); }); }
inline bool str_isspace(const std::string& s) { return str_all(s, [](int c) { return std::isspace(c); }); }
inline bool str_isupper(const std::string& s) {
    return std::any_of(s.begin(), s.end(), [](char c) { return std::isupper(static_cast<unsigned char>(c)); }) &&
           !std::any_of(s.begin(), s.end(), [](char c) { return std::islower(static_cast<unsigned char>(c)); });
}
inline bool str_islower(const std::string& s) {
    return std::any_of(s.begin(), s.end(), [](char c) { return std::islower(static_cast<unsigned char>(c)); }) &&
           !std::any_of(s.begin(), s.end(), [](char c) { return std::isupper(static_cast<unsigned char>(c)); });
}

inline std::int64_t ord(const std::string& s) {
    auto n = s.size();
    auto b = [&](std::size_t i) { return static_cast<unsigned char>(s[i]); };
    std::size_t width = n == 0 ? 0 : b(0) < 0x80 ? 1 : b(0) < 0xE0 ? 2 : b(0) < 0xF0 ? 3 : 4;
    if (n == 0 || n != width)
        raise("TypeError", "ord() expected a character, but string of length " + std::to_string(n) + " found");
    if (width == 1) return b(0);
    std::int64_t cp = b(0) & (0x7F >> width);
    for (std::size_t i = 1; i < width; ++i) cp = (cp << 6) | (b(i) & 0x3F);
    return cp;
}
inline std::string chr(std::int64_t cp) {
    if (cp < 0 || cp > 0x10FFFF) raise("ValueError", "chr() arg not in range(0x110000)");
    std::string out;
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
    return out;
}

// ---- bytes <-> str ----------------------------------------------------------

inline void check_encoding(const std::string& encoding) {
    std::string e = str_lower(encoding);
    if (e != "utf-8" && e != "utf8" && e != "ascii")
        raise("LookupError", "unknown encoding: " + encoding + " (seadash supports utf-8 and ascii)");
}

inline bytes str_encode(const std::string& s, const std::string& encoding = "utf-8") {
    check_encoding(encoding);
    if (str_lower(encoding) == "ascii")
        for (std::size_t i = 0; i < s.size(); ++i)
            if (static_cast<unsigned char>(s[i]) >= 0x80)
                raise("UnicodeError", "'ascii' codec can't encode character in position " + std::to_string(i));
    return bytes(s);  // str is already UTF-8
}

// Validates UTF-8 (strings in seadash are always valid UTF-8 text).
inline std::string bytes_decode(const bytes& b, const std::string& encoding = "utf-8") {
    check_encoding(encoding);
    bool ascii = str_lower(encoding) == "ascii";
    const std::string& s = b.data;
    auto fail = [&](std::size_t i, const char* why) {
        char buf[160];
        std::snprintf(buf, sizeof buf, "'%s' codec can't decode byte 0x%02x in position %zu: %s",
                      ascii ? "ascii" : "utf-8", static_cast<unsigned char>(s[i]), i, why);
        raise("UnicodeDecodeError", buf);
    };
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        std::size_t width = c < 0x80 ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : (c >> 3) == 0x1E ? 4 : 0;
        if (width == 0 || (ascii && width > 1)) fail(i, ascii ? "ordinal not in range(128)" : "invalid start byte");
        if (i + width > s.size()) fail(i, "unexpected end of data");
        for (std::size_t k = 1; k < width; ++k)
            if ((static_cast<unsigned char>(s[i + k]) >> 6) != 0x2) fail(i, "invalid continuation byte");
        i += width;
    }
    return s;
}

inline std::string bytes_hex(const bytes& b) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    for (unsigned char c : b.data) {
        out += digits[c >> 4];
        out += digits[c & 15];
    }
    return out;
}
inline bool bytes_startswith(const bytes& b, const bytes& p) { return b.data.starts_with(p.data); }
inline bool bytes_endswith(const bytes& b, const bytes& p) { return b.data.ends_with(p.data); }
inline std::int64_t bytes_find(const bytes& b, const bytes& sub) { return str_find(b.data, sub.data); }
inline std::int64_t bytes_count(const bytes& b, const bytes& sub) { return str_count(b.data, sub.data); }

// The str methods that make sense for bytes, applied to the underlying bytes.
inline bytes bytes_upper(const bytes& b) { return bytes(str_upper(b.data)); }
inline bytes bytes_lower(const bytes& b) { return bytes(str_lower(b.data)); }
inline bytes bytes_title(const bytes& b) { return bytes(str_title(b.data)); }
inline bytes bytes_capitalize(const bytes& b) { return bytes(str_capitalize(b.data)); }
inline bytes bytes_strip(const bytes& b) { return bytes(str_strip(b.data)); }
inline bytes bytes_strip(const bytes& b, const bytes& chars) { return bytes(str_strip(b.data, chars.data)); }
inline bytes bytes_lstrip(const bytes& b) { return bytes(str_lstrip(b.data)); }
inline bytes bytes_lstrip(const bytes& b, const bytes& chars) { return bytes(str_lstrip(b.data, chars.data)); }
inline bytes bytes_rstrip(const bytes& b) { return bytes(str_rstrip(b.data)); }
inline bytes bytes_rstrip(const bytes& b, const bytes& chars) { return bytes(str_rstrip(b.data, chars.data)); }
inline bool bytes_isdigit(const bytes& b) { return str_isdigit(b.data); }
inline bool bytes_isalpha(const bytes& b) { return str_isalpha(b.data); }
inline bool bytes_isalnum(const bytes& b) { return str_isalnum(b.data); }
inline bool bytes_isspace(const bytes& b) { return str_isspace(b.data); }
inline bool bytes_isupper(const bytes& b) { return str_isupper(b.data); }
inline bool bytes_islower(const bytes& b) { return str_islower(b.data); }
inline std::vector<bytes> as_bytes_list(const std::vector<std::string>& parts) {
    std::vector<bytes> out;
    for (const auto& p : parts) out.emplace_back(p);
    return out;
}
inline std::vector<bytes> bytes_split(const bytes& b) { return as_bytes_list(str_split(b.data)); }
inline std::vector<bytes> bytes_split(const bytes& b, const bytes& sep) { return as_bytes_list(str_split(b.data, sep.data)); }
inline std::vector<bytes> bytes_splitlines(const bytes& b) { return as_bytes_list(str_splitlines(b.data)); }
inline bytes bytes_replace(const bytes& b, const bytes& from, const bytes& to) { return bytes(str_replace(b.data, from.data, to.data)); }
template <class It>
bytes bytes_join(const bytes& sep, It&& parts) {
    std::string out;
    bool first = true;
    for (auto&& part : iter(std::forward<It>(parts))) {
        if (!first) out += sep.data;
        first = false;
        out += part.data;
    }
    return bytes(out);
}

inline bytes to_bytes() { return bytes(); }
inline bytes to_bytes(std::int64_t n) {
    if (n < 0) raise("ValueError", "negative count");
    return bytes(std::string(static_cast<std::size_t>(n), '\0'));
}
template <class It>
bytes to_bytes(It&& it) {
    bytes out;
    for (auto&& v : iter(std::forward<It>(it))) {
        std::int64_t x = v;
        if (x < 0 || x > 255) raise("ValueError", "bytes must be in range(0, 256)");
        out.push_back(static_cast<char>(x));
    }
    return out;
}

// Module functions that take bytes also accept str (as its UTF-8 bytes).
inline const std::string& raw(const bytes& b) { return b.data; }
inline const std::string& raw(const std::string& s) { return s; }

// ============================================================================
// Files
// ============================================================================

// OSError subclasses with Python's message: [Errno 2] No such file or directory: 'x.txt'
// (no path for sockets: [Errno 111] Connection refused)
[[noreturn]] inline void raise_os(int err, const std::optional<std::string>& path) {
    std::string msg = "[Errno " + std::to_string(err) + "] " + std::strerror(err) + (path ? ": " + repr_str(*path) : "");
    switch (err) {
        case ECONNREFUSED: raise<ConnectionRefusedError>(msg);
        case ECONNRESET: raise<ConnectionResetError>(msg);
        case ECONNABORTED: raise<ConnectionAbortedError>(msg);
        case EPIPE: raise<BrokenPipeError>(msg);
        case ETIMEDOUT: raise<TimeoutError>(msg);
        case ENOENT: raise<FileNotFoundError>(msg);
        case EEXIST: raise<FileExistsError>(msg);
        case EACCES:
        case EPERM: raise<PermissionError>(msg);
        case EISDIR: raise<IsADirectoryError>(msg);
        case ENOTDIR: raise<NotADirectoryError>(msg);
        default: raise<OSError>(msg);
    }
}

struct FileBase {
    std::FILE* fp;
    std::string path, mode;
    FileBase(std::FILE* f, std::string p, std::string m) : fp(f), path(std::move(p)), mode(std::move(m)) {}
    FileBase(const FileBase&) = delete;
    FileBase& operator=(const FileBase&) = delete;
    virtual ~FileBase() {
        if (fp) std::fclose(fp);  // files close when the last reference goes away
    }
    std::FILE* handle() const {
        if (!fp) raise("ValueError", "I/O operation on closed file.");
        return fp;
    }
    std::string read_raw(std::int64_t n) {
        std::FILE* f = handle();
        std::string out;
        if (n < 0) {
            char buf[64 * 1024];
            std::size_t k;
            while ((k = std::fread(buf, 1, sizeof buf, f)) > 0) out.append(buf, k);
        } else {
            out.resize(static_cast<std::size_t>(n));
            out.resize(std::fread(out.data(), 1, out.size(), f));
        }
        if (std::ferror(f)) raise_os(errno, path);
        return out;
    }
    std::string readline_raw() {
        std::FILE* f = handle();
        char* line = nullptr;
        std::size_t capacity = 0;
        ssize_t len = ::getline(&line, &capacity, f);  // binary-safe, unlike fgets
        std::string out = len > 0 ? std::string(line, static_cast<std::size_t>(len)) : std::string();
        std::free(line);
        return out;
    }
    std::int64_t write_raw(const std::string& s) {
        std::FILE* f = handle();
        if (std::fwrite(s.data(), 1, s.size(), f) != s.size()) raise_os(errno, path);
        return static_cast<std::int64_t>(s.size());
    }
    void close() {
        if (fp) {
            std::fclose(fp);
            fp = nullptr;
        }
    }
    void flush() { std::fflush(handle()); }
};

// `for line in f:` reads one line at a time, so big files don't need to fit in memory.
template <class F, class Line>
struct LineRange {
    std::shared_ptr<F> file;
    struct sentinel {};
    struct iterator {
        F* file;
        Line line;
        bool done = false;
        void advance() {
            line = Line(file->readline_raw());
            done = line.empty();
        }
        const Line& operator*() const { return line; }
        iterator& operator++() {
            advance();
            return *this;
        }
        bool operator!=(sentinel) const { return !done; }
    };
    iterator begin() const {
        iterator it{file.get(), Line{}};
        it.advance();
        return it;
    }
    sentinel end() const { return {}; }
};

struct TextFile : FileBase {
    using FileBase::FileBase;
    std::string read(std::int64_t n = -1) { return read_raw(n); }
    std::string readline() { return readline_raw(); }
    std::vector<std::string> readlines() {
        std::vector<std::string> out;
        for (std::string line; !(line = readline_raw()).empty();) out.push_back(line);
        return out;
    }
    std::int64_t write(const std::string& s) { return write_raw(s); }
    template <class It>
    void writelines(It&& lines) {
        for (auto&& line : iter(std::forward<It>(lines))) write_raw(line);
    }
    std::string sd_repr() const { return "<TextIO name=" + repr_str(path) + " mode=" + repr_str(mode) + ">"; }
};

struct BinaryFile : FileBase {
    using FileBase::FileBase;
    bytes read(std::int64_t n = -1) { return bytes(read_raw(n)); }
    bytes readline() { return bytes(readline_raw()); }
    std::vector<bytes> readlines() {
        std::vector<bytes> out;
        for (std::string line; !(line = readline_raw()).empty();) out.emplace_back(line);
        return out;
    }
    std::int64_t write(const bytes& b) { return write_raw(b.data); }
    template <class It>
    void writelines(It&& lines) {
        for (auto&& line : iter(std::forward<It>(lines))) write_raw(line.data);
    }
    std::string sd_repr() const { return "<BinaryIO name=" + repr_str(path) + " mode=" + repr_str(mode) + ">"; }
};

inline LineRange<TextFile, std::string> file_lines(const std::shared_ptr<TextFile>& f) {
    f->handle();
    return {f};
}
inline LineRange<BinaryFile, bytes> file_lines(const std::shared_ptr<BinaryFile>& f) {
    f->handle();
    return {f};
}

inline std::FILE* open_file(const std::string& path, const std::string& mode) {
    // Python's "x" (create, fail if it exists) is "wx" in C.
    std::string c_mode;
    for (char ch : mode)
        if (ch != 't') c_mode += ch == 'x' ? std::string("wx") : std::string(1, ch);
    if (c_mode.find('b') == std::string::npos) c_mode += 'b';  // no newline translation; text is UTF-8
    std::FILE* f = std::fopen(path.c_str(), c_mode.c_str());
    if (!f) raise_os(errno, path);
    struct stat info;
    if (fstat(fileno(f), &info) == 0 && S_ISDIR(info.st_mode)) {
        std::fclose(f);
        raise_os(EISDIR, path);
    }
    return f;
}

inline std::shared_ptr<TextFile> open_text(const std::string& path, const std::string& mode = "r",
                                           const std::string& encoding = "utf-8") {
    check_encoding(encoding);
    return std::make_shared<TextFile>(open_file(path, mode), path, mode);
}
inline std::shared_ptr<BinaryFile> open_binary(const std::string& path, const std::string& mode) {
    return std::make_shared<BinaryFile>(open_file(path, mode), path, mode);
}

template <class... Ts>
void print_to(const std::shared_ptr<TextFile>& file, std::string_view sep, std::string_view end, const Ts&... xs) {
    file->write_raw(print_line(sep, end, xs...));
}

inline std::string input(const std::string& prompt = "") {
    std::fwrite(prompt.data(), 1, prompt.size(), stdout);
    std::fflush(stdout);
    std::string line;
    if (!std::getline(std::cin, line)) raise("EOFError", "EOF when reading a line");
    return line;
}

// ============================================================================
// list / dict / set methods
// ============================================================================

template <class T>
T list_pop(std::vector<T>& v, std::int64_t i = -1) {
    if (v.empty()) raise("IndexError", "pop from empty list");
    if (i < 0) i += static_cast<std::int64_t>(v.size());
    if (i < 0 || i >= static_cast<std::int64_t>(v.size())) raise("IndexError", "pop index out of range");
    T out = std::move(v[static_cast<std::size_t>(i)]);
    v.erase(v.begin() + i);
    return out;
}
template <class T, class X>
void list_insert(std::vector<T>& v, std::int64_t i, X&& x) {
    std::int64_t n = static_cast<std::int64_t>(v.size());
    if (i < 0) i = std::max<std::int64_t>(0, i + n);
    v.insert(v.begin() + std::min(i, n), std::forward<X>(x));
}
template <class T, class X>
void list_remove(std::vector<T>& v, const X& x) {
    auto it = std::find(v.begin(), v.end(), x);
    if (it == v.end()) raise("ValueError", "list.remove(x): x not in list");
    v.erase(it);
}
template <class T, class X>
std::int64_t list_index(const std::vector<T>& v, const X& x) {
    auto it = std::find(v.begin(), v.end(), x);
    if (it == v.end()) raise("ValueError", repr(x) + " is not in list");
    return it - v.begin();
}
template <class T, class X>
std::int64_t list_count(const std::vector<T>& v, const X& x) {
    return std::count(v.begin(), v.end(), x);
}
template <class T, class It>
void list_extend(std::vector<T>& v, It&& it) {
    auto items = to_list(std::forward<It>(it));  // copy first: `xs.extend(xs)` is fine
    v.insert(v.end(), items.begin(), items.end());
}
template <class T>
void list_sort(std::vector<T>& v, bool reverse = false) {
    if (reverse)
        sort_values(v, [](const T& a, const T& b) { return b < a; });
    else
        sort_values(v, std::less<>{});
}
template <class T>
void list_reverse(std::vector<T>& v) {
    std::reverse(v.begin(), v.end());
}

template <class K, class V>
std::optional<V> dict_get(const dict<K, V>& d, const std::type_identity_t<K>& k) {
    const V* v = d.find(k);
    return v ? std::optional<V>(*v) : std::nullopt;
}
template <class K, class V>
V dict_get_or(const dict<K, V>& d, const std::type_identity_t<K>& k, const std::type_identity_t<V>& dflt) {
    const V* v = d.find(k);
    return v ? *v : dflt;
}
template <class K, class V>
V dict_pop(dict<K, V>& d, const std::type_identity_t<K>& k) {
    V out = d.at(k);
    d.erase(k);
    return out;
}
template <class K, class V>
V dict_pop(dict<K, V>& d, const std::type_identity_t<K>& k, const std::type_identity_t<V>& dflt) {
    const V* v = d.find(k);
    if (!v) return dflt;
    V out = *v;
    d.erase(k);
    return out;
}
template <class K, class V>
V& dict_setdefault(dict<K, V>& d, const std::type_identity_t<K>& k, const std::type_identity_t<V>& dflt) {
    if (!d.contains(k)) d[k] = dflt;
    return d.at(k);
}
template <class K, class V>
void dict_update(dict<K, V>& d, const dict<K, V>& other) {
    for (const auto& [k, v] : other) d[k] = v;
}

template <class T>
void set_remove(std::set<T>& s, const std::type_identity_t<T>& x) {
    if (!s.erase(x)) raise("KeyError", repr(x));
}
template <class T>
bool set_issubset(const std::set<T>& a, const std::set<T>& b) {
    return std::includes(b.begin(), b.end(), a.begin(), a.end());
}

// ============================================================================
// Program entry
// ============================================================================

inline std::vector<std::string>& argv() {
    static std::vector<std::string> args;
    return args;
}

// Run when the main program finishes, however it finishes (threading joins its threads here).
inline std::vector<std::function<void()>>& exit_hooks() {
    static std::vector<std::function<void()>> hooks;
    return hooks;
}

inline int run_main(int argc, char** argv_, void (*module_main)()) {
    for (int i = 0; i < argc; ++i) argv().emplace_back(argv_[i]);
    int code = 0;
    try {
        module_main();
    } catch (const Exit& e) {
        code = e.code;
    } catch (const Thrown& t) {
        std::fflush(stdout);
        std::string type = t.exc->sd_type();
        const std::string& msg = t.exc->message;
        std::fprintf(stderr, "%s%s%s\n", type.c_str(), msg.empty() ? "" : ": ", msg.c_str());
        code = 1;
    }
    for (auto& hook : exit_hooks()) hook();
    std::fflush(stdout);
    return code;
}

}  // namespace sd

using namespace sd::literals;
