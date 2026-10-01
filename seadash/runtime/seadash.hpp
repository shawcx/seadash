// seadash runtime: the small library every generated program includes.
//
// Python-flavoured behaviour lives here so the code generator can stay
// simple: printing and repr(), truthiness, Python-style // and %, negative
// indexing and slicing, str/list/dict/set methods, and Python-like errors.
#pragma once

#include <algorithm>
#include <bit>
#include <coroutine>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <format>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <mutex>
#include <numbers>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>
#include <tuple>
#include <type_traits>
#include <typeinfo>
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
// OSError carries errno, strerror and filename, like Python's (None unless the OS set them).
struct OSError : Exception {
    std::optional<std::int64_t> errno_;
    std::optional<std::string> strerror, filename;
    OSError() = default;
    explicit OSError(std::string msg, std::optional<std::int64_t> err = std::nullopt, std::optional<std::string> what = std::nullopt,
                     std::optional<std::string> path = std::nullopt)
        : Exception(std::move(msg)), errno_(err), strerror(std::move(what)), filename(std::move(path)) {}
    std::string sd_type() const override { return "OSError"; }
};
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
SD_EXCEPTION(UnicodeEncodeError, UnicodeError)
SD_EXCEPTION(StopIteration, Exception)
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
    if (kind == "UnicodeEncodeError") raise<UnicodeEncodeError>(msg);
    if (kind == "UnicodeError") raise<UnicodeError>(msg);
    raise<RuntimeError>(msg);
}

// ascii(x): repr(x) with every non-ASCII character escaped (\xe9, \u2603, \U0001f600).
inline std::string ascii(const std::string& text) {
    std::string out;
    char buf[16];
    for (std::size_t i = 0; i < text.size();) {
        unsigned char c = static_cast<unsigned char>(text[i]);
        if (c < 0x80) {
            out += static_cast<char>(c);
            ++i;
            continue;
        }
        int extra = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : c >= 0xC0 ? 1 : 0;
        std::uint32_t cp = extra == 3 ? c & 0x07 : extra == 2 ? c & 0x0F : extra == 1 ? c & 0x1F : c;
        std::size_t j = i + 1;
        for (int k = 0; k < extra && j < text.size(); ++k, ++j) cp = (cp << 6) | (static_cast<unsigned char>(text[j]) & 0x3F);
        if (cp < 0x100) {
            std::snprintf(buf, sizeof buf, "\\x%02x", cp);
        } else if (cp < 0x10000) {
            std::snprintf(buf, sizeof buf, "\\u%04x", cp);
        } else {
            std::snprintf(buf, sizeof buf, "\\U%08x", cp);
        }
        out += buf;
        i = j;
    }
    return out;
}

// A generator (or iter(xs)): a coroutine that runs until its next `yield` each time a
// value is wanted. Copies share it, like Python's iterator objects, so a `for` loop
// continues where next() left off.
template <class T>
class Generator {
public:
    struct promise_type {
        std::optional<T> value;
        std::exception_ptr error;
        Generator get_return_object() { return Generator(std::coroutine_handle<promise_type>::from_promise(*this)); }
        std::suspend_always initial_suspend() noexcept { return {}; }  // nothing runs until the first value is wanted
        std::suspend_always final_suspend() noexcept { return {}; }
        std::suspend_always yield_value(T v) {
            value = std::move(v);
            return {};
        }
        void return_void() {}
        void unhandled_exception() { error = std::current_exception(); }
    };
    using handle = std::coroutine_handle<promise_type>;
    using value_type = T;

private:
    struct State {
        handle h;
        bool finished = false, running = false;
        State(handle h_) : h(h_) {}
        State(const State&) = delete;
        ~State() {
            if (h) h.destroy();
        }
    };
    std::shared_ptr<State> s_;

public:
    Generator() = default;
    explicit Generator(handle h) : s_(std::make_shared<State>(h)) {}

    // Run to the next `yield`; false once the generator has finished. An exception in
    // the generator comes out here, as in Python.
    bool advance() const {
        if (!s_ || s_->finished) return false;
        if (s_->running) raise("ValueError", "generator already executing");
        s_->running = true;
        s_->h.promise().value.reset();
        s_->h.resume();
        s_->running = false;
        if (s_->h.done()) {
            s_->finished = true;
            if (auto e = std::exchange(s_->h.promise().error, nullptr)) std::rethrow_exception(e);
            return false;
        }
        return true;
    }
    T take() const { return std::move(*s_->h.promise().value); }

    struct sentinel {};
    struct iterator {
        const Generator* g;
        std::optional<T> current;
        void fetch() {
            current.reset();
            if (g->advance()) current.emplace(g->take());
        }
        const T& operator*() const { return *current; }
        iterator& operator++() {
            fetch();
            return *this;
        }
        bool operator!=(sentinel) const { return current.has_value(); }
        bool operator==(sentinel) const { return !current.has_value(); }
    };
    iterator begin() const {
        iterator it{this, std::nullopt};
        it.fetch();
        return it;
    }
    sentinel end() const { return {}; }
    std::string sd_repr() const { return "<generator object>"; }
};

// iter(xs): an iterator over (a copy of) a collection's items.
template <class T, class L>
Generator<T> iterate(L items) {
    for (auto& x : items) co_yield T(x);
}

template <class T>
T next(const Generator<T>& g) {
    if (!g.advance()) raise<StopIteration>("");
    return g.take();
}
template <class R, class T, class D>
R next_or(const Generator<T>& g, D fallback) {  // next(it, default)
    if (!g.advance()) return R(std::move(fallback));
    return R(g.take());
}

// The current thread's name, for logging ("MainThread", or a threading.Thread's name).
inline std::string& thread_name() {
    thread_local std::string name = "MainThread";
    return name;
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

// ============================================================================
// Allocation: list/dict/set handles are made and freed a lot, so their blocks come from
// a per-thread free list (capped: a block freed on another thread just joins that
// thread's list, and past the cap goes back to the heap).
// ============================================================================

template <std::size_t N>
struct BlockPool {
    struct Node {
        Node* next;
    };
    struct FreeList {
        Node* head = nullptr;
        std::size_t count = 0;
        ~FreeList() {
            while (head) {
                Node* n = head;
                head = n->next;
                ::operator delete(n);
            }
        }
    };
    static constexpr std::size_t size = N < sizeof(Node) ? sizeof(Node) : N;
    static constexpr std::size_t cap = 4096;
    static FreeList& free_list() {
        thread_local FreeList list;
        return list;
    }
    static void* get() {
        FreeList& f = free_list();
        if (Node* n = f.head) {
            f.head = n->next;
            --f.count;
            return n;
        }
        return ::operator new(size);
    }
    static void put(void* p) {
        FreeList& f = free_list();
        if (f.count >= cap) {
            ::operator delete(p);
            return;
        }
        Node* n = static_cast<Node*>(p);
        n->next = f.head;
        f.head = n;
        ++f.count;
    }
};

template <class T>
struct PoolAlloc {
    using value_type = T;
    PoolAlloc() = default;
    template <class U>
    PoolAlloc(const PoolAlloc<U>&) {}
    T* allocate(std::size_t n) {
        return n == 1 ? static_cast<T*>(BlockPool<sizeof(T)>::get()) : static_cast<T*>(::operator new(n * sizeof(T)));
    }
    void deallocate(T* p, std::size_t n) {
        if (n == 1)
            BlockPool<sizeof(T)>::put(p);
        else
            ::operator delete(p);
    }
    template <class U>
    bool operator==(const PoolAlloc<U>&) const { return true; }
};

// A variable declared before its first assignment (codegen declares locals up front): a
// list/dict/set handle made with `unset` has no storage until it's used, which the checker
// guarantees comes after an assignment. (It saves an allocation per list variable.)
struct unset_t {};
inline constexpr unset_t unset{};

// std::make_shared, with the block from the pool.
template <class T, class... A>
std::shared_ptr<T> make_pooled(A&&... a) {
    return std::allocate_shared<T>(PoolAlloc<T>{}, std::forward<A>(a)...);
}

// ============================================================================
// list: a shared reference to a vector, like a Python list
// ============================================================================
//
// Copying a list shares it (b = a, passing it to a function); copy() makes a new
// one. The handle is "shallowly const": a const list can still be changed, the
// way a Python list passed to a function can. Its begin()/end() are the vector's,
// for the runtime's own use; `for x in xs` goes through iter(), which tolerates
// the list changing during the loop (list_range).

template <class T>
class list {
public:
    using value_type = std::conditional_t<std::is_same_v<T, bool>, Bool, T>;
    using vector_type = std::vector<value_type>;
    using iterator = typename vector_type::iterator;
    using const_iterator = iterator;
    using reverse_iterator = typename vector_type::reverse_iterator;
    using size_type = std::size_t;
    using reference = value_type&;
    using const_reference = value_type&;

private:
    // (null only after being moved from; vec() then starts a new, empty list)
    mutable std::shared_ptr<vector_type> p_;
    template <class U> friend class list;

public:
    list() : p_(make_pooled<vector_type>()) {}
    list(unset_t) {}
    list(std::initializer_list<value_type> init) : p_(make_pooled<vector_type>(init)) {}
    list(vector_type v) : p_(make_pooled<vector_type>(std::move(v))) {}  // (runtime helpers build vectors)
    explicit list(size_type n, const value_type& x = value_type()) : p_(make_pooled<vector_type>(n, x)) {}
    template <class It>
        requires(!std::is_integral_v<It>)
    list(It first, It last) : p_(make_pooled<vector_type>(first, last)) {}
    // list[bool] and list[Bool] store the same thing: share it.
    template <class U>
        requires(!std::is_same_v<U, T> && std::is_same_v<typename list<U>::vector_type, vector_type>)
    list(const list<U>& o) : p_((o.vec(), o.p_)) {}

    vector_type& vec() const {
        if (!p_) [[unlikely]]
            p_ = make_pooled<vector_type>();
        return *p_;
    }
    operator vector_type&() const { return vec(); }
    list copy() const { return list(vec()); }
    using sd_is_handle = void;
    bool sd_unique() const { return p_.use_count() <= 1; }
    bool is(const list& o) const { return &vec() == &o.vec(); }
    const void* identity() const { return &vec(); }

    size_type size() const { return vec().size(); }
    bool empty() const { return vec().empty(); }
    value_type& operator[](size_type i) const { return (vec())[i]; }
    value_type& front() const { return vec().front(); }
    value_type& back() const { return vec().back(); }
    value_type* data() const { return vec().data(); }
    iterator begin() const { return vec().begin(); }
    iterator end() const { return vec().end(); }
    reverse_iterator rbegin() const { return vec().rbegin(); }
    reverse_iterator rend() const { return vec().rend(); }
    size_type capacity() const { return vec().capacity(); }

    template <class X>
    void push_back(X&& x) const { vec().push_back(std::forward<X>(x)); }
    template <class... A>
    value_type& emplace_back(A&&... a) const { return vec().emplace_back(std::forward<A>(a)...); }
    void pop_back() const { vec().pop_back(); }
    template <class... A>
    iterator insert(iterator pos, A&&... a) const { return vec().insert(pos, std::forward<A>(a)...); }
    iterator erase(iterator pos) const { return vec().erase(pos); }
    iterator erase(iterator first, iterator last) const { return vec().erase(first, last); }
    void clear() const { vec().clear(); }
    void reserve(size_type n) const { vec().reserve(n); }
    void resize(size_type n) const { vec().resize(n); }
    void resize(size_type n, const value_type& x) const { vec().resize(n, x); }
    template <class... A>
    void assign(A&&... a) const { vec().assign(std::forward<A>(a)...); }

    friend bool operator==(const list& a, const list& b) { return a.vec() == b.vec(); }
    friend auto operator<=>(const list& a, const list& b) { return a.vec() <=> b.vec(); }
};

// `for x in xs` over a list: by index, holding the list, so the loop sees items
// appended during it and nothing dangles if the list grows or is rebound.
template <class T>
struct list_range {
    list<T> xs;
    struct sentinel {};
    struct iterator {
        typename list<T>::vector_type* v;
        std::size_t i;
        typename list<T>::value_type& operator*() const { return (*v)[i]; }
        iterator& operator++() {
            ++i;
            return *this;
        }
        bool operator!=(sentinel) const { return i < v->size(); }
        bool operator==(sentinel) const { return i >= v->size(); }
    };
    iterator begin() const { return {&xs.vec(), 0}; }
    sentinel end() const { return {}; }
    std::size_t size() const { return xs.size(); }
};

struct bytes;
template <class T>
class hash_set;  // (below, after dict)

// Can a set of T be kept sorted, with `<` agreeing with `==`? Numbers and strings can;
// user types (a class with __eq__ and __hash__) are kept by hash, like in Python.
template <class T>
struct natural_order : std::bool_constant<std::is_arithmetic_v<T> || std::is_same_v<T, std::string> ||
                                          std::is_same_v<T, bytes> || std::is_same_v<T, Bool>> {};
template <class... A>
struct natural_order<std::tuple<A...>> : std::bool_constant<(natural_order<A>::value && ...)> {};
template <class T>
constexpr bool naturally_ordered() {
    return natural_order<T>::value;
}

// A set: a shared reference to its items, like list. Numbers and strings are kept in a
// std::set (so they print sorted); anything else by hash and ==, in insertion order.
template <class T>
class set {
public:
    using value_type = T;
    using set_type = std::conditional_t<naturally_ordered<T>(), std::set<T>, hash_set<T>>;
    using iterator = typename set_type::const_iterator;
    using const_iterator = iterator;
    using size_type = std::size_t;

private:
    mutable std::shared_ptr<set_type> p_;  // (null only after being moved from)

public:
    set() : p_(make_pooled<set_type>()) {}
    set(unset_t) {}
    set(std::initializer_list<T> init) : p_(make_pooled<set_type>(init)) {}
    set(set_type s) : p_(make_pooled<set_type>(std::move(s))) {}
    template <class It>
    set(It first, It last) : p_(make_pooled<set_type>(first, last)) {}

    set_type& std_set() const {
        if (!p_) [[unlikely]]
            p_ = make_pooled<set_type>();
        return *p_;
    }
    operator set_type&() const { return std_set(); }
    set copy() const { return set(std_set()); }
    using sd_is_handle = void;
    bool sd_unique() const { return p_.use_count() <= 1; }
    bool is(const set& o) const { return &std_set() == &o.std_set(); }
    const void* identity() const { return &std_set(); }

    size_type size() const { return std_set().size(); }
    bool empty() const { return std_set().empty(); }
    iterator begin() const { return std_set().begin(); }
    iterator end() const { return std_set().end(); }
    iterator find(const T& x) const { return std_set().find(x); }
    size_type count(const T& x) const { return std_set().count(x); }
    bool contains(const T& x) const { return std_set().contains(x); }
    template <class X>
    auto insert(X&& x) const { return std_set().insert(std::forward<X>(x)); }
    template <class It>
    void insert(It first, It last) const { std_set().insert(first, last); }
    iterator insert(iterator hint, const T& x) const { return std_set().insert(hint, x); }
    size_type erase(const T& x) const { return std_set().erase(x); }
    iterator erase(iterator it) const { return std_set().erase(it); }
    void clear() const { std_set().clear(); }

    friend bool operator==(const set& a, const set& b) { return a.std_set() == b.std_set(); }
};

// `for x in s`: raises, as Python does, if the set changes size during the loop
// (a std::set iterator would dangle if its item were removed).
template <class T>
struct set_range {
    set<T> s;
    struct sentinel {};
    struct iterator {
        const typename set<T>::set_type* p;
        typename set<T>::set_type::const_iterator it;
        std::size_t size;
        const T& operator*() const { return *it; }
        iterator& operator++() {
            if (p->size() != size) raise("RuntimeError", "Set changed size during iteration");
            ++it;
            return *this;
        }
        bool operator!=(sentinel) const { return it != p->end(); }
        bool operator==(sentinel) const { return it == p->end(); }
    };
    iterator begin() const { return {&s.std_set(), s.std_set().begin(), s.size()}; }
    sentinel end() const { return {}; }
    std::size_t size() const { return s.size(); }
};

template <class K, class V>
class dict;

template <class T> struct is_vector : std::false_type {};
template <class T, class A> struct is_vector<std::vector<T, A>> : std::true_type {};
template <class T> struct is_vector<list<T>> : std::true_type {};
template <class T> struct is_list : std::false_type {};
template <class T> struct is_list<list<T>> : std::true_type {};
template <class L> struct list_elem;
template <class T> struct list_elem<list<T>> { using type = T; };
template <class T> struct is_set : std::false_type {};
template <class T, class C, class A> struct is_set<std::set<T, C, A>> : std::true_type {};
template <class T> struct is_set<set<T>> : std::true_type {};
template <class T> struct is_sd_set : std::false_type {};
template <class T> struct is_sd_set<set<T>> : std::true_type {};
template <class T> struct is_dict : std::false_type {};
template <class K, class V> struct is_dict<dict<K, V>> : std::true_type {};
// A dict or a type built on one (collections.defaultdict / Counter).
template <class K, class V> std::true_type dict_base_test(const dict<K, V>*);
std::false_type dict_base_test(...);
template <class T> struct is_dict_like : decltype(dict_base_test(std::declval<T*>())) {};
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
    // Shared by every copy of the handle: a dict is a reference, like in Python.
    struct Data {
        std::vector<std::pair<K, V>> items;
        std::vector<std::size_t> hashes;
        std::vector<char> alive;
        std::vector<std::int32_t> table;  // entry index, or EMPTY / DELETED
        std::size_t live = 0;
    };
    mutable std::shared_ptr<Data> p_ = make_pooled<Data>();  // (null only after a move)
    Data& data() const {
        if (!p_) [[unlikely]]
            p_ = make_pooled<Data>();
        return *p_;
    }

    static constexpr std::int32_t EMPTY = -1, DELETED = -2;

    static std::size_t hash_of(const K& k) {
        std::size_t h = Hash{}(k);
        h ^= h >> 33;  // mix: std::hash of an int is the int itself
        h *= 0xff51afd7ed558ccdULL;
        h ^= h >> 33;
        return h;
    }

    // Index of k in data().items, or -1. `insert_at` gets the table slot where k would go.
    std::int64_t lookup(const K& k, std::size_t h, std::size_t* insert_at = nullptr) const {
        Data& D = data();
        if (D.table.empty()) return -1;
        std::size_t mask = D.table.size() - 1;
        std::size_t first_deleted = SIZE_MAX;
        for (std::size_t i = h & mask;; i = (i + 1) & mask) {
            std::int32_t slot = D.table[i];
            if (slot == EMPTY) {
                if (insert_at) *insert_at = first_deleted != SIZE_MAX ? first_deleted : i;
                return -1;
            }
            if (slot == DELETED) {
                if (first_deleted == SIZE_MAX) first_deleted = i;
            } else if (D.hashes[slot] == h && D.items[slot].first == k) {
                return slot;
            }
        }
    }

    void rebuild(std::size_t capacity) const {
        Data& D = data();
        if (D.live != D.items.size()) {  // compact out deleted entries
            std::size_t j = 0;
            for (std::size_t i = 0; i < D.items.size(); ++i) {
                if (!D.alive[i]) continue;
                if (i != j) {
                    D.items[j] = std::move(D.items[i]);
                    D.hashes[j] = D.hashes[i];
                }
                ++j;
            }
            D.items.resize(j);
            D.hashes.resize(j);
            D.alive.assign(j, 1);
        }
        std::size_t size = 8;
        while (size * 2 < capacity * 3) size *= 2;  // keep the load factor under 2/3
        D.table.assign(size, EMPTY);
        std::size_t mask = size - 1;
        for (std::size_t n = 0; n < D.items.size(); ++n) {
            std::size_t i = D.hashes[n] & mask;
            while (D.table[i] != EMPTY) i = (i + 1) & mask;
            D.table[i] = static_cast<std::int32_t>(n);
        }
    }

    std::size_t insert_new(K key, V value, std::size_t h) const {
        Data& D = data();
        if ((D.items.size() + 1) * 3 >= D.table.size() * 2) rebuild(D.live + 1 > 4 ? (D.live + 1) * 2 : 8);
        std::size_t at = 0;
        lookup(key, h, &at);
        std::size_t n = D.items.size();
        D.items.emplace_back(std::move(key), std::move(value));
        D.hashes.push_back(h);
        D.alive.push_back(1);
        D.table[at] = static_cast<std::int32_t>(n);
        ++D.live;
        return n;
    }

public:
    dict() = default;
    dict(unset_t) : p_() {}
    dict(std::initializer_list<std::pair<K, V>> init) {
        for (const auto& [k, v] : init) (*this)[k] = v;
    }

    // A new dict with the same items (dict(d), d.copy()); copying the handle shares it.
    dict copy() const {
        dict out;
        out.data() = data();
        return out;
    }
    bool is(const dict& o) const { return &data() == &o.data(); }
    using sd_is_handle = void;
    using mapped_type = V;
    bool sd_unique() const { return p_.use_count() <= 1; }
    const void* identity() const { return &data(); }
    // `for k in d`: the keys, raising (as Python does) if the dict changes size meanwhile.
    struct key_range {
        std::shared_ptr<Data> p;
        struct sentinel {};
        struct iterator {
            Data* d;
            std::size_t i, size;
            void skip() {
                while (i < d->items.size() && !d->alive[i]) ++i;
            }
            const K& operator*() const { return d->items[i].first; }
            iterator& operator++() {
                if (d->live != size) raise("RuntimeError", "dictionary changed size during iteration");
                ++i;
                skip();
                return *this;
            }
            bool operator!=(sentinel) const { return i < d->items.size(); }
        };
        iterator begin() const {
            iterator it{p.get(), 0, p->live};
            it.skip();
            return it;
        }
        sentinel end() const { return {}; }
        std::size_t size() const { return p->live; }
    };
    key_range sd_keys() const { return {(data(), p_)}; }

    // For value_copy: stop sharing (a Counter or defaultdict keeps its own type).
    void detach() { p_ = make_pooled<Data>(data()); }
    template <class F>
    void for_each_value(F f) const {
        Data& D = data();
        for (std::size_t i = 0; i < D.items.size(); ++i)
            if (D.alive[i]) f(D.items[i].second);
    }

    // d[k] = v : inserts if missing (assignment)
    V& operator[](const K& k) const {
        Data& D = data();
        std::size_t h = hash_of(k);
        std::int64_t i = lookup(k, h);
        if (i >= 0) return D.items[i].second;
        return D.items[insert_new(k, V{}, h)].second;
    }

    // d[k] as a value: KeyError if missing
    V& at(const K& k) const {
        Data& D = data();
        std::int64_t i = lookup(k, hash_of(k));
        if (i < 0) raise("KeyError", repr(k));
        return D.items[i].second;
    }

    const V* find(const K& k) const {
        Data& D = data();
        std::int64_t i = lookup(k, hash_of(k));
        return i < 0 ? nullptr : &D.items[i].second;
    }

    bool contains(const K& k) const { return lookup(k, hash_of(k)) >= 0; }
    std::size_t size() const { return data().live; }
    bool empty() const { return data().live == 0; }
    void clear() const {
        Data& D = data();
        D.items.clear();
        D.hashes.clear();
        D.alive.clear();
        D.table.clear();
        D.live = 0;
    }

    bool erase(const K& k) const {
        Data& D = data();
        std::size_t h = hash_of(k);
        std::size_t mask = D.table.empty() ? 0 : D.table.size() - 1;
        if (D.table.empty()) return false;
        for (std::size_t i = h & mask;; i = (i + 1) & mask) {
            std::int32_t slot = D.table[i];
            if (slot == EMPTY) return false;
            if (slot >= 0 && D.hashes[slot] == h && D.items[slot].first == k) {
                D.table[i] = DELETED;
                D.alive[slot] = 0;
                D.items[slot] = std::pair<K, V>{};  // release the memory now
                --D.live;
                return true;
            }
        }
    }

    // d.popitem(): removes and returns the last item added.
    std::tuple<K, V> popitem() const {
        Data& D = data();
        for (std::size_t i = D.items.size(); i-- > 0;) {
            if (!D.alive[i]) continue;
            std::tuple<K, V> out(D.items[i].first, D.items[i].second);
            erase(std::get<0>(out));
            return out;
        }
        raise("KeyError", "'popitem(): dictionary is empty'");
    }

    // Iteration skips deleted entries; yields (key, value) pairs in insertion order.
    struct const_iterator {
        const Data* d;
        std::size_t i;
        void skip() {
            while (i < d->items.size() && !d->alive[i]) ++i;
        }
        const std::pair<K, V>& operator*() const { return d->items[i]; }
        const std::pair<K, V>* operator->() const { return &d->items[i]; }
        const_iterator& operator++() {
            ++i;
            skip();
            return *this;
        }
        bool operator!=(const const_iterator& o) const { return i != o.i; }
        bool operator==(const const_iterator& o) const { return i == o.i; }
    };
    const_iterator begin() const {
        const_iterator it{&data(), 0};
        it.skip();
        return it;
    }
    const_iterator end() const { return {&data(), data().items.size()}; }

    std::vector<K> keys() const {
        std::vector<K> out;
        out.reserve(data().live);
        for (const auto& [k, v] : *this) out.push_back(k);
        return out;
    }
    list<V> values() const {
        list<V> out;
        out.reserve(data().live);
        for (const auto& [k, v] : *this) out.push_back(v);
        return out;
    }
    std::vector<std::tuple<K, V>> items() const {
        std::vector<std::tuple<K, V>> out;
        out.reserve(data().live);
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
// value_copy: a copy sharing nothing that can change
// ============================================================================
//
// Lists, dicts and sets are references; value_copy copies them, and what's inside
// them, all the way down. Structs copy themselves (their copy constructors
// value_copy their fields). Class instances and thread-safe objects are shared.
// Used where seadash promises a copy: struct fields, and values crossing into
// another thread.

// The storage of a set whose items aren't naturally ordered: a dict's keys (hash and ==,
// insertion order), with std::set's interface.
template <class T>
class hash_set {
    dict<T, char> d_;

public:
    using value_type = T;
    using key_type = T;
    struct const_iterator {
        typename dict<T, char>::const_iterator it;
        using iterator_category = std::forward_iterator_tag;
        using value_type = T;
        using difference_type = std::ptrdiff_t;
        using pointer = const T*;
        using reference = const T&;
        const T& operator*() const { return it->first; }
        const T* operator->() const { return &it->first; }
        const_iterator& operator++() {
            ++it;
            return *this;
        }
        const_iterator operator++(int) {
            const_iterator old = *this;
            ++it;
            return old;
        }
        bool operator==(const const_iterator& o) const { return it == o.it; }
        bool operator!=(const const_iterator& o) const { return it != o.it; }
    };
    using iterator = const_iterator;

    hash_set() = default;
    hash_set(const hash_set& o) : d_(o.d_.copy()) {}
    hash_set(hash_set&&) = default;
    hash_set& operator=(const hash_set& o) {
        d_ = o.d_.copy();
        return *this;
    }
    hash_set& operator=(hash_set&&) = default;
    hash_set(std::initializer_list<T> init) {
        for (const auto& x : init) insert(x);
    }
    template <class It>
    hash_set(It first, It last) {
        insert(first, last);
    }

    std::size_t size() const { return d_.size(); }
    bool empty() const { return d_.empty(); }
    const_iterator begin() const { return {d_.begin()}; }
    const_iterator end() const { return {d_.end()}; }
    bool contains(const T& x) const { return d_.contains(x); }
    std::size_t count(const T& x) const { return d_.contains(x) ? 1 : 0; }
    const_iterator find(const T& x) const {
        for (auto it = begin(); it != end(); ++it)
            if (*it == x) return it;
        return end();
    }
    std::pair<const_iterator, bool> insert(const T& x) {
        if (d_.contains(x)) return {find(x), false};
        d_[x] = 1;
        return {find(x), true};
    }
    const_iterator insert(const_iterator, const T& x) { return insert(x).first; }
    template <class It>
    void insert(It first, It last) {
        for (; first != last; ++first) insert(*first);
    }
    std::size_t erase(const T& x) { return d_.erase(x) ? 1 : 0; }
    const_iterator erase(const_iterator it) {
        const_iterator next = it;
        ++next;  // (erasing leaves a tombstone: the next position stays valid)
        d_.erase(*it);
        return next;
    }
    void clear() { d_.clear(); }
    bool operator==(const hash_set& o) const {
        if (size() != o.size()) return false;
        for (const auto& x : *this)
            if (!o.contains(x)) return false;
        return true;
    }
};

// Does a T hold lists, dicts or sets (so copying it must copy them)?
template <class T>
constexpr bool holds_handles() {
    if constexpr (requires { typename T::sd_is_handle; }) {
        return true;
    } else if constexpr (is_optional<T>::value) {
        return holds_handles<typename T::value_type>();
    } else if constexpr (is_tuple<T>::value) {
        return []<class... A>(std::tuple<A...>*) { return (holds_handles<A>() || ...); }(static_cast<T*>(nullptr));
    } else {
        return false;
    }
}

// What value_copy has copied so far, so that two references to one list come out as two
// references to one new list (like copy.deepcopy).
struct CopyMemo {
    std::unordered_map<const void*, std::shared_ptr<void>> done;
    template <class T>
    const T* find(const T& x) const {
        auto it = done.find(x.identity());
        return it == done.end() ? nullptr : static_cast<const T*>(it->second.get());
    }
    template <class T>
    void add(const T& original, const T& copy) {
        done.emplace(original.identity(), std::make_shared<T>(copy));
    }
};

template <class T>
T value_copy(const T& x, CopyMemo& memo) {
    if constexpr (requires { x.sd_value_copy(memo); }) {
        return x.sd_value_copy(memo);
    } else if constexpr (is_list<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        T out;
        memo.add(x, out);
        out.reserve(x.size());
        for (const auto& e : x) out.push_back(value_copy(e, memo));
        return out;
    } else if constexpr (is_sd_set<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        T out = x.copy();  // (items are hashable, so never lists)
        memo.add(x, out);
        return out;
    } else if constexpr (is_dict_like<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        T out = x;
        out.detach();
        memo.add(x, out);
        out.for_each_value([&](auto& v) { v = value_copy(v, memo); });
        return out;
    } else if constexpr (is_optional<T>::value) {
        return x ? T(value_copy(*x, memo)) : T();
    } else if constexpr (is_tuple<T>::value) {
        return std::apply([&](const auto&... e) { return T(value_copy(e, memo)...); }, x);
    } else {
        return x;  // a struct copies itself; a class instance is shared
    }
}

template <class T>
T value_copy(const T& x) {
    if constexpr (is_list<T>::value) {
        if constexpr (!holds_handles<typename T::value_type>()) {
            return x.copy();  // the common case: a flat list, nothing inside can be shared
        } else {
            CopyMemo memo;
            return value_copy(x, memo);
        }
    } else if constexpr (holds_handles<T>()) {
        CopyMemo memo;
        return value_copy(x, memo);
    } else {
        return x;
    }
}

// Is x the only reference to everything in it (so it can be handed to another thread
// without copying)? Structs say no: they copy themselves.
template <class T>
bool exclusive(const T& x) {
    if constexpr (requires { typename T::sd_is_handle; }) {
        if (!x.sd_unique()) return false;
        if constexpr (is_list<T>::value) {
            if constexpr (holds_handles<typename T::value_type>())
                for (const auto& e : x)
                    if (!exclusive(e)) return false;
        } else if constexpr (is_dict_like<T>::value) {
            if constexpr (holds_handles<typename T::mapped_type>()) {
                bool ok = true;
                x.for_each_value([&](const auto& v) { ok = ok && exclusive(v); });
                return ok;
            }
        } else if constexpr (requires { x.sd_exclusive_items(); }) {
            return x.sd_exclusive_items();
        }
        return true;
    } else if constexpr (is_optional<T>::value) {
        return !x || exclusive(*x);
    } else if constexpr (is_tuple<T>::value) {
        return std::apply([](const auto&... e) { return (exclusive(e) && ...); }, x);
    } else {
        return std::is_arithmetic_v<T> || std::is_same_v<T, std::string> || std::is_same_v<T, bytes> || is_shared<T>::value;
    }
}

// Crossing into another thread: the value itself when nothing else can reach it (the
// sender's last use of a list it built), otherwise a copy.
template <class T>
std::remove_cvref_t<T> send(T&& x) {
    using U = std::remove_cvref_t<T>;
    if constexpr (std::is_rvalue_reference_v<T&&> && !std::is_const_v<std::remove_reference_t<T>>) {
        if (exclusive(x)) return U(std::move(x));
    }
    return value_copy(static_cast<const U&>(x));
}

// What the compiler shares with a pool task instead of copying, because it has checked that
// nothing changes it until the task is done (inside `with ThreadPoolExecutor() as pool:`).
template <class T>
struct Lent {
    T value;
    operator const T&() const { return value; }
};
template <class T>
Lent<std::remove_cvref_t<T>> lend(T&& x) {
    return {std::forward<T>(x)};
}
template <class T>
Lent<T> send(Lent<T> x) {
    return x;
}

// xs *= 2, s |= t: Python changes the container in place (every reference to it sees
// the change), so the result replaces its contents rather than rebinding the name.
template <class T>
void assign_contents(const T& target, T&& value) {
    if constexpr (is_list<T>::value) {
        target.vec() = std::move(value.vec());
    } else {
        target.std_set() = std::move(value.std_set());
    }
}

// xs.copy(), dict(d), d.copy(): a new container holding the same items. (A Counter or
// defaultdict stays one.)
template <class T>
T shallow_copy(const T& x) {
    if constexpr (is_dict_like<T>::value) {
        T out = x;
        out.detach();
        return out;
    } else {
        return x.copy();
    }
}

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
    std::string type = sd_type();  // a module's exception shows its bare name: error('...'), not zlib.error('...')
    return type.substr(type.rfind('.') + 1) + "(" + (message.empty() ? "" : repr_str(message)) + ")";
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
// int(s, base), by Python's rules: a sign, then with base 16, 8 or 2 an optional 0x, 0o or 0b,
// then digits with single underscores between them. Base 0 reads the base from the prefix,
// as in source code (so "017" is an error).
inline std::int64_t to_int(const std::string& s, std::int64_t base) {
    if (base != 0 && (base < 2 || base > 36)) raise("ValueError", "int() base must be >= 2 and <= 36, or 0");
    auto bad = [&] { raise("ValueError", "invalid literal for int() with base " + std::to_string(base) + ": " + repr_str(s)); };
    std::string_view v = strip_view(s);
    bool negative = !v.empty() && v[0] == '-';
    if (!v.empty() && (v[0] == '+' || v[0] == '-')) v.remove_prefix(1);
    auto prefixed = [&](char letter) { return v.size() >= 2 && v[0] == '0' && (v[1] | 0x20) == letter; };
    std::uint64_t b = static_cast<std::uint64_t>(base);
    bool only_zero = false;  // base 0 and a leading 0 with no prefix: "0" and "00" are fine, "017" isn't
    if (base == 0) {
        b = prefixed('x') ? 16 : prefixed('o') ? 8 : prefixed('b') ? 2 : 10;
        only_zero = b == 10 && !v.empty() && v[0] == '0';
    }
    if ((b == 16 && prefixed('x')) || (b == 8 && prefixed('o')) || (b == 2 && prefixed('b'))) {
        v.remove_prefix(2);
        if (!v.empty() && v[0] == '_') v.remove_prefix(1);  // 0x_ff
    }
    if (v.empty() || v.front() == '_' || v.back() == '_') bad();
    std::uint64_t magnitude = 0;
    bool overflow = false;
    char previous = 0;
    for (char c : v) {
        if (c == '_') {
            if (previous == '_') bad();
        } else {
            std::uint64_t digit = c >= '0' && c <= '9' ? c - '0' : (c | 0x20) >= 'a' && (c | 0x20) <= 'z' ? (c | 0x20) - 'a' + 10 : 99;
            if (digit >= b) bad();
            overflow = overflow || __builtin_mul_overflow(magnitude, b, &magnitude) || __builtin_add_overflow(magnitude, digit, &magnitude);
        }
        previous = c;
    }
    if (only_zero && (magnitude != 0 || overflow)) bad();
    const std::uint64_t limit = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) + (negative ? 1 : 0);
    if (overflow || magnitude > limit) raise("OverflowError", "int() result doesn't fit in 64 bits: " + repr_str(s));
    return negative ? static_cast<std::int64_t>(0 - magnitude) : static_cast<std::int64_t>(magnitude);
}
inline std::int64_t to_int(const std::string& s) { return to_int(s, 10); }
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
// (The floor of the quotient: 1.0 // 0.1 is 10.0. Python works from the remainder and gives 9.0.)
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
inline std::tuple<std::int64_t, std::int64_t> divmod(std::int64_t a, std::int64_t b) {
    return {floordiv(a, b), mod(a, b)};
}
inline std::tuple<double, double> divmod(double a, double b) {
    if (b == 0) raise("ZeroDivisionError", "float divmod()");
    return {floordiv(a, b), mod(a, b)};
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
// pow(base, exp, mod): exponentiation by squaring, with Python's sign rules for %.
inline std::int64_t powmod(std::int64_t base, std::int64_t exp, std::int64_t mod) {
    if (mod == 0) raise("ValueError", "pow() 3rd argument cannot be 0");
    if (exp < 0) raise("ValueError", "base is not invertible for the given modulus");
    __int128 m = mod, result = 1 % m, b = ((base % m) + m) % m;
    for (; exp > 0; exp >>= 1) {
        if (exp & 1) result = result * b % m;
        b = b * b % m;
    }
    std::int64_t r = static_cast<std::int64_t>(result);
    return (r != 0 && ((r < 0) != (mod < 0))) ? r + mod : r;
}

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
inline std::int64_t round(std::int64_t x, std::int64_t digits) {  // round(1250, -2) is 1200: half to even
    if (digits >= 0) return x;
    if (digits < -18) return 0;
    std::int64_t unit = 1;
    for (std::int64_t i = 0; i < -digits; ++i) unit *= 10;
    std::int64_t q = floordiv(x, unit), r = mod(x, unit);
    if (r > unit - r || (r == unit - r && (q & 1))) ++q;
    return q * unit;
}

// bin(), oct(), hex(): the sign, the prefix, then the digits.
inline std::string int_digits(std::int64_t x, int base, const char* prefix) {
    char buf[72];
    std::uint64_t magnitude = x < 0 ? 0 - static_cast<std::uint64_t>(x) : static_cast<std::uint64_t>(x);
    auto end = std::to_chars(buf, buf + sizeof buf, magnitude, base).ptr;
    return std::string(x < 0 ? "-" : "") + prefix + std::string(buf, end);
}
inline std::string bin(std::int64_t x) { return int_digits(x, 2, "0b"); }
inline std::string oct(std::int64_t x) { return int_digits(x, 8, "0o"); }
inline std::string hex(std::int64_t x) { return int_digits(x, 16, "0x"); }

// ---- methods of int and float ----

inline std::int64_t int_bit_length(std::int64_t x) {
    return std::bit_width(x < 0 ? 0 - static_cast<std::uint64_t>(x) : static_cast<std::uint64_t>(x));
}
inline std::int64_t int_bit_count(std::int64_t x) {
    return std::popcount(x < 0 ? 0 - static_cast<std::uint64_t>(x) : static_cast<std::uint64_t>(x));
}
inline bool int_is_integer(std::int64_t) { return true; }
inline std::tuple<std::int64_t, std::int64_t> int_as_integer_ratio(std::int64_t x) { return {x, 1}; }
inline bool little_endian(const std::string& byteorder) {
    if (byteorder != "little" && byteorder != "big") raise("ValueError", "byteorder must be either 'little' or 'big'");
    return byteorder == "little";
}
inline bytes int_to_bytes(std::int64_t x, std::int64_t length, const std::string& byteorder, bool is_signed) {
    bool little = little_endian(byteorder);
    if (length < 0) raise("ValueError", "length argument must be non-negative");
    if (x < 0 && !is_signed) raise("OverflowError", "can't convert negative int to unsigned");
    if (length < 8) {  // (8 bytes hold any int)
        int bits = static_cast<int>(length) * 8;
        bool fits = bits == 0 ? x == 0 || (is_signed && x == -1)  // (CPython lets -1 through)
                  : is_signed ? x >= -(std::int64_t(1) << (bits - 1)) && x < (std::int64_t(1) << (bits - 1))
                              : x < (std::int64_t(1) << bits);
        if (!fits) raise("OverflowError", "int too big to convert");
    }
    std::string out(static_cast<std::size_t>(length), x < 0 ? '\xff' : '\0');
    auto value = static_cast<std::uint64_t>(x);
    for (std::size_t i = 0; i < out.size() && i < 8; ++i, value >>= 8)
        out[little ? i : out.size() - 1 - i] = static_cast<char>(value & 0xff);
    return bytes(std::move(out));
}
inline std::int64_t int_from_bytes(const bytes& b, const std::string& byteorder, bool is_signed) {
    std::string data = b.data;
    if (little_endian(byteorder)) std::reverse(data.begin(), data.end());  // now the most significant byte is first
    bool negative = is_signed && !data.empty() && (data[0] & 0x80);
    char pad = negative ? '\xff' : '\0';
    std::size_t skip = 0;
    while (data.size() - skip > 8 && data[skip] == pad) ++skip;
    std::uint64_t value = negative ? ~std::uint64_t(0) : 0;
    for (std::size_t i = skip; i < data.size(); ++i) value = (value << 8) | static_cast<unsigned char>(data[i]);
    if (data.size() - skip > 8 || negative != (static_cast<std::int64_t>(value) < 0))
        raise("OverflowError", "int.from_bytes() result doesn't fit in 64 bits");
    return static_cast<std::int64_t>(value);
}

inline bool float_is_integer(double x) { return std::isfinite(x) && std::floor(x) == x; }
inline std::string float_hex(double x) {  // 0x1.8000000000000p+1, as CPython writes it
    if (std::isnan(x)) return "nan";
    if (std::isinf(x)) return x < 0 ? "-inf" : "inf";
    std::string sign = std::signbit(x) ? "-" : "";
    if (x == 0) return sign + "0x0.0p+0";
    int e = 0;
    double m = std::frexp(std::fabs(x), &e);
    int shift = 1 - std::max(std::numeric_limits<double>::min_exponent - e, 0);
    m = std::ldexp(m, shift);
    e -= shift;
    static const char* digits = "0123456789abcdef";
    std::string out = sign + "0x";
    out += digits[static_cast<int>(m)];
    m -= static_cast<int>(m);
    out += '.';
    for (int i = 0; i < 13; ++i) {
        m *= 16.0;
        out += digits[static_cast<int>(m)];
        m -= static_cast<int>(m);
    }
    return out + "p" + (e < 0 ? "-" : "+") + std::to_string(e < 0 ? -e : e);
}
inline double float_fromhex(const std::string& s) {
    auto bad = [] { raise("ValueError", "invalid hexadecimal floating-point string"); };
    std::string_view v = strip_view(s);
    bool negative = !v.empty() && v[0] == '-';
    if (!v.empty() && (v[0] == '+' || v[0] == '-')) v.remove_prefix(1);
    std::string lower;
    for (char c : v) lower += static_cast<char>(c >= 'A' && c <= 'Z' ? c + 32 : c);
    if (lower == "inf" || lower == "infinity") return negative ? -HUGE_VAL : HUGE_VAL;
    if (lower == "nan") return std::numeric_limits<double>::quiet_NaN();
    std::string_view t = lower;
    if (t.starts_with("0x")) t.remove_prefix(2);
    auto is_hex = [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); };
    std::size_t i = 0, digits = 0;
    for (; i < t.size() && is_hex(t[i]); ++i) ++digits;
    if (i < t.size() && t[i] == '.')
        for (++i; i < t.size() && is_hex(t[i]); ++i) ++digits;
    if (digits == 0) bad();
    if (i < t.size() && t[i] == 'p') {
        ++i;
        if (i < t.size() && (t[i] == '+' || t[i] == '-')) ++i;
        std::size_t start = i;
        while (i < t.size() && t[i] >= '0' && t[i] <= '9') ++i;
        if (i == start) bad();
    }
    if (i != t.size()) bad();
    std::string text = "0x" + std::string(t);
    double value = std::strtod(text.c_str(), nullptr);
    if (std::isinf(value)) raise("OverflowError", "hexadecimal value too large to represent as a float");
    return negative ? -value : value;
}

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
template <class T>
auto& index(const list<T>& v, std::int64_t i) {
    auto& vec = v.vec();
    return vec[norm_index(i, vec.size(), "list")];
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
Seq slice(const Seq& s, opt_int lo, opt_int hi, opt_int step_);
template <class T>
list<T> slice(const list<T>& s, opt_int lo, opt_int hi, opt_int step_) {
    return list<T>(slice(s.vec(), lo, hi, step_));  // (on the vector: no handle per item)
}

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
    std::size_t size() const {  // len(range(...))
        if (step_ > 0) return stop_ > start_ ? static_cast<std::size_t>((stop_ - start_ + step_ - 1) / step_) : 0;
        return start_ > stop_ ? static_cast<std::size_t>((start_ - stop_ - step_ - 1) / -step_) : 0;
    }
};
// reversed(range(...)) is the range counted the other way: nothing is stored.
inline range reversed(range r) {
    auto n = static_cast<std::int64_t>(r.size());
    if (n == 0) return range(0, 0, 1);
    return range(r.start_ + (n - 1) * r.step_, r.start_ - r.step_, -r.step_);
}

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
    } else if constexpr (is_dict_like<U>::value) {
        return x.sd_keys();
    } else if constexpr (is_list<U>::value) {
        return list_range<typename list_elem<U>::type>{x};
    } else if constexpr (is_sd_set<U>::value) {
        return set_range<typename U::value_type>{x};
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
    set<elem_t<decltype(src)>> out;
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
void list_sort_by(const list<T>& v, F&& key, bool reverse = false) {
    sort_by_key(v.vec(), key, reverse);
}

// functools.reduce(f, items[, initial]): f(f(f(initial, x0), x1), x2)...
template <class A, class F, class It>
A reduce(F f, It&& items) {
    std::optional<A> acc;
    for (auto&& x : iter(std::forward<It>(items))) acc = acc ? A(f(std::move(*acc), x)) : A(x);
    if (!acc) raise("TypeError", "reduce() of empty iterable with no initial value");
    return std::move(*acc);
}
template <class A, class F, class It>
A reduce(F f, It&& items, A acc) {
    for (auto&& x : iter(std::forward<It>(items))) acc = A(f(std::move(acc), x));
    return acc;
}

// functools.cmp_to_key(cmp): keys that sort by what cmp(a, b) says (negative: a first).
template <class T>
struct CmpKey {
    T value;
    std::shared_ptr<std::function<double(const T&, const T&)>> cmp;
    bool operator<(const CmpKey& o) const { return (*cmp)(value, o.value) < 0; }
    bool operator==(const CmpKey& o) const { return (*cmp)(value, o.value) == 0; }
    std::string sd_repr() const { return "<functools.KeyWrapper object>"; }
};
template <class T, class F>
std::function<CmpKey<T>(const T&)> cmp_to_key(F cmp) {
    auto shared = std::make_shared<std::function<double(const T&, const T&)>>(
        [cmp = std::move(cmp)](const T& a, const T& b) mutable { return static_cast<double>(cmp(a, b)); });
    return [shared](const T& x) { return CmpKey<T>{x, shared}; };
}

template <class It, class F>
auto extreme_by(It&& it, F&& key, bool want_max, const char* name) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", std::string(name) + "() iterable argument is empty");
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
// min(xs, key=f, default=d) and max(...): the default if there are no items.
template <class R, class It, class F>
R min_by_or(It&& it, F&& key, R fallback) {
    auto values = to_list(std::forward<It>(it));
    return values.empty() ? fallback : R(extreme_by(values, key, false, "min"));
}
template <class R, class It, class F>
R max_by_or(It&& it, F&& key, R fallback) {
    auto values = to_list(std::forward<It>(it));
    return values.empty() ? fallback : R(extreme_by(values, key, true, "max"));
}

// Lazy map/filter/enumerate/zip: generators over their arguments (taken by value, so
// they outlive the call; a generator argument is shared, so it's consumed as they go).
template <class R, class F, class It>
Generator<R> map_lazy(F f, It items) {
    for (auto&& x : iter(items)) co_yield R(f(x));
}
template <class T, class F, class It>
Generator<T> filter_lazy(F f, It items) {
    for (auto&& x : iter(items))
        if (truthy(f(x))) co_yield T(x);
}
template <class T, class It>
Generator<std::tuple<std::int64_t, T>> enumerate_lazy(It items, std::int64_t start) {
    for (auto&& x : iter(items)) co_yield std::tuple<std::int64_t, T>(start++, T(x));
}
template <class R, std::size_t... I, class... Its>
Generator<R> zip_impl(std::index_sequence<I...>, bool strict, Its... its) {
    std::tuple<decltype(iter(std::declval<Its&>()))...> sources(iter(its)...);
    auto at = std::make_tuple(std::get<I>(sources).begin()...);
    auto end = std::make_tuple(std::get<I>(sources).end()...);
    while (((std::get<I>(at) != std::get<I>(end)) && ...)) {  // stops at the shortest
        co_yield R(*std::get<I>(at)...);
        (++std::get<I>(at), ...);
    }
    if (strict) {  // zip(..., strict=True): they must all have ended together
        bool more[] = {(std::get<I>(at) != std::get<I>(end))...};
        std::size_t stopped = 0;
        while (more[stopped]) ++stopped;
        auto before = [](std::size_t i) { return i == 1 ? std::string("argument 1") : "arguments 1-" + std::to_string(i); };
        if (stopped > 0)
            raise("ValueError", "zip() argument " + std::to_string(stopped + 1) + " is shorter than " + before(stopped));
        for (std::size_t i = 1; i < sizeof...(Its); ++i)
            if (more[i]) raise("ValueError", "zip() argument " + std::to_string(i + 1) + " is longer than " + before(i));
    }
}
template <class R, class... Its>
Generator<R> zip_lazy(Its... its) {
    return zip_impl<R>(std::index_sequence_for<Its...>{}, false, std::move(its)...);
}
template <class R, class... Its>
Generator<R> zip_strict(bool strict, Its... its) {
    return zip_impl<R>(std::index_sequence_for<Its...>{}, strict, std::move(its)...);
}
// map(f, xs, ys...): f of an item from each, until the shortest runs out.
template <class R, class F, std::size_t... I, class... Its>
Generator<R> map_impl(std::index_sequence<I...>, F f, Its... its) {
    std::tuple<decltype(iter(std::declval<Its&>()))...> sources(iter(its)...);
    auto at = std::make_tuple(std::get<I>(sources).begin()...);
    auto end = std::make_tuple(std::get<I>(sources).end()...);
    while (((std::get<I>(at) != std::get<I>(end)) && ...)) {
        co_yield R(f(*std::get<I>(at)...));
        (++std::get<I>(at), ...);
    }
}
template <class R, class F, class It, class It2, class... Its>
Generator<R> map_lazy(F f, It first, It2 second, Its... rest) {
    return map_impl<R>(std::index_sequence_for<It, It2, Its...>{}, std::move(f), std::move(first), std::move(second),
                       std::move(rest)...);
}
template <class T, class X>
bool contains(const Generator<T>& g, const X& x) {  // `x in gen` reads until it finds x, like Python
    for (auto&& v : g)
        if (v == x) return true;
    return false;
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

template <class It, class T>
T sum(It&& it, T total) {  // sum(items, start)
    for (auto&& v : iter(std::forward<It>(it))) total = total + v;
    return total;
}

// tuple[T, ...]: a tuple whose length isn't part of its type (tuple(xs), Path.parts).
// Immutable, hashable and ordered like a tuple; stored as a list.
template <class T>
struct vtuple {
    list<T> items;
    vtuple() = default;
    explicit vtuple(list<T> v) : items(std::move(v)) {}
    vtuple(std::initializer_list<T> v) : items(v) {}
    auto begin() const { return items.begin(); }
    auto end() const { return items.end(); }
    std::size_t size() const { return items.size(); }
    bool empty() const { return items.empty(); }
    auto operator<=>(const vtuple&) const = default;
    bool operator==(const vtuple&) const = default;
    friend vtuple operator+(const vtuple& a, const vtuple& b) {
        vtuple out(a.items.copy());
        out.items.insert(out.items.end(), b.items.begin(), b.items.end());
        return out;
    }
    std::int64_t count(const T& x) const { return std::count(items.begin(), items.end(), x); }
    std::int64_t index(const T& x) const {
        auto it = std::find(items.begin(), items.end(), x);
        if (it == items.end()) raise("ValueError", "tuple.index(x): x not in tuple");
        return it - items.begin();
    }
    std::string sd_repr() const {
        std::string out = "(";
        for (std::size_t i = 0; i < items.size(); ++i) out += (i ? ", " : "") + repr(items[i]);
        return out + (items.size() == 1 ? ",)" : ")");
    }
};

template <class T>
const T& index(const vtuple<T>& t, std::int64_t i) {
    return t.items[norm_index(i, t.items.size(), "tuple")];
}
template <class T, class X>
bool contains(const vtuple<T>& t, const X& x) {
    return std::find(t.items.begin(), t.items.end(), x) != t.items.end();
}
// `a, b = t`: the number of values must match, like Python.
inline void check_unpack(std::size_t have, std::size_t want) {
    if (have < want)
        raise("ValueError", "not enough values to unpack (expected " + std::to_string(want) + ", got " + std::to_string(have) + ")");
    if (have > want) raise("ValueError", "too many values to unpack (expected " + std::to_string(want) + ")");
}

template <class T>
vtuple<T> slice(const vtuple<T>& t, opt_int lo, opt_int hi, opt_int step) {
    return vtuple<T>(slice(t.items, lo, hi, step));
}

template <class It>
auto min_of(It&& it) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", "min() iterable argument is empty");
    return *std::min_element(values.begin(), values.end());
}
template <class It>
auto max_of(It&& it) {
    auto values = to_list(std::forward<It>(it));
    if (values.empty()) raise("ValueError", "max() iterable argument is empty");
    return *std::max_element(values.begin(), values.end());
}
template <class R, class It>
R min_of_or(It&& it, R fallback) {  // min(xs, default=d)
    auto values = to_list(std::forward<It>(it));
    return values.empty() ? fallback : R(*std::min_element(values.begin(), values.end()));
}
template <class R, class It>
R max_of_or(It&& it, R fallback) {
    auto values = to_list(std::forward<It>(it));
    return values.empty() ? fallback : R(*std::max_element(values.begin(), values.end()));
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
template <class T>
list<T> concat(const list<T>& a, const list<T>& b) {
    list<T> out = a.copy();
    out.insert(out.end(), b.begin(), b.end());
    return out;
}

// s * n: allocate once, then keep doubling the filled part (fast even for b"x" * 10**8).
template <class Seq>
Seq repeat(const Seq& s, std::int64_t n) {
    Seq out;
    if (n <= 0 || s.empty()) return out;
    if constexpr (requires { out.resize(std::size_t{1}); }) {
        std::size_t unit = s.size(), total = unit * static_cast<std::size_t>(n);
        out.resize(total);
        std::copy(s.begin(), s.end(), out.begin());
        for (std::size_t filled = unit; filled < total;) {
            std::size_t chunk = std::min(filled, total - filled);
            std::copy_n(out.begin(), chunk, out.begin() + filled);
            filled += chunk;
        }
    } else {
        for (std::int64_t i = 0; i < n; ++i) out.insert(out.end(), s.begin(), s.end());
    }
    return out;
}
inline bytes repeat(const bytes& b, std::int64_t n) { return bytes(repeat(b.data, n)); }

template <class T, class X>
bool contains(const std::vector<T>& v, const X& x) {
    return std::find(v.begin(), v.end(), x) != v.end();
}
template <class T, class X>
bool contains(const list<T>& v, const X& x) {
    return std::find(v.begin(), v.end(), x) != v.end();
}
template <class T, class X>
bool contains(const std::set<T>& s, const X& x) {
    return s.count(x) != 0;
}
template <class T, class X>
bool contains(const set<T>& s, const X& x) {
    return s.count(x) != 0;
}
template <class K, class V>
bool contains(const dict<K, V>& d, const std::type_identity_t<K>& k) {
    return d.contains(k);
}
inline bool contains(const std::string& s, const std::string& sub) { return s.find(sub) != std::string::npos; }
// `case {"a": x, **rest}`: rest is the other entries.
template <class K, class V>
dict<K, V> dict_without(const dict<K, V>& d, const std::vector<K>& keys) {
    dict<K, V> out;
    for (const auto& [k, v] : d)
        if (std::find(keys.begin(), keys.end(), k) == keys.end()) out[k] = v;
    return out;
}
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
set<T> set_or(const set<T>& a, const set<T>& b) {
    set<T> out = a.copy();
    out.insert(b.begin(), b.end());
    return out;
}
template <class T>
set<T> set_and(const set<T>& a, const set<T>& b) {
    set<T> out;
    for (const auto& x : a)
        if (b.contains(x)) out.insert(x);
    return out;
}
template <class T>
set<T> set_sub(const set<T>& a, const set<T>& b) {
    set<T> out;
    for (const auto& x : a)
        if (!b.contains(x)) out.insert(x);
    return out;
}
template <class T>
set<T> set_xor(const set<T>& a, const set<T>& b) {
    set<T> out;
    for (const auto& x : a)
        if (!b.contains(x)) out.insert(x);
    for (const auto& x : b)
        if (!a.contains(x)) out.insert(x);
    return out;
}

// The set methods take any iterables of items, not only sets: s.union([1, 2], other).
template <class T, class It>
set<T> as_set(const It& items) {
    if constexpr (std::is_same_v<It, set<T>>) {
        return items;
    } else {
        set<T> out;
        for (auto&& x : iter(items)) out.insert(T(x));
        return out;
    }
}
template <class T, class... Others>
set<T> set_union(const set<T>& s, const Others&... others) {
    set<T> out = s.copy();
    auto add = [&](const set<T>& o) { out.insert(o.begin(), o.end()); };
    (add(as_set<T>(others)), ...);
    (void)add;
    return out;
}
template <class T, class... Others>
set<T> set_intersection(const set<T>& s, const Others&... others) {
    set<T> out = s.copy();
    ((out = set_and(out, as_set<T>(others))), ...);
    return out;
}
template <class T, class... Others>
set<T> set_difference(const set<T>& s, const Others&... others) {
    set<T> out = s.copy();
    ((out = set_sub(out, as_set<T>(others))), ...);
    return out;
}
template <class T, class Other>
set<T> set_symmetric_difference(const set<T>& s, const Other& other) {
    return set_xor(s, as_set<T>(other));
}
// The _update methods change the set itself (through a new set, in case it's also an argument).
template <class T, class... Others>
void set_update(const set<T>& s, const Others&... others) {
    s.std_set() = set_union(s, others...).std_set();
}
template <class T, class... Others>
void set_intersection_update(const set<T>& s, const Others&... others) {
    s.std_set() = set_intersection(s, others...).std_set();
}
template <class T, class... Others>
void set_difference_update(const set<T>& s, const Others&... others) {
    s.std_set() = set_difference(s, others...).std_set();
}
template <class T, class Other>
void set_symmetric_difference_update(const set<T>& s, const Other& other) {
    s.std_set() = set_symmetric_difference(s, other).std_set();
}
template <class T, class Other>
bool set_isdisjoint(const set<T>& s, const Other& other) {
    for (auto&& x : iter(other))
        if (s.contains(T(x))) return false;
    return true;
}
template <class T>
T set_pop(const set<T>& s) {  // (the first item, in the order the set prints)
    if (s.empty()) raise("KeyError", "'pop from an empty set'");
    T out = *s.begin();
    s.erase(out);
    return out;
}

// t.count(x) and t.index(x) on a tuple, whose items may have different types.
template <class A, class B>
bool same_value(const A& a, const B& b) {
    if constexpr (requires { a == b; }) {
        return a == b;
    } else {
        return false;
    }
}
template <class... Ts, class X>
std::int64_t tuple_count(const std::tuple<Ts...>& t, const X& x) {
    return std::apply([&](const auto&... e) { return (std::int64_t(0) + ... + (same_value(e, x) ? 1 : 0)); }, t);
}
template <class... Ts, class X>
std::int64_t tuple_find(const std::tuple<Ts...>& t, const X& x) {
    std::int64_t at = -1, i = 0;
    std::apply([&](const auto&... e) { ((at < 0 && same_value(e, x) ? at = i : 0, ++i), ...); }, t);
    if (at < 0) raise("ValueError", "tuple.index(x): x not in tuple");
    return at;
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
inline std::tuple<std::string, std::string, std::string> str_partition(const std::string& s, const std::string& sep) {
    if (sep.empty()) raise("ValueError", "empty separator");
    auto at = s.find(sep);
    if (at == std::string::npos) return {s, "", ""};
    return {s.substr(0, at), sep, s.substr(at + sep.size())};
}
inline std::tuple<std::string, std::string, std::string> str_rpartition(const std::string& s, const std::string& sep) {
    if (sep.empty()) raise("ValueError", "empty separator");
    auto at = s.rfind(sep);
    if (at == std::string::npos) return {"", "", s};
    return {s.substr(0, at), sep, s.substr(at + sep.size())};
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

// ---- format specs -----------------------------------------------------------
// f"{x:spec}": Python's format-spec mini-language, with its output and errors,
// [[fill]align][sign]["z"]["#"]["0"][width][grouping]["." precision][type].

struct FormatSpec {
    std::string fill = " ";  // one character (UTF-8)
    char align = 0;          // '<', '>', '^', '=' or 0 for the type's default
    char sign = 0;           // '+', '-', ' ' or 0 when not given
    bool no_neg_zero = false, alternate = false;
    std::int64_t width = -1, precision = -1;
    char grouping = 0;  // ',' or '_'
    char type = 0;
};

[[noreturn]] inline void unknown_format_code(char type, std::string_view type_name) {
    char code[8];
    if (type > ' ' && type < 127)
        std::snprintf(code, sizeof code, "%c", type);
    else
        std::snprintf(code, sizeof code, "\\x%x", static_cast<unsigned char>(type));
    raise("ValueError", std::string("Unknown format code '") + code + "' for object of type '" + std::string(type_name) + "'");
}

inline FormatSpec parse_format_spec(std::string_view s, std::string_view type_name) {
    FormatSpec f;
    std::size_t pos = 0;
    auto is_align = [](char c) { return c == '<' || c == '>' || c == '^' || c == '='; };
    std::size_t first = s.empty() ? 0 : 1;  // the first character's length in bytes
    while (first < s.size() && (static_cast<unsigned char>(s[first]) & 0xC0) == 0x80) ++first;
    bool fill_given = false;
    if (first < s.size() && is_align(s[first])) {
        f.fill = std::string(s.substr(0, first));
        f.align = s[first];
        fill_given = true;
        pos = first + 1;
    } else if (!s.empty() && is_align(s[0])) {
        f.align = s[0];
        pos = 1;
    }
    if (pos < s.size() && (s[pos] == '+' || s[pos] == '-' || s[pos] == ' ')) f.sign = s[pos++];
    if (pos < s.size() && s[pos] == 'z') f.no_neg_zero = true, ++pos;
    if (pos < s.size() && s[pos] == '#') f.alternate = true, ++pos;
    if (!fill_given && pos < s.size() && s[pos] == '0') {  // 0-padding: fill with zeros after the sign
        f.fill = "0";
        if (f.align == 0 && type_name != "str") f.align = '=';
        ++pos;
    }
    auto number = [&]() -> std::int64_t {
        std::size_t start = pos;
        std::int64_t n = 0;
        while (pos < s.size() && s[pos] >= '0' && s[pos] <= '9') {
            if (n > (std::numeric_limits<std::int32_t>::max() - (s[pos] - '0')) / 10)
                raise("ValueError", "Too many decimal digits in format string");
            n = n * 10 + (s[pos++] - '0');
        }
        return pos == start ? -1 : n;
    };
    f.width = number();
    if (pos < s.size() && (s[pos] == ',' || s[pos] == '_')) {
        f.grouping = s[pos++];
        if (pos < s.size() && (s[pos] == ',' || s[pos] == '_')) {
            if (s[pos] == f.grouping)
                raise("ValueError", std::string("Cannot specify '") + s[pos] + "' with '" + s[pos] + "'.");
            raise("ValueError", "Cannot specify both ',' and '_'.");
        }
    }
    if (pos < s.size() && s[pos] == '.') {
        ++pos;
        f.precision = number();
        if (f.precision < 0) raise("ValueError", "Format specifier missing precision");
    }
    if (s.size() - pos > 1)
        raise("ValueError", "Invalid format specifier '" + std::string(s) + "' for object of type '" + std::string(type_name) + "'");
    if (pos < s.size()) f.type = s[pos];
    if (f.grouping) {
        char type = f.type ? f.type : (type_name == "str" ? 's' : 0);
        bool ok = std::string_view("defgEFG%").find(type) != std::string_view::npos || type == 0 ||
                  (f.grouping == '_' && std::string_view("boxX").find(type) != std::string_view::npos);
        if (!ok) raise("ValueError", std::string("Cannot specify '") + f.grouping + "' with '" + type + "'.");
    }
    return f;
}

inline std::size_t code_points(std::string_view s) {
    std::size_t n = 0;
    for (char c : s) n += (static_cast<unsigned char>(c) & 0xC0) != 0x80;
    return n;
}

// Pads `left + right` to the spec's width; '=' pads between them (after a sign).
inline std::string format_pad(const FormatSpec& f, char default_align, std::string_view left, std::string_view right) {
    std::size_t n = code_points(left) + code_points(right);
    std::size_t width = f.width < 0 ? 0 : static_cast<std::size_t>(f.width);
    if (n >= width) return std::string(left) + std::string(right);
    std::size_t pad = width - n, before = 0;
    switch (f.align ? f.align : default_align) {
        case '<': before = 0; break;
        case '^': before = pad / 2; break;
        default: before = pad; break;
    }
    std::string out;
    auto fill = [&](std::size_t k) {
        for (std::size_t i = 0; i < k; ++i) out += f.fill;
    };
    if (f.align == '=') {
        out += left;
        fill(pad);
        out += right;
        return out;
    }
    fill(before);
    out += left;
    out += right;
    fill(pad - before);
    return out;
}

// Groups the digits `d` by threes (or fours for '_' in bin/oct/hex), from the right.
// With zero-padding, leading zeros are grouped too, up to `min_width` characters.
inline std::string group_digits(std::string_view d, char sep, std::size_t size, std::int64_t min_width) {
    std::string out;  // built backwards
    std::int64_t remaining = static_cast<std::int64_t>(d.size());
    bool first = true;
    while (true) {
        std::int64_t l = std::min<std::int64_t>(size, std::max<std::int64_t>({remaining, min_width, 1}));
        std::int64_t zeros = std::max<std::int64_t>(0, l - remaining);
        std::int64_t chars = std::max<std::int64_t>(0, std::min(remaining, l));
        if (!first) out += sep;
        first = false;
        for (std::int64_t i = 0; i < chars; ++i) out += d[remaining - 1 - i];
        out.append(zeros, '0');
        remaining -= chars;
        min_width -= l;
        if (remaining <= 0 && min_width <= 0) break;
        min_width -= 1;
    }
    std::reverse(out.begin(), out.end());
    return out;
}

// A number laid out: sign, prefix (0x), integer digits (grouped), then the rest (.5e+10%).
inline std::string format_number(const FormatSpec& f, bool negative, std::string_view prefix, std::string_view digits,
                                 std::string_view rest, std::size_t group_size = 3) {
    std::string left;
    if (negative)
        left = "-";
    else if (f.sign == '+' || f.sign == ' ')
        left = std::string(1, f.sign);
    left += prefix;
    std::string body(digits);
    if (f.grouping) {
        std::int64_t min_width = 0;
        if (f.fill == "0" && f.align == '=')
            min_width = f.width - static_cast<std::int64_t>(left.size() + rest.size());
        body = group_digits(digits, f.grouping, group_size, min_width);
    }
    body += rest;
    return format_pad(f, '>', left, body);
}

inline std::string format_value(const std::string& s, std::string_view spec) {
    if (spec.empty()) return s;
    FormatSpec f = parse_format_spec(spec, "str");
    if (f.type && f.type != 's') unknown_format_code(f.type, "str");
    if (f.sign) raise("ValueError", f.sign == ' ' ? "Space not allowed in string format specifier" : "Sign not allowed in string format specifier");
    if (f.no_neg_zero) raise("ValueError", "Negative zero coercion (z) not allowed in string format specifier");
    if (f.alternate) raise("ValueError", "Alternate form (#) not allowed in string format specifier");
    if (f.align == '=') raise("ValueError", "'=' alignment not allowed in string format specifier");
    std::string_view text = s;
    if (f.precision >= 0) {  // at most `precision` characters
        std::size_t i = 0;
        for (std::int64_t n = 0; i < text.size(); ++i)
            if ((static_cast<unsigned char>(text[i]) & 0xC0) != 0x80 && n++ == f.precision) break;
        text = text.substr(0, i);
    }
    return format_pad(f, '<', text, "");
}

// Python's float formatting (format_float_short): `type` is 'e', 'f', 'g' or 'r' (repr).
inline std::string format_double(const FormatSpec& f, double x, char type, std::int64_t precision, bool add_dot_0,
                                 std::string_view suffix) {
    bool upper = f.type == 'E' || f.type == 'F' || f.type == 'G';
    if (!std::isfinite(x)) {
        std::string s = std::isnan(x) ? (upper ? "NAN" : "nan") : (upper ? "INF" : "inf");
        FormatSpec g = f;
        g.grouping = 0;
        return format_number(g, std::isinf(x) && x < 0, "", "", s + std::string(suffix));
    }
    // The significant digits (no trailing zeros) and the decimal point's position.
    std::vector<char> buf(static_cast<std::size_t>(precision) + 400);
    char *begin = buf.data(), *end = begin + buf.size();
    std::to_chars_result r;
    if (type == 'r')
        r = std::to_chars(begin, end, std::fabs(x), std::chars_format::scientific);
    else if (type == 'f')
        r = std::to_chars(begin, end, std::fabs(x), std::chars_format::fixed, static_cast<int>(precision));
    else
        r = std::to_chars(begin, end, std::fabs(x), std::chars_format::scientific,
                          static_cast<int>(std::max<std::int64_t>(type == 'e' ? precision : precision - 1, 0)));
    std::string_view out(begin, r.ptr);
    std::string digits;
    std::int64_t decpt;
    if (type == 'f') {
        auto dot = out.find('.');
        decpt = static_cast<std::int64_t>(dot == std::string_view::npos ? out.size() : dot);
        for (char c : out)
            if (c != '.') digits += c;
        std::size_t lead = 0;
        while (lead + 1 < digits.size() && digits[lead] == '0') ++lead;
        digits.erase(0, lead);
        decpt -= static_cast<std::int64_t>(lead);
    } else {
        auto e = out.find('e');
        for (char c : out.substr(0, e))
            if (c != '.') digits += c;
        decpt = std::stoll(std::string(out.substr(e + 1))) + 1;
    }
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    if (digits == "0") decpt = 1;
    std::int64_t ndigits = static_cast<std::int64_t>(digits.size());

    bool use_exp = false;
    std::int64_t vend = ndigits;
    switch (type) {
        case 'e': use_exp = true, vend = precision + 1; break;
        case 'f': vend = decpt + precision; break;
        case 'g':
            use_exp = decpt <= -4 || decpt > (add_dot_0 ? precision - 1 : precision);
            if (f.alternate) vend = precision;
            break;
        default: use_exp = decpt <= -4 || decpt > 16; break;
    }
    std::int64_t exp = 0;
    if (use_exp) exp = decpt - 1, decpt = 1;
    std::int64_t vstart = decpt <= 0 ? decpt - 1 : 0;
    vend = std::max(vend, !use_exp && add_dot_0 ? decpt + 1 : decpt);
    std::string s;  // the virtual digit string [vstart, vend) with a point before index decpt
    for (std::int64_t i = vstart; i < vend; ++i) {
        if (i == decpt) s += '.';
        s += i >= 0 && i < ndigits ? digits[i] : '0';
    }
    if (vend == decpt) s += '.';
    if (s.back() == '.' && !f.alternate) s.pop_back();
    if (use_exp) {
        char e[16];
        std::snprintf(e, sizeof e, "%c%+.02lld", upper ? 'E' : 'e', static_cast<long long>(exp));
        s += e;
    }
    bool negative = std::signbit(x);
    if (negative && f.no_neg_zero && s.find_first_not_of("0.") == s.find_first_of("eE%"))
        negative = false;  // z: -0.00 -> 0.00
    s += suffix;
    auto int_end = std::min(s.find('.'), s.find_first_of("eE%"));
    if (int_end == std::string::npos) int_end = s.size();
    return format_number(f, negative, "", std::string_view(s).substr(0, int_end), std::string_view(s).substr(int_end));
}

inline std::string format_float(const FormatSpec& f, double x) {
    char type = f.type;
    std::int64_t precision = f.precision;
    bool add_dot_0 = false;
    std::string_view suffix;
    switch (type) {
        case 0:
            add_dot_0 = true;
            type = precision < 0 ? 'r' : 'g';
            break;
        case 'n': type = 'g'; break;
        case '%': type = 'f', x *= 100, suffix = "%"; break;
        case 'E': type = 'e'; break;
        case 'F': type = 'f'; break;
        case 'G': type = 'g'; break;
    }
    if (precision < 0) precision = 6;
    if (type == 'g' && precision == 0) precision = 1;
    return format_double(f, x, type, precision, add_dot_0, suffix);
}

inline std::string format_value(double x, std::string_view spec) {
    if (spec.empty()) return float_repr(x);
    FormatSpec f = parse_format_spec(spec, "float");
    if (std::string_view("eEfFgGn%").find(f.type) == std::string_view::npos && f.type != 0)
        unknown_format_code(f.type, "float");
    return format_float(f, x);
}

inline std::string format_int(std::int64_t n, std::string_view spec, std::string_view type_name) {
    FormatSpec f = parse_format_spec(spec, type_name);
    switch (f.type) {
        case 'e': case 'E': case 'f': case 'F': case 'g': case 'G': case '%':
            return format_float(f, static_cast<double>(n));
        case 0: case 'd': case 'n': case 'b': case 'o': case 'x': case 'X': case 'c': break;
        default: unknown_format_code(f.type, type_name);
    }
    if (f.precision >= 0) raise("ValueError", "Precision not allowed in integer format specifier");
    if (f.no_neg_zero) raise("ValueError", "Negative zero coercion (z) not allowed in integer format specifier");
    if (f.type == 'c') {
        if (f.sign) raise("ValueError", "Sign not allowed with integer format specifier 'c'");
        if (f.alternate) raise("ValueError", "Alternate form (#) not allowed with integer format specifier 'c'");
        if (n < 0 || n > 0x10FFFF) raise("OverflowError", "%c arg not in range(0x110000)");
        return format_number(f, false, "", chr(n), "");
    }
    int base = 10;
    std::string_view prefix;
    switch (f.type) {
        case 'b': base = 2, prefix = "0b"; break;
        case 'o': base = 8, prefix = "0o"; break;
        case 'x': base = 16, prefix = "0x"; break;
        case 'X': base = 16, prefix = "0X"; break;
    }
    std::uint64_t magnitude = n < 0 ? 0 - static_cast<std::uint64_t>(n) : static_cast<std::uint64_t>(n);
    char buf[72];
    auto r = std::to_chars(buf, buf + sizeof buf, magnitude, base);
    if (f.type == 'X')
        for (char* p = buf; p != r.ptr; ++p)
            if (*p >= 'a') *p = static_cast<char>(*p - 'a' + 'A');
    return format_number(f, n < 0, f.alternate ? prefix : "", std::string_view(buf, r.ptr), "", base == 10 ? 3 : 4);
}

inline std::string format_value(std::int64_t n, std::string_view spec) {
    if (spec.empty()) return std::to_string(n);
    return format_int(n, spec, "int");
}

inline std::string format_value(bool b, std::string_view spec) {
    if (spec.empty()) return b ? "True" : "False";
    return format_int(b, spec, "bool");
}

// format(x, spec) and f"{x:spec}" for any value: the overloads above, or a date's
// (found by argument-dependent lookup). Other values only take an empty spec.
template <class T>
std::string format_any(const T& x, std::string_view spec, std::string_view type_name = "object") {
    if constexpr (requires { format_value(x, spec); }) {
        return format_value(x, spec);
    } else if constexpr (is_optional<T>::value) {
        if (x) return format_any(*x, spec, type_name);
        return format_any(std::nullopt, spec, "NoneType");
    } else {
        if (!spec.empty())
            raise("TypeError", "unsupported format string passed to " + std::string(type_name) + ".__format__");
        return str(x);
    }
}

// str.format() with a format string only known at run time. (A literal format
// string is compiled like an f-string instead.)
struct FormatArg {
    std::string (*format)(const void* p, char conversion, std::string_view spec, std::string_view type_name);
    const void* p;
    std::string_view type_name;
};

template <class T>
std::string format_arg(const void* p, char conversion, std::string_view spec, std::string_view type_name) {
    const T& x = *static_cast<const T*>(p);
    switch (conversion) {
        case 'r': return format_value(repr(x), spec);
        case 's': return format_value(str(x), spec);
        case 'a': return format_value(ascii(repr(x)), spec);
        default: return format_any(x, spec, type_name);
    }
}

struct StrFormatter {
    const std::vector<FormatArg>& args;
    std::size_t positional;               // args[0, positional) are positional, the rest keywords
    const std::vector<std::string_view>& names;  // the keywords' names
    std::int64_t next_auto = 0;           // -1 once a field is numbered by hand

    const FormatArg& field(std::string_view name) {
        std::size_t end = name.find_first_of(".[");
        std::string_view first = name.substr(0, end);
        const FormatArg& arg = argument(first);
        if (end != std::string_view::npos)
            raise("ValueError", "seadash can't look up '" + std::string(name.substr(end)) +
                                    "' in a format string made at run time (use a literal format string)");
        return arg;
    }

    const FormatArg& argument(std::string_view first) {
        auto index = [&](std::size_t i) -> const FormatArg& {
            if (i >= positional)
                raise("IndexError", "Replacement index " + std::to_string(i) + " out of range for positional args tuple");
            return args[i];
        };
        if (first.empty()) {
            if (next_auto < 0)
                raise("ValueError", "cannot switch from manual field specification to automatic field numbering");
            return index(static_cast<std::size_t>(next_auto++));
        }
        if (std::all_of(first.begin(), first.end(), [](char c) { return c >= '0' && c <= '9'; })) {
            if (next_auto > 0)
                raise("ValueError", "cannot switch from automatic field numbering to manual field specification");
            next_auto = -1;
            std::size_t i = 0;
            for (char c : first) i = std::min<std::size_t>(i * 10 + (c - '0'), std::numeric_limits<std::int32_t>::max());
            return index(i);
        }
        for (std::size_t i = 0; i < names.size(); ++i)
            if (names[i] == first) return args[positional + i];
        raise("KeyError", repr_str(std::string(first)));
    }

    std::string run(std::string_view fmt, int depth) {
        if (depth <= 0) raise("ValueError", "Max string recursion exceeded");
        std::string out;
        std::size_t i = 0;
        while (i < fmt.size()) {
            char c = fmt[i];
            if (c == '}') {
                if (i + 1 < fmt.size() && fmt[i + 1] == '}') {
                    out += '}', i += 2;
                    continue;
                }
                raise("ValueError", "Single '}' encountered in format string");
            }
            if (c != '{') {
                out += c, ++i;
                continue;
            }
            if (i + 1 < fmt.size() && fmt[i + 1] == '{') {
                out += '{', i += 2;
                continue;
            }
            if (i + 1 == fmt.size()) raise("ValueError", "Single '{' encountered in format string");
            // name[!conversion][:spec]}, where brackets in the name may hold anything but ']'.
            std::size_t start = ++i;
            auto unterminated = [] { raise("ValueError", "expected '}' before end of string"); };
            while (i < fmt.size() && fmt[i] != '}' && fmt[i] != ':' && fmt[i] != '!') {
                if (fmt[i] == '{') raise("ValueError", "unexpected '{' in field name");
                if (fmt[i] == '[') {
                    i = fmt.find(']', i);
                    if (i == std::string_view::npos) unterminated();
                }
                ++i;
            }
            if (i == fmt.size()) unterminated();
            std::string_view name = fmt.substr(start, i - start);
            char conversion = 0;
            if (fmt[i] == '!') {
                if (i + 1 == fmt.size()) raise("ValueError", "end of string while looking for conversion specifier");
                conversion = fmt[i + 1];
                i += 2;
                if (i < fmt.size() && fmt[i] != ':' && fmt[i] != '}')
                    raise("ValueError", "expected ':' after conversion specifier");
            }
            std::string_view spec;
            if (i < fmt.size() && fmt[i] == ':') {
                std::size_t spec_start = ++i;
                for (int nesting = 1; i < fmt.size(); ++i) {
                    if (fmt[i] == '{') ++nesting;
                    if (fmt[i] == '}' && --nesting == 0) break;
                }
                spec = fmt.substr(spec_start, i - spec_start);
            }
            if (i >= fmt.size()) raise("ValueError", "unmatched '{' in format spec");
            ++i;  // the closing '}'
            const FormatArg& arg = field(name);
            if (conversion && conversion != 'r' && conversion != 's' && conversion != 'a')
                raise("ValueError", std::string("Unknown conversion specifier ") + conversion);
            std::string expanded;
            if (spec.find('{') != std::string_view::npos) {
                expanded = run(spec, depth - 1);
                spec = expanded;
            }
            out += arg.format(arg.p, conversion, spec, arg.type_name);
        }
        return out;
    }
};

// str_format(fmt, {"list", "int"...}, {"key"...}, positional..., keywords...)
template <class... Ts>
std::string str_format(std::string_view fmt, std::initializer_list<std::string_view> types,
                       std::initializer_list<std::string_view> keywords, const Ts&... values) {
    std::vector<FormatArg> args;
    auto type = types.begin();
    (args.push_back(FormatArg{&format_arg<Ts>, &values, *type++}), ...);
    std::vector<std::string_view> names(keywords);
    StrFormatter f{args, args.size() - names.size(), names};
    return f.run(fmt, 2);
}

// ---- bytes <-> str ----------------------------------------------------------

inline void check_encoding(const std::string& encoding) {
    std::string e = str_lower(encoding);
    if (e != "utf-8" && e != "utf8" && e != "ascii")
        raise("LookupError", "unknown encoding: " + encoding + " (seadash supports utf-8 and ascii)");
}

// The codecs of s.encode() and b.decode(), by any of Python's names for them.
enum class Codec { utf8, ascii, latin1 };
inline Codec text_codec(const std::string& encoding) {
    std::string e;
    for (char c : str_lower(encoding)) e += c == '-' || c == ' ' ? '_' : c;
    if (e == "utf_8" || e == "utf8" || e == "u8" || e == "utf") return Codec::utf8;
    if (e == "ascii" || e == "us_ascii" || e == "646") return Codec::ascii;
    if (e == "latin_1" || e == "latin1" || e == "iso_8859_1" || e == "iso8859_1" || e == "l1" || e == "cp819" || e == "8859")
        return Codec::latin1;
    raise("LookupError", "unknown encoding: " + encoding + " (seadash supports utf-8, ascii and latin-1)");
}

inline bytes str_encode(const std::string& s, const std::string& encoding = "utf-8") {
    Codec codec = text_codec(encoding);
    if (codec == Codec::utf8) return bytes(s);  // str is already UTF-8
    std::uint32_t limit = codec == Codec::ascii ? 0x80 : 0x100;
    auto decode = [&](std::size_t i, std::size_t& width) {  // the character starting at byte i
        unsigned char c = static_cast<unsigned char>(s[i]);
        width = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        std::uint32_t cp = width == 1 ? c : c & (0xFF >> (width + 1));
        for (std::size_t k = 1; k < width && i + k < s.size(); ++k) cp = (cp << 6) | (static_cast<unsigned char>(s[i + k]) & 0x3F);
        return cp;
    };
    std::string out;
    std::size_t position = 0, width = 0;  // the position in characters, as Python counts
    for (std::size_t i = 0; i < s.size(); i += width, ++position) {
        std::uint32_t cp = decode(i, width);
        if (cp < limit) {
            out += static_cast<char>(cp);
            continue;
        }
        std::size_t last = position, w = 0;  // the error names the whole run of characters that don't fit
        for (std::size_t j = i + width; j < s.size() && decode(j, w) >= limit; j += w) ++last;
        std::string which = last == position
            ? "character " + ascii(repr_str(s.substr(i, width))) + " in position " + std::to_string(position)
            : "characters in position " + std::to_string(position) + "-" + std::to_string(last);
        raise("UnicodeEncodeError", std::string("'") + (codec == Codec::ascii ? "ascii" : "latin-1") + "' codec can't encode " +
                                        which + ": ordinal not in range(" + std::to_string(limit) + ")");
    }
    return bytes(std::move(out));
}

// Validates UTF-8 (strings in seadash are always valid UTF-8 text).
inline std::string bytes_decode(const bytes& b, const std::string& encoding = "utf-8") {
    Codec codec = text_codec(encoding);
    bool ascii = codec == Codec::ascii;
    const std::string& s = b.data;
    if (codec == Codec::latin1) {  // every byte is the character with that number
        std::string out;
        for (unsigned char c : s) {
            if (c < 0x80) {
                out += static_cast<char>(c);
            } else {
                out += static_cast<char>(0xC0 | (c >> 6));
                out += static_cast<char>(0x80 | (c & 0x3F));
            }
        }
        return out;
    }
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
inline std::tuple<bytes, bytes, bytes> bytes_partition(const bytes& b, const bytes& sep) {
    auto [x, y, z] = str_partition(b.data, sep.data);
    return {bytes(x), bytes(y), bytes(z)};
}
inline std::tuple<bytes, bytes, bytes> bytes_rpartition(const bytes& b, const bytes& sep) {
    auto [x, y, z] = str_rpartition(b.data, sep.data);
    return {bytes(x), bytes(y), bytes(z)};
}
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

// ============================================================================
// More str and bytes methods: padding, searching with start/end, splitting with
// maxsplit, rsplit, translate... Widths count characters for str (code points: UTF-8)
// and bytes for bytes; positions are byte offsets, like indexing. Classification and
// case follow ASCII rules, like upper() and lower().
// ============================================================================

// s[start:end] as byte offsets, Python's slice rules (for find, count, index...).
inline std::pair<std::int64_t, std::int64_t> sub_bounds(std::size_t size, opt_int start, opt_int end) {
    std::int64_t n = static_cast<std::int64_t>(size);
    auto fix = [&](opt_int v, std::int64_t dflt) {
        if (!v) return dflt;
        std::int64_t i = *v;
        if (i < 0) i = std::max<std::int64_t>(i + n, 0);
        return std::min(i, n);
    };
    std::int64_t lo = start ? (*start < 0 ? std::max<std::int64_t>(*start + n, 0) : *start) : 0;
    return {lo, fix(end, n)};
}

inline std::int64_t str_find(const std::string& s, const std::string& sub, opt_int start, opt_int end = std::nullopt) {
    auto [lo, hi] = sub_bounds(s.size(), start, end);
    if (lo > static_cast<std::int64_t>(s.size()) || hi - lo < static_cast<std::int64_t>(sub.size())) return -1;
    auto pos = s.substr(0, static_cast<std::size_t>(hi)).find(sub, static_cast<std::size_t>(lo));
    return pos == std::string::npos ? -1 : static_cast<std::int64_t>(pos);
}
inline std::int64_t str_rfind(const std::string& s, const std::string& sub, opt_int start = std::nullopt,
                              opt_int end = std::nullopt) {
    auto [lo, hi] = sub_bounds(s.size(), start, end);
    if (lo > static_cast<std::int64_t>(s.size()) || hi - lo < static_cast<std::int64_t>(sub.size())) return -1;
    auto pos = s.substr(0, static_cast<std::size_t>(hi)).rfind(sub);
    return pos == std::string::npos || static_cast<std::int64_t>(pos) < lo ? -1 : static_cast<std::int64_t>(pos);
}
inline std::int64_t str_index(const std::string& s, const std::string& sub, opt_int start = std::nullopt,
                              opt_int end = std::nullopt) {
    std::int64_t i = str_find(s, sub, start, end);
    if (i < 0) raise("ValueError", "substring not found");
    return i;
}
inline std::int64_t str_rindex(const std::string& s, const std::string& sub, opt_int start = std::nullopt,
                               opt_int end = std::nullopt) {
    std::int64_t i = str_rfind(s, sub, start, end);
    if (i < 0) raise("ValueError", "substring not found");
    return i;
}
inline std::int64_t str_count(const std::string& s, const std::string& sub, opt_int start, opt_int end = std::nullopt) {
    auto [lo, hi] = sub_bounds(s.size(), start, end);
    if (lo > hi) return 0;
    return str_count(s.substr(static_cast<std::size_t>(lo), static_cast<std::size_t>(hi - lo)), sub);
}

// split(sep=None, maxsplit=-1) and rsplit(): by runs of whitespace, or by sep.
inline bool is_ws(char c) { return std::strchr(" \t\n\r\f\v", c) != nullptr && c != '\0'; }
inline std::vector<std::string> str_split(const std::string& s, const std::optional<std::string>& sep, std::int64_t maxsplit) {
    if (maxsplit < 0) return sep ? str_split(s, *sep) : str_split(s);
    std::vector<std::string> out;
    if (sep) {
        if (sep->empty()) raise("ValueError", "empty separator");
        std::size_t start = 0, pos;
        while (static_cast<std::int64_t>(out.size()) < maxsplit && (pos = s.find(*sep, start)) != std::string::npos) {
            out.push_back(s.substr(start, pos - start));
            start = pos + sep->size();
        }
        out.push_back(s.substr(start));
        return out;
    }
    std::size_t i = 0, n = s.size();
    while (true) {
        while (i < n && is_ws(s[i])) ++i;
        if (i == n) break;
        if (static_cast<std::int64_t>(out.size()) == maxsplit) {  // the rest, as it is (after its leading space)
            out.push_back(s.substr(i));
            break;
        }
        std::size_t j = i;
        while (j < n && !is_ws(s[j])) ++j;
        out.push_back(s.substr(i, j - i));
        i = j;
    }
    return out;
}
inline std::vector<std::string> str_rsplit(const std::string& s, const std::optional<std::string>& sep = std::nullopt,
                                           std::int64_t maxsplit = -1) {
    std::vector<std::string> out;
    if (sep) {
        if (sep->empty()) raise("ValueError", "empty separator");
        std::size_t end = s.size();
        while (maxsplit < 0 || static_cast<std::int64_t>(out.size()) < maxsplit) {
            std::size_t pos = end < sep->size() ? std::string::npos : s.rfind(*sep, end - sep->size());
            if (pos == std::string::npos) break;
            out.push_back(s.substr(pos + sep->size(), end - pos - sep->size()));
            end = pos;
        }
        out.push_back(s.substr(0, end));
    } else {
        std::size_t j = s.size();
        while (true) {
            while (j > 0 && is_ws(s[j - 1])) --j;
            if (j == 0) break;
            if (maxsplit >= 0 && static_cast<std::int64_t>(out.size()) == maxsplit) {
                out.push_back(s.substr(0, j));
                break;
            }
            std::size_t i = j;
            while (i > 0 && !is_ws(s[i - 1])) --i;
            out.push_back(s.substr(i, j - i));
            j = i;
        }
    }
    std::reverse(out.begin(), out.end());
    return out;
}

// Padding. `chars` is how long the text is (characters for str, bytes for bytes).
inline std::string pad_to(const std::string& s, std::int64_t width, const std::string& fill, std::size_t chars, int how) {
    std::int64_t len = static_cast<std::int64_t>(chars);
    if (width <= len) return s;
    std::int64_t margin = width - len, left = 0;
    if (how < 0) left = 0;                                  // ljust
    else if (how > 0) left = margin;                        // rjust
    else left = margin / 2 + (margin & width & 1);          // center, as CPython rounds it
    std::string out;
    for (std::int64_t i = 0; i < left; ++i) out += fill;
    out += s;
    for (std::int64_t i = 0; i < margin - left; ++i) out += fill;
    return out;
}
inline const std::string& fill_char(const std::string& fill) {
    if (code_points(fill) != 1) raise("TypeError", "The fill character must be exactly one character long");
    return fill;
}
inline std::string str_ljust(const std::string& s, std::int64_t width, const std::string& fill = " ") {
    return pad_to(s, width, fill_char(fill), code_points(s), -1);
}
inline std::string str_rjust(const std::string& s, std::int64_t width, const std::string& fill = " ") {
    return pad_to(s, width, fill_char(fill), code_points(s), 1);
}
inline std::string str_center(const std::string& s, std::int64_t width, const std::string& fill = " ") {
    return pad_to(s, width, fill_char(fill), code_points(s), 0);
}
inline std::string zfill_to(const std::string& s, std::int64_t width, std::size_t chars) {
    std::int64_t len = static_cast<std::int64_t>(chars);
    if (width <= len) return s;
    std::string zeros(static_cast<std::size_t>(width - len), '0');
    if (!s.empty() && (s[0] == '+' || s[0] == '-')) return s.substr(0, 1) + zeros + s.substr(1);
    return zeros + s;
}
inline std::string str_zfill(const std::string& s, std::int64_t width) { return zfill_to(s, width, code_points(s)); }
inline std::string expand_tabs(const std::string& s, std::int64_t tabsize, bool utf8) {
    std::string out;
    std::int64_t column = 0;
    for (char c : s) {
        if (c == '\t') {
            if (tabsize > 0) {
                std::int64_t spaces = tabsize - column % tabsize;
                out.append(static_cast<std::size_t>(spaces), ' ');
                column += spaces;
            }
        } else {
            out += c;
            if (c == '\n' || c == '\r') column = 0;
            else if (!utf8 || (static_cast<unsigned char>(c) & 0xC0) != 0x80) ++column;
        }
    }
    return out;
}
inline std::string str_expandtabs(const std::string& s, std::int64_t tabsize = 8) { return expand_tabs(s, tabsize, true); }

inline std::string str_removeprefix(const std::string& s, const std::string& p) { return s.starts_with(p) ? s.substr(p.size()) : s; }
inline std::string str_removesuffix(const std::string& s, const std::string& p) {
    return !p.empty() && s.ends_with(p) ? s.substr(0, s.size() - p.size()) : s;
}
inline std::string str_casefold(const std::string& s) { return str_lower(s); }
inline std::string str_swapcase(std::string s) {
    for (char& c : s) {
        unsigned char u = static_cast<unsigned char>(c);
        if (std::isupper(u)) c = static_cast<char>(std::tolower(u));
        else if (std::islower(u)) c = static_cast<char>(std::toupper(u));
    }
    return s;
}
inline bool str_isascii(const std::string& s) {
    return std::all_of(s.begin(), s.end(), [](char c) { return static_cast<unsigned char>(c) < 0x80; });
}
inline bool str_isdecimal(const std::string& s) { return str_isdigit(s); }
inline bool str_isnumeric(const std::string& s) { return str_isdigit(s); }
inline bool str_isidentifier(const std::string& s) {
    if (s.empty() || std::isdigit(static_cast<unsigned char>(s[0]))) return false;
    return std::all_of(s.begin(), s.end(), [](char c) {
        unsigned char u = static_cast<unsigned char>(c);
        return std::isalnum(u) || c == '_' || u >= 0x80;  // (non-ASCII letters are allowed, as in Python)
    });
}
inline bool str_isprintable(const std::string& s) {
    return std::all_of(s.begin(), s.end(), [](char c) {
        unsigned char u = static_cast<unsigned char>(c);
        return u >= 0x80 || (u >= 0x20 && u < 0x7F);
    });
}
inline bool str_istitle(const std::string& s) {
    bool cased = false, previous_cased = false;
    for (char c : s) {
        unsigned char u = static_cast<unsigned char>(c);
        if (std::isupper(u)) {
            if (previous_cased) return false;
            previous_cased = cased = true;
        } else if (std::islower(u)) {
            if (!previous_cased) return false;
            previous_cased = cased = true;
        } else {
            previous_cased = false;
        }
    }
    return cased;
}

// str.maketrans / str.translate: a table from code points to a replacement (None: delete).
inline std::vector<std::int64_t> utf8_code_points(const std::string& s) {
    std::vector<std::int64_t> out;
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        std::size_t width = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        std::int64_t cp = width == 1 ? c : c & (0xFF >> (width + 1));
        for (std::size_t k = 1; k < width && i + k < s.size(); ++k) cp = (cp << 6) | (s[i + k] & 0x3F);
        out.push_back(cp);
        i += width;
    }
    return out;
}
inline dict<std::int64_t, std::optional<std::int64_t>> str_maketrans(const std::string& x, const std::string& y,
                                                                     const std::string& z = "") {
    auto from = utf8_code_points(x), to = utf8_code_points(y);
    if (from.size() != to.size()) raise("ValueError", "the first two maketrans arguments must have equal length");
    dict<std::int64_t, std::optional<std::int64_t>> table;
    for (std::size_t i = 0; i < from.size(); ++i) table[from[i]] = to[i];
    for (std::int64_t cp : utf8_code_points(z)) table[cp] = std::nullopt;
    return table;
}
template <class V>
dict<std::int64_t, std::optional<std::string>> str_maketrans(const dict<std::string, V>& mapping) {
    dict<std::int64_t, std::optional<std::string>> table;
    for (const auto& [k, v] : mapping) {
        auto cps = utf8_code_points(k);
        if (cps.size() != 1) raise("ValueError", "string keys in translate table must be of length 1");
        if constexpr (is_optional<V>::value)
            table[cps[0]] = v;
        else
            table[cps[0]] = std::optional<std::string>(v);
    }
    return table;
}
template <class V>
std::string str_translate(const std::string& s, const dict<std::int64_t, V>& table) {
    std::string out;
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        std::size_t width = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
        std::string piece = s.substr(i, width);
        std::int64_t cp = utf8_code_points(piece)[0];
        i += width;
        const V* to = table.find(cp);
        if (!to) {
            out += piece;
            continue;
        }
        auto put = [&](const auto& x) {
            using X = std::remove_cvref_t<decltype(x)>;
            if constexpr (std::is_same_v<X, std::string>) out += x;
            else out += chr(x);
        };
        if constexpr (is_optional<V>::value) {
            if (*to) put(**to);  // None deletes it
        } else {
            put(*to);
        }
    }
    return out;
}

// str.format_map(mapping): str.format with the mapping's items as keywords.
template <class K, class V>
std::string str_format_map(std::string_view fmt, const dict<K, V>& mapping, std::string_view type_name) {
    std::vector<FormatArg> args;
    std::vector<std::string_view> names;
    std::vector<std::string> keys;
    keys.reserve(mapping.size());
    for (const auto& [k, v] : mapping) {
        keys.push_back(k);
        args.push_back(FormatArg{&format_arg<V>, &v, type_name});
    }
    for (const auto& k : keys) names.push_back(k);
    StrFormatter f{args, 0, names};
    return f.run(fmt, 2);
}

// ---- the same for bytes (widths and positions in bytes) ----

inline bytes bytes_ljust(const bytes& b, std::int64_t width, const bytes& fill = bytes(" ")) {
    if (fill.size() != 1) raise("TypeError", "ljust() argument 2 must be a byte string of length 1, not bytes");
    return bytes(pad_to(b.data, width, fill.data, b.size(), -1));
}
inline bytes bytes_rjust(const bytes& b, std::int64_t width, const bytes& fill = bytes(" ")) {
    if (fill.size() != 1) raise("TypeError", "rjust() argument 2 must be a byte string of length 1, not bytes");
    return bytes(pad_to(b.data, width, fill.data, b.size(), 1));
}
inline bytes bytes_center(const bytes& b, std::int64_t width, const bytes& fill = bytes(" ")) {
    if (fill.size() != 1) raise("TypeError", "center() argument 2 must be a byte string of length 1, not bytes");
    return bytes(pad_to(b.data, width, fill.data, b.size(), 0));
}
inline bytes bytes_zfill(const bytes& b, std::int64_t width) { return bytes(zfill_to(b.data, width, b.size())); }
inline bytes bytes_expandtabs(const bytes& b, std::int64_t tabsize = 8) { return bytes(expand_tabs(b.data, tabsize, false)); }
inline std::int64_t bytes_find(const bytes& b, const bytes& sub, opt_int start, opt_int end = std::nullopt) {
    return str_find(b.data, sub.data, start, end);
}
inline std::int64_t bytes_rfind(const bytes& b, const bytes& sub, opt_int start = std::nullopt, opt_int end = std::nullopt) {
    return str_rfind(b.data, sub.data, start, end);
}
inline std::int64_t bytes_index(const bytes& b, const bytes& sub, opt_int start = std::nullopt, opt_int end = std::nullopt) {
    return str_index(b.data, sub.data, start, end);
}
inline std::int64_t bytes_rindex(const bytes& b, const bytes& sub, opt_int start = std::nullopt, opt_int end = std::nullopt) {
    return str_rindex(b.data, sub.data, start, end);
}
inline std::int64_t bytes_count(const bytes& b, const bytes& sub, opt_int start, opt_int end = std::nullopt) {
    return str_count(b.data, sub.data, start, end);
}
inline std::vector<bytes> bytes_split(const bytes& b, const std::optional<bytes>& sep, std::int64_t maxsplit) {
    return as_bytes_list(str_split(b.data, sep ? std::optional<std::string>(sep->data) : std::nullopt, maxsplit));
}
inline std::vector<bytes> bytes_rsplit(const bytes& b, const std::optional<bytes>& sep = std::nullopt, std::int64_t maxsplit = -1) {
    return as_bytes_list(str_rsplit(b.data, sep ? std::optional<std::string>(sep->data) : std::nullopt, maxsplit));
}
inline bytes bytes_removeprefix(const bytes& b, const bytes& p) { return bytes(str_removeprefix(b.data, p.data)); }
inline bytes bytes_removesuffix(const bytes& b, const bytes& p) { return bytes(str_removesuffix(b.data, p.data)); }
inline bytes bytes_swapcase(const bytes& b) { return bytes(str_swapcase(b.data)); }
inline bool bytes_isascii(const bytes& b) { return str_isascii(b.data); }
inline bool bytes_istitle(const bytes& b) { return str_istitle(b.data); }
inline bytes bytes_fromhex(const std::string& s) {
    std::string out;
    int high = -1;
    for (std::size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (is_ws(c)) {
            if (high >= 0) raise("ValueError", "non-hexadecimal number found in fromhex() arg at position " + std::to_string(i));
            continue;
        }
        int v = std::isdigit(static_cast<unsigned char>(c)) ? c - '0'
                : (c >= 'a' && c <= 'f') ? c - 'a' + 10
                : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
        if (v < 0) raise("ValueError", "non-hexadecimal number found in fromhex() arg at position " + std::to_string(i));
        if (high < 0) {
            high = v;
        } else {
            out += static_cast<char>(high * 16 + v);
            high = -1;
        }
    }
    if (high >= 0) raise("ValueError", "non-hexadecimal number found in fromhex() arg at position " + std::to_string(s.size()));
    return bytes(out);
}
inline bytes bytes_maketrans(const bytes& from, const bytes& to) {
    if (from.size() != to.size()) raise("ValueError", "maketrans arguments must have same length");
    std::string table(256, '\0');
    for (int i = 0; i < 256; ++i) table[static_cast<std::size_t>(i)] = static_cast<char>(i);
    for (std::size_t i = 0; i < from.size(); ++i) table[static_cast<unsigned char>(from.data[i])] = to.data[i];
    return bytes(table);
}
inline bytes bytes_translate(const bytes& b, const std::optional<bytes>& table, const bytes& remove = bytes()) {
    if (table && table->size() != 256) raise("ValueError", "translation table must be 256 characters long");
    std::string out;
    for (char c : b.data) {
        if (remove.data.find(c) != std::string::npos) continue;
        out += table ? table->data[static_cast<unsigned char>(c)] : c;
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
    std::string what = std::strerror(err);
    std::string msg = "[Errno " + std::to_string(err) + "] " + what + (path ? ": " + repr_str(*path) : "");
    auto with = [&]<class E>() { return Thrown{std::make_shared<E>(msg, err, what, path)}; };
    switch (err) {
        case ECONNREFUSED: throw with.template operator()<ConnectionRefusedError>();
        case ECONNRESET: throw with.template operator()<ConnectionResetError>();
        case ECONNABORTED: throw with.template operator()<ConnectionAbortedError>();
        case EPIPE: throw with.template operator()<BrokenPipeError>();
        case ETIMEDOUT: throw with.template operator()<TimeoutError>();
        case ENOENT: throw with.template operator()<FileNotFoundError>();
        case EEXIST: throw with.template operator()<FileExistsError>();
        case EACCES:
        case EPERM: throw with.template operator()<PermissionError>();
        case EISDIR: throw with.template operator()<IsADirectoryError>();
        case ENOTDIR: throw with.template operator()<NotADirectoryError>();
    }
    throw Thrown{std::make_shared<OSError>(msg, err, what, path)};
}

// A file over callbacks (gzip.open...) can't throw through C stdio: the callback stores
// the exception here and fails the read, and the file object rethrows it.
inline thread_local std::exception_ptr pending_file_error;

struct FileBase {
    std::FILE* fp;
    std::string path, mode;
    bool owned = true;  // sys.stdout and friends aren't closed
    FileBase(std::FILE* f, std::string p, std::string m) : fp(f), path(std::move(p)), mode(std::move(m)) {}
    FileBase(const FileBase&) = delete;
    FileBase& operator=(const FileBase&) = delete;
    virtual ~FileBase() {
        if (fp && owned) std::fclose(fp);  // files close when the last reference goes away
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
        check_error(f);
        return out;
    }
    std::string readline_raw() {
        std::FILE* f = handle();
        char* line = nullptr;
        std::size_t capacity = 0;
        ssize_t len = ::getline(&line, &capacity, f);  // binary-safe, unlike fgets
        std::string out = len > 0 ? std::string(line, static_cast<std::size_t>(len)) : std::string();
        std::free(line);
        if (len < 0) check_error(f);
        return out;
    }
    std::int64_t write_raw(const std::string& s) {
        std::FILE* f = handle();
        if (std::fwrite(s.data(), 1, s.size(), f) != s.size()) check_error(f, true);
        return static_cast<std::int64_t>(s.size());
    }
    void check_error(std::FILE* f, bool failed = false) const {
        if (auto e = std::exchange(pending_file_error, nullptr)) std::rethrow_exception(e);
        if (failed || std::ferror(f)) raise_os(errno, path);
    }
    void close() {
        if (fp && owned) std::fclose(fp);
        fp = nullptr;
    }
    void flush() { std::fflush(handle()); }
    // f.closed, f.name, f.mode
    bool is_closed() const { return !fp; }
    std::string get_name() const { return path; }
    std::string get_mode() const { return mode; }
    std::FILE* open_handle() const {
        if (!fp) raise("ValueError", "I/O operation on closed file");
        return fp;
    }
    bool can_write() const { return mode.find_first_of("wax+") != std::string::npos; }
    bool readable() const { return open_handle() && mode.find_first_of("r+") != std::string::npos; }
    bool writable() const { return open_handle() && can_write(); }
    bool seekable() const { return ::lseek((fileno)(open_handle()), 0, SEEK_CUR) != -1; }
    bool isatty() const { return ::isatty((fileno)(open_handle())) == 1; }
    std::int64_t tell() {
        off_t at = ::ftello(handle());
        if (at < 0) raise_os(errno, std::nullopt);
        return at;
    }
    std::int64_t seek_raw(std::int64_t offset, std::int64_t whence) {
        if (::fseeko(handle(), offset, static_cast<int>(whence)) != 0) raise_os(errno, std::nullopt);
        return tell();
    }
    std::int64_t truncate(std::optional<std::int64_t> size = std::nullopt) {  // (the position stays where it was)
        std::FILE* f = handle();
        if (!can_write()) raise("OSError", "truncate");  // (Python's io.UnsupportedOperation says just this)
        std::int64_t n = size ? *size : tell();
        std::fflush(f);
        if (n < 0 || ::ftruncate((fileno)(f), n) != 0) raise_os(n < 0 ? EINVAL : errno, std::nullopt);
        return n;
    }
    // f.fileno(). ((fileno) calls the function: on macOS `fileno` is also a macro.)
    std::int64_t fileno_() const { return (fileno)(handle()); }
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
            line = Line(file->readline());  // (a text file's lines get universal newlines)
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

// Python's universal newlines: reading text turns \r\n (and a lone \r) into \n, unless the
// file was opened with newline="" (as the csv module wants).
inline std::string universal(std::string s) {
    if (s.find('\r') == std::string::npos) return s;
    std::string out;
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\r') {
            out += '\n';
            if (i + 1 < s.size() && s[i + 1] == '\n') ++i;
        } else {
            out += s[i];
        }
    }
    return out;
}

struct TextFile : FileBase {
    using FileBase::FileBase;
    bool translate_newlines = true;
    std::string text(std::string s) const { return translate_newlines ? universal(std::move(s)) : s; }
    std::string read(std::int64_t n = -1) { return text(read_raw(n)); }
    std::string readline() {
        if (!translate_newlines) return readline_raw();
        std::FILE* f = handle();  // a line ends at \n, \r\n or a lone \r (read as \n)
        std::string out;
        ::flockfile(f);
        for (int c; (c = getc_unlocked(f)) != EOF;) {
            if (c == '\r') {
                int next = getc_unlocked(f);
                if (next != '\n' && next != EOF) ungetc(next, f);
                out += '\n';
                break;
            }
            out += static_cast<char>(c);
            if (c == '\n') break;
        }
        ::funlockfile(f);
        if (out.empty()) check_error(f);
        return out;
    }
    std::vector<std::string> readlines() {
        std::vector<std::string> out;
        for (std::string line; !(line = readline()).empty();) out.push_back(line);
        return out;
    }
    std::int64_t write(const std::string& s) { return write_raw(s); }
    template <class It>
    void writelines(It&& lines) {
        for (auto&& line : iter(std::forward<It>(lines))) write_raw(line);
    }
    // A text file only seeks to a position tell() gave, or to its start or end, like Python's.
    std::int64_t seek(std::int64_t offset, std::int64_t whence = 0) {
        handle();
        if (whence < 0 || whence > 2)
            raise("ValueError", "invalid whence (" + std::to_string(whence) + ", should be 0, 1 or 2)");
        if (whence != 0 && offset != 0)
            raise("OSError", std::string("can't do nonzero ") + (whence == 1 ? "cur" : "end") + "-relative seeks");
        if (offset < 0) raise("ValueError", "negative seek position " + std::to_string(offset));
        return seek_raw(offset, whence);
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
    std::int64_t seek(std::int64_t offset, std::int64_t whence = 0) {
        handle();
        if (whence < 0 || whence > 2) raise("ValueError", "whence value " + std::to_string(whence) + " unsupported");
        return seek_raw(offset, whence);
    }
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

#if defined(__APPLE__)
#define SD_PLATFORM "darwin"
#elif defined(_WIN32)
#define SD_PLATFORM "win32"
#elif defined(__FreeBSD__)
#define SD_PLATFORM "freebsd"
#else
#define SD_PLATFORM "linux"
#endif

// sys.stdin / sys.stdout / sys.stderr (0, 1, 2): shared, and never closed.
inline std::shared_ptr<TextFile> std_stream(int which) {
    static const std::shared_ptr<TextFile> streams[3] = {
        [] { auto f = std::make_shared<TextFile>(stdin, "<stdin>", "r"); f->owned = false; return f; }(),
        [] { auto f = std::make_shared<TextFile>(stdout, "<stdout>", "w"); f->owned = false; return f; }(),
        [] { auto f = std::make_shared<TextFile>(stderr, "<stderr>", "w"); f->owned = false; return f; }(),
    };
    return streams[which];
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
    if (mode.find('a') != std::string::npos) ::fseeko(f, 0, SEEK_END);  // an appending file starts at its end, like Python's
    return f;
}

inline std::shared_ptr<TextFile> open_text(const std::string& path, const std::string& mode = "r",
                                           const std::string& encoding = "utf-8",
                                           std::optional<std::string> newline = std::nullopt) {
    check_encoding(encoding);
    auto f = std::make_shared<TextFile>(open_file(path, mode), path, mode);
    f->translate_newlines = !newline;  // newline="" (or "\n", ...): lines are read as they are
    return f;
}
inline std::shared_ptr<BinaryFile> open_binary(const std::string& path, const std::string& mode) {
    return std::make_shared<BinaryFile>(open_file(path, mode), path, mode);
}

// open(fd) / os.fdopen(fd): a file object for a descriptor the program already has. With
// closefd=False, closing the file leaves the descriptor open (the file uses a dup of it).
inline std::FILE* open_fd(std::int64_t fd, const std::string& mode, bool closefd) {
    std::string c_mode;
    for (char ch : mode)
        if (ch != 't') c_mode += ch == 'x' ? 'w' : ch;
    if (c_mode.find('b') == std::string::npos) c_mode += 'b';
    int use = closefd ? static_cast<int>(fd) : ::dup(static_cast<int>(fd));
    if (use < 0) raise_os(errno, std::nullopt);
    std::FILE* f = ::fdopen(use, c_mode.c_str());
    if (!f) {
        int err = errno;
        if (!closefd) ::close(use);
        raise_os(err, std::nullopt);
    }
    return f;
}
inline std::shared_ptr<TextFile> open_text(std::int64_t fd, const std::string& mode = "r",
                                           const std::string& encoding = "utf-8",
                                           std::optional<std::string> newline = std::nullopt, bool closefd = true) {
    check_encoding(encoding);
    auto f = std::make_shared<TextFile>(open_fd(fd, mode, closefd), std::to_string(fd), mode);
    f->translate_newlines = !newline;
    return f;
}
inline std::shared_ptr<BinaryFile> open_binary(std::int64_t fd, const std::string& mode, bool closefd = true) {
    return std::make_shared<BinaryFile>(open_fd(fd, mode, closefd), std::to_string(fd), mode);
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
T list_pop(const list<T>& v, std::int64_t i = -1) {
    if (v.empty()) raise("IndexError", "pop from empty list");
    if (i < 0) i += static_cast<std::int64_t>(v.size());
    if (i < 0 || i >= static_cast<std::int64_t>(v.size())) raise("IndexError", "pop index out of range");
    T out = std::move(v[static_cast<std::size_t>(i)]);
    v.erase(v.begin() + i);
    return out;
}
template <class T, class X>
void list_insert(const list<T>& v, std::int64_t i, X&& x) {
    std::int64_t n = static_cast<std::int64_t>(v.size());
    if (i < 0) i = std::max<std::int64_t>(0, i + n);
    v.insert(v.begin() + std::min(i, n), std::forward<X>(x));
}
template <class T, class X>
void list_remove(const list<T>& v, const X& x) {
    auto it = std::find(v.begin(), v.end(), x);
    if (it == v.end()) raise("ValueError", "list.remove(x): x not in list");
    v.erase(it);
}
template <class T, class X>
std::int64_t list_index(const list<T>& v, const X& x) {
    auto it = std::find(v.begin(), v.end(), x);
    if (it == v.end()) raise("ValueError", repr(x) + " is not in list");
    return it - v.begin();
}
template <class T, class X>
std::int64_t list_count(const list<T>& v, const X& x) {
    return std::count(v.begin(), v.end(), x);
}
template <class T, class It>
void list_extend(const list<T>& v, It&& it) {
    using U = std::remove_cvref_t<It>;
    if constexpr (is_list<U>::value) {
        if (it.identity() != v.identity()) {  // another list: straight from its vector
            auto& src = it.vec();
            v.vec().insert(v.vec().end(), src.begin(), src.end());
            return;
        }
    }
    auto items = to_list(std::forward<It>(it));  // copy first: `xs.extend(xs)` is fine
    v.insert(v.end(), items.begin(), items.end());
}
template <class T>
void list_sort(const list<T>& v, bool reverse = false) {
    if (reverse)
        sort_values(v.vec(), [](const auto& a, const auto& b) { return b < a; });
    else
        sort_values(v.vec(), std::less<>{});
}
template <class T>
void list_reverse(const list<T>& v) {
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
template <class K, class V, class It>
dict<K, V> dict_from_pairs(const It& pairs) {  // dict([(k, v), ...])
    dict<K, V> out;
    for (auto&& [k, v] : iter(pairs)) out[k] = v;
    return out;
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
void dict_update(const dict<K, V>& d, const dict<K, V>& other) {
    if (d.is(other)) return;
    for (const auto& [k, v] : other) d[k] = v;
}
template <class K, class V, class It>
    requires(!is_dict_like<It>::value)
void dict_update(const dict<K, V>& d, const It& pairs) {  // d.update([(k, v), ...])
    for (auto&& [k, v] : iter(pairs)) d[K(k)] = V(v);
}
template <class K, class V>
dict<K, V> dict_or(const dict<K, V>& a, const dict<K, V>& b) {  // a | b
    dict<K, V> out = a.copy();
    dict_update(out, b);
    return out;
}
template <class K, class V>
std::tuple<K, V> dict_popitem(const dict<K, V>& d) {
    return d.popitem();
}
template <class K, class V, class It>
dict<K, V> dict_fromkeys(const It& keys, const std::type_identity_t<V>& value) {
    dict<K, V> out;
    for (auto&& k : iter(keys)) out[K(k)] = value;
    return out;
}

template <class T>
void set_remove(const set<T>& s, const std::type_identity_t<T>& x) {
    if (!s.erase(x)) raise("KeyError", repr(x));
}
template <class T, class Other>
bool set_issubset(const set<T>& a, const Other& other) {
    set<T> b = as_set<T>(other);
    for (const auto& x : a)
        if (!b.contains(x)) return false;
    return true;
}
template <class T, class Other>
bool set_issuperset(const set<T>& a, const Other& other) {
    for (auto&& x : iter(other))
        if (!a.contains(T(x))) return false;
    return true;
}
template <class T>
bool set_proper_subset(const set<T>& a, const set<T>& b) {  // a < b
    return a.size() < b.size() && set_issubset(a, b);
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

template <class T>
struct std::hash<sd::vtuple<T>> {
    std::size_t operator()(const sd::vtuple<T>& t) const {
        std::size_t h = 0x345678;
        for (const auto& e : t.items) h = (h * 1000003) ^ sd::Hash{}(e);
        return h;
    }
};

using namespace sd::literals;
