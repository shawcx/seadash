// The `tomllib` module: TOML 1.0 documents.
//
//   loads<T>(text)  parse into a json::Value tree (tables, arrays, strings, numbers,
//                   booleans, dates and times), then decode it into T like json.loads
//   load<T>(file)   the same from a file opened "rb"
//
// The parser is a port of CPython's (Lib/tomllib/_parser.py), so documents are accepted
// and rejected alike, with the same messages and positions: "Invalid value (at line 2,
// column 5)". Positions count characters, as Python's do, not bytes.
#pragma once

#include <map>

#include "json.hpp"

namespace sd::tomllib {

struct TOMLDecodeError : ValueError {
    std::string msg, doc;
    std::int64_t pos = 0, lineno = 0, colno = 0;
    TOMLDecodeError(std::string full, std::string m, std::string d, std::int64_t p, std::int64_t line, std::int64_t col)
        : msg(std::move(m)), doc(std::move(d)), pos(p), lineno(line), colno(col) {
        message = std::move(full);  // (in the body: BaseException is a virtual base)
    }
    std::string sd_type() const override { return "tomllib.TOMLDecodeError"; }
};

namespace detail {

using json::Value;
using Key = std::vector<std::string>;

// Thrown by the nesting helpers where Python raises KeyError; the caller reports it.
struct NoNest {};

inline bool is_ws(char c) { return c == ' ' || c == '\t'; }
inline bool is_digit(char c) { return c >= '0' && c <= '9'; }
inline bool is_hex(char c) { return is_digit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); }
inline bool is_bare_key(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || is_digit(c) || c == '-' || c == '_';
}
// ASCII control characters, minus the ones a context allows (tab, and newline in multiline strings).
inline bool is_illegal(char c, bool newline_ok) {
    auto u = static_cast<unsigned char>(c);
    return (u < 0x20 || u == 0x7f) && c != '\t' && !(newline_ok && c == '\n');
}

inline std::string key_repr(const Key& key) {  // as a Python tuple: ('a', 'b') or ('a',)
    std::string out = "(";
    for (std::size_t i = 0; i < key.size(); ++i) out += (i ? ", " : "") + repr_str(key[i]);
    return out + (key.size() == 1 ? ",)" : ")");
}
inline Key prefix(const Key& key, std::size_t n) { return Key(key.begin(), key.begin() + static_cast<std::ptrdiff_t>(n)); }
inline Key concat(const Key& a, const Key& b) {
    Key out = a;
    out.insert(out.end(), b.begin(), b.end());
    return out;
}

// Flags that map to parsed keys and namespaces.
class Flags {
    struct Node {
        int flags = 0, recursive = 0;
        std::map<std::string, std::unique_ptr<Node>> nested;
    };
    Node root_;
    std::vector<std::pair<Key, int>> pending_;

    static Node& child(Node& n, const std::string& k) {
        auto& slot = n.nested[k];
        if (!slot) slot = std::make_unique<Node>();
        return *slot;
    }

public:
    static constexpr int FROZEN = 1;         // an inline array or table: immutable
    static constexpr int EXPLICIT_NEST = 2;  // created explicitly: can't be opened with [table] again

    void add_pending(Key key, int flag) { pending_.emplace_back(std::move(key), flag); }
    void finalize_pending() {
        for (const auto& [key, flag] : pending_) set(key, flag, false);
        pending_.clear();
    }
    void unset_all(const Key& key) {
        Node* cont = &root_;
        for (std::size_t i = 0; i + 1 < key.size(); ++i) {
            auto it = cont->nested.find(key[i]);
            if (it == cont->nested.end()) return;
            cont = it->second.get();
        }
        cont->nested.erase(key.back());
    }
    void set(const Key& key, int flag, bool recursive) {
        Node* cont = &root_;
        for (std::size_t i = 0; i + 1 < key.size(); ++i) cont = &child(*cont, key[i]);
        Node& stem = child(*cont, key.back());
        (recursive ? stem.recursive : stem.flags) |= flag;
    }
    bool is(const Key& key, int flag) const {
        if (key.empty()) return false;  // the document root has no flags
        const Node* cont = &root_;
        for (std::size_t i = 0; i + 1 < key.size(); ++i) {
            auto it = cont->nested.find(key[i]);
            if (it == cont->nested.end()) return false;
            if (it->second->recursive & flag) return true;
            cont = it->second.get();
        }
        auto it = cont->nested.find(key.back());
        return it != cont->nested.end() && ((it->second->flags | it->second->recursive) & flag);
    }
};

// The table at `key` (its first n parts) under `cont`, creating tables on the way; the
// last table of an array of tables stands for the array.
inline Value get_or_create_nest(Value cont, const Key& key, std::size_t n, bool access_lists = true) {
    for (std::size_t i = 0; i < n; ++i) {
        const Value* found = cont.find(key[i]);
        if (!found) {
            cont.set(key[i], Value::object());
            found = cont.find(key[i]);
        }
        Value next = *found;
        if (access_lists && next.is_list()) {
            if (next.arr_->empty()) throw NoNest{};
            next = next.arr_->back();
        }
        if (!next.is_dict()) throw NoNest{};
        cont = next;
    }
    return cont;
}

inline void append_nest_to_list(const Value& root, const Key& key) {
    Value cont = get_or_create_nest(root, key, key.size() - 1);
    if (const Value* found = cont.find(key.back())) {
        if (!found->is_list()) throw NoNest{};
        Value list = *found;
        list.push(Value::object());
    } else {
        Value list = Value::array();
        list.push(Value::object());
        cont.set(key.back(), list);
    }
}

class Parser {
    std::string src;
    Value root = Value::object();
    Flags flags;

public:
    explicit Parser(const std::string& s) {
        // The spec allows reading "\r\n" as "\n", even in strings, as Python does.
        src.reserve(s.size());
        for (std::size_t i = 0; i < s.size(); ++i) {
            if (s[i] == '\r' && i + 1 < s.size() && s[i + 1] == '\n') continue;
            src += s[i];
        }
    }

    Value parse() {
        std::size_t pos = 0;
        Key header;
        while (true) {
            pos = skip_ws(pos);
            if (pos >= src.size()) break;
            char c = src[pos];
            if (c == '\n') {
                ++pos;
                continue;
            }
            if (is_bare_key(c) || c == '"' || c == '\'') {
                pos = key_value_rule(pos, header);
                pos = skip_ws(pos);
            } else if (c == '[') {
                flags.finalize_pending();
                if (at(pos + 1) == '[') {
                    pos = create_list_rule(pos, header);
                } else {
                    pos = create_dict_rule(pos, header);
                }
                pos = skip_ws(pos);
            } else if (c != '#') {
                fail("Invalid statement", pos);
            }
            pos = skip_comment(pos);
            if (pos >= src.size()) break;
            if (src[pos] != '\n') fail("Expected newline or end of document after a statement", pos);
            ++pos;
        }
        return root;
    }

private:
    char at(std::size_t pos) const { return pos < src.size() ? src[pos] : '\0'; }
    bool starts(std::size_t pos, std::string_view s) const { return pos <= src.size() && std::string_view(src).substr(pos).starts_with(s); }

    // Characters (code points) in src[from, to): Python counts positions in characters.
    std::int64_t chars(std::size_t from, std::size_t to) const {
        std::int64_t n = 0;
        std::size_t end = std::min(to, src.size());
        for (std::size_t i = from; i < end; ++i) n += (static_cast<unsigned char>(src[i]) & 0xC0) != 0x80;
        return n + static_cast<std::int64_t>(to > end ? to - end : 0);
    }

    [[noreturn]] void fail(const std::string& msg, std::size_t pos) const {
        std::size_t end = std::min(pos, src.size());
        auto lineno = 1 + static_cast<std::int64_t>(std::count(src.begin(), src.begin() + static_cast<std::ptrdiff_t>(end), '\n'));
        std::int64_t colno;
        if (lineno == 1) {
            colno = chars(0, pos) + 1;
        } else {
            colno = chars(src.rfind('\n', end - 1), pos);
        }
        std::string where = pos >= src.size() ? "end of document"
                                              : "line " + std::to_string(lineno) + ", column " + std::to_string(colno);
        throw Thrown{std::make_shared<TOMLDecodeError>(msg + " (at " + where + ")", msg, src, chars(0, pos), lineno, colno)};
    }

    std::size_t skip_ws(std::size_t pos) const {
        while (pos < src.size() && is_ws(src[pos])) ++pos;
        return pos;
    }
    std::size_t skip_ws_and_newlines(std::size_t pos) const {
        while (pos < src.size() && (is_ws(src[pos]) || src[pos] == '\n')) ++pos;
        return pos;
    }

    // The position of `expect` at or after pos, checking the characters before it.
    std::size_t skip_until(std::size_t pos, std::string_view expect, bool newline_ok, bool error_on_eof) const {
        std::size_t found = src.find(expect, pos);
        if (found == std::string::npos) {
            found = src.size();
            if (error_on_eof) fail("Expected " + repr_str(std::string(expect)), found);
        }
        for (std::size_t i = pos; i < found; ++i)
            if (is_illegal(src[i], newline_ok)) fail("Found invalid character " + repr_str(std::string(1, src[i])), i);
        return found;
    }

    std::size_t skip_comment(std::size_t pos) const {
        if (at(pos) == '#') return skip_until(pos + 1, "\n", false, false);
        return pos;
    }

    std::size_t skip_comments_and_array_ws(std::size_t pos) const {
        while (true) {
            std::size_t before = pos;
            pos = skip_comment(skip_ws_and_newlines(pos));
            if (pos == before) return pos;
        }
    }

    std::size_t create_dict_rule(std::size_t pos, Key& header) {
        pos = skip_ws(pos + 1);
        Key key;
        pos = parse_key(pos, key);
        if (flags.is(key, Flags::EXPLICIT_NEST) || flags.is(key, Flags::FROZEN))
            fail("Cannot declare " + key_repr(key) + " twice", pos);
        flags.set(key, Flags::EXPLICIT_NEST, false);
        try {
            get_or_create_nest(root, key, key.size());
        } catch (NoNest&) {
            fail("Cannot overwrite a value", pos);
        }
        if (!starts(pos, "]")) fail("Expected ']' at the end of a table declaration", pos);
        header = std::move(key);
        return pos + 1;
    }

    std::size_t create_list_rule(std::size_t pos, Key& header) {
        pos = skip_ws(pos + 2);
        Key key;
        pos = parse_key(pos, key);
        if (flags.is(key, Flags::FROZEN)) fail("Cannot mutate immutable namespace " + key_repr(key), pos);
        // Free the namespace now that it points to another empty list item...
        flags.unset_all(key);
        // ...but this key precisely is still prohibited from table declaration
        flags.set(key, Flags::EXPLICIT_NEST, false);
        try {
            append_nest_to_list(root, key);
        } catch (NoNest&) {
            fail("Cannot overwrite a value", pos);
        }
        if (!starts(pos, "]]")) fail("Expected ']]' at the end of an array declaration", pos);
        header = std::move(key);
        return pos + 2;
    }

    std::size_t key_value_rule(std::size_t pos, const Key& header) {
        Key key;
        Value value;
        pos = parse_key_value_pair(pos, key, value);
        Key abs_parent = concat(header, prefix(key, key.size() - 1));
        for (std::size_t i = 1; i < key.size(); ++i) {
            Key cont_key = concat(header, prefix(key, i));
            // Dotted keys can't redefine an existing table...
            if (flags.is(cont_key, Flags::EXPLICIT_NEST)) fail("Cannot redefine namespace " + key_repr(cont_key), pos);
            // ...and the tables they make can't be opened again by later [table] sections.
            flags.add_pending(std::move(cont_key), Flags::EXPLICIT_NEST);
        }
        if (flags.is(abs_parent, Flags::FROZEN)) fail("Cannot mutate immutable namespace " + key_repr(abs_parent), pos);
        Value nest;
        try {
            nest = get_or_create_nest(root, abs_parent, abs_parent.size());
        } catch (NoNest&) {
            fail("Cannot overwrite a value", pos);
        }
        if (nest.find(key.back())) fail("Cannot overwrite a value", pos);
        if (value.is_dict() || value.is_list()) flags.set(concat(header, key), Flags::FROZEN, true);
        nest.set(key.back(), std::move(value));
        return pos;
    }

    std::size_t parse_key_value_pair(std::size_t pos, Key& key, Value& value) {
        pos = parse_key(pos, key);
        if (at(pos) != '=' || pos >= src.size()) fail("Expected '=' after a key in a key/value pair", pos);
        pos = skip_ws(pos + 1);
        return parse_value(pos, value);
    }

    std::size_t parse_key(std::size_t pos, Key& key) {
        std::string part;
        pos = parse_key_part(pos, part);
        key.push_back(std::move(part));
        pos = skip_ws(pos);
        while (at(pos) == '.' && pos < src.size()) {
            pos = skip_ws(pos + 1);
            pos = parse_key_part(pos, part);
            key.push_back(std::move(part));
            pos = skip_ws(pos);
        }
        return pos;
    }

    std::size_t parse_key_part(std::size_t pos, std::string& out) {
        char c = at(pos);
        if (pos < src.size() && is_bare_key(c)) {
            std::size_t start = pos;
            while (pos < src.size() && is_bare_key(src[pos])) ++pos;
            out = src.substr(start, pos - start);
            return pos;
        }
        if (c == '\'' && pos < src.size()) return parse_literal_str(pos, out);
        if (c == '"' && pos < src.size()) return parse_basic_str(pos + 1, false, out);
        fail("Invalid initial character for a key part", pos);
    }

    std::size_t parse_array(std::size_t pos, Value& out) {
        out = Value::array();
        pos = skip_comments_and_array_ws(pos + 1);
        if (starts(pos, "]")) return pos + 1;
        while (true) {
            Value item;
            pos = parse_value(pos, item);
            out.push(std::move(item));
            pos = skip_comments_and_array_ws(pos);
            char c = at(pos);
            if (c == ']' && pos < src.size()) return pos + 1;
            if (c != ',' || pos >= src.size()) fail("Unclosed array", pos);
            pos = skip_comments_and_array_ws(pos + 1);
            if (starts(pos, "]")) return pos + 1;
        }
    }

    std::size_t parse_inline_table(std::size_t pos, Value& out) {
        out = Value::object();
        Flags local;
        pos = skip_ws(pos + 1);
        if (starts(pos, "}")) return pos + 1;
        while (true) {
            Key key;
            Value value;
            pos = parse_key_value_pair(pos, key, value);
            if (local.is(key, Flags::FROZEN)) fail("Cannot mutate immutable namespace " + key_repr(key), pos);
            Value nest;
            try {
                nest = get_or_create_nest(out, key, key.size() - 1, false);
            } catch (NoNest&) {
                fail("Cannot overwrite a value", pos);
            }
            if (nest.find(key.back())) fail("Duplicate inline table key " + repr_str(key.back()), pos);
            bool nested = value.is_dict() || value.is_list();
            nest.set(key.back(), std::move(value));
            pos = skip_ws(pos);
            char c = at(pos);
            if (c == '}' && pos < src.size()) return pos + 1;
            if (c != ',' || pos >= src.size()) fail("Unclosed inline table", pos);
            if (nested) local.set(key, Flags::FROZEN, true);
            pos = skip_ws(pos + 1);
        }
    }

    std::size_t parse_hex_char(std::size_t pos, std::size_t len, std::string& out) {
        if (pos + len > src.size()) fail("Invalid hex value", pos);
        std::uint32_t cp = 0;
        for (std::size_t i = 0; i < len; ++i) {
            char c = src[pos + i];
            if (!is_hex(c)) fail("Invalid hex value", pos);
            cp = cp * 16 + static_cast<std::uint32_t>(is_digit(c) ? c - '0' : (c | 0x20) - 'a' + 10);
        }
        pos += len;
        if (!(cp <= 0xD7FF || (cp >= 0xE000 && cp <= 0x10FFFF))) fail("Escaped character is not a Unicode scalar value", pos);
        out += chr(static_cast<std::int64_t>(cp));
        return pos;
    }

    std::size_t parse_escape(std::size_t pos, bool multiline, std::string& out) {
        char e = at(pos + 1);
        bool have = pos + 1 < src.size();
        pos += 2;
        if (multiline && have && (e == ' ' || e == '\t' || e == '\n')) {
            // A line-ending backslash: skip whitespace up to the next non-whitespace character.
            if (e != '\n') {
                pos = skip_ws(pos);
                if (pos >= src.size()) return pos;
                if (src[pos] != '\n') fail("Unescaped '\\' in a string", pos);
                ++pos;
            }
            return skip_ws_and_newlines(pos);
        }
        if (have) {
            switch (e) {
                case 'u': return parse_hex_char(pos, 4, out);
                case 'U': return parse_hex_char(pos, 8, out);
                case 'b': out += '\b'; return pos;
                case 't': out += '\t'; return pos;
                case 'n': out += '\n'; return pos;
                case 'f': out += '\f'; return pos;
                case 'r': out += '\r'; return pos;
                case '"': out += '"'; return pos;
                case '\\': out += '\\'; return pos;
                default: break;
            }
        }
        fail("Unescaped '\\' in a string", pos);
    }

    std::size_t parse_literal_str(std::size_t pos, std::string& out) {
        std::size_t start = pos + 1;
        std::size_t end = skip_until(start, "'", false, true);
        out = src.substr(start, end - start);
        return end + 1;
    }

    std::size_t parse_multiline_str(std::size_t pos, bool literal, std::string& out) {
        pos += 3;
        if (starts(pos, "\n")) ++pos;
        char delim = literal ? '\'' : '"';
        if (literal) {
            std::size_t end = skip_until(pos, "'''", true, true);
            out = src.substr(pos, end - pos);
            pos = end + 3;
        } else {
            pos = parse_basic_str(pos, true, out);
        }
        // Up to two more quotes when the closing run is 4 or 5 long.
        for (int extra = 0; extra < 2 && at(pos) == delim && pos < src.size(); ++extra, ++pos) out += delim;
        return pos;
    }

    std::size_t parse_basic_str(std::size_t pos, bool multiline, std::string& out) {
        out.clear();
        std::size_t start = pos;
        while (true) {
            if (pos >= src.size()) fail("Unterminated string", pos);
            char c = src[pos];
            if (c == '"') {
                if (!multiline) {
                    out.append(src, start, pos - start);
                    return pos + 1;
                }
                if (starts(pos, "\"\"\"")) {
                    out.append(src, start, pos - start);
                    return pos + 3;
                }
                ++pos;
                continue;
            }
            if (c == '\\') {
                out.append(src, start, pos - start);
                pos = parse_escape(pos, multiline, out);
                start = pos;
                continue;
            }
            if (is_illegal(c, multiline)) fail("Illegal character " + repr_str(std::string(1, c)), pos);
            ++pos;
        }
    }

    // ---- dates, times and numbers: hand-written versions of Python's regular expressions

    bool digits(std::size_t pos, std::size_t n) const {
        if (pos + n > src.size()) return false;
        for (std::size_t i = 0; i < n; ++i)
            if (!is_digit(src[pos + i])) return false;
        return true;
    }
    std::int64_t int_at(std::size_t pos, std::size_t n) const {
        std::int64_t v = 0;
        for (std::size_t i = 0; i < n; ++i) v = v * 10 + (src[pos + i] - '0');
        return v;
    }
    // [01][0-9]|2[0-3]
    bool hour_at(std::size_t pos) const {
        return digits(pos, 2) && (src[pos] <= '1' || (src[pos] == '2' && src[pos + 1] <= '3'));
    }
    // HH:MM:SS(.fraction)?, with the fraction's first 6 digits kept; returns the end, or 0.
    std::size_t time_at(std::size_t pos, std::int64_t (&parts)[4]) const {
        if (!(hour_at(pos) && at(pos + 2) == ':' && digits(pos + 3, 2) && src[pos + 3] <= '5' && at(pos + 5) == ':' &&
              digits(pos + 6, 2) && src[pos + 6] <= '5'))
            return 0;
        parts[0] = int_at(pos, 2), parts[1] = int_at(pos + 3, 2), parts[2] = int_at(pos + 6, 2), parts[3] = 0;
        std::size_t end = pos + 8;
        if (at(end) == '.' && digits(end + 1, 1)) {
            ++end;
            std::int64_t scale = 100000;
            for (; end < src.size() && is_digit(src[end]); ++end) {
                parts[3] += (src[end] - '0') * scale;
                scale /= 10;
            }
        }
        return end;
    }

    // A date or datetime at pos (Python's RE_DATETIME); returns its end, or 0.
    std::size_t datetime_at(std::size_t pos, Value& out) const {
        if (!(digits(pos, 4) && at(pos + 4) == '-' && digits(pos + 5, 2) && at(pos + 7) == '-' && digits(pos + 8, 2)))
            return 0;
        std::int64_t year = int_at(pos, 4), month = int_at(pos + 5, 2), day = int_at(pos + 8, 2);
        if (month < 1 || month > 12 || day < 1 || day > 31) return 0;
        std::size_t end = pos + 10;
        std::int64_t t[4];
        std::size_t time_end = 0;
        char sep = at(end);
        if ((sep == 'T' || sep == 't' || sep == ' ') && end < src.size()) time_end = time_at(end + 1, t);
        if (year < datetime::MINYEAR || day > datetime::days_in_month(year, month)) fail("Invalid date or datetime", pos);
        if (!time_end) {
            out = Value::moment(datetime::date(year, month, day));
            return end;
        }
        end = time_end;
        std::optional<datetime::timezone> tz;
        char z = at(end);
        if ((z == 'Z' || z == 'z') && end < src.size()) {
            tz = datetime::timezone::utc();
            ++end;
        } else if ((z == '+' || z == '-') && end < src.size() && hour_at(end + 1) && at(end + 3) == ':' &&
                   digits(end + 4, 2) && src[end + 4] <= '5') {
            std::int64_t sign = z == '+' ? 1 : -1, hours = int_at(end + 1, 2), minutes = int_at(end + 4, 2);
            if (hours == 0 && minutes == 0) {
                tz = datetime::timezone::utc();  // Python's timezone(timedelta(0)) is timezone.utc
            } else {
                tz = datetime::timezone(datetime::timedelta(0, 0, 0, 0, static_cast<double>(sign * minutes),
                                                            static_cast<double>(sign * hours)));
            }
            end += 6;
        }
        out = Value::moment(datetime::datetime(year, month, day, t[0], t[1], t[2], t[3], tz));
        return end;
    }

    // [0-9](_?[0-9])* in some base, from pos; returns the end.
    std::size_t digit_run(std::size_t pos, bool (*ok)(char)) const {
        while (pos < src.size()) {
            if (ok(src[pos])) {
                ++pos;
            } else if (src[pos] == '_' && pos + 1 < src.size() && ok(src[pos + 1])) {
                pos += 2;
            } else {
                break;
            }
        }
        return pos;
    }

    static std::string without_underscores(std::string_view s) {
        std::string out;
        for (char c : s)
            if (c != '_') out += c;
        return out;
    }

    // An integer or float at pos (Python's RE_NUMBER); returns its end, or 0.
    std::size_t number_at(std::size_t pos, Value& out) const {
        static constexpr auto bin = [](char c) { return c == '0' || c == '1'; };
        static constexpr auto oct = [](char c) { return c >= '0' && c <= '7'; };
        char base_char = at(pos + 1);
        if (at(pos) == '0' && pos + 2 < src.size()) {
            bool (*ok)(char) = base_char == 'x' ? is_hex : base_char == 'b' ? +bin : base_char == 'o' ? +oct : nullptr;
            if (ok && ok(src[pos + 2])) {
                std::size_t end = digit_run(pos + 2, ok);
                std::string text = without_underscores(std::string_view(src).substr(pos + 2, end - pos - 2));
                std::uint64_t v = 0;
                auto [ptr, ec] = std::from_chars(text.data(), text.data() + text.size(), v, base_char == 'x' ? 16 : base_char == 'b' ? 2 : 8);
                if (ec != std::errc{} || v > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
                    fail("Integer out of range", pos);
                out = Value::integer(static_cast<std::int64_t>(v));
                return end;
            }
        }
        std::size_t end = pos;
        if (at(end) == '+' || at(end) == '-') ++end;
        if (at(end) == '0' && end < src.size()) {
            ++end;
        } else if (end < src.size() && src[end] >= '1' && src[end] <= '9') {
            end = digit_run(end + 1, is_digit);
        } else {
            return 0;
        }
        std::size_t int_end = end;
        if (at(end) == '.' && digits(end + 1, 1)) end = digit_run(end + 2, is_digit);
        if ((at(end) == 'e' || at(end) == 'E') && end < src.size()) {
            std::size_t exp = end + 1;
            if (at(exp) == '+' || at(exp) == '-') ++exp;
            if (digits(exp, 1)) end = digit_run(exp + 1, is_digit);
        }
        std::string text = without_underscores(std::string_view(src).substr(pos, end - pos));
        if (end != int_end) {
            out = Value::number(std::strtod(text.c_str(), nullptr));  // strtod: 1e400 is inf, as in Python
            return end;
        }
        std::int64_t v = 0;
        const char* first = text.data() + (text[0] == '+');
        auto [ptr, ec] = std::from_chars(first, text.data() + text.size(), v);
        if (ec != std::errc{}) fail("Integer out of range", pos);
        out = Value::integer(v);
        return end;
    }

    std::size_t parse_value(std::size_t pos, Value& out) {
        char c = at(pos);
        bool more = pos < src.size();
        if (more && c == '"') {
            std::string s;
            pos = starts(pos, "\"\"\"") ? parse_multiline_str(pos, false, s) : parse_basic_str(pos + 1, false, s);
            out = Value::string(std::move(s));
            return pos;
        }
        if (more && c == '\'') {
            std::string s;
            pos = starts(pos, "'''") ? parse_multiline_str(pos, true, s) : parse_literal_str(pos, s);
            out = Value::string(std::move(s));
            return pos;
        }
        if (starts(pos, "true")) {
            out = Value::boolean(true);
            return pos + 4;
        }
        if (starts(pos, "false")) {
            out = Value::boolean(false);
            return pos + 5;
        }
        if (more && c == '[') return parse_array(pos, out);
        if (more && c == '{') return parse_inline_table(pos, out);
        if (std::size_t end = datetime_at(pos, out)) return end;
        std::int64_t t[4];
        if (std::size_t end = time_at(pos, t)) {
            out = Value::moment(datetime::time(t[0], t[1], t[2], t[3]));
            return end;
        }
        if (std::size_t end = number_at(pos, out)) return end;
        for (std::string_view special : {"inf", "nan", "+inf", "+nan", "-inf", "-nan"}) {
            if (starts(pos, special)) {
                double v = special.ends_with("inf") ? std::numeric_limits<double>::infinity()
                                                    : std::numeric_limits<double>::quiet_NaN();
                out = Value::number(special[0] == '-' ? -v : v);
                return pos + special.size();
            }
        }
        fail("Invalid value", pos);
    }
};

}  // namespace detail

inline json::Value parse(const std::string& text) { return detail::Parser(text).parse(); }

template <class T>
T loads(const std::string& text) {
    json::Value doc = parse(text);
    struct Toml {  // decoding errors say "tomllib: ..." and "table"
        bool was = json::decoding_toml;
        Toml() { json::decoding_toml = true; }
        ~Toml() { json::decoding_toml = was; }
    } toml;
    return json::decode<T>(doc, "$");
}

template <class T, class File>
T load(const File& file) {
    return loads<T>(bytes_decode(file->read()));
}

}  // namespace sd::tomllib
