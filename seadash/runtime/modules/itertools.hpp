// The `itertools` module: lazy iterator building blocks, as generators. Arguments are
// taken by value (the iterators outlive the call); an iterator argument is shared,
// so it's consumed as the result is read, like Python.
#pragma once

#include <array>
#include <deque>

namespace sd::itertools {

// Any iterable as a lazy Generator<T> (converting each item to T).
template <class T, class It>
Generator<T> as_generator(It items) {
    for (auto&& x : iter(items)) co_yield T(x);
}

// A tuple (fixed r) or tuple[T, ...] from the items of a vector.
template <class Out, class T, std::size_t... I>
Out tuple_from(const std::vector<T>& v, std::index_sequence<I...>) {
    return Out(v[I]...);
}
template <class X> struct is_vtuple : std::false_type {};
template <class X> struct is_vtuple<vtuple<X>> : std::true_type {};

template <class Out, class T>
Out make_out(const std::vector<T>& v) {
    if constexpr (is_vtuple<Out>::value) {
        return Out(list<T>(v.begin(), v.end()));
    } else {
        return tuple_from<Out>(v, std::make_index_sequence<std::tuple_size_v<Out>>{});
    }
}

// ---- infinite iterators ----------------------------------------------------------

template <class T>
Generator<T> count(T start, T step) {
    for (T v = start;; v += step) co_yield v;
}

template <class T, class It>
Generator<T> cycle(It items) {
    std::vector<T> saved;
    for (auto&& x : iter(items)) {
        saved.push_back(T(x));
        co_yield T(x);
    }
    if (saved.empty()) co_return;
    for (;;)
        for (const auto& x : saved) co_yield x;
}

template <class T>
Generator<T> repeat(T value, std::optional<std::int64_t> times) {
    for (std::int64_t i = 0; !times || i < *times; ++i) co_yield value;
}

// ---- finite iterators ------------------------------------------------------------

template <class T, class It, class F>
Generator<T> accumulate(It items, F func, std::optional<T> initial) {
    std::optional<T> total = initial;
    if (total) co_yield *total;
    for (auto&& x : iter(items)) {
        total = total ? T(func(*total, T(x))) : T(x);
        co_yield *total;
    }
}

template <class T>
Generator<T> chain(std::vector<Generator<T>> parts) {
    for (auto& part : parts)
        for (auto&& x : part) co_yield x;
}

template <class T, class It>
Generator<T> from_iterable(It outer) {
    for (auto&& inner : iter(outer))
        for (auto&& x : iter(inner)) co_yield T(x);
}

template <class T, class Data, class Selectors>
Generator<T> compress(Data data, Selectors selectors) {
    auto d = as_generator<T>(std::move(data));
    auto s = iter(selectors);
    auto si = s.begin();
    for (auto&& x : d) {
        if (!(si != s.end())) co_return;
        if (truthy(*si)) co_yield x;
        ++si;
    }
}

template <class T, class F, class It>
Generator<T> dropwhile(F pred, It items) {
    bool dropping = true;
    for (auto&& x : iter(items)) {
        if (dropping && truthy(pred(x))) continue;
        dropping = false;
        co_yield T(x);
    }
}

template <class T, class F, class It>
Generator<T> takewhile(F pred, It items) {
    for (auto&& x : iter(items)) {
        if (!truthy(pred(x))) co_return;
        co_yield T(x);
    }
}

template <class T, class F, class It>
Generator<T> filterfalse(F pred, It items) {
    for (auto&& x : iter(items))
        if (!truthy(pred(x))) co_yield T(x);
}

// Consecutive items with the same key; each group is an iterator over its items.
template <class K, class T, class It, class F>
Generator<std::tuple<K, Generator<T>>> groupby(It items, F key) {
    std::optional<K> current;
    list<T> group;
    for (auto&& x : iter(items)) {
        K k = K(key(x));
        if (current && !(*current == k)) {
            co_yield std::tuple<K, Generator<T>>(*current, iterate<T>(std::move(group)));
            group = {};
        }
        current = k;
        group.push_back(T(x));
    }
    if (current) co_yield std::tuple<K, Generator<T>>(*current, iterate<T>(std::move(group)));
}

// Items start, start+step, ... before stop -- reading no further than it needs to.
template <class T, class It>
Generator<T> islice(It items, std::optional<std::int64_t> start, std::optional<std::int64_t> stop,
                    std::optional<std::int64_t> step) {
    std::int64_t from = start.value_or(0), by = step.value_or(1);
    if (from < 0 || (stop && *stop < 0))
        raise("ValueError", stop && *stop < 0 ? "Stop argument for islice() must be None or an integer: 0 <= x <= sys.maxsize."
                                              : "Indices for islice() must be None or an integer: 0 <= x <= sys.maxsize.");
    if (by < 1) raise("ValueError", "Step for islice() must be a positive integer or None.");
    if (stop && *stop <= from) co_return;
    std::int64_t i = 0, next = from;
    for (auto&& x : iter(items)) {
        if (i == next) {
            co_yield T(x);
            next += by;
            if (stop && next >= *stop) co_return;
        }
        ++i;
    }
}

template <class R, class F, class It>
Generator<R> starmap(F func, It items) {
    for (auto&& args : iter(items)) co_yield R(std::apply(func, args));
}

template <class T, class It>
Generator<std::tuple<T, T>> pairwise(It items) {
    std::optional<T> previous;
    for (auto&& x : iter(items)) {
        if (previous) co_yield std::tuple<T, T>(*previous, T(x));
        previous = T(x);
    }
}

template <class T, class It>
Generator<vtuple<T>> batched(It items, std::int64_t n) {
    if (n < 1) raise("ValueError", "n must be at least one");
    list<T> batch;
    for (auto&& x : iter(items)) {
        batch.push_back(T(x));
        if (static_cast<std::int64_t>(batch.size()) == n) {
            co_yield vtuple<T>(std::move(batch));
            batch = {};
        }
    }
    if (!batch.empty()) co_yield vtuple<T>(std::move(batch));
}

// tee(): n independent iterators over one source, sharing what's been read.
template <class T>
struct TeeSource {
    Generator<T> source;
    std::vector<T> seen;
};
template <class T>
Generator<T> tee_one(std::shared_ptr<TeeSource<T>> shared) {
    for (std::size_t i = 0;; ++i) {
        while (shared->seen.size() <= i) {
            if (!shared->source.advance()) co_return;
            shared->seen.push_back(shared->source.take());
        }
        co_yield shared->seen[i];
    }
}
template <class T, std::size_t... I>
auto tee_n(std::shared_ptr<TeeSource<T>> shared, std::index_sequence<I...>) {
    return std::make_tuple(((void)I, tee_one<T>(shared))...);
}
template <class T, std::size_t N, class It>
auto tee(It items) {
    auto shared = std::make_shared<TeeSource<T>>();
    shared->source = as_generator<T>(std::move(items));
    return tee_n<T>(shared, std::make_index_sequence<N>{});
}

// zip_longest: until every input is exhausted, filling the shorter ones.
template <class R, class F, std::size_t... I, class... Gs>
Generator<R> zip_longest_impl(std::index_sequence<I...>, F fill, Gs... gens) {
    std::array<bool, sizeof...(I)> done{};
    auto pull = [&](auto& g, std::size_t j) {
        using E = decltype(g.take());
        if (!done[j] && g.advance()) return std::optional<E>(g.take());
        done[j] = true;
        return std::optional<E>();
    };
    while (true) {
        std::tuple<decltype(pull(gens, I))...> row{pull(gens, I)...};  // (in order, left to right)
        if (std::all_of(done.begin(), done.end(), [](bool d) { return d; })) co_return;
        co_yield R((std::get<I>(row) ? std::tuple_element_t<I, R>(*std::get<I>(row)) : std::tuple_element_t<I, R>(fill))...);
    }
}
template <class R, class F, class... Gs>
Generator<R> zip_longest(F fill, Gs... gens) {
    return zip_longest_impl<R>(std::index_sequence_for<Gs...>{}, std::move(fill), std::move(gens)...);
}

// ---- combinatorics (their inputs are read fully first, like Python) --------------------

template <class R, std::size_t... I, class... Pools>
Generator<R> product_impl(std::index_sequence<I...>, Pools... pools) {
    if ((pools.empty() || ...)) co_return;
    std::array<std::size_t, sizeof...(I)> at{};
    std::array<std::size_t, sizeof...(I)> sizes{pools.size()...};
    while (true) {
        co_yield R(pools[at[I]]...);
        std::size_t k = sizeof...(I);  // odometer: the rightmost position turns fastest
        while (k > 0) {
            --k;
            if (++at[k] < sizes[k]) break;
            at[k] = 0;
            if (k == 0) co_return;
        }
        if (sizeof...(I) == 0) co_return;
    }
}
template <class R, class... Pools>
Generator<R> product(Pools... pools) {
    return product_impl<R>(std::index_sequence_for<Pools...>{}, std::move(pools)...);
}

template <class Out, class T>
Generator<Out> permutations(list<T> pool, std::optional<std::int64_t> r_) {
    std::int64_t n = static_cast<std::int64_t>(pool.size()), r = r_.value_or(n);
    if (r < 0) raise("ValueError", "r must be non-negative");
    if (r > n) co_return;
    std::vector<std::int64_t> indices(n), cycles(r);
    for (std::int64_t i = 0; i < n; ++i) indices[i] = i;
    for (std::int64_t i = 0; i < r; ++i) cycles[i] = n - i;
    auto emit = [&] {
        std::vector<T> out;
        for (std::int64_t i = 0; i < r; ++i) out.push_back(pool[indices[i]]);
        return make_out<Out>(out);
    };
    co_yield emit();
    while (n) {
        std::int64_t i = r - 1;
        for (; i >= 0; --i) {
            if (--cycles[i] == 0) {
                std::rotate(indices.begin() + i, indices.begin() + i + 1, indices.end());
                cycles[i] = n - i;
            } else {
                std::swap(indices[i], indices[n - cycles[i]]);
                co_yield emit();
                break;
            }
        }
        if (i < 0) co_return;
    }
}

template <class Out, class T>
Generator<Out> combinations(list<T> pool, std::int64_t r, bool with_replacement) {
    std::int64_t n = static_cast<std::int64_t>(pool.size());
    if (r < 0) raise("ValueError", "r must be non-negative");
    if (with_replacement ? (n == 0 && r > 0) : r > n) co_return;
    std::vector<std::int64_t> indices(r);
    for (std::int64_t i = 0; i < r; ++i) indices[i] = with_replacement ? 0 : i;
    auto emit = [&] {
        std::vector<T> out;
        for (auto i : indices) out.push_back(pool[i]);
        return make_out<Out>(out);
    };
    co_yield emit();
    while (true) {
        std::int64_t i = r - 1;
        while (i >= 0 && indices[i] == (with_replacement ? n - 1 : i + n - r)) --i;
        if (i < 0) co_return;
        if (with_replacement) {
            std::int64_t v = indices[i] + 1;
            for (std::int64_t j = i; j < r; ++j) indices[j] = v;
        } else {
            ++indices[i];
            for (std::int64_t j = i + 1; j < r; ++j) indices[j] = indices[j - 1] + 1;
        }
        co_yield emit();
    }
}

}  // namespace sd::itertools
