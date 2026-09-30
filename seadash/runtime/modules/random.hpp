// The `random` module, reproducing CPython's results exactly: the same Mersenne
// Twister, seeded the same way (init_by_array), and the same algorithms on top
// (randrange's rejection sampling, shuffle, sample, choices, gauss). So
// random.seed(42) gives the same numbers in seadash as in Python.
#pragma once

#include <mutex>
#include <random>

namespace sd::random {

// MT19937, as in CPython's _randommodule.c.
class MT19937 {
    static constexpr int N = 624, M = 397;
    std::uint32_t mt[N];
    int mti = N + 1;

    void init_genrand(std::uint32_t s) {
        mt[0] = s;
        for (mti = 1; mti < N; mti++)
            mt[mti] = 1812433253U * (mt[mti - 1] ^ (mt[mti - 1] >> 30)) + static_cast<std::uint32_t>(mti);
    }

public:
    void init_by_array(const std::vector<std::uint32_t>& key) {
        init_genrand(19650218U);
        std::size_t i = 1, j = 0;
        for (std::size_t k = std::max<std::size_t>(N, key.size()); k; k--) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1664525U)) + key[j] + static_cast<std::uint32_t>(j);
            i++;
            j++;
            if (i >= N) {
                mt[0] = mt[N - 1];
                i = 1;
            }
            if (j >= key.size()) j = 0;
        }
        for (std::size_t k = N - 1; k; k--) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1566083941U)) - static_cast<std::uint32_t>(i);
            i++;
            if (i >= N) {
                mt[0] = mt[N - 1];
                i = 1;
            }
        }
        mt[0] = 0x80000000U;
    }

    std::uint32_t next() {
        static constexpr std::uint32_t mag01[2] = {0x0U, 0x9908b0dfU};
        std::uint32_t y;
        if (mti >= N) {
            int kk;
            for (kk = 0; kk < N - M; kk++) {
                y = (mt[kk] & 0x80000000U) | (mt[kk + 1] & 0x7fffffffU);
                mt[kk] = mt[kk + M] ^ (y >> 1) ^ mag01[y & 0x1U];
            }
            for (; kk < N - 1; kk++) {
                y = (mt[kk] & 0x80000000U) | (mt[kk + 1] & 0x7fffffffU);
                mt[kk] = mt[kk + (M - N)] ^ (y >> 1) ^ mag01[y & 0x1U];
            }
            y = (mt[N - 1] & 0x80000000U) | (mt[0] & 0x7fffffffU);
            mt[N - 1] = mt[M - 1] ^ (y >> 1) ^ mag01[y & 0x1U];
            mti = 0;
        }
        y = mt[mti++];
        y ^= (y >> 11);
        y ^= (y << 7) & 0x9d2c5680U;
        y ^= (y << 15) & 0xefc60000U;
        y ^= (y >> 18);
        return y;
    }
};

struct State {
    MT19937 mt;
    std::optional<double> gauss_next;
    std::recursive_mutex mu;  // one generator shared by all threads, like Python's
};

inline void seed_state(State& s, std::optional<std::int64_t> a) {
    std::vector<std::uint32_t> key;
    if (a) {
        // Python: the 32-bit words of abs(a), least significant first ([0] for 0).
        std::uint64_t n = *a < 0 ? static_cast<std::uint64_t>(-(*a + 1)) + 1 : static_cast<std::uint64_t>(*a);
        do {
            key.push_back(static_cast<std::uint32_t>(n & 0xffffffffU));
            n >>= 32;
        } while (n);
    } else {
        std::random_device device;  // like Python's os.urandom seeding
        for (int i = 0; i < 624; ++i) key.push_back(device());
    }
    s.mt.init_by_array(key);
    s.gauss_next.reset();
}

inline State& state() {
    static State s;
    static const bool seeded = (seed_state(s, std::nullopt), true);  // thread-safe one-time init
    (void)seeded;
    return s;
}

// Every function that touches the shared generator holds its lock for the whole call.
#define SD_RANDOM_LOCK std::lock_guard<std::recursive_mutex> sd_random_lock(state().mu)

inline void seed(std::optional<std::int64_t> a = std::nullopt) {
    SD_RANDOM_LOCK;
    seed_state(state(), a);
}

// A float in [0, 1) with 53 random bits, exactly as CPython builds it.
inline double random() {
    SD_RANDOM_LOCK;
    std::uint32_t a = state().mt.next() >> 5, b = state().mt.next() >> 6;
    return (a * 67108864.0 + b) * (1.0 / 9007199254740992.0);
}

inline std::int64_t getrandbits(std::int64_t k) {
    if (k < 0) raise("ValueError", "number of bits must be non-negative");
    if (k > 63) raise("ValueError", "getrandbits() in seadash returns an int, so k must be at most 63");
    if (k == 0) return 0;
    SD_RANDOM_LOCK;
    if (k <= 32) return state().mt.next() >> (32 - k);
    std::uint64_t low = state().mt.next();
    std::uint64_t high = state().mt.next() >> (64 - k);
    return static_cast<std::int64_t>(low | (high << 32));
}

inline std::int64_t bit_length(std::uint64_t n) {
    std::int64_t bits = 0;
    while (n) {
        ++bits;
        n >>= 1;
    }
    return bits;
}

// A uniform int in [0, n): rejection sampling on getrandbits, like CPython's _randbelow.
inline std::int64_t randbelow(std::int64_t n) {
    SD_RANDOM_LOCK;
    std::int64_t k = bit_length(static_cast<std::uint64_t>(n));
    std::int64_t r = getrandbits(k);
    while (r >= n) r = getrandbits(k);
    return r;
}

inline std::int64_t randrange(std::int64_t start, std::optional<std::int64_t> stop = std::nullopt, std::int64_t step = 1) {
    if (!stop) {
        if (start > 0) return randbelow(start);
        raise("ValueError", "empty range for randrange()");
    }
    std::int64_t width = *stop - start;
    if (step == 1) {
        if (width > 0) return start + randbelow(width);
        raise("ValueError", "empty range in randrange(" + std::to_string(start) + ", " + std::to_string(*stop) + ")");
    }
    if (step == 0) raise("ValueError", "zero step for randrange()");
    std::int64_t n = step > 0 ? (width + step - 1) / step : (width + step + 1) / step;
    if (n <= 0) raise("ValueError", "empty range in randrange()");
    return start + step * randbelow(n);
}

inline std::int64_t randint(std::int64_t a, std::int64_t b) { return randrange(a, b + 1); }

inline double uniform(double a, double b) { return a + (b - a) * random(); }

inline double gauss(double mu = 0.0, double sigma = 1.0) {
    SD_RANDOM_LOCK;
    State& s = state();
    double z;
    if (s.gauss_next) {
        z = *s.gauss_next;
        s.gauss_next.reset();
    } else {
        double x2pi = random() * 2 * std::numbers::pi;
        double g2rad = std::sqrt(-2.0 * std::log(1.0 - random()));
        z = std::cos(x2pi) * g2rad;
        s.gauss_next = std::sin(x2pi) * g2rad;
    }
    return mu + z * sigma;
}

// Sequences: lists, strings, bytes, and anything else via to_list (e.g. range()).
template <class Seq>
decltype(auto) as_sequence(const Seq& s) {
    if constexpr (is_vector<Seq>::value || std::is_same_v<Seq, std::string>) {
        return (s);
    } else {
        return to_list(s);
    }
}
template <class Seq>
auto pick(const Seq& s, std::int64_t i) {
    if constexpr (std::is_same_v<Seq, std::string>) {
        return std::string(1, s[static_cast<std::size_t>(i)]);
    } else {
        return s[static_cast<std::size_t>(i)];
    }
}

template <class Seq>
auto choice(const Seq& population) {
    auto&& seq = as_sequence(population);
    if (seq.empty()) raise("IndexError", "Cannot choose from an empty sequence");
    return pick(seq, randbelow(static_cast<std::int64_t>(seq.size())));
}

template <class T>
void shuffle(std::vector<T>& x) {
    SD_RANDOM_LOCK;
    for (std::size_t i = x.size(); i-- > 1;) {
        std::size_t j = static_cast<std::size_t>(randbelow(static_cast<std::int64_t>(i) + 1));
        std::swap(x[i], x[j]);
    }
}

template <class Seq>
auto sample(const Seq& population, std::int64_t k) {
    SD_RANDOM_LOCK;
    auto&& seq = as_sequence(population);
    std::int64_t n = static_cast<std::int64_t>(seq.size());
    if (k < 0 || k > n) raise("ValueError", "Sample larger than population or is negative");
    using Elem = decltype(pick(seq, 0));
    std::vector<Elem> result(static_cast<std::size_t>(k));
    // CPython picks a strategy by size; follow it so the same seed gives the same sample.
    std::int64_t setsize = 21;
    if (k > 5) setsize += static_cast<std::int64_t>(std::pow(4.0, std::ceil(std::log(k * 3.0) / std::log(4.0))));
    if (n <= setsize) {
        std::vector<Elem> pool;
        pool.reserve(static_cast<std::size_t>(n));
        for (std::int64_t i = 0; i < n; ++i) pool.push_back(pick(seq, i));
        for (std::int64_t i = 0; i < k; ++i) {
            std::int64_t j = randbelow(n - i);
            result[i] = pool[j];
            pool[j] = pool[n - i - 1];
        }
    } else {
        std::set<std::int64_t> selected;
        for (std::int64_t i = 0; i < k; ++i) {
            std::int64_t j = randbelow(n);
            while (selected.count(j)) j = randbelow(n);
            selected.insert(j);
            result[i] = pick(seq, j);
        }
    }
    return result;
}

template <class Seq>
auto choices(const Seq& population, std::optional<std::vector<double>> weights = std::nullopt,
             std::optional<std::vector<double>> cum_weights = std::nullopt, std::int64_t k = 1) {
    SD_RANDOM_LOCK;
    auto&& seq = as_sequence(population);
    std::int64_t n = static_cast<std::int64_t>(seq.size());
    std::vector<decltype(pick(seq, 0))> out;
    if (!weights && !cum_weights) {
        if (n == 0) raise("IndexError", "Cannot choose from an empty population");
        for (std::int64_t i = 0; i < k; ++i)
            out.push_back(pick(seq, static_cast<std::int64_t>(std::floor(random() * static_cast<double>(n)))));
        return out;
    }
    if (weights && cum_weights) raise("TypeError", "Cannot specify both weights and cumulative weights");
    std::vector<double> cum = cum_weights ? *cum_weights : std::vector<double>{};
    if (weights) {
        double total = 0;
        for (double w : *weights) cum.push_back(total += w);
    }
    if (static_cast<std::int64_t>(cum.size()) != n) raise("ValueError", "The number of weights does not match the population");
    double total = cum.empty() ? 0.0 : cum.back();
    if (total <= 0.0) raise("ValueError", "Total of weights must be greater than zero");
    for (std::int64_t i = 0; i < k; ++i) {
        double x = random() * total;
        auto it = std::upper_bound(cum.begin(), cum.begin() + (n - 1), x);  // bisect_right(cum, x, 0, n - 1)
        out.push_back(pick(seq, it - cum.begin()));
    }
    return out;
}

inline bytes randbytes(std::int64_t n) {
    if (n < 0) raise("ValueError", "negative argument not allowed");
    SD_RANDOM_LOCK;
    // CPython: getrandbits(n * 8) as little-endian bytes, generated a 32-bit word at a time.
    std::string out;
    std::int64_t bits = n * 8;
    while (bits > 0) {
        std::uint32_t word = state().mt.next();
        if (bits < 32) word >>= (32 - bits);
        for (int b = 0; b < 4 && static_cast<std::int64_t>(out.size()) < n; ++b) out += static_cast<char>((word >> (8 * b)) & 0xff);
        bits -= 32;
    }
    return bytes(out);
}

#undef SD_RANDOM_LOCK

}  // namespace sd::random
