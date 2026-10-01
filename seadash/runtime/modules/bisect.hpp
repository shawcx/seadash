// The `bisect` module: binary search in a sorted list, and insertion keeping it sorted.
// With key=, x is compared with key(item) (bisect_*), or key(x) is (insort_*), as in Python.
#pragma once

namespace sd::bisect {

// The search range, checked like Python's: hi=None (or -1, which CPython takes as None)
// is the list's length.
inline std::int64_t start(std::int64_t lo) {
    if (lo < 0) raise("ValueError", "lo must be non-negative");
    return lo;
}
inline std::int64_t stop(std::optional<std::int64_t> hi, std::size_t size) {
    return !hi || *hi == -1 ? static_cast<std::int64_t>(size) : *hi;
}

template <class V, class K>
decltype(auto) key_at(const V& v, std::int64_t i, K& key) {
    if (static_cast<std::size_t>(i) >= v.size()) raise("IndexError", "list index out of range");  // (hi past the end)
    if constexpr (std::is_null_pointer_v<K>) return (v[static_cast<std::size_t>(i)]);
    else return key(v[static_cast<std::size_t>(i)]);
}

template <class T, class X, class K = std::nullptr_t>
std::int64_t bisect_left(const list<T>& a, const X& x, std::int64_t lo = 0, std::optional<std::int64_t> hi = std::nullopt,
                         K key = nullptr) {
    const auto& v = a.vec();
    lo = start(lo);
    std::int64_t h = stop(hi, v.size());
    while (lo < h) {
        std::int64_t mid = lo + (h - lo) / 2;
        if (key_at(v, mid, key) < x) lo = mid + 1;
        else h = mid;
    }
    return lo;
}

template <class T, class X, class K = std::nullptr_t>
std::int64_t bisect_right(const list<T>& a, const X& x, std::int64_t lo = 0, std::optional<std::int64_t> hi = std::nullopt,
                          K key = nullptr) {
    const auto& v = a.vec();
    lo = start(lo);
    std::int64_t h = stop(hi, v.size());
    while (lo < h) {
        std::int64_t mid = lo + (h - lo) / 2;
        if (x < key_at(v, mid, key)) h = mid;
        else lo = mid + 1;
    }
    return lo;
}

template <class T, class K = std::nullptr_t>
void insort_left(const list<T>& a, typename list<T>::value_type x, std::int64_t lo = 0,
                 std::optional<std::int64_t> hi = std::nullopt, K key = nullptr) {
    std::int64_t i;
    if constexpr (std::is_null_pointer_v<K>) i = bisect_left(a, x, lo, hi);
    else i = bisect_left(a, key(x), lo, hi, key);
    list_insert(a, i, std::move(x));
}

template <class T, class K = std::nullptr_t>
void insort_right(const list<T>& a, typename list<T>::value_type x, std::int64_t lo = 0,
                  std::optional<std::int64_t> hi = std::nullopt, K key = nullptr) {
    std::int64_t i;
    if constexpr (std::is_null_pointer_v<K>) i = bisect_right(a, x, lo, hi);
    else i = bisect_right(a, key(x), lo, hi, key);
    list_insert(a, i, std::move(x));
}

}  // namespace sd::bisect
