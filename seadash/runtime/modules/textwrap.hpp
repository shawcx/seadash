// The `textwrap` module: a port of Python's TextWrapper (same word splitting, including
// hyphenated words and em-dashes, and the same line filling), plus shorten, dedent and
// indent. Widths count characters, not bytes, like Python.
#pragma once

#include "re.hpp"

namespace sd::textwrap {

inline std::size_t width_of(const std::string& s) {  // characters in UTF-8 text
    std::size_t n = 0;
    for (unsigned char c : s) n += (c & 0xC0) != 0x80;
    return n;
}
inline std::size_t byte_at(const std::string& s, std::size_t chars) {  // byte offset of a character
    std::size_t i = 0;
    for (std::size_t seen = 0; i < s.size(); ++i) {
        if ((static_cast<unsigned char>(s[i]) & 0xC0) != 0x80) {
            if (seen == chars) return i;
            ++seen;
        }
    }
    return s.size();
}
inline bool is_blank(const std::string& s) { return s.find_first_not_of("\t\n\x0b\x0c\r ") == std::string::npos; }

struct TextWrapper {
    std::int64_t width = 70;
    std::string initial_indent, subsequent_indent;
    bool expand_tabs = true, replace_whitespace = true, fix_sentence_endings = false, break_long_words = true,
         drop_whitespace = true, break_on_hyphens = true;
    std::int64_t tabsize = 8;
    std::optional<std::int64_t> max_lines;
    std::string placeholder = " [...]";

    static const re::Pattern& wordsep() {
        static const re::Pattern p(R"re((?x)
            ( # any whitespace
              [\t\n\x0b\x0c\r ]+
            | # em-dash between words
              (?<=[\w!"'&.,?]) -{2,} (?=\w)
            | # word, possibly hyphenated
              [^\t\n\x0b\x0c\r ]+? (?:
                # hyphenated word
                  -(?: (?<=[^\d\W]{2}-) | (?<=[^\d\W]-[^\d\W]-))
                  (?= [^\d\W] -? [^\d\W])
                | # end of word
                  (?=[\t\n\x0b\x0c\r ]|\Z)
                | # em-dash
                  (?<=[\w!"'&.,?]) (?=-{2,}\w)
                )
            ))re");
        return p;
    }

    std::string munge_whitespace(const std::string& text) const {
        std::string out;
        std::size_t column = 0;
        for (char c : text) {
            if (c == '\t' && expand_tabs) {
                std::size_t spaces = tabsize > 0 ? tabsize - column % tabsize : 0;
                out.append(spaces, ' ');
                column += spaces;
                continue;
            }
            if (c == '\n' || c == '\r') column = 0;
            else if ((static_cast<unsigned char>(c) & 0xC0) != 0x80) ++column;
            if (replace_whitespace && (c == '\t' || c == '\n' || c == '\x0b' || c == '\x0c' || c == '\r')) c = ' ';
            out += c;
        }
        return out;
    }

    list<std::string> split_chunks(const std::string& text) const {
        list<std::string> chunks;
        if (break_on_hyphens) {
            for (auto& piece : wordsep().split_opt(munge_whitespace(text)))
                if (piece && !piece->empty()) chunks.push_back(*piece);
        } else {
            std::string t = munge_whitespace(text), current;
            for (char c : t) {  // runs of whitespace, and everything between
                bool ws = std::string("\t\n\x0b\x0c\r ").find(c) != std::string::npos;
                if (!current.empty() && ws != is_blank(current)) {
                    chunks.push_back(current);
                    current.clear();
                }
                current += c;
            }
            if (!current.empty()) chunks.push_back(current);
        }
        if (fix_sentence_endings) {
            for (std::size_t i = 0; i + 1 < chunks.size();) {
                const std::string& c = chunks[i];
                std::size_t n = c.size();
                bool ends = false;  // [a-z][.!?]["']? at the end
                if (n >= 2) {
                    std::size_t k = (c[n - 1] == '"' || c[n - 1] == '\'') ? n - 1 : n;
                    ends = k >= 2 && std::string(".!?").find(c[k - 1]) != std::string::npos && std::islower(static_cast<unsigned char>(c[k - 2]));
                }
                if (chunks[i + 1] == " " && ends) {
                    chunks[i + 1] = "  ";
                    i += 2;
                } else {
                    ++i;
                }
            }
        }
        return chunks;
    }

    void handle_long_word(list<std::string>& reversed, list<std::string>& line, std::size_t line_len, std::int64_t w) const {
        std::size_t space_left = w < 1 ? 1 : static_cast<std::size_t>(w) - line_len;
        if (break_long_words) {
            std::string chunk = reversed.back();
            std::size_t end = space_left;
            if (break_on_hyphens && width_of(chunk) > space_left) {
                std::string head = chunk.substr(0, byte_at(chunk, space_left));
                auto hyphen = head.rfind('-');
                if (hyphen != std::string::npos && hyphen > 0 && head.find_first_not_of('-') < hyphen)
                    end = width_of(head.substr(0, hyphen)) + 1;
            }
            line.push_back(chunk.substr(0, byte_at(chunk, end)));
            reversed.back() = chunk.substr(byte_at(chunk, end));
        } else if (line.empty()) {
            line.push_back(reversed.back());
            reversed.pop_back();
        }
    }

    list<std::string> wrap(const std::string& text) const {
        list<std::string> lines;
        if (width <= 0) raise("ValueError", "invalid width " + std::to_string(width) + " (must be > 0)");
        if (max_lines) {
            const std::string& indent = *max_lines > 1 ? subsequent_indent : initial_indent;
            std::string ph = placeholder.substr(placeholder.find_first_not_of(" \t\n\r\x0b\x0c") == std::string::npos ? placeholder.size() : placeholder.find_first_not_of(" \t\n\r\x0b\x0c"));
            if (static_cast<std::int64_t>(width_of(indent) + width_of(ph)) > width)
                raise("ValueError", "placeholder too large for max width");
        }
        list<std::string> chunks = split_chunks(text);
        std::reverse(chunks.begin(), chunks.end());
        while (!chunks.empty()) {
            list<std::string> line;
            std::size_t len = 0;
            const std::string& indent = lines.empty() ? initial_indent : subsequent_indent;
            std::int64_t w = width - static_cast<std::int64_t>(width_of(indent));
            if (drop_whitespace && is_blank(chunks.back()) && !lines.empty()) chunks.pop_back();
            while (!chunks.empty()) {
                std::size_t l = width_of(chunks.back());
                if (static_cast<std::int64_t>(len + l) <= w) {
                    line.push_back(chunks.back());
                    chunks.pop_back();
                    len += l;
                } else {
                    break;
                }
            }
            if (!chunks.empty() && static_cast<std::int64_t>(width_of(chunks.back())) > w) {
                handle_long_word(chunks, line, len, w);
                len = 0;
                for (auto& c : line) len += width_of(c);
            }
            if (drop_whitespace && !line.empty() && is_blank(line.back())) {
                len -= width_of(line.back());
                line.pop_back();
            }
            if (line.empty()) continue;
            auto joined = [&] {
                std::string out = indent;
                for (auto& c : line) out += c;
                return out;
            };
            bool rest_blank = chunks.empty() || (drop_whitespace && chunks.size() == 1 && is_blank(chunks[0]));
            if (!max_lines || static_cast<std::int64_t>(lines.size()) + 1 < *max_lines ||
                (rest_blank && static_cast<std::int64_t>(len) <= w)) {
                lines.push_back(joined());
                continue;
            }
            // The last line allowed: end it with the placeholder.
            bool placed = false;
            while (!line.empty()) {
                if (!is_blank(line.back()) && static_cast<std::int64_t>(len + width_of(placeholder)) <= w) {
                    line.push_back(placeholder);
                    lines.push_back(joined());
                    placed = true;
                    break;
                }
                len -= width_of(line.back());
                line.pop_back();
            }
            if (!placed) {
                if (!lines.empty()) {
                    std::string prev = lines.back();
                    prev.erase(prev.find_last_not_of(" \t\n\r\x0b\x0c") + 1);
                    if (static_cast<std::int64_t>(width_of(prev) + width_of(placeholder)) <= width) {
                        lines.back() = prev + placeholder;
                        break;
                    }
                }
                std::size_t start = placeholder.find_first_not_of(" \t\n\r\x0b\x0c");
                lines.push_back(indent + (start == std::string::npos ? "" : placeholder.substr(start)));
            }
            break;
        }
        return lines;
    }
    std::string fill(const std::string& text) const {
        std::string out;
        auto lines = wrap(text);
        for (std::size_t i = 0; i < lines.size(); ++i) out += (i ? "\n" : "") + lines[i];
        return out;
    }
    std::string sd_repr() const { return "<textwrap.TextWrapper object>"; }
};

inline TextWrapper make(std::int64_t width, std::string initial_indent, std::string subsequent_indent, bool expand_tabs,
                        bool replace_whitespace, bool fix_sentence_endings, bool break_long_words, bool drop_whitespace,
                        bool break_on_hyphens, std::int64_t tabsize, std::optional<std::int64_t> max_lines,
                        std::string placeholder) {
    TextWrapper w;
    w.width = width, w.initial_indent = std::move(initial_indent), w.subsequent_indent = std::move(subsequent_indent);
    w.expand_tabs = expand_tabs, w.replace_whitespace = replace_whitespace, w.fix_sentence_endings = fix_sentence_endings;
    w.break_long_words = break_long_words, w.drop_whitespace = drop_whitespace, w.break_on_hyphens = break_on_hyphens;
    w.tabsize = tabsize, w.max_lines = max_lines, w.placeholder = std::move(placeholder);
    return w;
}

template <class... Options>
list<std::string> wrap(const std::string& text, Options... options) {
    return make(options...).wrap(text);
}
template <class... Options>
std::string fill(const std::string& text, Options... options) {
    return make(options...).fill(text);
}
template <class... Options>
std::string shorten(const std::string& text, std::int64_t width, Options... options) {
    std::string collapsed;  // ' '.join(text.strip().split())
    std::size_t i = 0;
    while (i < text.size()) {
        while (i < text.size() && std::isspace(static_cast<unsigned char>(text[i]))) ++i;
        std::size_t j = i;
        while (j < text.size() && !std::isspace(static_cast<unsigned char>(text[j]))) ++j;
        if (j > i) collapsed += (collapsed.empty() ? "" : " ") + text.substr(i, j - i);
        i = j;
    }
    TextWrapper w = make(width, options...);
    w.max_lines = 1;
    return w.fill(collapsed);
}

// Remove the whitespace every (non-blank) line starts with.
inline std::string dedent(const std::string& text) {
    list<std::string> lines;
    for (std::size_t start = 0; start <= text.size();) {
        std::size_t end = text.find('\n', start);
        lines.push_back(text.substr(start, end == std::string::npos ? std::string::npos : end - start));
        if (end == std::string::npos) break;
        start = end + 1;
    }
    std::optional<std::string> margin;
    for (auto& line : lines) {
        if (line.find_first_not_of(" \t") == std::string::npos) {
            line.clear();  // whitespace-only lines become empty
            continue;
        }
        std::string indent = line.substr(0, line.find_first_not_of(" \t"));
        if (!margin) {
            margin = indent;
        } else if (indent.rfind(*margin, 0) == 0) {
        } else if (margin->rfind(indent, 0) == 0) {
            margin = indent;
        } else {
            std::size_t k = 0;
            while (k < margin->size() && k < indent.size() && (*margin)[k] == indent[k]) ++k;
            margin = margin->substr(0, k);
        }
    }
    std::string out;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        std::string line = lines[i];
        if (margin && !margin->empty() && line.rfind(*margin, 0) == 0) line = line.substr(margin->size());
        out += (i ? "\n" : "") + line;
    }
    return out;
}

// Add `prefix` to lines that aren't just whitespace (or those `predicate` picks).
template <class P>
std::string indent(const std::string& text, const std::string& prefix, P predicate) {
    std::string out;
    for (std::size_t start = 0; start < text.size();) {
        std::size_t end = text.find('\n', start);
        std::string line = text.substr(start, end == std::string::npos ? std::string::npos : end - start + 1);
        if (predicate(line)) out += prefix;
        out += line;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return out;
}
inline std::string indent(const std::string& text, const std::string& prefix) {
    return indent(text, prefix, [](const std::string& line) { return !is_blank(line); });
}

}  // namespace sd::textwrap
