// The `copy` module: copy.copy (a new container, or a new object, holding the same items)
// and copy.deepcopy (copies all the way down, class instances included, keeping objects
// that appear twice shared and following cycles, like Python's memo).
//
// A class gets two virtual hooks from the compiler when the program imports copy:
// sd_copy() (a new object of its runtime class with the same field values, or what its
// __copy__ returns) and sd_deep_copy(memo), both returning an sd::object pointer.
// (value_copy in seadash.hpp is the thread-boundary copy: it shares class instances.)
#pragma once

#include <unordered_map>

namespace sd::copymod {

template <class T>
T deep_copy(const T& x, CopyMemo& memo);

template <class K, class V>
dict<K, V> as_dict(const dict<K, V>*);  // (only its type: the dict a Counter or defaultdict is)
template <class K, class V>
K dict_key(const dict<K, V>*);

// Does a T hold class instances (which a deep copy must copy, even as dict keys or set items)?
template <class T>
constexpr bool holds_objects() {
    if constexpr (is_shared<T>::value) {
        return requires(const T& x, CopyMemo& m) { x->sd_deep_copy(m); };
    } else if constexpr (is_optional<T>::value) {
        return holds_objects<typename T::value_type>();
    } else if constexpr (is_tuple<T>::value) {
        return []<class... A>(std::tuple<A...>*) { return (holds_objects<A>() || ...); }(static_cast<T*>(nullptr));
    } else {
        return requires { typename T::sd_is_handle; };  // (what's inside is checked when it's copied)
    }
}

template <class T>
T deep_copy(const T& x, CopyMemo& memo) {
    if constexpr (is_shared<T>::value && requires { x->sd_deep_copy(memo); }) {
        if (!x) return x;
        auto it = memo.done.find(dynamic_cast<const void*>(x.get()));
        if (it != memo.done.end()) return std::dynamic_pointer_cast<typename T::element_type>(std::static_pointer_cast<object>(it->second));
        return std::dynamic_pointer_cast<typename T::element_type>(x->sd_deep_copy(memo));
    } else if constexpr (is_list<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        T out;
        memo.add(x, out);
        out.reserve(x.size());
        for (const auto& e : x) out.push_back(deep_copy(e, memo));
        return out;
    } else if constexpr (is_sd_set<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        if constexpr (holds_objects<typename T::value_type>()) {
            T out;
            memo.add(x, out);
            for (const auto& e : x) out.insert(deep_copy(e, memo));
            return out;
        } else {
            T out = x.copy();
            memo.add(x, out);
            return out;
        }
    } else if constexpr (is_dict_like<T>::value) {
        if (const T* seen = memo.find(x)) return *seen;
        T out = x;
        out.detach();  // (a Counter or defaultdict keeps its type, and a defaultdict its factory)
        memo.add(x, out);
        using Dict = decltype(as_dict(static_cast<const T*>(nullptr)));
        if constexpr (holds_objects<decltype(dict_key(static_cast<const T*>(nullptr)))>()) {
            out.clear();
            auto& d = static_cast<const Dict&>(out);
            for (const auto& [k, v] : x) {
                auto key = deep_copy(k, memo);
                d[key] = deep_copy(v, memo);
            }
        } else {
            out.for_each_value([&](auto& v) { v = deep_copy(v, memo); });
        }
        return out;
    } else if constexpr (requires { typename T::sd_is_handle; x.appendleft(std::declval<typename T::value_type>()); }) {
        if (const T* seen = memo.find(x)) return *seen;  // a deque
        T out(x.maxlen());
        memo.add(x, out);
        for (const auto& e : x) out.append(deep_copy(e, memo));
        return out;
    } else if constexpr (is_optional<T>::value) {
        return x ? T(deep_copy(*x, memo)) : T();
    } else if constexpr (is_tuple<T>::value) {
        return std::apply([&](const auto&... e) { return T(deep_copy(e, memo)...); }, x);
    } else {
        return x;  // numbers, strings, functions; a @value class copies itself
    }
}

// copy.deepcopy(x)
template <class T>
T deepcopy(const T& x) {
    CopyMemo memo;
    return deep_copy(x, memo);
}

// copy.copy(x)
template <class T>
T copy(const T& x) {
    if constexpr (is_shared<T>::value && requires { x->sd_copy(); }) {
        return x ? std::dynamic_pointer_cast<typename T::element_type>(x->sd_copy()) : x;
    } else if constexpr (requires { typename T::sd_is_handle; }) {
        return shallow_copy(x);  // (a deque keeps its maxlen)
    } else if constexpr (is_optional<T>::value) {
        return x ? T(copy(*x)) : T();
    } else {
        return x;  // a tuple holds the same items; a @value class copies itself
    }
}

// copy.copy(x) where x's class inherits __copy__, which makes objects of class R.
template <class R, class T>
auto copy_as(const T& x) {
    if constexpr (is_optional<T>::value) {
        return x ? std::optional<std::shared_ptr<R>>(copy_as<R>(*x)) : std::nullopt;
    } else {
        return std::dynamic_pointer_cast<R>(x->sd_copy());
    }
}

// A class's sd_deep_copy: `out` is a new object holding this one's fields; copy them all
// the way down, after noting the copy (so a cycle back to this object finds it).
template <class C>
void note_copy(const C* original, const std::shared_ptr<C>& out, CopyMemo& memo) {
    memo.done.emplace(dynamic_cast<const void*>(original), std::shared_ptr<void>(std::shared_ptr<object>(out)));
}

}  // namespace sd::copymod
