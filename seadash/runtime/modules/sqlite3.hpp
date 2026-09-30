// The `sqlite3` module: connections and cursors over the system SQLite, with Python's
// transactions (an implicit BEGIN before the first DML statement; commit/rollback; `with`),
// parameter binding and errors. Rows are typed by what the program expects
// (`row: tuple[int, str] | None = cur.fetchone()`): a column that doesn't fit is a TypeError.
// Not supported yet: row_factory/sqlite3.Row, create_function, iterating a cursor directly.
#pragma once

#include <sqlite3.h>

#include "pathlib.hpp"

namespace sd::sqlite3 {

#define SD_SQLITE_EXCEPTION(Name, Base)                                        \
    struct Name : Base {                                                         \
        using Base::Base;                                                        \
        std::string sd_type() const override { return "sqlite3." #Name; }        \
    };
SD_SQLITE_EXCEPTION(Warning, Exception)
SD_SQLITE_EXCEPTION(Error, Exception)
SD_SQLITE_EXCEPTION(InterfaceError, Error)
SD_SQLITE_EXCEPTION(DatabaseError, Error)
SD_SQLITE_EXCEPTION(DataError, DatabaseError)
SD_SQLITE_EXCEPTION(OperationalError, DatabaseError)
SD_SQLITE_EXCEPTION(IntegrityError, DatabaseError)
SD_SQLITE_EXCEPTION(InternalError, DatabaseError)
SD_SQLITE_EXCEPTION(ProgrammingError, DatabaseError)
SD_SQLITE_EXCEPTION(NotSupportedError, DatabaseError)
#undef SD_SQLITE_EXCEPTION

template <class E>
[[noreturn]] void fail(const std::string& msg) {
    throw Thrown{std::make_shared<E>(msg)};
}

// SQLite's result codes as Python's exception classes.
[[noreturn]] inline void raise_sqlite(::sqlite3* db, int code) {
    std::string msg = db ? ::sqlite3_errmsg(db) : ::sqlite3_errstr(code);
    switch (code & 0xFF) {
        case SQLITE_INTERNAL:
        case SQLITE_NOTFOUND: fail<InternalError>(msg);
        case SQLITE_NOMEM: raise("MemoryError", "");
        case SQLITE_ERROR:
        case SQLITE_PERM:
        case SQLITE_ABORT:
        case SQLITE_BUSY:
        case SQLITE_LOCKED:
        case SQLITE_READONLY:
        case SQLITE_INTERRUPT:
        case SQLITE_IOERR:
        case SQLITE_FULL:
        case SQLITE_CANTOPEN:
        case SQLITE_PROTOCOL:
        case SQLITE_EMPTY:
        case SQLITE_SCHEMA: fail<OperationalError>(msg);
        case SQLITE_CORRUPT: fail<DatabaseError>(msg);
        case SQLITE_TOOBIG: fail<DataError>(msg);
        case SQLITE_CONSTRAINT:
        case SQLITE_MISMATCH: fail<IntegrityError>(msg);
        case SQLITE_MISUSE:
        case SQLITE_RANGE: fail<InterfaceError>(msg);
        default: fail<DatabaseError>(msg);
    }
}

inline std::string version() { return SQLITE_VERSION; }

// ---- binding parameters ---------------------------------------------------------------

inline void bind_one(::sqlite3_stmt* st, int i, std::int64_t v) { ::sqlite3_bind_int64(st, i, v); }
inline void bind_one(::sqlite3_stmt* st, int i, bool v) { ::sqlite3_bind_int64(st, i, v ? 1 : 0); }
inline void bind_one(::sqlite3_stmt* st, int i, double v) { ::sqlite3_bind_double(st, i, v); }
inline void bind_one(::sqlite3_stmt* st, int i, const std::string& v) {
    ::sqlite3_bind_text64(st, i, v.data(), v.size(), SQLITE_TRANSIENT, SQLITE_UTF8);
}
inline void bind_one(::sqlite3_stmt* st, int i, const bytes& v) {
    ::sqlite3_bind_blob64(st, i, v.data.data(), v.size(), SQLITE_TRANSIENT);
}
inline void bind_one(::sqlite3_stmt* st, int i, std::nullopt_t) { ::sqlite3_bind_null(st, i); }
template <class T>
void bind_one(::sqlite3_stmt* st, int i, const std::optional<T>& v) {
    if (v) bind_one(st, i, *v);
    else ::sqlite3_bind_null(st, i);
}

inline void check_count(::sqlite3_stmt* st, std::size_t given) {
    int wanted = ::sqlite3_bind_parameter_count(st);
    if (static_cast<std::size_t>(wanted) != given)
        fail<ProgrammingError>("Incorrect number of bindings supplied. The current statement uses " + std::to_string(wanted) +
                               ", and there are " + std::to_string(given) + " supplied.");
}

template <class... Ts>
void bind_all(::sqlite3_stmt* st, const std::tuple<Ts...>& params) {
    check_count(st, sizeof...(Ts));
    int i = 0;
    std::apply([&](const auto&... p) { (bind_one(st, ++i, p), ...); }, params);
}
template <class T>
void bind_all(::sqlite3_stmt* st, const list<T>& params) {
    check_count(st, params.size());
    int i = 0;
    for (const auto& p : params) bind_one(st, ++i, p);
}
template <class T>
void bind_all(::sqlite3_stmt* st, const dict<std::string, T>& params) {  // :name / @name / $name
    int n = ::sqlite3_bind_parameter_count(st);
    for (int i = 1; i <= n; ++i) {
        const char* name = ::sqlite3_bind_parameter_name(st, i);
        if (!name) fail<ProgrammingError>("Binding " + std::to_string(i) + " has no name, but you supplied a dictionary (which has only names).");
        const T* value = params.find(std::string(name + 1));
        if (!value) fail<ProgrammingError>("You did not supply a value for binding parameter " + std::string(name) + ".");
        bind_one(st, i, *value);
    }
}
inline void bind_all(::sqlite3_stmt* st, std::nullopt_t) { check_count(st, 0); }

// ---- reading columns as the expected types --------------------------------------------

template <class T>
T column_as(::sqlite3_stmt* st, int i) {
    int kind = ::sqlite3_column_type(st, i);
    auto wrong = [&](const char* want) -> T {
        static const char* names[] = {"", "an integer", "a float", "text", "a blob", "NULL"};
        std::string name = ::sqlite3_column_name(st, i);
        std::string msg = "column " + std::to_string(i) + " (" + name + ") holds " + names[kind] + ", not " + want;
        if (kind == SQLITE_NULL) msg += "; declare it as " + std::string(want) + " | None";
        raise("TypeError", msg);
    };
    if constexpr (is_optional<T>::value) {
        if (kind == SQLITE_NULL) return std::nullopt;
        return column_as<typename T::value_type>(st, i);
    } else if constexpr (std::is_same_v<T, bool>) {
        if (kind != SQLITE_INTEGER) return wrong("bool");
        return ::sqlite3_column_int64(st, i) != 0;
    } else if constexpr (std::is_same_v<T, std::int64_t>) {
        if (kind != SQLITE_INTEGER) return wrong("int");
        return ::sqlite3_column_int64(st, i);
    } else if constexpr (std::is_same_v<T, double>) {
        if (kind != SQLITE_FLOAT && kind != SQLITE_INTEGER) return wrong("float");
        return ::sqlite3_column_double(st, i);
    } else if constexpr (std::is_same_v<T, std::string>) {
        if (kind != SQLITE_TEXT) return wrong("str");
        return std::string(reinterpret_cast<const char*>(::sqlite3_column_text(st, i)), static_cast<std::size_t>(::sqlite3_column_bytes(st, i)));
    } else {
        static_assert(std::is_same_v<T, bytes>, "a row column is int, float, str, bytes or bool, or one of those | None");
        if (kind != SQLITE_BLOB) return wrong("bytes");
        return bytes(std::string(static_cast<const char*>(::sqlite3_column_blob(st, i)), static_cast<std::size_t>(::sqlite3_column_bytes(st, i))));
    }
}

template <class Row, std::size_t... I>
Row read_row(::sqlite3_stmt* st, std::index_sequence<I...>) {
    int n = ::sqlite3_column_count(st);
    if (static_cast<std::size_t>(n) != sizeof...(I))
        raise("TypeError", "the query has " + std::to_string(n) + " columns, but the row type has " + std::to_string(sizeof...(I)));
    return Row{column_as<std::tuple_element_t<I, Row>>(st, static_cast<int>(I))...};
}

// ---- connections and cursors ------------------------------------------------------------

struct ConnState {
    ::sqlite3* db = nullptr;
    std::optional<std::string> isolation_level;  // None: autocommit; "" / "DEFERRED"...: BEGIN before DML
    ~ConnState() {
        if (db) ::sqlite3_close_v2(db);
    }
    ::sqlite3* handle() const {
        if (!db) fail<ProgrammingError>("Cannot operate on a closed database.");
        return db;
    }
    bool in_transaction() const { return db && !::sqlite3_get_autocommit(db); }
    void exec(const char* sql) {
        int rc = ::sqlite3_exec(handle(), sql, nullptr, nullptr, nullptr);
        if (rc != SQLITE_OK) raise_sqlite(db, rc);
    }
};

// The first word of a statement, uppercased: "INSERT", "SELECT"...
inline std::string first_word(const std::string& sql) {
    std::size_t i = 0;
    while (i < sql.size() && std::isspace(static_cast<unsigned char>(sql[i]))) ++i;
    std::string w;
    while (i < sql.size() && std::isalpha(static_cast<unsigned char>(sql[i]))) w += static_cast<char>(std::toupper(static_cast<unsigned char>(sql[i++])));
    return w;
}
inline bool is_dml(const std::string& word) { return word == "INSERT" || word == "UPDATE" || word == "DELETE" || word == "REPLACE"; }

class Cursor {
    struct State {
        std::shared_ptr<ConnState> conn;
        ::sqlite3_stmt* stmt = nullptr;
        bool has_row = false;  // a row is waiting to be fetched
        std::int64_t rowcount = -1;
        std::optional<std::int64_t> lastrowid;
        std::optional<list<std::tuple<std::string, std::nullopt_t, std::nullopt_t, std::nullopt_t, std::nullopt_t, std::nullopt_t, std::nullopt_t>>> description;
        ~State() { finalize(); }
        void finalize() {
            if (stmt) ::sqlite3_finalize(stmt);
            stmt = nullptr;
            has_row = false;
        }
    };
    std::shared_ptr<State> s_;

    ::sqlite3_stmt* prepare(const std::string& sql) {
        ::sqlite3* db = s_->conn->handle();
        s_->finalize();
        s_->description.reset();
        ::sqlite3_stmt* st = nullptr;
        const char* tail = nullptr;
        int rc = ::sqlite3_prepare_v2(db, sql.c_str(), static_cast<int>(sql.size()), &st, &tail);
        if (rc != SQLITE_OK) raise_sqlite(db, rc);
        for (const char* p = tail; p && *p; ++p) {
            if (!std::isspace(static_cast<unsigned char>(*p))) {
                ::sqlite3_finalize(st);
                fail<ProgrammingError>("You can only execute one statement at a time.");
            }
        }
        if (!st) fail<ProgrammingError>("You can only execute one statement at a time.");  // (an empty statement)
        s_->stmt = st;
        return st;
    }
    void begin_if_needed(const std::string& word) {
        if (s_->conn->isolation_level && is_dml(word) && !s_->conn->in_transaction())
            s_->conn->exec(("BEGIN " + *s_->conn->isolation_level).c_str());
    }
    void step_first(const std::string& word) {
        ::sqlite3_stmt* st = s_->stmt;
        int rc = ::sqlite3_step(st);
        if (rc == SQLITE_ROW) {
            s_->has_row = true;
        } else if (rc != SQLITE_DONE) {
            ::sqlite3* db = s_->conn->db;
            s_->finalize();
            raise_sqlite(db, rc);
        }
        int columns = ::sqlite3_column_count(st);
        if (columns > 0) {
            s_->description.emplace();
            for (int i = 0; i < columns; ++i)
                s_->description->push_back(std::tuple{std::string(::sqlite3_column_name(st, i)), std::nullopt, std::nullopt, std::nullopt, std::nullopt, std::nullopt, std::nullopt});
        }
        s_->rowcount = is_dml(word) ? ::sqlite3_changes(s_->conn->db) : -1;
        s_->lastrowid = ::sqlite3_last_insert_rowid(s_->conn->db);
    }

public:
    Cursor() = default;
    explicit Cursor(std::shared_ptr<ConnState> conn) : s_(std::make_shared<State>()) { s_->conn = std::move(conn); }

    template <class P>
    Cursor execute(const std::string& sql, const P& params) {
        ::sqlite3_stmt* st = prepare(sql);
        bind_all(st, params);
        std::string word = first_word(sql);
        begin_if_needed(word);
        step_first(word);
        return *this;
    }
    Cursor execute(const std::string& sql) { return execute(sql, std::nullopt); }

    template <class Seq>
    Cursor executemany(const std::string& sql, const Seq& rows) {
        ::sqlite3_stmt* st = prepare(sql);
        std::string word = first_word(sql);
        begin_if_needed(word);
        std::int64_t changes = 0;
        for (const auto& row : rows) {
            ::sqlite3_reset(st);
            ::sqlite3_clear_bindings(st);
            bind_all(st, row);
            int rc = ::sqlite3_step(st);
            if (rc == SQLITE_ROW) fail<ProgrammingError>("executemany() can only execute DML statements.");
            if (rc != SQLITE_DONE) {
                ::sqlite3* db = s_->conn->db;
                s_->finalize();
                raise_sqlite(db, rc);
            }
            changes += ::sqlite3_changes(s_->conn->db);
        }
        s_->finalize();
        s_->rowcount = is_dml(word) ? changes : -1;
        return *this;
    }

    Cursor executescript(const std::string& script) {
        s_->finalize();
        s_->description.reset();
        if (s_->conn->in_transaction()) s_->conn->exec("COMMIT");
        s_->conn->exec(script.c_str());
        s_->rowcount = -1;
        return *this;
    }

    template <class Row>
    std::optional<Row> fetchone() {
        s_->conn->handle();
        if (!s_->has_row) return std::nullopt;
        Row row = read_row<Row>(s_->stmt, std::make_index_sequence<std::tuple_size_v<Row>>());
        int rc = ::sqlite3_step(s_->stmt);
        if (rc == SQLITE_ROW) {
            s_->has_row = true;
        } else {
            s_->has_row = false;
            if (rc != SQLITE_DONE) raise_sqlite(s_->conn->db, rc);
        }
        return row;
    }
    template <class Row>
    list<Row> fetchmany(std::int64_t size = 1) {
        list<Row> out;
        for (std::int64_t k = 0; k < size; ++k) {
            auto row = fetchone<Row>();
            if (!row) break;
            out.push_back(std::move(*row));
        }
        return out;
    }
    template <class Row>
    list<Row> fetchall() {
        list<Row> out;
        while (auto row = fetchone<Row>()) out.push_back(std::move(*row));
        return out;
    }

    void close() {
        s_->finalize();
        s_->description.reset();
    }
    std::int64_t rowcount() const { return s_->rowcount; }
    std::optional<std::int64_t> lastrowid() const { return s_->lastrowid; }
    auto description() const { return s_->description; }
    std::string sd_repr() const { return "<sqlite3.Cursor object>"; }
};

class Connection {
    std::shared_ptr<ConnState> s_;

public:
    Connection() = default;
    Connection(const std::string& database, double timeout, std::optional<std::string> isolation_level) : s_(std::make_shared<ConnState>()) {
        int rc = ::sqlite3_open_v2(database.c_str(), &s_->db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_URI, nullptr);
        if (rc != SQLITE_OK) {
            ::sqlite3* db = s_->db;
            s_->db = nullptr;
            std::string msg = db ? ::sqlite3_errmsg(db) : ::sqlite3_errstr(rc);
            if (db) ::sqlite3_close_v2(db);
            fail<OperationalError>(msg);
        }
        ::sqlite3_busy_timeout(s_->db, static_cast<int>(timeout * 1000));
        if (isolation_level) {
            std::string level = str_upper(*isolation_level);
            if (level != "" && level != "DEFERRED" && level != "IMMEDIATE" && level != "EXCLUSIVE")
                raise("ValueError", "isolation_level string must be '', 'DEFERRED', 'IMMEDIATE', or 'EXCLUSIVE'");
            s_->isolation_level = level;
        }
    }
    Cursor cursor() {
        s_->handle();
        return Cursor(s_);
    }
    template <class P>
    Cursor execute(const std::string& sql, const P& params) { return cursor().execute(sql, params); }
    Cursor execute(const std::string& sql) { return cursor().execute(sql); }
    template <class Seq>
    Cursor executemany(const std::string& sql, const Seq& rows) { return cursor().executemany(sql, rows); }
    Cursor executescript(const std::string& script) { return cursor().executescript(script); }
    void commit() {
        if (s_->handle() && s_->in_transaction()) s_->exec("COMMIT");
    }
    void rollback() {
        if (s_->handle() && s_->in_transaction()) s_->exec("ROLLBACK");
    }
    void close() {
        if (s_->db) ::sqlite3_close_v2(s_->db);
        s_->db = nullptr;
    }
    bool in_transaction() const { return s_->in_transaction(); }
    std::int64_t total_changes() const { return ::sqlite3_total_changes64(s_->handle()); }
    std::optional<std::string> isolation_level() const { return s_->isolation_level; }
    // `with conn:` commits, or rolls back if the block raised (the connection stays open).
    void sd_exit(bool failed) {
        if (failed) rollback();
        else commit();
    }
    std::string sd_repr() const { return "<sqlite3.Connection object>"; }
};

inline Connection connect(const pathlib::Path& database, double timeout = 5.0, std::optional<std::string> isolation_level = std::string()) {
    return Connection(database.str(), timeout, std::move(isolation_level));
}

}  // namespace sd::sqlite3
