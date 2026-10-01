// The `fnmatch` module: shell-style wildcards (`*`, `?`, `[seq]`, `[!seq]`), done as
// Python does it: translate() turns the pattern into a regular expression (the same
// string as Python's), which is compiled with the `re` module's PCRE2 and cached.
// Patterns and names are text, so `?` matches one character, not one byte.
// os.path.normcase does nothing on POSIX, so fnmatch() is fnmatchcase().
#pragma once

#include "re.hpp"

namespace sd::fnmatch {

namespace detail {

inline bool continuation(char c) { return (static_cast<unsigned char>(c) & 0xC0) == 0x80; }

// The byte offset `chars` characters after `i` (UTF-8), at most s.size().
inline std::size_t advance(const std::string& s, std::size_t i, std::size_t chars) {
    for (; chars > 0 && i < s.size(); --chars) {
        ++i;
        while (i < s.size() && continuation(s[i])) ++i;
    }
    return std::min(i, s.size());
}
inline std::size_t last_char(const std::string& s) {  // byte offset of the last character
    std::size_t i = s.size() - 1;
    while (i > 0 && continuation(s[i])) --i;
    return i;
}

inline std::string replace_all(std::string s, std::string_view from, std::string_view to) {
    for (std::size_t i = 0; (i = s.find(from, i)) != std::string::npos; i += to.size()) s.replace(i, from.size(), to);
    return s;
}

struct Translated {
    std::vector<std::string> parts;
    std::vector<std::size_t> stars;  // indexes of the `*` parts
};

// A port of Python's fnmatch._translate: each pattern piece as a regex piece.
inline Translated translate_parts(const std::string& pat, const std::string& star, const std::string& question_mark) {
    Translated t;
    auto& res = t.parts;
    std::size_t i = 0, n = pat.size();
    while (i < n) {
        char c = pat[i];
        if (c == '*') {
            ++i;
            t.stars.push_back(res.size());
            res.push_back(star);
            while (i < n && pat[i] == '*') ++i;  // `**` is `*`
        } else if (c == '?') {
            ++i;
            res.push_back(question_mark);
        } else if (c == '[') {
            ++i;
            std::size_t j = i;
            if (j < n && pat[j] == '!') ++j;
            if (j < n && pat[j] == ']') ++j;
            while (j < n && pat[j] != ']') ++j;
            if (j >= n) {
                res.push_back("\\[");
                continue;
            }
            std::string stuff = pat.substr(i, j - i);
            if (stuff.find('-') == std::string::npos) {
                stuff = replace_all(stuff, "\\", "\\\\");
            } else {
                std::vector<std::string> chunks;
                std::size_t k = advance(pat, pat[i] == '!' ? i + 1 : i, 1);
                for (;;) {
                    k = k < j ? pat.find('-', k) : std::string::npos;
                    if (k == std::string::npos || k >= j) break;
                    chunks.push_back(pat.substr(i, k - i));
                    i = k + 1;
                    k = advance(pat, k, 3);
                }
                std::string chunk = pat.substr(i, j - i);
                if (!chunk.empty()) {
                    chunks.push_back(chunk);
                } else {
                    chunks.back() += '-';
                }
                // Remove empty ranges (`z-a`): invalid in a regex.
                for (std::size_t m = chunks.size() - 1; m > 0; --m) {
                    std::string& before = chunks[m - 1];
                    const std::string& after = chunks[m];
                    std::size_t last = last_char(before), first_end = advance(after, 0, 1);
                    if (before.compare(last, std::string::npos, after, 0, first_end) > 0) {
                        before = before.substr(0, last) + after.substr(first_end);
                        chunks.erase(chunks.begin() + static_cast<std::ptrdiff_t>(m));
                    }
                }
                // Escape backslashes, and hyphens that don't make ranges (`--` is set difference).
                stuff.clear();
                for (std::size_t m = 0; m < chunks.size(); ++m) {
                    if (m) stuff += '-';
                    stuff += replace_all(replace_all(chunks[m], "\\", "\\\\"), "-", "\\-");
                }
            }
            i = j + 1;
            if (stuff.empty()) {
                res.push_back("(?!)");  // an empty range never matches
            } else if (stuff == "!") {
                res.push_back(".");  // a negated empty range matches any character
            } else {
                std::string escaped;  // escape set operations (&&, ~~ and ||)
                for (char ch : stuff) {
                    if (ch == '&' || ch == '~' || ch == '|') escaped += '\\';
                    escaped += ch;
                }
                stuff = std::move(escaped);
                if (stuff[0] == '!') {
                    stuff[0] = '^';
                } else if (stuff[0] == '^' || stuff[0] == '[') {
                    stuff = "\\" + stuff;
                }
                res.push_back("[" + stuff + "]");
            }
        } else {
            std::size_t next = advance(pat, i, 1);
            res.push_back(next == i + 1 ? re::escape(std::string(1, c)) : pat.substr(i, next - i));
            i = next;
        }
    }
    return t;
}

inline std::string join_parts(const Translated& t) {
    auto join = [&](std::size_t from, std::size_t to) {
        std::string s;
        for (std::size_t k = from; k < to; ++k) s += t.parts[k];
        return s;
    };
    if (t.stars.empty()) return "(?s:" + join(0, t.parts.size()) + ")\\z";
    // Fixed pieces at the start, then each interior `* fixed` as a minimal match that
    // can't backtrack (an atomic group), then the last `*` and what follows it.
    std::string res = join(0, t.stars[0]);
    std::size_t i = t.stars[0] + 1;
    for (std::size_t s = 1; s < t.stars.size(); ++s) {
        res += "(?>.*?" + join(i, t.stars[s]) + ")";
        i = t.stars[s] + 1;
    }
    res += ".*" + join(i, t.parts.size());
    return "(?s:" + res + ")\\z";
}

// The compiled regex for a pattern, cached like Python's (whose cache holds 32768).
inline re::Pattern compiled(const std::string& pat) {
    static std::mutex mu;
    static std::unordered_map<std::string, re::Pattern> cache;
    {
        std::lock_guard lk(mu);
        if (auto it = cache.find(pat); it != cache.end()) return it->second;
    }
    re::Pattern p(join_parts(translate_parts(pat, ".*", ".")));
    std::lock_guard lk(mu);
    if (cache.size() >= 32768) cache.clear();
    cache.emplace(pat, p);
    return p;
}

inline bool matches(const re::Pattern& p, const std::string& name) {
    return p.exec(name, 0, name.size(), PCRE2_ANCHORED).has_value();
}

}  // namespace detail

inline std::string translate(const std::string& pat) {
    return detail::join_parts(detail::translate_parts(pat, ".*", "."));
}

inline bool fnmatchcase(const std::string& name, const std::string& pat) {
    return detail::matches(detail::compiled(pat), name);
}

inline bool fnmatch(const std::string& name, const std::string& pat) { return fnmatchcase(name, pat); }

template <class It>
list<std::string> filter(It names, const std::string& pat) {
    re::Pattern p = detail::compiled(pat);
    list<std::string> out;
    for (auto&& name : iter(names))
        if (detail::matches(p, name)) out.push_back(name);
    return out;
}

template <class It>
list<std::string> filterfalse(It names, const std::string& pat) {
    re::Pattern p = detail::compiled(pat);
    list<std::string> out;
    for (auto&& name : iter(names))
        if (!detail::matches(p, name)) out.push_back(name);
    return out;
}

}  // namespace sd::fnmatch
