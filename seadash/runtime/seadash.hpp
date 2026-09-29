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
#include <cstdlib>
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
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

using namespace std::string_literals;

namespace sd {

// ============================================================================
// Errors
// ============================================================================

struct Error : std::runtime_error {
    const char* kind;  // "IndexError", "KeyError", ...
    Error(const char* kind, const std::string& msg) : std::runtime_error(msg), kind(kind) {}
};

[[noreturn]] inline void raise(const char* kind, const std::string& msg) { throw Error(kind, msg); }

struct Exit {
    int code;
};

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
    } else {
        return repr(x);
    }
}

// ============================================================================
// dict: a hash map that remembers insertion order, like Python's
// ============================================================================

template <class K, class V>
class dict {
    std::vector<std::pair<K, V>> items_;
    std::unordered_map<K, std::size_t, Hash> index_;

public:
    dict() = default;
    dict(std::initializer_list<std::pair<K, V>> init) {
        for (const auto& [k, v] : init) (*this)[k] = v;
    }

    // d[k] = v : inserts if missing (assignment)
    V& operator[](const K& k) {
        auto it = index_.find(k);
        if (it != index_.end()) return items_[it->second].second;
        index_.emplace(k, items_.size());
        items_.emplace_back(k, V{});
        return items_.back().second;
    }

    // d[k] as a value: KeyError if missing
    V& at(const K& k) {
        auto it = index_.find(k);
        if (it == index_.end()) raise("KeyError", repr(k));
        return items_[it->second].second;
    }
    const V& at(const K& k) const { return const_cast<dict*>(this)->at(k); }

    const V* find(const K& k) const {
        auto it = index_.find(k);
        return it == index_.end() ? nullptr : &items_[it->second].second;
    }

    bool contains(const K& k) const { return index_.count(k) != 0; }
    std::size_t size() const { return items_.size(); }
    bool empty() const { return items_.empty(); }
    void clear() {
        items_.clear();
        index_.clear();
    }

    bool erase(const K& k) {
        auto it = index_.find(k);
        if (it == index_.end()) return false;
        std::size_t i = it->second;
        index_.erase(it);
        items_.erase(items_.begin() + static_cast<std::ptrdiff_t>(i));
        for (auto& [key, j] : index_)
            if (j > i) --j;
        return true;
    }

    auto begin() const { return items_.begin(); }
    auto end() const { return items_.end(); }

    std::vector<K> keys() const {
        std::vector<K> out;
        for (const auto& [k, v] : items_) out.push_back(k);
        return out;
    }
    list<V> values() const {
        list<V> out;
        for (const auto& [k, v] : items_) out.push_back(v);
        return out;
    }
    std::vector<std::tuple<K, V>> items() const {
        std::vector<std::tuple<K, V>> out;
        for (const auto& [k, v] : items_) out.emplace_back(k, v);
        return out;
    }

    bool operator==(const dict& other) const {
        if (size() != other.size()) return false;
        for (const auto& [k, v] : items_) {
            const V* o = other.find(k);
            if (!o || !(*o == v)) return false;
        }
        return true;
    }
};

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
    } else if constexpr (is_shared<T>::value) {
        return x ? x->sd_repr() : "None";
    } else if constexpr (requires { x.sd_repr(); }) {
        return x.sd_repr();
    } else {
        static_assert(always_false<T>, "no repr for this type");
    }
}

template <class... Ts>
void print(std::string_view sep, std::string_view end, const Ts&... xs) {
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
        return x != nullptr;
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
    } else if constexpr (is_dict<U>::value) {
        return x.keys();
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
    for (auto&& v : src) out.push_back(v);
    return out;
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
        std::stable_sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return b < a; });
    else
        std::stable_sort(out.begin(), out.end());
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
        std::stable_sort(v.begin(), v.end(), [](const T& a, const T& b) { return b < a; });
    else
        std::stable_sort(v.begin(), v.end());
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

inline int run_main(int argc, char** argv_, void (*module_main)()) {
    for (int i = 0; i < argc; ++i) argv().emplace_back(argv_[i]);
    try {
        module_main();
    } catch (const Exit& e) {
        std::fflush(stdout);
        return e.code;
    } catch (const Error& e) {
        std::fflush(stdout);
        std::string msg = e.what();
        std::fprintf(stderr, "%s%s%s\n", e.kind, msg.empty() ? "" : ": ", msg.c_str());
        return 1;
    }
    std::fflush(stdout);
    return 0;
}

}  // namespace sd

using namespace sd::literals;
