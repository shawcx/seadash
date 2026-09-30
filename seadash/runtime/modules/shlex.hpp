// The `shlex` module: split, quote and join, with the POSIX shell's rules like Python's
// shlex.split (a port of its state machine, in whitespace_split mode).
#pragma once

namespace sd::shlex {

inline bool is_space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
inline bool is_quote(char c) { return c == '\'' || c == '"'; }

inline list<std::string> split(const std::string& s, bool comments = false, bool posix = true) {
    if (!posix) raise("ValueError", "shlex.split(posix=False) is not supported");
    list<std::string> out;
    std::size_t i = 0;
    while (true) {
        // One token, as Python's read_token does it: `state` is ' ' between tokens, 'a'
        // inside one, a quote character inside quotes, or '\\' after a backslash.
        std::string token;
        char state = ' ', escaped_state = ' ';
        bool quoted = false, at_end = false;
        while (true) {
            char c = i < s.size() ? s[i++] : '\0';
            if (state == ' ') {
                if (!c) {
                    at_end = true;
                    break;
                } else if (is_space(c)) {
                    if (!token.empty() || quoted) break;
                } else if (comments && c == '#') {
                    while (i < s.size() && s[i++] != '\n') {}
                } else if (c == '\\') {
                    escaped_state = 'a';
                    state = c;
                } else if (is_quote(c)) {
                    state = c;
                } else {
                    token = c;
                    state = 'a';
                }
            } else if (is_quote(state)) {
                quoted = true;
                if (!c) raise("ValueError", "No closing quotation");
                if (c == state) {
                    state = 'a';
                } else if (c == '\\' && state == '"') {
                    escaped_state = state;
                    state = c;
                } else {
                    token += c;
                }
            } else if (state == '\\') {
                if (!c) raise("ValueError", "No escaped character");
                // In quotes, a backslash only escapes the quote and itself.
                if (is_quote(escaped_state) && c != state && c != escaped_state) token += state;
                token += c;
                state = escaped_state;
            } else {  // 'a'
                if (!c) {
                    at_end = true;
                    break;
                } else if (is_space(c)) {
                    state = ' ';
                    if (!token.empty() || quoted) break;
                } else if (comments && c == '#') {
                    while (i < s.size() && s[i++] != '\n') {}
                    state = ' ';
                    if (!token.empty() || quoted) break;
                } else if (is_quote(c)) {
                    state = c;
                } else if (c == '\\') {
                    escaped_state = 'a';
                    state = c;
                } else {
                    token += c;
                }
            }
        }
        if (token.empty() && !quoted) {  // (an unquoted empty token is the end of the input)
            if (at_end) break;
            continue;
        }
        out.push_back(std::move(token));
        if (at_end) break;
    }
    return out;
}

inline std::string quote(const std::string& s) {
    if (s.empty()) return "''";
    auto safe = [](unsigned char c) {  // (ASCII only, like Python: r'[\w@%+=:,./-]' with re.ASCII)
        return std::isalnum(c) || c == '_' || std::strchr("@%+=:,./-", c) != nullptr;
    };
    if (std::all_of(s.begin(), s.end(), [&](char c) { return c < 0 ? false : safe(static_cast<unsigned char>(c)); })) return s;
    std::string out = "'";
    for (char c : s) {
        if (c == '\'') out += "'\"'\"'";  // '...' can't hold a quote: end it, add "'", resume
        else out += c;
    }
    return out + "'";
}

inline std::string join(const list<std::string>& words) {
    std::string out;
    for (const auto& w : words) {
        if (!out.empty()) out += ' ';
        out += quote(w);
    }
    return out;
}

}  // namespace sd::shlex
