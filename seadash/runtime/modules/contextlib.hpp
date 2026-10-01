// The `contextlib` module: @contextmanager, nullcontext, closing, suppress and ExitStack.
//
// Every context manager contextlib makes is a ContextManager<T> (a shared handle, like
// Python's objects): enter() gives what `with ... as x` binds (nothing when T is void),
// and sd_exit(e) runs the exit, given the exception leaving the with block (nullptr when
// it ended normally). sd_exit returns true to swallow the exception; it may also throw
// a different one.
//
// A @contextmanager function is a coroutine returning ContextManager<T>. Its one `yield`
// suspends it while the with block runs; an exception from the block is thrown in at the
// yield (the awaiter's await_resume rethrows it), so try/except/finally around the yield
// see it, as in Python.
#pragma once

#include <variant>

namespace sd::contextlib {

template <class T>
using Slot = std::conditional_t<std::is_void_v<T>, std::monostate, T>;  // (a bare `yield` gives None)

template <class T>
struct Impl {
    virtual ~Impl() = default;
    virtual T enter() = 0;
    virtual bool exit(std::exception_ptr e) = 0;
};

template <class T>
class ContextManager {
public:
    struct promise_type;
    using handle = std::coroutine_handle<promise_type>;

    struct Resume {  // what `yield` waits on: resumed by the exit, with the block's exception if any
        promise_type* p;
        bool await_ready() noexcept { return false; }
        void await_suspend(handle) noexcept {}
        void await_resume() {
            if (auto e = std::exchange(p->thrown_in, nullptr)) std::rethrow_exception(e);
        }
    };
    struct promise_type {
        std::optional<Slot<T>> value;
        std::exception_ptr error, thrown_in;
        ContextManager get_return_object();
        std::suspend_always initial_suspend() noexcept { return {}; }  // the body runs on enter()
        std::suspend_always final_suspend() noexcept { return {}; }
        Resume yield_value(Slot<T> v) {
            value = std::move(v);
            return {this};
        }
        void return_void() {}
        void unhandled_exception() { error = std::current_exception(); }
    };

    std::shared_ptr<Impl<T>> impl;

    T enter() const { return impl->enter(); }
    bool sd_exit(std::exception_ptr e) const { return impl->exit(std::move(e)); }
    std::string sd_repr() const { return "<contextlib context manager>"; }
};

// The coroutine behind a @contextmanager function's call: Python's _GeneratorContextManager.
template <class T>
struct GeneratorImpl : Impl<T> {
    using handle = typename ContextManager<T>::handle;
    handle h;
    bool entered = false;
    explicit GeneratorImpl(handle h_) : h(h_) {}
    GeneratorImpl(const GeneratorImpl&) = delete;
    ~GeneratorImpl() override { h.destroy(); }

    bool step() {  // run to the next yield: false once the generator has finished
        h.resume();
        if (!h.done()) return true;
        if (auto e = std::exchange(h.promise().error, nullptr)) std::rethrow_exception(e);
        return false;
    }
    T enter() override {
        if (entered) raise<AttributeError>("'_GeneratorContextManager' object has no attribute 'args'");
        entered = true;
        if (!step()) raise<RuntimeError>("generator didn't yield");
        if constexpr (std::is_void_v<T>) return; else return std::move(*h.promise().value);
    }
    bool exit(std::exception_ptr e) override {
        if (h.done()) return false;
        if (!e) {
            if (!step()) return false;
            raise<RuntimeError>("generator didn't stop");
        }
        h.promise().thrown_in = e;
        if (step()) raise<RuntimeError>("generator didn't stop after throw()");
        return true;  // it caught the exception and finished: swallowed (raising again came out of step())
    }
};

template <class T>
ContextManager<T> ContextManager<T>::promise_type::get_return_object() {
    return ContextManager{std::make_shared<GeneratorImpl<T>>(handle::from_promise(*this))};
}

template <class T>
struct NullImpl : Impl<T> {
    Slot<T> value;
    explicit NullImpl(Slot<T> v) : value(std::move(v)) {}
    T enter() override {
        if constexpr (std::is_void_v<T>) return; else return value;
    }
    bool exit(std::exception_ptr) override { return false; }
};

template <class T>
ContextManager<T> nullcontext(T value) {
    return {std::make_shared<NullImpl<T>>(std::move(value))};
}
inline ContextManager<void> nullcontext() { return {std::make_shared<NullImpl<void>>(std::monostate{})}; }

template <class T, class F>
struct ClosingImpl : Impl<T> {
    T thing;
    F close;
    ClosingImpl(T t, F f) : thing(std::move(t)), close(std::move(f)) {}
    T enter() override { return thing; }
    bool exit(std::exception_ptr) override {
        close(thing);
        return false;
    }
};

template <class T, class F>
ContextManager<T> closing(T thing, F close) {  // close(thing) calls its close() method
    return {std::make_shared<ClosingImpl<T, F>>(std::move(thing), std::move(close))};
}

template <class F>
struct SuppressImpl : Impl<void> {
    F matches;  // is this exception one of the classes given?
    explicit SuppressImpl(F f) : matches(std::move(f)) {}
    void enter() override {}
    bool exit(std::exception_ptr e) override {
        if (!e) return false;
        try {
            std::rethrow_exception(e);
        } catch (const Thrown& t) {
            return matches(t);
        } catch (...) {
            return false;  // (sys.exit() isn't an exception seadash code can catch)
        }
    }
};

template <class F>
ContextManager<void> suppress(F matches) {
    return {std::make_shared<SuppressImpl<F>>(std::move(matches))};
}

// The exception a with block raised, as the class an __exit__(exc) parameter takes (null if
// it's another class, or not an exception seadash code can see).
template <class E>
std::shared_ptr<E> thrown_as(const std::exception_ptr& e) {
    if (!e) return nullptr;
    try {
        std::rethrow_exception(e);
    } catch (const Thrown& t) {
        return std::dynamic_pointer_cast<E>(t.exc);
    } catch (...) {
        return nullptr;
    }
}

// ExitStack: exits run last-in first-out when it's closed, each seeing the exception still
// in flight (one that swallows it hides it from the rest; one that raises replaces it).
// Copies share the stack.
class ExitStack {
    using Exit = std::function<bool(std::exception_ptr)>;
    std::shared_ptr<std::vector<Exit>> exits_ = std::make_shared<std::vector<Exit>>();

public:
    void push_exit(Exit f) const { exits_->push_back(std::move(f)); }

    template <class F, class... A>
    F callback(F f, A... args) const {
        push_exit([f, args...](std::exception_ptr) mutable {
            f(args...);
            return false;
        });
        return f;
    }

    ExitStack pop_all() const {  // a new stack with this one's exits; this one is left empty
        ExitStack out;
        out.exits_->swap(*exits_);
        return out;
    }

    ExitStack enter() const { return *this; }
    void close() const { sd_exit(nullptr); }

    bool sd_exit(std::exception_ptr exc) const {
        bool received = exc != nullptr, suppressed = false, pending = false;
        while (!exits_->empty()) {
            Exit f = std::move(exits_->back());
            exits_->pop_back();
            try {
                if (f(exc)) {
                    suppressed = true;
                    pending = false;
                    exc = nullptr;
                }
            } catch (...) {
                exc = std::current_exception();
                pending = true;
            }
        }
        if (pending) std::rethrow_exception(exc);
        return received && suppressed;
    }
    std::string sd_repr() const { return "<contextlib.ExitStack object>"; }
};

// stack.enter_context(cm) for contextlib's own context managers (and another ExitStack).
template <class C>
auto enter_context(const ExitStack& stack, C cm) {
    if constexpr (std::is_void_v<decltype(cm.enter())>) {
        cm.enter();
        stack.push_exit([cm](std::exception_ptr e) { return cm.sd_exit(std::move(e)); });
    } else {
        auto value = cm.enter();
        stack.push_exit([cm](std::exception_ptr e) { return cm.sd_exit(std::move(e)); });
        return value;
    }
}

}  // namespace sd::contextlib
