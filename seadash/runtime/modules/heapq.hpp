// The `heapq` module: binary min-heaps kept in a list, plus nlargest, nsmallest and a
// lazy merge. The sift functions are CPython's own (heapq.py, which _heapqmodule.c
// follows), so a heap holds its items in exactly the order Python's would.
#pragma once

#include <algorithm>
#include <numeric>

namespace sd::heapq {

// Move the item at `pos` up towards `start` until its parent isn't greater. Wherever an
// exception from __lt__ stops it, the item goes back in the hole, so no item is lost.
template <class V, class Less>
void siftdown(V& heap, std::size_t start, std::size_t pos, Less less) {
    auto item = std::move(heap[pos]);
    try {
        while (pos > start) {
            std::size_t parent = (pos - 1) >> 1;
            if (!less(item, heap[parent])) break;
            heap[pos] = std::move(heap[parent]);
            pos = parent;
        }
    } catch (...) {
        heap[pos] = std::move(item);
        throw;
    }
    heap[pos] = std::move(item);
}

// Move the smaller child up until a leaf is reached, then sift the item at `pos` down
// there (fewer comparisons than stopping early: the leaf is usually its place).
template <class V, class Less>
void siftup(V& heap, std::size_t pos, Less less) {
    std::size_t end = heap.size(), start = pos, child = 2 * pos + 1;
    auto item = std::move(heap[pos]);
    try {
        while (child < end) {
            std::size_t right = child + 1;
            if (right < end && !less(heap[child], heap[right])) child = right;
            heap[pos] = std::move(heap[child]);
            pos = child;
            child = 2 * pos + 1;
        }
    } catch (...) {
        heap[pos] = std::move(item);
        throw;
    }
    heap[pos] = std::move(item);
    siftdown(heap, start, pos, less);
}

template <class V, class Less>
void heapify_with(V& heap, Less less) {
    for (std::size_t i = heap.size() / 2; i-- > 0;) siftup(heap, i, less);
}

template <class T>
void heappush(const list<T>& heap, typename list<T>::value_type item) {
    auto& v = heap.vec();
    v.push_back(std::move(item));
    siftdown(v, 0, v.size() - 1, std::less<>{});
}

template <class T>
typename list<T>::value_type heappop(const list<T>& heap) {
    auto& v = heap.vec();
    if (v.empty()) raise("IndexError", "index out of range");
    auto last = std::move(v.back());
    v.pop_back();
    if (v.empty()) return last;
    auto top = std::exchange(v[0], std::move(last));
    siftup(v, 0, std::less<>{});
    return top;
}

template <class T>
void heapify(const list<T>& heap) {
    heapify_with(heap.vec(), std::less<>{});
}

// Pop the smallest, then push item (the heap must not be empty).
template <class T>
typename list<T>::value_type heapreplace(const list<T>& heap, typename list<T>::value_type item) {
    auto& v = heap.vec();
    if (v.empty()) raise("IndexError", "index out of range");
    auto top = std::exchange(v[0], std::move(item));
    siftup(v, 0, std::less<>{});
    return top;
}

// Push item, then pop the smallest (item itself if it's no bigger than the top).
template <class T>
typename list<T>::value_type heappushpop(const list<T>& heap, typename list<T>::value_type item) {
    auto& v = heap.vec();
    if (!v.empty() && v[0] < item) {
        std::swap(item, v[0]);
        siftup(v, 0, std::less<>{});
    }
    return item;
}

// nsmallest/nlargest: the same as sorted(items, key=key, reverse=largest)[:n] (ties keep
// their order), with each key computed once and only n items kept in order.
template <class T, class It, class K>
list<T> select(std::int64_t n, It items, K key, bool largest) {
    list<T> out;
    if (n <= 0) return out;  // (the items aren't read, as in Python)
    std::vector<T> values;
    for (auto&& x : iter(items)) values.push_back(T(x));
    auto pick = [&](const auto& keys) {
        std::vector<std::size_t> order(values.size());
        std::iota(order.begin(), order.end(), 0);
        auto first = [&](std::size_t a, std::size_t b) {
            if (largest ? keys[b] < keys[a] : keys[a] < keys[b]) return true;
            if (largest ? keys[a] < keys[b] : keys[b] < keys[a]) return false;
            return a < b;
        };
        std::size_t m = std::min<std::size_t>(static_cast<std::size_t>(n), values.size());
        std::partial_sort(order.begin(), order.begin() + static_cast<std::ptrdiff_t>(m), order.end(), first);
        auto& v = out.vec();
        v.reserve(m);
        for (std::size_t i = 0; i < m; ++i) v.push_back(values[order[i]]);
    };
    if constexpr (std::is_null_pointer_v<K>) {
        pick(values);
    } else {
        using KT = std::remove_cvref_t<decltype(key(values[0]))>;
        std::vector<KT> keys;
        keys.reserve(values.size());
        for (const auto& x : values) keys.push_back(key(x));
        pick(keys);
    }
    return out;
}
template <class T, class It, class K = std::nullptr_t>
list<T> nsmallest(std::int64_t n, It items, K key = nullptr) {
    return select<T>(n, std::move(items), std::move(key), false);
}
template <class T, class It, class K = std::nullptr_t>
list<T> nlargest(std::int64_t n, It items, K key = nullptr) {
    return select<T>(n, std::move(items), std::move(key), true);
}

// Any iterable as a lazy Generator<T>, for merge.
template <class T, class It>
Generator<T> as_generator(It items) {
    for (auto&& x : iter(items)) co_yield T(x);
}

template <class T, class K>
struct key_result {
    using type = std::remove_cvref_t<std::invoke_result_t<K&, const T&>>;
};
template <class T>
struct key_result<T, std::nullptr_t> {
    using type = std::nullptr_t;
};

// merge(*iterables, key=None, reverse=False): CPython's algorithm, a heap of each input's
// next item (and its key), ordered by (key, input number) so equal items come out in input
// order.
// It reads the first item of every input when the first value is wanted.
template <class T, class K>
Generator<T> merge(std::vector<Generator<T>> inputs, K key, bool reverse) {
    constexpr bool keyed = !std::is_null_pointer_v<K>;
    using KT = typename key_result<T, K>::type;
    struct Entry {
        KT k;
        std::int64_t order;
        T value;
        std::size_t input;
    };
    auto key_of = [&](const T& value) -> KT {
        if constexpr (keyed) return key(value);
        else return nullptr;
    };
    // Python compares the lists [key, order, ...]: the first items that aren't == decide,
    // by <. (So items that are neither ==, < nor > don't fall through to the order.)
    auto less = [&](const Entry& a, const Entry& b) {
        if constexpr (keyed) {
            if (!(a.k == b.k)) return bool(a.k < b.k);
        } else {
            if (!(a.value == b.value)) return bool(a.value < b.value);
        }
        return a.order < b.order;
    };
    auto greater = [&](const Entry& a, const Entry& b) { return less(b, a); };
    std::int64_t direction = reverse ? -1 : 1;
    std::vector<Entry> heap;
    for (std::size_t i = 0; i < inputs.size(); ++i) {
        if (!inputs[i].advance()) continue;
        T value = inputs[i].take();
        KT k = key_of(value);
        heap.push_back(Entry{std::move(k), static_cast<std::int64_t>(i) * direction, std::move(value), i});
    }
    auto sift = [&](std::size_t pos) {
        if (reverse) siftup(heap, pos, greater);
        else siftup(heap, pos, less);
    };
    if (reverse) heapify_with(heap, greater);
    else heapify_with(heap, less);
    while (heap.size() > 1) {
        Entry& top = heap[0];
        co_yield top.value;
        if (inputs[top.input].advance()) {
            top.value = inputs[top.input].take();
            top.k = key_of(top.value);
            sift(0);
        } else {  // (that input is done: pop it)
            heap[0] = std::move(heap.back());
            heap.pop_back();
            if (heap.size() > 1) sift(0);
        }
    }
    if (heap.empty()) co_return;
    co_yield heap[0].value;  // the last input left: the rest of it as it comes
    auto& last = inputs[heap[0].input];
    while (last.advance()) co_yield last.take();
}

}  // namespace sd::heapq
