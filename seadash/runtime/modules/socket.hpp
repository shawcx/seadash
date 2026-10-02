// The `socket` module: blocking BSD sockets with Python's API and errors.
// A Socket is a handle (copies share the connection) and is safe to share between
// threads: its state is guarded, and blocking calls simply run in parallel.
#pragma once

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <mutex>

namespace sd::socket {

struct gaierror : OSError {
    using OSError::OSError;
    std::string sd_type() const override { return "socket.gaierror"; }
};

using Address = std::tuple<std::string, std::int64_t>;

[[noreturn]] inline void fail() { raise_os(errno, std::nullopt); }

[[noreturn]] inline void timed_out() { raise<TimeoutError>("timed out"); }

inline Address from_sockaddr(const sockaddr_storage& sa) {
    char host[INET6_ADDRSTRLEN] = {};
    if (sa.ss_family == AF_INET6) {
        auto& a = reinterpret_cast<const sockaddr_in6&>(sa);
        inet_ntop(AF_INET6, &a.sin6_addr, host, sizeof host);
        return {host, ntohs(a.sin6_port)};
    }
    auto& a = reinterpret_cast<const sockaddr_in&>(sa);
    inet_ntop(AF_INET, &a.sin_addr, host, sizeof host);
    return {host, ntohs(a.sin_port)};
}

// Resolve (host, port) like Python: "" means any address (for bind), otherwise a name or IP.
inline sockaddr_storage resolve(const Address& addr, int family, int type, bool passive, socklen_t& len) {
    const auto& [host, port] = addr;
    if (port < 0 || port > 65535) raise("OverflowError", "port must be 0-65535.");
    addrinfo hints{};
    hints.ai_family = family;
    hints.ai_socktype = type;
    hints.ai_flags = passive ? AI_PASSIVE : 0;
    addrinfo* result = nullptr;
    std::string service = std::to_string(port);
    int rc = getaddrinfo(host.empty() ? nullptr : host.c_str(), service.c_str(), &hints, &result);
    if (rc != 0) {
        throw Thrown{std::make_shared<gaierror>("[Errno " + std::to_string(rc) + "] " + gai_strerror(rc))};
    }
    sockaddr_storage out{};
    std::memcpy(&out, result->ai_addr, result->ai_addrlen);
    len = result->ai_addrlen;
    freeaddrinfo(result);
    return out;
}

class Socket {
    struct State {
        std::atomic<int> fd{-1};
        int family = AF_INET, type = SOCK_STREAM;
        std::mutex mu;
        std::optional<double> timeout;
        ~State() {
            if (fd >= 0) ::close(fd);
        }
    };
    std::shared_ptr<State> s_;

    State& state() const {
        if (!s_) raise("OSError", "socket was never created");
        return *s_;
    }
    int handle() const {
        int fd = state().fd.load();
        if (fd < 0) raise_os(EBADF, std::nullopt);
        return fd;
    }
    std::optional<double> timeout() const {
        std::lock_guard lk(state().mu);
        return state().timeout;
    }
    // Wait until the socket is readable/writable, respecting the timeout.
    void wait_for(short events) const {
        auto t = timeout();
        if (!t) return;
        pollfd p{handle(), events, 0};
        int rc = ::poll(&p, 1, static_cast<int>(*t * 1000));
        if (rc == 0 && *t == 0) raise_os(EAGAIN, std::nullopt);  // non-blocking: BlockingIOError, like Python
        if (rc == 0) timed_out();
        if (rc < 0) fail();
    }

public:
    Socket() = default;
    Socket(std::int64_t family, std::int64_t type, std::int64_t proto = 0,
           std::optional<std::int64_t> fileno = std::nullopt)
        : s_(std::make_shared<State>()) {
        if (fileno) {  // socket.socket(fileno=fd): take over an existing socket (closed with this one)
            int fd = static_cast<int>(*fileno);
            int kind = 0;
            socklen_t len = sizeof kind;
            if (::getsockopt(fd, SOL_SOCKET, SO_TYPE, &kind, &len) != 0) fail();
            sockaddr_storage sa{};
            socklen_t sa_len = sizeof sa;
            if (::getsockname(fd, reinterpret_cast<sockaddr*>(&sa), &sa_len) != 0) fail();
            s_->fd = fd;
            s_->family = sa.ss_family;
            s_->type = kind;
            return;
        }
        int fd = ::socket(static_cast<int>(family), static_cast<int>(type), static_cast<int>(proto));
        if (fd < 0) fail();
        s_->fd = fd;
        s_->family = static_cast<int>(family);
        s_->type = static_cast<int>(type);
    }
    static Socket adopt(int fd, int family, int type, std::optional<double> timeout) {
        Socket s;
        s.s_ = std::make_shared<State>();
        s.s_->fd = fd;
        s.s_->family = family;
        s.s_->type = type;
        s.s_->timeout = timeout;
        return s;
    }

    void settimeout(std::optional<double> t) {
        if (t && *t < 0) raise("ValueError", "Timeout value out of range");
        std::lock_guard lk(state().mu);
        state().timeout = t;
    }
    std::optional<double> gettimeout() const { return timeout(); }
    // setblocking(False) is settimeout(0.0): calls that would wait raise BlockingIOError.
    void setblocking(bool flag) { settimeout(flag ? std::nullopt : std::optional<double>(0.0)); }
    bool getblocking() const { return timeout() != 0.0; }

    void connect(const Address& addr) {
        socklen_t len = 0;
        sockaddr_storage sa = resolve(addr, state().family, state().type, false, len);
        int fd = handle();
        if (!timeout()) {
            if (::connect(fd, reinterpret_cast<sockaddr*>(&sa), len) != 0) fail();
            return;
        }
        // With a timeout: connect without blocking, then wait for the result.
        int flags = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        int rc = ::connect(fd, reinterpret_cast<sockaddr*>(&sa), len);
        int err = rc == 0 ? 0 : errno;
        if (err == EINPROGRESS && *timeout() == 0) {  // non-blocking: it goes on connecting (select for writing)
            fcntl(fd, F_SETFL, flags);
            raise_os(err, std::nullopt);
        }
        if (err == EINPROGRESS) {
            pollfd p{fd, POLLOUT, 0};
            int ready = ::poll(&p, 1, static_cast<int>(*timeout() * 1000));
            if (ready == 0) {
                fcntl(fd, F_SETFL, flags);
                timed_out();
            }
            socklen_t errlen = sizeof err;
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &errlen);
        }
        fcntl(fd, F_SETFL, flags);
        if (err != 0) raise_os(err, std::nullopt);
    }
    void bind(const Address& addr) {
        socklen_t len = 0;
        sockaddr_storage sa = resolve(addr, state().family, state().type, true, len);
        if (::bind(handle(), reinterpret_cast<sockaddr*>(&sa), len) != 0) fail();
    }
    void listen(std::int64_t backlog = 128) {
        if (::listen(handle(), static_cast<int>(backlog)) != 0) fail();
    }
    std::tuple<Socket, Address> accept() {
        wait_for(POLLIN);
        sockaddr_storage sa{};
        socklen_t len = sizeof sa;
        int fd = ::accept(handle(), reinterpret_cast<sockaddr*>(&sa), &len);
        if (fd < 0) fail();
        return {adopt(fd, state().family, state().type, std::nullopt), from_sockaddr(sa)};  // blocking, as in Python
    }

    template <class B>
    std::int64_t send(const B& data) {
        const std::string& d = raw(data);
        wait_for(POLLOUT);
        ssize_t n = ::send(handle(), d.data(), d.size(), MSG_NOSIGNAL);  // EPIPE, not SIGPIPE, like Python
        if (n < 0) fail();
        return n;
    }
    template <class B>
    void sendall(const B& data) {
        const std::string& d = raw(data);
        std::size_t sent = 0;
        while (sent < d.size()) {
            wait_for(POLLOUT);
            ssize_t n = ::send(handle(), d.data() + sent, d.size() - sent, MSG_NOSIGNAL);
            if (n < 0) fail();
            sent += static_cast<std::size_t>(n);
        }
    }
    bytes recv(std::int64_t bufsize) {
        if (bufsize < 0) raise("ValueError", "negative buffersize in recv");
        wait_for(POLLIN);
        std::string buf(static_cast<std::size_t>(bufsize), '\0');
        ssize_t n = ::recv(handle(), buf.data(), buf.size(), 0);
        if (n < 0) fail();
        buf.resize(static_cast<std::size_t>(n));  // b"" means the other side closed
        return bytes(buf);
    }
    template <class B>
    std::int64_t sendto(const B& data, const Address& addr) {
        const std::string& d = raw(data);
        socklen_t len = 0;
        sockaddr_storage sa = resolve(addr, state().family, state().type, false, len);
        wait_for(POLLOUT);
        ssize_t n = ::sendto(handle(), d.data(), d.size(), MSG_NOSIGNAL, reinterpret_cast<sockaddr*>(&sa), len);
        if (n < 0) fail();
        return n;
    }
    std::tuple<bytes, Address> recvfrom(std::int64_t bufsize) {
        wait_for(POLLIN);
        std::string buf(static_cast<std::size_t>(bufsize), '\0');
        sockaddr_storage sa{};
        socklen_t len = sizeof sa;
        ssize_t n = ::recvfrom(handle(), buf.data(), buf.size(), 0, reinterpret_cast<sockaddr*>(&sa), &len);
        if (n < 0) fail();
        buf.resize(static_cast<std::size_t>(n));
        return {bytes(buf), from_sockaddr(sa)};
    }

    void setsockopt(std::int64_t level, std::int64_t option, std::int64_t value) {
        int v = static_cast<int>(value);
        if (::setsockopt(handle(), static_cast<int>(level), static_cast<int>(option), &v, sizeof v) != 0) fail();
    }
    Address getsockname() const {
        sockaddr_storage sa{};
        socklen_t len = sizeof sa;
        if (::getsockname(handle(), reinterpret_cast<sockaddr*>(&sa), &len) != 0) fail();
        return from_sockaddr(sa);
    }
    Address getpeername() const {
        sockaddr_storage sa{};
        socklen_t len = sizeof sa;
        if (::getpeername(handle(), reinterpret_cast<sockaddr*>(&sa), &len) != 0) fail();
        return from_sockaddr(sa);
    }
    void shutdown(std::int64_t how) {
        if (::shutdown(handle(), static_cast<int>(how)) != 0) fail();
    }
    void close() {
        if (!s_) return;
        int fd = s_->fd.exchange(-1);
        if (fd >= 0) ::close(fd);
    }
    std::int64_t fileno() const { return s_ ? s_->fd.load() : -1; }
    bool is(const Socket& o) const { return s_ == o.s_; }  // the same socket object (Python's `is`)
    friend bool operator==(const Socket& a, const Socket& b) { return a.is(b); }  // (Python's == is `is` too)
    std::string sd_repr() const {
        return "<socket fd=" + std::to_string(fileno()) + (s_ ? ", family=" + std::to_string(s_->family) +
               ", type=" + std::to_string(s_->type) : "") + ">";
    }
};

inline Socket create_connection(const Address& addr, std::optional<double> timeout = std::nullopt) {
    socklen_t len = 0;
    sockaddr_storage sa = resolve(addr, AF_UNSPEC, SOCK_STREAM, false, len);
    Socket s(sa.ss_family, SOCK_STREAM);
    s.settimeout(timeout);
    s.connect(addr);
    return s;
}

inline Socket create_server(const Address& addr, std::int64_t family = AF_INET,
                            std::optional<std::int64_t> backlog = std::nullopt, bool reuse_port = false) {
    Socket s(family, SOCK_STREAM);
    s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1);  // like Python on POSIX: restart servers without waiting
    if (reuse_port) s.setsockopt(SOL_SOCKET, SO_REUSEPORT, 1);
    s.bind(addr);
    s.listen(backlog ? *backlog : 128);
    return s;
}

inline std::string gethostname() {
    char name[256] = {};
    if (::gethostname(name, sizeof name) != 0) fail();
    return name;
}

inline std::string gethostbyname(const std::string& host) {
    socklen_t len = 0;
    sockaddr_storage sa = resolve({host, 0}, AF_INET, SOCK_STREAM, false, len);
    return std::get<0>(from_sockaddr(sa));
}

}  // namespace sd::socket
