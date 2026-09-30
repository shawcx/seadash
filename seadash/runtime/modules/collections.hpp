// The `collections` module: defaultdict, Counter and deque. All are references, like
// list and dict: assigning or passing one shares it.
#pragma once

#include <algorithm>
#include <deque>
#include <functional>

namespace sd {

// A dict whose d[k] first stores factory() for a missing key. It is a dict, so every
// dict operation works on it; only d[k] (and repr) differ.
template <class K, class V>
class defaultdict : public dict<K, V> {
    using base = dict<K, V>;
    std::function<V()> factory_;
    std::string factory_repr_;  // e.g. "<class 'list'>", for repr()

public:
    defaultdict() = default;
    defaultdict(std::function<V()> factory, std::string factory_repr, base init = {})
        : base(init.copy()), factory_(std::move(factory)), factory_repr_(std::move(factory_repr)) {}

    V& operator[](const K& k) const {
        if (const V* v = this->find(k)) return const_cast<V&>(*v);
        if (!factory_) return this->at(k);  // raises KeyError
        V value = factory_();
        return base::operator[](k) = std::move(value);
    }
    std::string sd_repr() const {
        return "defaultdict(" + (factory_repr_.empty() ? std::string("None") : factory_repr_) + ", " +
               repr(static_cast<const base&>(*this)) + ")";
    }
};

template <class K, class V>
V& index(const defaultdict<K, V>& d, const std::type_identity_t<K>& k) {
    return d[k];  // stores factory() for a missing key, even when only reading it, like Python
}

// A dict of counts. c[k] reads 0 for a missing key (without storing it); c[k] += 1 stores.
template <class K>
class Counter : public dict<K, std::int64_t> {
    using base = dict<K, std::int64_t>;

public:
    Counter() = default;
    explicit Counter(const base& counts) : base(counts.copy()) {}
    template <class It>
    static Counter from_items(const It& xs) {
        Counter c;
        c.update_items(xs);
        return c;
    }

    std::int64_t count_of(const K& k) const {
        const std::int64_t* v = this->find(k);
        return v ? *v : 0;
    }
    template <class It>
    void update_items(const It& xs) {
        for (auto&& x : iter(xs)) ++(*this)[x];
    }
    void update_counts(const base& other) {
        for (const auto& [k, v] : other) (*this)[k] += v;
    }
    template <class It>
    void subtract_items(const It& xs) {
        for (auto&& x : iter(xs)) --(*this)[x];
    }
    void subtract_counts(const base& other) {
        for (const auto& [k, v] : other) (*this)[k] -= v;
    }
    // Most common first; equal counts keep the order they were first seen.
    list<std::tuple<K, std::int64_t>> most_common(std::optional<std::int64_t> n = std::nullopt) const {
        list<std::tuple<K, std::int64_t>> out;
        for (const auto& [k, v] : *this) out.emplace_back(k, v);
        std::stable_sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return std::get<1>(a) > std::get<1>(b); });
        if (n && *n < static_cast<std::int64_t>(out.size())) out.resize(static_cast<std::size_t>(std::max<std::int64_t>(*n, 0)));
        return out;
    }
    list<K> elements() const {
        list<K> out;
        for (const auto& [k, v] : *this)
            for (std::int64_t i = 0; i < v; ++i) out.push_back(k);
        return out;
    }
    std::int64_t total() const {
        std::int64_t sum = 0;
        for (const auto& [k, v] : *this) sum += v;
        return sum;
    }

    // Arithmetic keeps only positive counts, like Python.
    template <class F>
    Counter combine(const Counter& o, F f) const {
        Counter out;
        for (const auto& [k, v] : *this)
            if (auto r = f(v, o.count_of(k)); r > 0) out[k] = r;
        for (const auto& [k, v] : o)
            if (!this->contains(k))
                if (auto r = f(std::int64_t{0}, v); r > 0) out[k] = r;
        return out;
    }
    Counter operator+(const Counter& o) const { return combine(o, [](auto a, auto b) { return a + b; }); }
    Counter operator-(const Counter& o) const { return combine(o, [](auto a, auto b) { return a - b; }); }
    Counter operator|(const Counter& o) const { return combine(o, [](std::int64_t a, std::int64_t b) { return std::max(a, b); }); }
    Counter operator&(const Counter& o) const { return combine(o, [](std::int64_t a, std::int64_t b) { return std::min(a, b); }); }

    std::string sd_repr() const {
        if (this->empty()) return "Counter()";
        std::string out = "Counter({";
        bool first = true;
        for (const auto& [k, v] : most_common()) {
            if (!first) out += ", ";
            first = false;
            out += repr(k) + ": " + std::to_string(v);
        }
        return out + "})";
    }
};

template <class K>
std::int64_t index(const Counter<K>& c, const std::type_identity_t<K>& k) {
    return c.count_of(k);
}

// A double-ended queue, optionally bounded: with maxlen, adding to one end drops
// items from the other.
template <class T>
class deque {
    struct State {
        std::deque<T> d;
        std::optional<std::int64_t> maxlen;
    };
    mutable std::shared_ptr<State> s_ = std::make_shared<State>();  // shared by copies, like list
    State& st() const {
        if (!s_) [[unlikely]]
            s_ = std::make_shared<State>();  // (only after being moved from)
        return *s_;
    }

    bool full() const { return st().maxlen && static_cast<std::int64_t>(st().d.size()) >= *st().maxlen; }
    template <class It>
    static std::vector<T> snapshot(const It& xs) {  // so d.extend(d) works
        std::vector<T> out;
        for (auto&& x : iter(xs)) out.push_back(x);
        return out;
    }

public:
    using value_type = T;
    deque() = default;
    explicit deque(std::optional<std::int64_t> maxlen) {
        st().maxlen = maxlen;
        if (maxlen && *maxlen < 0) raise("ValueError", "maxlen must be non-negative");
    }
    template <class It>
    deque(const It& xs, std::optional<std::int64_t> maxlen) : deque(maxlen) {
        extend(xs);
    }

    void append(T x) const {
        if (st().maxlen && *st().maxlen == 0) return;
        if (full()) st().d.pop_front();
        st().d.push_back(std::move(x));
    }
    void appendleft(T x) const {
        if (st().maxlen && *st().maxlen == 0) return;
        if (full()) st().d.pop_back();
        st().d.push_front(std::move(x));
    }
    T pop() const {
        if (st().d.empty()) raise("IndexError", "pop from an empty deque");
        T x = std::move(st().d.back());
        st().d.pop_back();
        return x;
    }
    T popleft() const {
        if (st().d.empty()) raise("IndexError", "pop from an empty deque");
        T x = std::move(st().d.front());
        st().d.pop_front();
        return x;
    }
    template <class It>
    void extend(const It& xs) const {
        for (auto& x : snapshot(xs)) append(std::move(x));
    }
    template <class It>
    void extendleft(const It& xs) const {
        for (auto& x : snapshot(xs)) appendleft(std::move(x));
    }
    void rotate(std::int64_t n = 1) const {  // to the right; negative rotates left
        auto size = static_cast<std::int64_t>(st().d.size());
        if (size <= 1) return;
        n = ((n % size) + size) % size;
        std::rotate(st().d.begin(), st().d.end() - n, st().d.end());
    }
    void clear() const { st().d.clear(); }
    void reverse() const { std::reverse(st().d.begin(), st().d.end()); }
    void insert(std::int64_t i, T x) const {
        if (full()) raise("IndexError", "deque already at its maximum size");
        auto size = static_cast<std::int64_t>(st().d.size());
        if (i < 0) i = std::max<std::int64_t>(i + size, 0);
        st().d.insert(st().d.begin() + std::min(i, size), std::move(x));
    }
    std::int64_t count(const T& x) const { return std::count(st().d.begin(), st().d.end(), x); }
    std::int64_t index(const T& x) const {
        auto it = std::find(st().d.begin(), st().d.end(), x);
        if (it == st().d.end()) raise("ValueError", repr(x) + " is not in deque");
        return it - st().d.begin();
    }
    void remove(const T& x) const {
        auto it = std::find(st().d.begin(), st().d.end(), x);
        if (it == st().d.end()) raise("ValueError", "deque.remove(x): x not in deque");
        st().d.erase(it);
    }
    std::optional<std::int64_t> maxlen() const { return st().maxlen; }

    T& at(std::int64_t i) const { return st().d[norm_index(i, st().d.size(), "deque")]; }
    auto begin() const { return st().d.begin(); }
    auto end() const { return st().d.end(); }
    const void* identity() const { return &st(); }
    deque copy() const {
        deque out;
        out.st() = st();
        return out;
    }
    deque sd_value_copy() const {
        deque out(st().maxlen);
        for (const auto& x : st().d) out.st().d.push_back(value_copy(x));
        return out;
    }

    // `for x in d`: raises, as Python does, if the deque changes during the loop.
    struct range {
        deque q;
        struct sentinel {};
        struct iterator {
            std::shared_ptr<State> s;
            std::size_t i, size;
            T& operator*() const { return s->d[i]; }
            iterator& operator++() {
                if (s->d.size() != size) raise("RuntimeError", "deque mutated during iteration");
                ++i;
                return *this;
            }
            bool operator!=(sentinel) const { return i < s->d.size(); }
        };
        iterator begin() const { return {(q.st(), q.s_), 0, q.size()}; }
        sentinel end() const { return {}; }
    };
    range sd_iter() const { return {*this}; }
    std::size_t size() const { return st().d.size(); }
    bool empty() const { return st().d.empty(); }
    bool operator==(const deque& o) const { return st().d == o.st().d; }  // maxlen doesn't matter, like Python

    std::string sd_repr() const {
        std::string out = "deque([";
        bool first = true;
        for (const auto& x : st().d) {
            if (!first) out += ", ";
            first = false;
            out += repr(x);
        }
        out += "]";
        if (st().maxlen) out += ", maxlen=" + std::to_string(*st().maxlen);
        return out + ")";
    }
};

template <class T>
T& index(const deque<T>& d, std::int64_t i) {
    return d.at(i);
}
template <class T, class X>
bool contains(const deque<T>& d, const X& x) {
    return std::find(d.begin(), d.end(), x) != d.end();
}

}  // namespace sd
