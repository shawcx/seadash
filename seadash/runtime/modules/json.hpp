// The `json` module.
//
//   loads<T>(text)  parse, then decode into T (checked, with a path in errors)
//   dumps(x, ...)   convert x to a Value tree, then write it like Python's json
//   Value           a dynamically typed JSON value, for data of unknown shape
//
// Parse errors use Python's messages: "Expecting value: line 1 column 1 (char 0)".
//
// tomllib shares Value and the typed decoder: a TOML document is a tree of the same
// values plus dates and times, which JSON itself never produces.
#pragma once

#include <variant>

#include "datetime.hpp"

namespace sd::json {

struct JSONDecodeError : ValueError {
    using ValueError::ValueError;
    std::string sd_type() const override { return "json.decoder.JSONDecodeError"; }
};

// ============================================================================
// Value
// ============================================================================

struct Value {
    enum class Kind { Null, Bool, Int, Float, Str, List, Dict, Date, Time, DateTime };
    using When = std::variant<datetime::date, datetime::time, datetime::datetime>;
    Kind kind = Kind::Null;
    bool b = false;
    std::int64_t i = 0;
    double f = 0;
    std::string s;
    // Arrays and objects are shared: a Value is read-only once built, so sharing is invisible.
    std::shared_ptr<std::vector<Value>> arr_;
    std::shared_ptr<dict<std::string, Value>> obj_;
    std::shared_ptr<const When> when_;  // a TOML date, time or datetime

    static Value boolean(bool x) {
        Value v;
        v.kind = Kind::Bool;
        v.b = x;
        return v;
    }
    static Value integer(std::int64_t x) {
        Value v;
        v.kind = Kind::Int;
        v.i = x;
        return v;
    }
    static Value number(double x) {
        Value v;
        v.kind = Kind::Float;
        v.f = x;
        return v;
    }
    static Value string(std::string x) {
        Value v;
        v.kind = Kind::Str;
        v.s = std::move(x);
        return v;
    }
    static Value array() {
        Value v;
        v.kind = Kind::List;
        v.arr_ = std::make_shared<std::vector<Value>>();
        return v;
    }
    static Value object() {
        Value v;
        v.kind = Kind::Dict;
        v.obj_ = std::make_shared<dict<std::string, Value>>();
        return v;
    }
    template <class D>
    static Value moment(D x) {
        Value v;
        v.kind = std::is_same_v<D, datetime::date> ? Kind::Date : std::is_same_v<D, datetime::time> ? Kind::Time : Kind::DateTime;
        v.when_ = std::make_shared<const When>(std::move(x));
        return v;
    }
    void push(Value v) { arr_->push_back(std::move(v)); }
    void set(const std::string& key, Value v) { (*obj_)[key] = std::move(v); }
    const Value* find(const std::string& key) const { return kind == Kind::Dict ? obj_->find(key) : nullptr; }

    // "an int", "a string", ... for error messages
    const char* a_name() const {
        switch (kind) {
            case Kind::Null: return "null";
            case Kind::Bool: return "a bool";
            case Kind::Int: return "an int";
            case Kind::Float: return "a float";
            case Kind::Str: return "a string";
            case Kind::List: return "an array";
            case Kind::Dict: return "an object";
            case Kind::Date: return "a date";
            case Kind::Time: return "a time";
            case Kind::DateTime: return "a datetime";
        }
        return "?";
    }
    // The Python type name of a date or time, for messages: "date"
    const char* moment_type() const { return kind == Kind::Date ? "date" : kind == Kind::Time ? "time" : "datetime"; }
    bool is_moment() const { return kind == Kind::Date || kind == Kind::Time || kind == Kind::DateTime; }
    [[noreturn]] void wrong(const char* want) const {
        raise("TypeError", std::string("this json.Value is ") + a_name() + ", not " + want);
    }

    // ---- the seadash-facing API ------------------------------------------------
    std::int64_t as_int() const {
        if (kind != Kind::Int) wrong("an int");
        return i;
    }
    double as_float() const {
        if (kind == Kind::Int) return static_cast<double>(i);
        if (kind != Kind::Float) wrong("a number");
        return f;
    }
    std::string as_str() const {
        if (kind != Kind::Str) wrong("a string");
        return s;
    }
    bool as_bool() const {
        if (kind != Kind::Bool) wrong("a bool");
        return b;
    }
    std::vector<Value> as_list() const {
        if (kind != Kind::List) wrong("an array");
        return *arr_;
    }
    dict<std::string, Value> as_dict() const {
        if (kind != Kind::Dict) wrong("an object");
        return *obj_;
    }
    datetime::date as_date() const {
        if (kind != Kind::Date) wrong("a date");
        return std::get<datetime::date>(*when_);
    }
    datetime::time as_time() const {
        if (kind != Kind::Time) wrong("a time");
        return std::get<datetime::time>(*when_);
    }
    datetime::datetime as_datetime() const {
        if (kind != Kind::DateTime) wrong("a datetime");
        return std::get<datetime::datetime>(*when_);
    }
    bool is_null() const { return kind == Kind::Null; }
    bool is_bool() const { return kind == Kind::Bool; }
    bool is_int() const { return kind == Kind::Int; }
    bool is_float() const { return kind == Kind::Float; }
    bool is_str() const { return kind == Kind::Str; }
    bool is_list() const { return kind == Kind::List; }
    bool is_dict() const { return kind == Kind::Dict; }
    bool is_date() const { return kind == Kind::Date; }
    bool is_time() const { return kind == Kind::Time; }
    bool is_datetime() const { return kind == Kind::DateTime; }

    std::vector<std::string> keys() const {
        if (kind != Kind::Dict) wrong("an object");
        return obj_->keys();
    }
    std::vector<Value> values() const {
        if (kind != Kind::Dict) wrong("an object");
        return obj_->values();
    }
    std::vector<std::tuple<std::string, Value>> items() const {
        if (kind != Kind::Dict) wrong("an object");
        return obj_->items();
    }
    std::optional<Value> get(const std::string& key) const {
        if (kind != Kind::Dict) wrong("an object");
        const Value* v = obj_->find(key);
        return v ? std::optional<Value>(*v) : std::nullopt;
    }

    std::size_t size() const {
        switch (kind) {
            case Kind::List: return arr_->size();
            case Kind::Dict: return obj_->size();
            case Kind::Str: return s.size();
            default: raise("TypeError", std::string("len() of a json.Value that is ") + a_name());
        }
    }
    bool sd_truthy() const {
        switch (kind) {
            case Kind::Null: return false;
            case Kind::Bool: return b;
            case Kind::Int: return i != 0;
            case Kind::Float: return f != 0;
            case Kind::Str: return !s.empty();
            case Kind::List: return !arr_->empty();
            case Kind::Dict: return !obj_->empty();
            default: return true;  // dates and times
        }
    }
    // str() of a string is the text itself, like Python, and of a date its ISO form;
    // anything else prints as repr.
    std::string sd_str() const {
        if (kind == Kind::Str) return s;
        if (is_moment()) return std::visit([](const auto& w) { return w.sd_str(); }, *when_);
        return sd_repr();
    }
    // Prints like the equivalent Python object: {'a': [1, 2.5, None, True]}
    std::string sd_repr() const {
        switch (kind) {
            case Kind::Null: return "None";
            case Kind::Bool: return b ? "True" : "False";
            case Kind::Int: return std::to_string(i);
            case Kind::Float: return float_repr(f);
            case Kind::Str: return repr_str(s);
            case Kind::List: return repr(*arr_);
            case Kind::Dict: return repr(*obj_);
            default: return std::visit([](const auto& w) { return w.sd_repr(); }, *when_);
        }
    }
    auto begin() const {
        if (kind != Kind::List) wrong("an array (only arrays can be looped over; use .keys() for objects)");
        return arr_->cbegin();
    }
    auto end() const { return arr_->cend(); }

    bool operator==(const Value& o) const {
        bool num = kind == Kind::Int || kind == Kind::Float;
        bool onum = o.kind == Kind::Int || o.kind == Kind::Float;
        if (num && onum) {
            if (kind == Kind::Int && o.kind == Kind::Int) return i == o.i;
            return as_float() == o.as_float();
        }
        if (kind != o.kind) return false;
        switch (kind) {
            case Kind::Null: return true;
            case Kind::Bool: return b == o.b;
            case Kind::Str: return s == o.s;
            case Kind::List: return *arr_ == *o.arr_;
            case Kind::Dict: return *obj_ == *o.obj_;
            case Kind::Date: case Kind::Time: case Kind::DateTime: return *when_ == *o.when_;
            default: return false;
        }
    }
};

// ============================================================================
// Parsing
// ============================================================================

class Parser {
    std::string_view text;
    std::size_t pos = 0;

public:
    explicit Parser(std::string_view t) : text(t) {}

    Value parse_document() {
        Value v = parse_value();
        skip_ws();
        if (pos != text.size()) fail("Extra data", pos);
        return v;
    }

private:
    [[noreturn]] void fail(const std::string& msg, std::size_t at) const {
        std::size_t line = 1 + static_cast<std::size_t>(std::count(text.begin(), text.begin() + at, '\n'));
        std::size_t last_nl = text.rfind('\n', at == 0 ? 0 : at - 1);
        std::size_t col = (last_nl == std::string_view::npos || last_nl >= at) ? at + 1 : at - last_nl;
        throw Thrown{std::make_shared<JSONDecodeError>(msg + ": line " + std::to_string(line) + " column " +
                                                       std::to_string(col) + " (char " + std::to_string(at) + ")")};
    }
    char peek() const { return pos < text.size() ? text[pos] : '\0'; }
    void skip_ws() {
        while (pos < text.size() && (text[pos] == ' ' || text[pos] == '\t' || text[pos] == '\n' || text[pos] == '\r'))
            ++pos;
    }
    bool literal(std::string_view word) {
        if (text.substr(pos, word.size()) != word) return false;
        pos += word.size();
        return true;
    }

    Value parse_value() {
        skip_ws();
        if (pos >= text.size()) fail("Expecting value", pos);
        char c = text[pos];
        if (c == '{') return parse_object();
        if (c == '[') return parse_array();
        if (c == '"') return Value::string(parse_string());
        if (literal("true")) return Value::boolean(true);
        if (literal("false")) return Value::boolean(false);
        if (literal("null")) return Value();
        if (literal("NaN")) return Value::number(std::numeric_limits<double>::quiet_NaN());
        if (literal("Infinity")) return Value::number(std::numeric_limits<double>::infinity());
        if (literal("-Infinity")) return Value::number(-std::numeric_limits<double>::infinity());
        if (c == '-' || (c >= '0' && c <= '9')) return parse_number();
        fail("Expecting value", pos);
    }

    Value parse_object() {
        Value obj = Value::object();
        ++pos;
        skip_ws();
        if (peek() == '}') {
            ++pos;
            return obj;
        }
        while (true) {
            skip_ws();
            if (peek() != '"') fail("Expecting property name enclosed in double quotes", pos);
            std::string key = parse_string();
            skip_ws();
            if (peek() != ':') fail("Expecting ':' delimiter", pos);
            ++pos;
            obj.set(key, parse_value());
            skip_ws();
            if (peek() == ',') {
                ++pos;
                continue;
            }
            if (peek() == '}') {
                ++pos;
                return obj;
            }
            fail("Expecting ',' delimiter", pos);
        }
    }

    Value parse_array() {
        Value arr = Value::array();
        ++pos;
        skip_ws();
        if (peek() == ']') {
            ++pos;
            return arr;
        }
        while (true) {
            arr.push(parse_value());
            skip_ws();
            if (peek() == ',') {
                ++pos;
                continue;
            }
            if (peek() == ']') {
                ++pos;
                return arr;
            }
            fail("Expecting ',' delimiter", pos);
        }
    }

    static void append_utf8(std::string& out, std::uint32_t cp) { out += chr(static_cast<std::int64_t>(cp)); }

    std::uint32_t hex4(std::size_t at) {
        if (at + 4 > text.size()) fail("Invalid \\uXXXX escape", at - 1);
        std::uint32_t v = 0;
        for (std::size_t k = 0; k < 4; ++k) {
            char c = text[at + k];
            int d = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
            if (d < 0) fail("Invalid \\uXXXX escape", at - 1);
            v = v * 16 + static_cast<std::uint32_t>(d);
        }
        return v;
    }

    std::string parse_string() {
        std::size_t start = pos++;
        std::string out;
        while (true) {
            if (pos >= text.size()) fail("Unterminated string starting at", start);
            char c = text[pos];
            if (c == '"') {
                ++pos;
                return out;
            }
            if (static_cast<unsigned char>(c) < 0x20) fail("Invalid control character at", pos);
            if (c != '\\') {
                out += c;
                ++pos;
                continue;
            }
            std::size_t esc = pos;
            if (pos + 1 >= text.size()) fail("Unterminated string starting at", start);
            char e = text[pos + 1];
            pos += 2;
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    std::uint32_t cp = hex4(pos);
                    pos += 4;
                    if (cp >= 0xD800 && cp <= 0xDBFF && text.substr(pos, 2) == "\\u") {
                        std::uint32_t low = hex4(pos + 2);
                        if (low >= 0xDC00 && low <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                            pos += 6;
                        }
                    }
                    append_utf8(out, cp);
                    break;
                }
                default: fail("Invalid \\escape", esc);
            }
        }
    }

    Value parse_number() {
        std::size_t start = pos;
        if (peek() == '-') ++pos;
        if (peek() == '0') {
            ++pos;
        } else if (peek() >= '1' && peek() <= '9') {
            while (peek() >= '0' && peek() <= '9') ++pos;
        } else {
            fail("Expecting value", start);
        }
        bool is_float = false;
        if (peek() == '.' && pos + 1 < text.size() && text[pos + 1] >= '0' && text[pos + 1] <= '9') {
            is_float = true;
            ++pos;
            while (peek() >= '0' && peek() <= '9') ++pos;
        }
        if ((peek() == 'e' || peek() == 'E')) {
            std::size_t save = pos++;
            if (peek() == '+' || peek() == '-') ++pos;
            if (peek() >= '0' && peek() <= '9') {
                is_float = true;
                while (peek() >= '0' && peek() <= '9') ++pos;
            } else {
                pos = save;
            }
        }
        std::string_view num = text.substr(start, pos - start);
        if (!is_float) {
            std::int64_t v = 0;
            auto res = std::from_chars(num.data(), num.data() + num.size(), v);
            if (res.ec == std::errc{}) return Value::integer(v);
            // too big for a 64-bit int: keep it as a float rather than failing
        }
        // strtod, not from_chars: on overflow (1e400) it gives inf, like Python.
        return Value::number(std::strtod(std::string(num).c_str(), nullptr));
    }
};

inline Value parse(std::string_view text) { return Parser(text).parse_document(); }

// ============================================================================
// Typed decoding: Value -> T
// ============================================================================

// Whether tomllib is decoding (see tomllib.hpp), for messages: TOML says "table" for an object.
inline thread_local bool decoding_toml = false;
inline const char* an_object() { return decoding_toml ? "a table" : "an object"; }

[[noreturn]] inline void mismatch(const char* want, const Value& v, const std::string& path) {
    raise("ValueError", std::string(decoding_toml ? "tomllib" : "json") + ": expected " + want + " at " + path + ", got " +
                            (v.is_dict() ? an_object() : v.a_name()));
}
inline void expect_object(const Value& v, const std::string& path) {
    if (!v.is_dict()) mismatch(an_object(), v, path);
}
[[noreturn]] inline void missing_field(const std::string& name, const std::string& path) {
    raise("ValueError", std::string(decoding_toml ? "tomllib" : "json") + ": missing field '" + name + "' at " + path);
}

template <class T>
T decode(const Value& v, const std::string& path) {
    if constexpr (std::is_same_v<T, Value>) {
        return v;
    } else if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, Bool>) {
        if (!v.is_bool()) mismatch("a bool", v, path);
        return v.b;
    } else if constexpr (std::is_same_v<T, std::int64_t>) {
        if (!v.is_int()) mismatch("an int", v, path);
        return v.i;
    } else if constexpr (std::is_same_v<T, double>) {
        if (!v.is_int() && !v.is_float()) mismatch("a number", v, path);
        return v.as_float();
    } else if constexpr (std::is_same_v<T, std::string>) {
        if (!v.is_str()) mismatch("a string", v, path);
        return v.s;
    } else if constexpr (std::is_same_v<T, datetime::date>) {
        if (!v.is_date()) mismatch("a date", v, path);
        return v.as_date();
    } else if constexpr (std::is_same_v<T, datetime::time>) {
        if (!v.is_time()) mismatch("a time", v, path);
        return v.as_time();
    } else if constexpr (std::is_same_v<T, datetime::datetime>) {
        if (!v.is_datetime()) mismatch("a datetime", v, path);
        return v.as_datetime();
    } else if constexpr (is_optional<T>::value) {
        if (v.is_null()) return T{};
        return T(decode<typename T::value_type>(v, path));
    } else if constexpr (is_vector<T>::value || is_set<T>::value) {
        if (!v.is_list()) mismatch("an array", v, path);
        T out;
        std::size_t k = 0;
        for (const Value& item : *v.arr_) {
            auto elem = decode<typename T::value_type>(item, path + "[" + std::to_string(k++) + "]");
            if constexpr (is_set<T>::value) {
                out.insert(std::move(elem));
            } else {
                out.push_back(std::move(elem));
            }
        }
        return out;
    } else if constexpr (is_dict<T>::value) {
        if (!v.is_dict()) mismatch(an_object(), v, path);
        T out;
        for (const auto& [key, item] : *v.obj_) {
            using V = std::remove_cvref_t<decltype(out[key])>;
            out[key] = decode<V>(item, path + "." + key);
        }
        return out;
    } else if constexpr (is_tuple<T>::value) {
        constexpr std::size_t n = std::tuple_size_v<T>;
        if (!v.is_list() || v.arr_->size() != n) {
            std::string want = "an array of " + std::to_string(n) + " items";
            mismatch(want.c_str(), v, path);
        }
        return [&]<std::size_t... I>(std::index_sequence<I...>) {
            return T(decode<std::tuple_element_t<I, T>>((*v.arr_)[I], path + "[" + std::to_string(I) + "]")...);
        }(std::make_index_sequence<n>{});
    } else if constexpr (is_shared<T>::value) {
        return T::element_type::sd_from_json(v, path);
    } else {
        return T::sd_from_json(v, path);  // a struct: generated by the compiler
    }
}

template <class T, class B>
T loads(const B& text) {
    return decode<T>(parse(raw(text)), "$");
}

template <class T>
T load(const std::shared_ptr<TextFile>& file) {
    return loads<T>(file->read());
}

// ============================================================================
// Encoding: T -> Value -> text
// ============================================================================

template <class K>
std::string key_string(const K& k) {
    if constexpr (std::is_same_v<K, std::string>) {
        return k;
    } else if constexpr (std::is_same_v<K, bool>) {
        return k ? "true" : "false";
    } else if constexpr (std::is_same_v<K, double>) {
        return float_repr(k);
    } else {
        return std::to_string(k);
    }
}

template <class T>
Value to_value(const T& x) {
    if constexpr (std::is_same_v<T, Value>) {
        return x;
    } else if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, Bool>) {
        return Value::boolean(x);
    } else if constexpr (std::is_same_v<T, std::int64_t>) {
        return Value::integer(x);
    } else if constexpr (std::is_same_v<T, double>) {
        return Value::number(x);
    } else if constexpr (std::is_same_v<T, std::string>) {
        return Value::string(x);
    } else if constexpr (is_optional<T>::value) {
        return x ? to_value(*x) : Value();
    } else if constexpr (is_vector<T>::value || is_set<T>::value) {
        Value out = Value::array();
        for (const auto& item : x) out.push(to_value(item));
        return out;
    } else if constexpr (is_dict<T>::value) {
        Value out = Value::object();
        for (const auto& [k, item] : x) out.set(key_string(k), to_value(item));
        return out;
    } else if constexpr (is_tuple<T>::value) {
        Value out = Value::array();
        std::apply([&](const auto&... e) { (out.push(to_value(e)), ...); }, x);
        return out;
    } else if constexpr (is_shared<T>::value) {
        return x->sd_to_json();
    } else {
        return x.sd_to_json();  // a struct: generated by the compiler
    }
}

struct WriteOptions {
    std::optional<std::int64_t> indent;
    bool sort_keys;
    bool ensure_ascii;
    std::string item_sep, key_sep;
};

inline void write_string(std::string& out, const std::string& s, bool ensure_ascii) {
    static const char* hex = "0123456789abcdef";
    auto u_escape = [&](std::uint32_t cp) {
        out += "\\u";
        for (int shift = 12; shift >= 0; shift -= 4) out += hex[(cp >> shift) & 15];
    };
    out += '"';
    for (std::size_t k = 0; k < s.size(); ++k) {
        unsigned char c = static_cast<unsigned char>(s[k]);
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20 || (c == 0x7f && ensure_ascii)) {  // Python escapes DEL too, when ASCII-only
                    u_escape(c);
                } else if (c >= 0x80 && ensure_ascii) {
                    // Decode one UTF-8 sequence (strings are valid UTF-8) and escape it.
                    std::size_t width = c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : 2;
                    std::uint32_t cp = c & (0x7F >> width);
                    for (std::size_t n = 1; n < width; ++n) cp = (cp << 6) | (static_cast<unsigned char>(s[k + n]) & 0x3F);
                    k += width - 1;
                    if (cp >= 0x10000) {  // outside the BMP: a UTF-16 surrogate pair, like Python
                        cp -= 0x10000;
                        u_escape(0xD800 + (cp >> 10));
                        u_escape(0xDC00 + (cp & 0x3FF));
                    } else {
                        u_escape(cp);
                    }
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    out += '"';
}

inline void write(std::string& out, const Value& v, const WriteOptions& o, std::int64_t depth) {
    auto newline = [&](std::int64_t d) {
        if (o.indent) {
            out += '\n';
            out.append(static_cast<std::size_t>(std::max<std::int64_t>(0, *o.indent) * d), ' ');
        }
    };
    switch (v.kind) {
        case Value::Kind::Null: out += "null"; return;
        case Value::Kind::Bool: out += v.b ? "true" : "false"; return;
        case Value::Kind::Int: out += std::to_string(v.i); return;
        case Value::Kind::Float:
            if (std::isnan(v.f)) out += "NaN";
            else if (std::isinf(v.f)) out += v.f > 0 ? "Infinity" : "-Infinity";
            else out += float_repr(v.f);
            return;
        case Value::Kind::Str: write_string(out, v.s, o.ensure_ascii); return;
        case Value::Kind::List: {
            if (v.arr_->empty()) {
                out += "[]";
                return;
            }
            out += '[';
            bool first = true;
            for (const Value& item : *v.arr_) {
                if (!first) out += o.item_sep;
                first = false;
                newline(depth + 1);
                write(out, item, o, depth + 1);
            }
            newline(depth);
            out += ']';
            return;
        }
        case Value::Kind::Dict: {
            if (v.obj_->empty()) {
                out += "{}";
                return;
            }
            std::vector<std::string> keys = v.obj_->keys();
            if (o.sort_keys) std::sort(keys.begin(), keys.end());
            out += '{';
            bool first = true;
            for (const std::string& key : keys) {
                if (!first) out += o.item_sep;
                first = false;
                newline(depth + 1);
                write_string(out, key, o.ensure_ascii);
                out += o.key_sep;
                write(out, *v.obj_->find(key), o, depth + 1);
            }
            newline(depth);
            out += '}';
            return;
        }
        default:  // a TOML date or time: Python's json can't write them either
            raise("TypeError", std::string("Object of type ") + v.moment_type() + " is not JSON serializable");
    }
}

template <class T>
std::string dumps(const T& x, std::optional<std::int64_t> indent = std::nullopt, bool sort_keys = false,
                  bool ensure_ascii = true, std::optional<std::tuple<std::string, std::string>> separators = std::nullopt) {
    // Python's defaults: ", " between items, or "," when indenting (the newline follows).
    WriteOptions o{indent, sort_keys, ensure_ascii, indent ? "," : ", ", ": "};
    if (separators) std::tie(o.item_sep, o.key_sep) = *separators;
    std::string out;
    write(out, to_value(x), o, 0);
    return out;
}

// `case 3:` / `case "a":` / `case True:` against a Value, with Python's rules: numbers compare
// by value (3 == 3.0, 1 == True), while True/False patterns only match booleans.
inline bool equals_literal(const Value& v, bool x) { return v.kind == Value::Kind::Bool && v.b == x; }
inline bool equals_literal(const Value& v, std::int64_t x) {
    switch (v.kind) {
        case Value::Kind::Int: return v.i == x;
        case Value::Kind::Float: return v.f == static_cast<double>(x);
        case Value::Kind::Bool: return static_cast<std::int64_t>(v.b) == x;
        default: return false;
    }
}
inline bool equals_literal(const Value& v, double x) {
    switch (v.kind) {
        case Value::Kind::Int: return static_cast<double>(v.i) == x;
        case Value::Kind::Float: return v.f == x;
        case Value::Kind::Bool: return static_cast<double>(v.b) == x;
        default: return false;
    }
}
inline bool equals_literal(const Value& v, const std::string& x) { return v.kind == Value::Kind::Str && v.s == x; }

template <class T>
void dump(const T& x, const std::shared_ptr<TextFile>& file, std::optional<std::int64_t> indent = std::nullopt,
          bool sort_keys = false, bool ensure_ascii = true,
          std::optional<std::tuple<std::string, std::string>> separators = std::nullopt) {
    file->write(dumps(x, indent, sort_keys, ensure_ascii, separators));
}

}  // namespace sd::json

namespace sd {

// Indexing, `in` and friends for json.Value (generated code calls sd::index etc.)
inline const json::Value& index(const json::Value& v, const std::string& key) {
    if (!v.is_dict()) v.wrong("an object (it was indexed with a string)");
    const json::Value* found = v.find(key);
    if (!found) raise("KeyError", repr_str(key));
    return *found;
}
inline const json::Value& index(const json::Value& v, std::int64_t i) {
    if (!v.is_list()) v.wrong("an array (it was indexed with an int)");
    return (*v.arr_)[norm_index(i, v.arr_->size(), "list")];
}
inline bool contains(const json::Value& v, const std::string& key) {
    if (!v.is_dict()) v.wrong("an object");
    return v.find(key) != nullptr;
}

}  // namespace sd
