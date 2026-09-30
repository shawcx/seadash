// The `csv` module: a port of CPython's reader state machine and writer quoting rules,
// so quoting edge cases come out the same. Readers are lazy (generators of rows).
#pragma once

namespace sd::csv {

inline constexpr std::int64_t QUOTE_MINIMAL = 0, QUOTE_ALL = 1, QUOTE_NONNUMERIC = 2, QUOTE_NONE = 3,
                              QUOTE_STRINGS = 4, QUOTE_NOTNULL = 5;

struct Error : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "_csv.Error"; }
};
[[noreturn]] inline void fail(const std::string& message) { throw Thrown{std::make_shared<Error>(message)}; }

struct Dialect {
    char delimiter = ',';
    std::optional<char> quotechar = '"';
    std::optional<char> escapechar;
    bool doublequote = true, skipinitialspace = false, strict = false;
    std::string lineterminator = "\r\n";
    std::int64_t quoting = QUOTE_MINIMAL;
};

inline std::optional<char> one_char(const std::optional<std::string>& s, const char* what) {
    if (!s) return std::nullopt;
    if (s->size() != 1) fail(std::string("\"") + what + "\" must be a 1-character string");
    return (*s)[0];
}

inline Dialect make_dialect(const std::string& name, std::optional<std::string> delimiter,
                            std::optional<std::optional<std::string>> quotechar, std::optional<std::string> escapechar,
                            std::optional<bool> doublequote, std::optional<bool> skipinitialspace,
                            std::optional<std::string> lineterminator, std::optional<std::int64_t> quoting,
                            std::optional<bool> strict) {
    Dialect d;
    if (name == "excel-tab") {
        d.delimiter = '\t';
    } else if (name == "unix") {
        d.lineterminator = "\n";
        d.quoting = QUOTE_ALL;
    } else if (name != "excel") {
        fail("unknown dialect");
    }
    if (delimiter) d.delimiter = *one_char(delimiter, "delimiter");
    if (quotechar) d.quotechar = one_char(*quotechar, "quotechar");
    if (escapechar) d.escapechar = one_char(escapechar, "escapechar");
    if (doublequote) d.doublequote = *doublequote;
    if (skipinitialspace) d.skipinitialspace = *skipinitialspace;
    if (lineterminator) d.lineterminator = *lineterminator;
    if (quoting) d.quoting = *quoting;
    if (strict) d.strict = *strict;
    if (d.quoting < 0 || d.quoting > 5) raise("TypeError", "bad \"quoting\" value");
    if (!d.quotechar && d.quoting != QUOTE_NONE) raise("TypeError", "quotechar must be set if quoting enabled");
    return d;
}

// ---- reading ------------------------------------------------------------------------

class Parser {
    enum State { START_RECORD, START_FIELD, ESCAPED_CHAR, IN_FIELD, IN_QUOTED_FIELD, ESCAPE_IN_QUOTED_FIELD,
                 QUOTE_IN_QUOTED_FIELD, EAT_CRNL };
    static constexpr int EOL = -2;
    const Dialect& d_;
    State state_ = START_RECORD;
    std::string field_;
    bool field_started_ = false;

    void save_field() {
        fields.push_back(std::move(field_));
        field_.clear();
        field_started_ = false;
    }
    void add_char(char c) {
        field_ += c;
        field_started_ = true;
    }
    bool is_quote(int c) const { return d_.quotechar && d_.quoting != QUOTE_NONE && c == *d_.quotechar; }
    bool is_escape(int c) const { return d_.escapechar && c == *d_.escapechar; }

public:
    list<std::string> fields;
    explicit Parser(const Dialect& d) : d_(d) {}

    void process(int c) {
        switch (state_) {
            case START_RECORD:
                if (c == EOL) return;  // an empty line: an empty row
                if (c == '\n' || c == '\r') {
                    state_ = EAT_CRNL;
                    return;
                }
                state_ = START_FIELD;
                [[fallthrough]];
            case START_FIELD:
                if (c == '\n' || c == '\r' || c == EOL) {
                    save_field();
                    state_ = c == EOL ? START_RECORD : EAT_CRNL;
                } else if (is_quote(c)) {
                    field_started_ = true;
                    state_ = IN_QUOTED_FIELD;
                } else if (is_escape(c)) {
                    state_ = ESCAPED_CHAR;
                } else if (c == ' ' && d_.skipinitialspace) {
                } else if (c == d_.delimiter) {
                    save_field();
                } else {
                    add_char(static_cast<char>(c));
                    state_ = IN_FIELD;
                }
                break;
            case ESCAPED_CHAR:
                if (c == EOL) c = '\n';
                add_char(static_cast<char>(c));
                state_ = IN_FIELD;
                break;
            case IN_FIELD:
                if (c == '\n' || c == '\r' || c == EOL) {
                    save_field();
                    state_ = c == EOL ? START_RECORD : EAT_CRNL;
                } else if (is_escape(c)) {
                    state_ = ESCAPED_CHAR;
                } else if (c == d_.delimiter) {
                    save_field();
                    state_ = START_FIELD;
                } else {
                    add_char(static_cast<char>(c));
                }
                break;
            case IN_QUOTED_FIELD:
                if (c == EOL) {
                } else if (is_escape(c)) {
                    state_ = ESCAPE_IN_QUOTED_FIELD;
                } else if (is_quote(c)) {
                    state_ = d_.doublequote ? QUOTE_IN_QUOTED_FIELD : IN_FIELD;
                } else {
                    add_char(static_cast<char>(c));
                }
                break;
            case ESCAPE_IN_QUOTED_FIELD:
                if (c == EOL) c = '\n';
                add_char(static_cast<char>(c));
                state_ = IN_QUOTED_FIELD;
                break;
            case QUOTE_IN_QUOTED_FIELD:
                if (is_quote(c)) {  // a doubled quote is one quote
                    add_char(static_cast<char>(c));
                    state_ = IN_QUOTED_FIELD;
                } else if (c == d_.delimiter) {
                    save_field();
                    state_ = START_FIELD;
                } else if (c == '\n' || c == '\r' || c == EOL) {
                    save_field();
                    state_ = c == EOL ? START_RECORD : EAT_CRNL;
                } else if (!d_.strict) {
                    add_char(static_cast<char>(c));
                    state_ = IN_FIELD;
                } else {
                    fail(std::string("'") + d_.delimiter + "' expected after '" + *d_.quotechar + "'");
                }
                break;
            case EAT_CRNL:
                if (c == '\n' || c == '\r') {
                } else if (c == EOL) {
                    state_ = START_RECORD;
                } else {
                    fail("new-line character seen in unquoted field - do you need to open the file with newline=''?");
                }
                break;
        }
    }
    void end_line() { process(EOL); }
    bool record_done() const { return state_ == START_RECORD; }
    bool pending() const { return field_started_ || !field_.empty() || state_ == IN_QUOTED_FIELD; }
    bool in_quotes() const { return state_ == IN_QUOTED_FIELD; }
    void finish_field() { save_field(); }
    void reset() {
        fields.clear();
        state_ = START_RECORD;
    }
};

// csv.reader(lines): each record as a list of fields (a quoted field may span lines).
template <class It>
Generator<list<std::string>> reader(It lines, Dialect d) {
    Parser p(d);
    list<std::string> row;
    for (auto&& line : iter(lines)) {
        const std::string& text = line;
        for (char c : text) {
            if (c == '\0') fail("line contains NUL");
            p.process(static_cast<unsigned char>(c));
        }
        p.end_line();
        if (p.record_done()) {
            co_yield std::move(p.fields);
            p.reset();
        }
    }
    if (p.pending()) {  // the input ended inside a record
        if (d.strict && p.in_quotes()) fail("unexpected end of data");
        p.finish_field();
        co_yield std::move(p.fields);
    }
}

// csv.DictReader: rows as {fieldname: value}. (Missing fields get restval, "" by default;
// extra fields are dropped: seadash's rows are dict[str, str].)
class DictReader {
    struct State {
        Generator<list<std::string>> rows;
        std::optional<list<std::string>> fieldnames;
        std::string restval;
        std::optional<Generator<dict<std::string, std::string>>> stream;
    };
    std::shared_ptr<State> s_;

    static Generator<dict<std::string, std::string>> rows_of(std::shared_ptr<State> s, list<std::string> names) {
        while (s->rows.advance()) {
            list<std::string> row = s->rows.take();
            if (row.empty()) continue;  // blank lines are skipped, like Python
            dict<std::string, std::string> out;
            for (std::size_t i = 0; i < names.size(); ++i) out[names[i]] = i < row.size() ? row[i] : s->restval;
            co_yield out;
        }
    }

public:
    DictReader() = default;
    template <class It>
    DictReader(It lines, std::optional<list<std::string>> fieldnames, std::optional<std::string> restval, Dialect d)
        : s_(std::make_shared<State>()) {
        s_->rows = reader(std::move(lines), d);
        s_->fieldnames = std::move(fieldnames);
        s_->restval = restval.value_or("");
    }
    list<std::string> fieldnames() const {
        if (!s_->fieldnames) s_->fieldnames = s_->rows.advance() ? s_->rows.take() : list<std::string>{};  // the header
        return *s_->fieldnames;
    }
    // The rows, as one shared stream (so `next(r)` and `for row in r` continue each other).
    const Generator<dict<std::string, std::string>>& stream() const {
        if (!s_->stream) s_->stream = rows_of(s_, fieldnames());
        return *s_->stream;
    }
    auto begin() const { return stream().begin(); }
    auto end() const { return stream().end(); }
    std::string sd_repr() const { return "<csv.DictReader object>"; }
};

// ---- writing ------------------------------------------------------------------------

struct Field {
    std::string text;
    bool number = false, none = false, string = false;
};
inline Field field(const std::string& s) { return {s, false, false, true}; }
inline Field field(const char* s) { return field(std::string(s)); }
inline Field field(std::nullopt_t) { return {"", false, true, false}; }
template <class T>
Field field(const T& x) {
    if constexpr (is_optional<T>::value) {
        return x ? field(*x) : field(std::nullopt);
    } else if constexpr (std::is_arithmetic_v<T> || std::is_same_v<T, Bool>) {
        return {str(x), true, false, false};
    } else {
        return {str(x), false, false, false};
    }
}
template <class Row>
std::vector<Field> fields_of(const Row& row) {
    std::vector<Field> out;
    if constexpr (is_tuple<Row>::value) {
        std::apply([&](const auto&... x) { (out.push_back(field(x)), ...); }, row);
    } else {
        for (auto&& x : iter(row)) out.push_back(field(x));
    }
    return out;
}

inline std::string join_row(const std::vector<Field>& row, const Dialect& d) {
    std::string out;
    for (std::size_t i = 0; i < row.size(); ++i) {
        const Field& f = row[i];
        if (i) out += d.delimiter;
        bool quoted = d.quoting == QUOTE_ALL || (d.quoting == QUOTE_NONNUMERIC && !f.number) ||
                      (d.quoting == QUOTE_STRINGS && f.string) || (d.quoting == QUOTE_NOTNULL && !f.none);
        if (row.size() == 1 && f.text.empty() && !f.none) {  // a lone empty field must be quoted
            if (d.quoting == QUOTE_NONE) fail("single empty field record must be quoted");
            quoted = true;
        }
        std::string text;
        for (char c : f.text) {
            bool special = c == d.delimiter || (d.escapechar && c == *d.escapechar) || (d.quotechar && c == *d.quotechar) ||
                           c == '\n' || c == '\r' || d.lineterminator.find(c) != std::string::npos;
            bool escape = false;
            if (special) {
                if (d.quoting == QUOTE_NONE) {
                    escape = true;
                } else {
                    if (d.quotechar && c == *d.quotechar) {
                        if (d.doublequote) text += c;
                        else escape = true;
                    } else if (d.escapechar && c == *d.escapechar) {
                        escape = true;
                    }
                    if (!escape) quoted = true;
                }
                if (escape) {
                    if (!d.escapechar) fail("need to escape, but no escapechar set");
                    text += *d.escapechar;
                }
            }
            text += c;
        }
        if (quoted) text = std::string(1, *d.quotechar) + text + *d.quotechar;
        out += text;
    }
    return out + d.lineterminator;
}

class Writer {
    std::shared_ptr<TextFile> file_;
    Dialect d_;

public:
    Writer() = default;
    Writer(std::shared_ptr<TextFile> f, Dialect d) : file_(std::move(f)), d_(std::move(d)) {}
    template <class Row>
    std::int64_t writerow(const Row& row) const {
        return writerow_fields(fields_of(row));
    }
    std::int64_t writerow_fields(const std::vector<Field>& row) const { return file_->write(join_row(row, d_)); }
    template <class Rows>
    void writerows(const Rows& rows) const {
        for (auto&& row : iter(rows)) writerow(row);
    }
    std::string sd_repr() const { return "<_csv.writer object>"; }
};

class DictWriter {
    Writer w_;
    list<std::string> names_;
    std::string restval_;
    bool raise_extras_ = true;

public:
    DictWriter() = default;
    template <class Names>
    DictWriter(std::shared_ptr<TextFile> f, const Names& fieldnames, std::string restval, const std::string& extrasaction,
               Dialect d)
        : w_(std::move(f), std::move(d)), restval_(std::move(restval)) {
        for (auto&& n : iter(fieldnames)) names_.push_back(n);
        if (extrasaction != "raise" && extrasaction != "ignore")
            raise("ValueError", "extrasaction (" + extrasaction + ") must be 'raise' or 'ignore'");
        raise_extras_ = extrasaction == "raise";
    }
    std::int64_t writeheader() const { return w_.writerow(names_); }
    template <class V>
    std::int64_t writerow(const dict<std::string, V>& row) const {
        if (raise_extras_) {
            list<std::string> extra;
            for (const auto& [k, v] : row)
                if (std::find(names_.begin(), names_.end(), k) == names_.end()) extra.push_back(repr(k));
            if (!extra.empty()) {
                std::string joined;
                for (std::size_t i = 0; i < extra.size(); ++i) joined += (i ? ", " : "") + extra[i];
                raise("ValueError", "dict contains fields not in fieldnames: " + joined);
            }
        }
        std::vector<Field> out;
        for (const auto& n : names_) {
            const V* v = row.find(n);
            out.push_back(v ? field(*v) : field(restval_));
        }
        return w_.writerow_fields(out);
    }
    template <class Rows>
    void writerows(const Rows& rows) const {
        for (auto&& row : iter(rows)) writerow(row);
    }
    std::string sd_repr() const { return "<csv.DictWriter object>"; }
};

}  // namespace sd::csv
