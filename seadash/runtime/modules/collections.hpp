// The `collections` module: defaultdict, Counter and deque. All are values, like
// list and dict: assigning or passing one makes a copy.
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
        : base(std::move(init)), factory_(std::move(factory)), factory_repr_(std::move(factory_repr)) {}

    V& operator[](const K& k) {
        if (const V* v = this->find(k)) return const_cast<V&>(*v);
        if (!factory_) return this->at(k);  // raises KeyError
        V value = factory_();
        return base::operator[](k) = std::move(value);
    }
    // Reading through a const reference can't store the new value; it still gets one.
    V get_or_make(const K& k) const {
        if (const V* v = this->find(k)) return *v;
        if (!factory_) return this->at(k);
        return factory_();
    }
    std::string sd_repr() const {
        return "defaultdict(" + (factory_repr_.empty() ? std::string("None") : factory_repr_) + ", " +
               repr(static_cast<const base&>(*this)) + ")";
    }
};

template <class K, class V>
V& index(defaultdict<K, V>& d, const std::type_identity_t<K>& k) {
    return d[k];
}
template <class K, class V>
V index(const defaultdict<K, V>& d, const std::type_identity_t<K>& k) {
    return d.get_or_make(k);
}

// A dict of counts. c[k] reads 0 for a missing key (without storing it); c[k] += 1 stores.
template <class K>
class Counter : public dict<K, std::int64_t> {
    using base = dict<K, std::int64_t>;

public:
    Counter() = default;
    explicit Counter(base counts) : base(std::move(counts)) {}
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
    std::deque<T> d_;
    std::optional<std::int64_t> maxlen_;

    bool full() const { return maxlen_ && static_cast<std::int64_t>(d_.size()) >= *maxlen_; }
    template <class It>
    static std::vector<T> snapshot(const It& xs) {  // so d.extend(d) works
        std::vector<T> out;
        for (auto&& x : iter(xs)) out.push_back(x);
        return out;
    }

public:
    using value_type = T;
    deque() = default;
    explicit deque(std::optional<std::int64_t> maxlen) : maxlen_(maxlen) {
        if (maxlen && *maxlen < 0) raise("ValueError", "maxlen must be non-negative");
    }
    template <class It>
    deque(const It& xs, std::optional<std::int64_t> maxlen) : deque(maxlen) {
        extend(xs);
    }

    void append(T x) {
        if (maxlen_ && *maxlen_ == 0) return;
        if (full()) d_.pop_front();
        d_.push_back(std::move(x));
    }
    void appendleft(T x) {
        if (maxlen_ && *maxlen_ == 0) return;
        if (full()) d_.pop_back();
        d_.push_front(std::move(x));
    }
    T pop() {
        if (d_.empty()) raise("IndexError", "pop from an empty deque");
        T x = std::move(d_.back());
        d_.pop_back();
        return x;
    }
    T popleft() {
        if (d_.empty()) raise("IndexError", "pop from an empty deque");
        T x = std::move(d_.front());
        d_.pop_front();
        return x;
    }
    template <class It>
    void extend(const It& xs) {
        for (auto& x : snapshot(xs)) append(std::move(x));
    }
    template <class It>
    void extendleft(const It& xs) {
        for (auto& x : snapshot(xs)) appendleft(std::move(x));
    }
    void rotate(std::int64_t n = 1) {  // to the right; negative rotates left
        auto size = static_cast<std::int64_t>(d_.size());
        if (size <= 1) return;
        n = ((n % size) + size) % size;
        std::rotate(d_.begin(), d_.end() - n, d_.end());
    }
    void clear() { d_.clear(); }
    void reverse() { std::reverse(d_.begin(), d_.end()); }
    void insert(std::int64_t i, T x) {
        if (full()) raise("IndexError", "deque already at its maximum size");
        auto size = static_cast<std::int64_t>(d_.size());
        if (i < 0) i = std::max<std::int64_t>(i + size, 0);
        d_.insert(d_.begin() + std::min(i, size), std::move(x));
    }
    std::int64_t count(const T& x) const { return std::count(d_.begin(), d_.end(), x); }
    std::int64_t index(const T& x) const {
        auto it = std::find(d_.begin(), d_.end(), x);
        if (it == d_.end()) raise("ValueError", repr(x) + " is not in deque");
        return it - d_.begin();
    }
    void remove(const T& x) {
        auto it = std::find(d_.begin(), d_.end(), x);
        if (it == d_.end()) raise("ValueError", "deque.remove(x): x not in deque");
        d_.erase(it);
    }
    std::optional<std::int64_t> maxlen() const { return maxlen_; }

    T& at(std::int64_t i) { return d_[norm_index(i, d_.size(), "deque")]; }
    const T& at(std::int64_t i) const { return d_[norm_index(i, d_.size(), "deque")]; }
    auto begin() const { return d_.begin(); }
    auto end() const { return d_.end(); }
    auto begin() { return d_.begin(); }
    auto end() { return d_.end(); }
    std::size_t size() const { return d_.size(); }
    bool empty() const { return d_.empty(); }
    bool operator==(const deque& o) const { return d_ == o.d_; }  // maxlen doesn't matter, like Python

    std::string sd_repr() const {
        std::string out = "deque([";
        bool first = true;
        for (const auto& x : d_) {
            if (!first) out += ", ";
            first = false;
            out += repr(x);
        }
        out += "]";
        if (maxlen_) out += ", maxlen=" + std::to_string(*maxlen_);
        return out + ")";
    }
};

template <class T>
T& index(deque<T>& d, std::int64_t i) {
    return d.at(i);
}
template <class T>
const T& index(const deque<T>& d, std::int64_t i) {
    return d.at(i);
}
template <class T, class X>
bool contains(const deque<T>& d, const X& x) {
    return std::find(d.begin(), d.end(), x) != d.end();
}

}  // namespace sd
