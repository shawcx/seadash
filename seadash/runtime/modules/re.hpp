// The `re` module on PCRE2, with Python's semantics: Unicode-aware classes for str
// patterns, Python's replacement syntax (\1, \g<name>), and Python's rules for
// empty matches in findall/sub/split. Offsets are byte offsets into the UTF-8
// string, like every other seadash string index.
//
// A Pattern is immutable and shares its compiled code, so copies are cheap and one
// pattern can be used from several threads at once.
#pragma once

#define PCRE2_CODE_UNIT_WIDTH 8
#include <pcre2.h>

#include <mutex>
#include <unordered_map>

namespace sd::re {

struct error : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "re.error"; }
};

inline constexpr std::int64_t IGNORECASE = 2, MULTILINE = 8, DOTALL = 16, UNICODE = 32, VERBOSE = 64,
                              ASCII = 256;

// Python syntax that PCRE2 spells differently: \Z (end of string) is PCRE2's \z,
// and {,n} means {0,n}.
inline std::string translate(const std::string& p) {
    std::string out;
    bool in_class = false;
    for (std::size_t i = 0; i < p.size(); ++i) {
        char c = p[i];
        if (c == '\\' && i + 1 < p.size()) {
            out += (p[i + 1] == 'Z' && !in_class) ? std::string("\\z") : p.substr(i, 2);
            ++i;
        } else if (in_class) {
            if (c == ']') in_class = false;
            out += c;
        } else if (c == '[') {
            in_class = true;
            out += c;
            if (i + 1 < p.size() && p[i + 1] == '^') out += p[++i];
            if (i + 1 < p.size() && p[i + 1] == ']') out += p[++i];  // `[]a]`: a literal ]
        } else if (c == '{' && i + 1 < p.size() && p[i + 1] == ',') {
            std::size_t j = i + 2;
            while (j < p.size() && std::isdigit(static_cast<unsigned char>(p[j]))) ++j;
            out += (j > i + 2 && j < p.size() && p[j] == '}') ? "{0" : "{";
        } else {
            out += c;
        }
    }
    return out;
}

inline std::string flags_repr(std::int64_t flags) {
    static const std::pair<std::int64_t, const char*> names[] = {
        {IGNORECASE, "IGNORECASE"}, {MULTILINE, "MULTILINE"}, {DOTALL, "DOTALL"}, {VERBOSE, "VERBOSE"}, {ASCII, "ASCII"},
    };
    std::string out;
    for (auto [bit, name] : names) {
        if (flags & bit) out += std::string(out.empty() ? "" : "|") + "re." + name;
    }
    return out;
}

using Spans = std::vector<std::pair<std::int64_t, std::int64_t>>;  // (-1, -1): group didn't match

class Match;

class Pattern {
    struct Code {
        pcre2_code* code = nullptr;
        std::string pattern;
        std::int64_t flags = 0;
        std::int64_t groups = 0;
        std::vector<std::pair<std::string, std::int64_t>> names;
        ~Code() {
            if (code) pcre2_code_free(code);
        }
    };
    std::shared_ptr<const Code> c_;

    const Code& code() const {
        if (!c_) raise("ValueError", "pattern was never compiled");
        return *c_;
    }
    template <class F>
    void each_match(const std::string& s, std::int64_t limit, F on_match) const;
    std::string expand_template(const std::string& repl, const std::string& s, const Spans& spans) const;

public:
    Pattern() = default;
    Pattern(const std::string& pattern, std::int64_t flags = 0) {
        auto c = std::make_shared<Code>();
        c->pattern = pattern;
        c->flags = flags;
        std::uint32_t opts = PCRE2_UTF | PCRE2_MATCH_INVALID_UTF;
        if (!(flags & ASCII)) opts |= PCRE2_UCP;  // \d, \w, \s understand Unicode, as in Python 3
        if (flags & IGNORECASE) opts |= PCRE2_CASELESS;
        if (flags & MULTILINE) opts |= PCRE2_MULTILINE;
        if (flags & DOTALL) opts |= PCRE2_DOTALL;
        if (flags & VERBOSE) opts |= PCRE2_EXTENDED;
        std::string translated = translate(pattern);
        int err = 0;
        PCRE2_SIZE offset = 0;
        c->code = pcre2_compile(reinterpret_cast<PCRE2_SPTR>(translated.data()), translated.size(), opts, &err,
                                &offset, nullptr);
        if (!c->code) {
            PCRE2_UCHAR buf[256];
            pcre2_get_error_message(err, buf, sizeof buf);
            throw Thrown{std::make_shared<error>(reinterpret_cast<char*>(buf) + std::string(" at position ") +
                                                 std::to_string(offset))};
        }
        pcre2_jit_compile(c->code, PCRE2_JIT_COMPLETE);  // falls back to the interpreter if unavailable
        std::uint32_t count = 0, name_count = 0, entry_size = 0;
        PCRE2_SPTR table = nullptr;
        pcre2_pattern_info(c->code, PCRE2_INFO_CAPTURECOUNT, &count);
        pcre2_pattern_info(c->code, PCRE2_INFO_NAMECOUNT, &name_count);
        pcre2_pattern_info(c->code, PCRE2_INFO_NAMEENTRYSIZE, &entry_size);
        pcre2_pattern_info(c->code, PCRE2_INFO_NAMETABLE, &table);
        c->groups = count;
        for (std::uint32_t i = 0; i < name_count; ++i) {
            PCRE2_SPTR entry = table + i * entry_size;
            c->names.emplace_back(reinterpret_cast<const char*>(entry + 2), (entry[0] << 8) | entry[1]);
        }
        c_ = std::move(c);
    }

    // One match attempt at `start`, seeing only s[:end]; nullopt if none.
    std::optional<Spans> exec(const std::string& s, std::size_t start, std::size_t end, std::uint32_t opts) const {
        const Code& c = code();
        thread_local std::unique_ptr<pcre2_match_data, decltype(&pcre2_match_data_free)> md{nullptr, pcre2_match_data_free};
        thread_local std::uint32_t capacity = 0;
        auto pairs = static_cast<std::uint32_t>(c.groups + 1);
        if (capacity < pairs) {
            md.reset(pcre2_match_data_create(pairs, nullptr));
            capacity = pairs;
        }
        int rc = pcre2_match(c.code, reinterpret_cast<PCRE2_SPTR>(s.data()), end, start, opts, md.get(), nullptr);
        if (rc == PCRE2_ERROR_NOMATCH) return std::nullopt;
        if (rc < 0) {
            PCRE2_UCHAR buf[256];
            pcre2_get_error_message(rc, buf, sizeof buf);
            throw Thrown{std::make_shared<error>(reinterpret_cast<char*>(buf))};
        }
        PCRE2_SIZE* ov = pcre2_get_ovector_pointer(md.get());
        Spans spans(pairs);
        for (std::uint32_t i = 0; i < pairs; ++i) {
            if (i < static_cast<std::uint32_t>(rc) && ov[2 * i] != PCRE2_UNSET) {
                spans[i] = {static_cast<std::int64_t>(ov[2 * i]), static_cast<std::int64_t>(ov[2 * i + 1])};
            } else {
                spans[i] = {-1, -1};
            }
        }
        return spans;
    }

    std::optional<Match> search(const std::string& s, std::int64_t pos = 0, std::optional<std::int64_t> endpos = std::nullopt) const;
    std::optional<Match> match(const std::string& s, std::int64_t pos = 0, std::optional<std::int64_t> endpos = std::nullopt) const;
    std::optional<Match> fullmatch(const std::string& s, std::int64_t pos = 0, std::optional<std::int64_t> endpos = std::nullopt) const;
    list<std::string> findall(const std::string& s) const;
    template <std::size_t N>
    list<decltype(std::tuple_cat(std::array<std::string, N>{}))> findall_tuples(const std::string& s) const;
    list<Match> finditer(const std::string& s) const;
    std::tuple<std::string, std::int64_t> subn(const std::string& repl, const std::string& s, std::int64_t count = 0) const;
    template <class F>
    std::tuple<std::string, std::int64_t> subn_fn(F repl, const std::string& s, std::int64_t count = 0) const;
    std::string sub(const std::string& repl, const std::string& s, std::int64_t count = 0) const {
        return std::get<0>(subn(repl, s, count));
    }
    template <class F>
    std::string sub_fn(F repl, const std::string& s, std::int64_t count = 0) const {
        return std::get<0>(subn_fn(repl, s, count));
    }
    list<std::optional<std::string>> split_opt(const std::string& s, std::int64_t maxsplit = 0) const;
    list<std::string> split(const std::string& s, std::int64_t maxsplit = 0) const {
        list<std::string> out;
        for (auto& piece : split_opt(s, maxsplit)) out.push_back(piece ? std::move(*piece) : std::string());
        return out;
    }

    std::string pattern() const { return code().pattern; }
    std::int64_t flags() const { return code().flags | ((code().flags & ASCII) ? 0 : UNICODE); }
    std::int64_t groups() const { return code().groups; }
    dict<std::string, std::int64_t> groupindex() const {
        dict<std::string, std::int64_t> out;
        for (const auto& [name, i] : code().names) out[name] = i;
        return out;
    }
    std::optional<std::int64_t> group_number(const std::string& name) const {
        for (const auto& [n, i] : code().names)
            if (n == name) return i;
        return std::nullopt;
    }
    std::string sd_repr() const {
        std::string f = flags_repr(code().flags);
        return "re.compile(" + repr(code().pattern) + (f.empty() ? "" : ", " + f) + ")";
    }
    bool operator==(const Pattern& o) const { return code().pattern == o.code().pattern && code().flags == o.code().flags; }
    friend class Match;
};

class Match {
    Pattern p_;
    std::shared_ptr<const std::string> s_;
    Spans spans_;
    std::int64_t pos_ = 0, endpos_ = 0;

    std::size_t checked(std::int64_t i) const {
        if (i < 0 || i >= static_cast<std::int64_t>(spans_.size())) raise("IndexError", "no such group");
        return static_cast<std::size_t>(i);
    }
    std::int64_t number(const std::string& name) const {
        auto i = p_.group_number(name);
        if (!i) raise("IndexError", "no such group");
        return *i;
    }

public:
    Match() = default;
    Match(Pattern p, std::shared_ptr<const std::string> s, Spans spans, std::int64_t pos, std::int64_t endpos)
        : p_(std::move(p)), s_(std::move(s)), spans_(std::move(spans)), pos_(pos), endpos_(endpos) {}

    std::optional<std::string> group_opt(std::int64_t i) const {
        auto [b, e] = spans_[checked(i)];
        if (b < 0) return std::nullopt;
        return s_->substr(static_cast<std::size_t>(b), static_cast<std::size_t>(e - b));
    }
    std::optional<std::string> group_opt(const std::string& name) const { return group_opt(number(name)); }
    // For groups the compiler knows always take part in a match.
    std::string group_str(std::int64_t i) const { return group_opt(i).value_or(""); }
    std::string group_str(const std::string& name) const { return group_str(number(name)); }
    list<std::optional<std::string>> groups_list() const {
        list<std::optional<std::string>> out;
        for (std::size_t i = 1; i < spans_.size(); ++i) out.push_back(group_opt(static_cast<std::int64_t>(i)));
        return out;
    }
    dict<std::string, std::optional<std::string>> groupdict() const {
        dict<std::string, std::optional<std::string>> out;
        for (const auto& [name, i] : p_.code().names) out[name] = group_opt(i);
        return out;
    }
    dict<std::string, std::string> groupdict_str() const {
        dict<std::string, std::string> out;
        for (const auto& [name, i] : p_.code().names) out[name] = group_str(i);
        return out;
    }
    std::int64_t start(std::int64_t i = 0) const { return spans_[checked(i)].first; }
    std::int64_t end(std::int64_t i = 0) const { return spans_[checked(i)].second; }
    std::int64_t start(const std::string& name) const { return start(number(name)); }
    std::int64_t end(const std::string& name) const { return end(number(name)); }
    std::tuple<std::int64_t, std::int64_t> span(std::int64_t i = 0) const { return spans_[checked(i)]; }
    std::tuple<std::int64_t, std::int64_t> span(const std::string& name) const { return span(number(name)); }
    std::string expand(const std::string& templ) const { return p_.expand_template(templ, *s_, spans_); }

    std::string string() const { return *s_; }
    std::int64_t pos() const { return pos_; }
    std::int64_t endpos() const { return endpos_; }
    Pattern re() const { return p_; }
    std::optional<std::int64_t> lastindex() const {
        // The group that closed last: the matched group with the greatest end (ties: the outer one).
        std::optional<std::int64_t> best;
        for (std::size_t i = 1; i < spans_.size(); ++i) {
            if (spans_[i].first >= 0 && (!best || spans_[i].second > spans_[*best].second)) best = i;
        }
        return best;
    }
    const Spans& spans() const { return spans_; }
    std::string sd_repr() const {
        auto [b, e] = spans_[0];
        return "<re.Match object; span=(" + std::to_string(b) + ", " + std::to_string(e) + "), match=" +
               repr(group_str(0)) + ">";
    }
};

// Every match from the left, Python style: after an empty match, the next one may not
// be empty at the same place (retry there, then move on one character).
template <class F>
void Pattern::each_match(const std::string& s, std::int64_t limit, F on_match) const {
    std::size_t start = 0;
    std::uint32_t opts = 0;
    std::int64_t found = 0;
    while (start <= s.size() && (limit <= 0 || found < limit)) {
        auto m = exec(s, start, s.size(), opts);
        if (!m) {
            if (opts == 0) break;
            opts = 0;  // nothing non-empty here: step over one character (UTF-8 aware)
            ++start;
            while (start < s.size() && (static_cast<unsigned char>(s[start]) & 0xC0) == 0x80) ++start;
            continue;
        }
        ++found;
        auto [b, e] = (*m)[0];
        on_match(*m);
        opts = b == e ? (PCRE2_NOTEMPTY_ATSTART | PCRE2_ANCHORED) : 0;
        start = static_cast<std::size_t>(e);
    }
}

inline std::optional<Match> Pattern::search(const std::string& s, std::int64_t pos, std::optional<std::int64_t> endpos) const {
    auto n = static_cast<std::int64_t>(s.size());
    std::int64_t end = std::clamp<std::int64_t>(endpos.value_or(n), 0, n);
    pos = std::clamp<std::int64_t>(pos, 0, n);
    if (pos > end) return std::nullopt;
    auto spans = exec(s, pos, end, 0);
    if (!spans) return std::nullopt;
    return Match(*this, std::make_shared<const std::string>(s), std::move(*spans), pos, end);
}
inline std::optional<Match> Pattern::match(const std::string& s, std::int64_t pos, std::optional<std::int64_t> endpos) const {
    auto n = static_cast<std::int64_t>(s.size());
    std::int64_t end = std::clamp<std::int64_t>(endpos.value_or(n), 0, n);
    pos = std::clamp<std::int64_t>(pos, 0, n);
    if (pos > end) return std::nullopt;
    auto spans = exec(s, pos, end, PCRE2_ANCHORED);
    if (!spans) return std::nullopt;
    return Match(*this, std::make_shared<const std::string>(s), std::move(*spans), pos, end);
}
inline std::optional<Match> Pattern::fullmatch(const std::string& s, std::int64_t pos, std::optional<std::int64_t> endpos) const {
    auto n = static_cast<std::int64_t>(s.size());
    std::int64_t end = std::clamp<std::int64_t>(endpos.value_or(n), 0, n);
    pos = std::clamp<std::int64_t>(pos, 0, n);
    if (pos > end) return std::nullopt;
    auto spans = exec(s, pos, end, PCRE2_ANCHORED | PCRE2_ENDANCHORED);
    if (!spans) return std::nullopt;
    return Match(*this, std::make_shared<const std::string>(s), std::move(*spans), pos, end);
}

inline list<std::string> Pattern::findall(const std::string& s) const {
    if (groups() > 1)
        raise("TypeError", "re.findall() with several groups needs the pattern written as a string literal");
    list<std::string> out;
    each_match(s, 0, [&](const Spans& m) {
        auto [b, e] = m[groups() == 1 ? 1 : 0];
        out.push_back(b < 0 ? std::string() : s.substr(b, e - b));  // a group that didn't match: ""
    });
    return out;
}

template <std::size_t N>
list<decltype(std::tuple_cat(std::array<std::string, N>{}))> Pattern::findall_tuples(const std::string& s) const {
    list<decltype(std::tuple_cat(std::array<std::string, N>{}))> out;
    each_match(s, 0, [&](const Spans& m) {
        std::array<std::string, N> parts;
        for (std::size_t i = 0; i < N; ++i) {
            auto [b, e] = m[i + 1];
            if (b >= 0) parts[i] = s.substr(b, e - b);
        }
        out.push_back(std::tuple_cat(parts));
    });
    return out;
}

inline list<Match> Pattern::finditer(const std::string& s) const {
    list<Match> out;
    auto shared = std::make_shared<const std::string>(s);
    each_match(s, 0, [&](const Spans& m) {
        out.emplace_back(*this, shared, m, 0, static_cast<std::int64_t>(s.size()));
    });
    return out;
}

// Python's replacement syntax: \1 .. \99, \g<1>, \g<name>, and the usual escapes.
inline std::string Pattern::expand_template(const std::string& repl, const std::string& s, const Spans& spans) const {
    std::string out;
    auto group_text = [&](std::int64_t i, std::size_t at) {
        if (i < 0 || i >= static_cast<std::int64_t>(spans.size()))
            throw Thrown{std::make_shared<error>("invalid group reference " + std::to_string(i) + " at position " +
                                                 std::to_string(at))};
        auto [b, e] = spans[i];
        if (b >= 0) out.append(s, static_cast<std::size_t>(b), static_cast<std::size_t>(e - b));
    };
    for (std::size_t i = 0; i < repl.size(); ++i) {
        char c = repl[i];
        if (c != '\\' || i + 1 == repl.size()) {
            out += c;
            continue;
        }
        char n = repl[i + 1];
        if (std::isdigit(static_cast<unsigned char>(n)) && n != '0') {
            std::size_t j = i + 1;
            std::int64_t g = 0;
            while (j < repl.size() && j < i + 3 && std::isdigit(static_cast<unsigned char>(repl[j]))) g = g * 10 + (repl[j++] - '0');
            group_text(g, i);
            i = j - 1;
        } else if (n == 'g' && i + 2 < repl.size() && repl[i + 2] == '<') {
            auto close = repl.find('>', i + 3);
            if (close == std::string::npos)
                throw Thrown{std::make_shared<error>("missing >, unterminated name at position " + std::to_string(i + 3))};
            std::string name = repl.substr(i + 3, close - i - 3);
            bool numeric = !name.empty() && std::all_of(name.begin(), name.end(), [](char ch) { return std::isdigit(static_cast<unsigned char>(ch)); });
            if (numeric) {
                group_text(std::stoll(name), i);
            } else if (auto g = group_number(name)) {
                group_text(*g, i);
            } else {
                throw Thrown{std::make_shared<IndexError>("unknown group name '" + name + "'")};
            }
            i = close;
        } else {
            switch (n) {
                case '\\': out += '\\'; break;
                case 'n': out += '\n'; break;
                case 't': out += '\t'; break;
                case 'r': out += '\r'; break;
                case 'f': out += '\f'; break;
                case 'v': out += '\v'; break;
                case 'a': out += '\a'; break;
                case 'b': out += '\b'; break;
                case '0': out += '\0'; break;
                default:
                    if (std::isalpha(static_cast<unsigned char>(n)))
                        throw Thrown{std::make_shared<error>(std::string("bad escape \\") + n + " at position " + std::to_string(i))};
                    out += '\\';  // other escapes (\&, \-) keep their backslash, like Python
                    out += n;
            }
            ++i;
        }
    }
    return out;
}

inline std::tuple<std::string, std::int64_t> Pattern::subn(const std::string& repl, const std::string& s, std::int64_t count) const {
    std::string out;
    std::size_t last = 0;
    std::int64_t n = 0;
    each_match(s, count, [&](const Spans& m) {
        out.append(s, last, static_cast<std::size_t>(m[0].first) - last);
        out += expand_template(repl, s, m);
        last = static_cast<std::size_t>(m[0].second);
        ++n;
    });
    out.append(s, last, std::string::npos);
    return {out, n};
}

template <class F>
std::tuple<std::string, std::int64_t> Pattern::subn_fn(F repl, const std::string& s, std::int64_t count) const {
    std::string out;
    std::size_t last = 0;
    std::int64_t n = 0;
    auto shared = std::make_shared<const std::string>(s);
    each_match(s, count, [&](const Spans& m) {
        out.append(s, last, static_cast<std::size_t>(m[0].first) - last);
        out += repl(Match(*this, shared, m, 0, static_cast<std::int64_t>(s.size())));
        last = static_cast<std::size_t>(m[0].second);
        ++n;
    });
    out.append(s, last, std::string::npos);
    return {out, n};
}

inline list<std::optional<std::string>> Pattern::split_opt(const std::string& s, std::int64_t maxsplit) const {
    list<std::optional<std::string>> out;
    std::size_t last = 0;
    each_match(s, maxsplit, [&](const Spans& m) {
        out.push_back(s.substr(last, static_cast<std::size_t>(m[0].first) - last));
        for (std::size_t g = 1; g < m.size(); ++g) {  // groups in the pattern are kept in the result
            auto [b, e] = m[g];
            out.push_back(b < 0 ? std::nullopt : std::optional<std::string>(s.substr(b, e - b)));
        }
        last = static_cast<std::size_t>(m[0].second);
    });
    out.push_back(s.substr(last));
    return out;
}

// re.search(pattern, ...) with a pattern that isn't a literal: compiled patterns are
// cached, like Python's re module.
inline Pattern compile_cached(const std::string& pattern, std::int64_t flags = 0) {
    static std::mutex mu;
    static std::unordered_map<std::string, Pattern> cache;
    std::string key = std::to_string(flags) + ":" + pattern;
    {
        std::lock_guard lk(mu);
        if (auto it = cache.find(key); it != cache.end()) return it->second;
    }
    Pattern p(pattern, flags);
    std::lock_guard lk(mu);
    if (cache.size() >= 512) cache.clear();
    cache.emplace(key, p);
    return p;
}

inline std::string escape(const std::string& s) {
    static const std::string special = "()[]{}?*+-|^$\\.&~# \t\n\r\v\f";
    std::string out;
    for (char c : s) {
        if (special.find(c) != std::string::npos) out += '\\';
        out += c;
    }
    return out;
}

}  // namespace sd::re
