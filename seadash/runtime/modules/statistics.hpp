// The `statistics` module, following CPython's statistics.py: the same algorithms, error
// messages and edge cases. Python does mean and variance in exact fractions and rounds
// once; here they're computed in doubles with exactly rounded sums (fsum, and a sumprod
// whose products are kept exactly), so a result can differ from Python's in the last digit.
// Numeric results are floats (Python gives an int when the data are ints and the result
// happens to be whole); median_low/median_high/mode/multimode return the data's own items.
#pragma once

#include <algorithm>
#include <cmath>
#include <initializer_list>
#include <numbers>
#include <optional>
#include <string>
#include <vector>

#include "random.hpp"

namespace sd::statistics {

struct StatisticsError : ValueError {
    using ValueError::ValueError;
    std::string sd_type() const override { return "statistics.StatisticsError"; }
};

[[noreturn]] inline void error(const std::string& msg) { sd::raise<StatisticsError>(msg); }

// The data as doubles (ints, floats or bools from any iterable).
template <class It>
std::vector<double> floats(It&& data) {
    std::vector<double> out;
    for (auto&& x : iter(std::forward<It>(data))) out.push_back(static_cast<double>(x));
    return out;
}

// math.fsum: Shewchuk's exactly rounded sum, as CPython does it.
inline double fsum(const std::vector<double>& xs) {
    std::vector<double> p;
    double special_sum = 0.0, inf_sum = 0.0;
    for (double x : xs) {
        double xsave = x;
        std::size_t i = 0;
        for (double y : p) {
            if (std::fabs(x) < std::fabs(y)) std::swap(x, y);
            double hi = x + y;
            double lo = y - (hi - x);
            if (lo != 0.0) p[i++] = lo;
            x = hi;
        }
        p.resize(i);
        if (x != 0.0) {
            if (!std::isfinite(x)) {
                if (std::isfinite(xsave)) raise("OverflowError", "intermediate overflow in fsum");
                if (std::isinf(xsave)) inf_sum += xsave;
                special_sum += xsave;
                p.clear();
            } else {
                p.push_back(x);
            }
        }
    }
    if (special_sum != 0.0) {
        if (std::isnan(inf_sum)) raise("ValueError", "-inf + inf in fsum");
        return special_sum;
    }
    double hi = 0.0;
    std::size_t n = p.size();
    if (n > 0) {
        hi = p[--n];
        double lo = 0.0;
        while (n > 0) {
            double x = hi, y = p[--n];
            hi = x + y;
            double yr = hi - x;
            lo = y - yr;
            if (lo != 0.0) break;
        }
        if (n > 0 && ((lo < 0.0 && p[n - 1] < 0.0) || (lo > 0.0 && p[n - 1] > 0.0))) {
            double y = lo * 2.0;
            double x = hi + y;
            double yr = x - hi;
            if (y == yr) hi = x;
        }
    }
    return hi;
}

// Adds a*b to the parts of a sum exactly (the product's rounding error is a double too).
inline void add_product(std::vector<double>& parts, double a, double b) {
    double p = a * b;
    parts.push_back(p);
    if (std::isfinite(p)) parts.push_back(std::fma(a, b, -p));
}

// math.sumprod, exactly rounded.
inline double sumprod(const std::vector<double>& a, const std::vector<double>& b) {
    std::vector<double> parts;
    for (std::size_t i = 0; i < a.size(); ++i) add_product(parts, a[i], b[i]);
    return fsum(parts);
}

// Python's mean and variance work in fractions and round once at the end. These come
// close: `parts` is a sum held exactly (as doubles whose sum is the value), and the
// division (or square root) is corrected by its exactly computed remainder.
inline double divide(std::vector<double> parts, double d) {
    double q = fsum(parts) / d;
    if (!std::isfinite(q) || q == 0.0) return q;
    double p = q * d;
    parts.push_back(-p);
    parts.push_back(-std::fma(q, d, -p));
    return q + fsum(parts) / d;  // (parts now sum to the remainder)
}

inline double divide(double n, const std::vector<double>& parts) {
    double t = fsum(parts), q = n / t;
    if (!std::isfinite(q) || q == 0.0) return q;
    std::vector<double> rest{n};
    for (double x : parts) {
        double p = q * x;
        rest.push_back(-p);
        rest.push_back(-std::fma(q, x, -p));
    }
    return q + fsum(rest) / t;
}

// sqrt(sum(parts) / d), nearly always correctly rounded.
inline double sqrt_ratio(std::vector<double> parts, double d) {
    double s = std::sqrt(divide(parts, d));
    if (!std::isfinite(s) || s == 0.0) return s;
    double a = s * s, b = std::fma(s, s, -a);  // s*s == a + b
    double c1 = a * d, c3 = b * d;
    for (double c : {c1, std::fma(a, d, -c1), c3, std::fma(b, d, -c3)}) parts.push_back(-c);
    return s + fsum(parts) / (2.0 * s * d);
}

template <class It>
double mean(It&& data) {
    auto xs = floats(std::forward<It>(data));
    if (xs.empty()) error("mean requires at least one data point");
    return divide(xs, static_cast<double>(xs.size()));
}

template <class It, class W = std::nullopt_t>
double fmean(It&& data, const W& weights = std::nullopt) {
    auto xs = floats(std::forward<It>(data));
    if constexpr (std::is_same_v<W, std::nullopt_t>) {
        if (xs.empty()) error("fmean requires at least one data point");
        return fsum(xs) / static_cast<double>(xs.size());
    } else {
        auto ws = floats(weights);
        if (ws.size() != xs.size()) error("data and weights must be the same length");
        double num = sumprod(xs, ws);
        double den = fsum(ws);
        if (den == 0.0) error("sum of weights must be non-zero");
        return num / den;
    }
}

template <class It>
double geometric_mean(It&& data) {
    std::vector<double> logs;
    std::size_t n = 0;
    bool found_zero = false;
    for (auto&& item : iter(std::forward<It>(data))) {
        ++n;
        double x = static_cast<double>(item);
        if (x > 0.0 || std::isnan(x))
            logs.push_back(std::log(x));
        else if (x == 0.0)
            found_zero = true;
        else
            error("('No negative inputs allowed', " + repr(item) + ")");
    }
    double total = fsum(logs);
    if (n == 0) error("Must have a non-empty dataset");
    if (std::isnan(total)) return std::nan("");
    if (found_zero) return total == INFINITY ? std::nan("") : 0.0;
    return std::exp(total / static_cast<double>(n));
}

template <class It, class W = std::nullopt_t>
double harmonic_mean(It&& data, const W& weights = std::nullopt) {
    const std::string negative = "harmonic mean does not support negative values";
    auto xs = floats(std::forward<It>(data));
    std::size_t n = xs.size();
    std::vector<double> ws;
    bool weighted = false;
    if constexpr (!std::is_same_v<W, std::nullopt_t>) {
        ws = floats(weights);
        weighted = true;
    }
    if (n < 1) error("harmonic_mean requires at least one data point");
    if (n == 1 && !weighted) {
        if (xs[0] < 0) error(negative);
        return xs[0];
    }
    double sum_weights = static_cast<double>(n);
    if (weighted) {
        if (ws.size() != n) error("Number of weights does not match data size");
        for (double w : ws)
            if (w < 0) error(negative);
        sum_weights = fsum(ws);
    } else {
        ws.assign(n, 1.0);
    }
    std::vector<double> terms;
    for (std::size_t i = 0; i < n; ++i) {
        if (xs[i] < 0) error(negative);
        if (ws[i] == 0.0) {
            terms.push_back(0.0);
            continue;
        }
        if (xs[i] == 0.0) return 0.0;  // (Python: a ZeroDivisionError, caught)
        terms.push_back(ws[i] / xs[i]);
    }
    if (fsum(terms) <= 0) error("Weighted sum must be positive");
    return divide(sum_weights, terms);
}

template <class It>
auto sorted_data(It&& data) {
    auto xs = to_list(std::forward<It>(data));
    sort_values(xs, std::less<>{});
    return xs;
}

template <class It>
double median(It&& data) {
    auto xs = floats(std::forward<It>(data));
    std::sort(xs.begin(), xs.end());
    std::size_t n = xs.size();
    if (n == 0) error("no median for empty data");
    if (n % 2 == 1) return xs[n / 2];
    return (xs[n / 2 - 1] + xs[n / 2]) / 2.0;
}

template <class It>
auto median_low(It&& data) {
    auto xs = sorted_data(std::forward<It>(data));
    std::size_t n = xs.size();
    if (n == 0) error("no median for empty data");
    return n % 2 == 1 ? xs[n / 2] : xs[n / 2 - 1];
}

template <class It>
auto median_high(It&& data) {
    auto xs = sorted_data(std::forward<It>(data));
    if (xs.empty()) error("no median for empty data");
    return xs[xs.size() / 2];
}

template <class It>
double median_grouped(It&& data, double interval = 1.0) {
    auto xs = floats(std::forward<It>(data));
    std::sort(xs.begin(), xs.end());
    std::size_t n = xs.size();
    if (n == 0) error("no median for empty data");
    double x = xs[n / 2];
    auto i = std::lower_bound(xs.begin(), xs.end(), x) - xs.begin();
    auto j = std::upper_bound(xs.begin() + i, xs.end(), x) - xs.begin();
    double L = x - interval / 2.0;
    double cf = static_cast<double>(i);
    double f = static_cast<double>(j - i);
    return L + interval * (static_cast<double>(n) / 2.0 - cf) / f;
}

// The distinct items in the order first seen, with how often each occurs (a Counter).
template <class It>
auto counted(It&& data) {
    auto xs = to_list(std::forward<It>(data));
    using T = typename decltype(xs)::value_type;
    dict<T, std::int64_t> index;
    std::vector<T> items;
    std::vector<std::int64_t> counts;
    for (auto& x : xs) {
        std::int64_t& slot = index[x];
        if (slot == 0) {
            items.push_back(x);
            counts.push_back(0);
            slot = static_cast<std::int64_t>(items.size());
        }
        ++counts[slot - 1];
    }
    return std::make_pair(std::move(items), std::move(counts));
}

template <class It>
auto mode(It&& data) {
    auto [items, counts] = counted(std::forward<It>(data));
    if (items.empty()) error("no mode for empty data");
    std::size_t best = 0;
    for (std::size_t i = 1; i < items.size(); ++i)
        if (counts[i] > counts[best]) best = i;
    return items[best];
}

template <class It>
auto multimode(It&& data) {
    auto [items, counts] = counted(std::forward<It>(data));
    using T = typename decltype(items)::value_type;
    list<T> out;
    if (items.empty()) return out;
    std::int64_t most = *std::max_element(counts.begin(), counts.end());
    for (std::size_t i = 0; i < items.size(); ++i)
        if (counts[i] == most) out.push_back(items[i]);
    return out;
}

template <class It>
list<double> quantiles(It&& data, std::int64_t n = 4, const std::string& method = "exclusive") {
    if (n < 1) error("n must be at least 1");
    auto xs = floats(std::forward<It>(data));
    std::sort(xs.begin(), xs.end());
    auto ld = static_cast<std::int64_t>(xs.size());
    std::vector<double> result;
    if (ld < 2) {
        if (ld == 1) return list<double>(std::vector<double>(static_cast<std::size_t>(n - 1), xs[0]));
        error("must have at least one data point");
    }
    double dn = static_cast<double>(n);
    if (method == "inclusive") {
        std::int64_t m = ld - 1;
        for (std::int64_t i = 1; i < n; ++i) {
            std::int64_t j = i * m / n, delta = i * m % n;
            result.push_back((xs[j] * static_cast<double>(n - delta) + xs[j + 1] * static_cast<double>(delta)) / dn);
        }
        return result;
    }
    if (method == "exclusive") {
        std::int64_t m = ld + 1;
        for (std::int64_t i = 1; i < n; ++i) {
            std::int64_t j = std::clamp<std::int64_t>(i * m / n, 1, ld - 1);
            std::int64_t delta = i * m - j * n;
            result.push_back((xs[j - 1] * static_cast<double>(n - delta) + xs[j] * static_cast<double>(delta)) / dn);
        }
        return result;
    }
    raise("ValueError", "Unknown method: " + repr(method));
}

// The mean and the sum of squared deviations (held exactly, as parts). The deviations from
// a first estimate of the mean are kept exactly (as two doubles each) and squared exactly.
struct SumSquares {
    double mean;
    std::vector<double> ss;
    std::size_t n;
};

inline SumSquares sum_squares(const std::vector<double>& xs, std::optional<double> c) {
    std::size_t n = xs.size();
    std::vector<double> squares;
    if (c) {  // Python: the sum of (x - c) ** 2, each computed in floats
        for (double x : xs) {
            double d = x - *c;
            squares.push_back(d * d);
        }
        return {*c, squares, n};
    }
    if (n == 0) return {0.0, {}, 0};
    double dn = static_cast<double>(n);
    double m = fsum(xs) / dn;
    if (!std::isfinite(m)) return {m, {m}, n};
    std::vector<double> devs;
    for (double x : xs) {
        double hi = x - m;
        double back = hi - x;  // (two-sum: x - m == hi + lo exactly)
        double lo = (x - (hi - back)) + (-m - back);
        devs.push_back(hi);
        devs.push_back(lo);
        add_product(squares, hi, hi);
        add_product(squares, 2.0 * hi, lo);
        squares.push_back(lo * lo);
    }
    double s1 = fsum(devs);  // the sum of the deviations: tiny
    squares.push_back(-(s1 * s1 / dn));
    return {divide(xs, dn), squares, n};
}

template <class It>
double variance(It&& data, std::optional<double> xbar = std::nullopt) {
    auto s = sum_squares(floats(std::forward<It>(data)), xbar);
    if (s.n < 2) error("variance requires at least two data points");
    return divide(s.ss, static_cast<double>(s.n - 1));
}

template <class It>
double pvariance(It&& data, std::optional<double> mu = std::nullopt) {
    auto s = sum_squares(floats(std::forward<It>(data)), mu);
    if (s.n < 1) error("pvariance requires at least one data point");
    return divide(s.ss, static_cast<double>(s.n));
}

template <class It>
double stdev(It&& data, std::optional<double> xbar = std::nullopt) {
    auto s = sum_squares(floats(std::forward<It>(data)), xbar);
    if (s.n < 2) error("stdev requires at least two data points");
    return sqrt_ratio(s.ss, static_cast<double>(s.n - 1));
}

template <class It>
double pstdev(It&& data, std::optional<double> mu = std::nullopt) {
    auto s = sum_squares(floats(std::forward<It>(data)), mu);
    if (s.n < 1) error("pstdev requires at least one data point");
    return sqrt_ratio(s.ss, static_cast<double>(s.n));
}

inline std::vector<double> minus(const std::vector<double>& xs, double c) {
    std::vector<double> out;
    for (double x : xs) out.push_back(x - c);
    return out;
}

template <class X, class Y>
double covariance(X&& x, Y&& y) {
    auto xs = floats(std::forward<X>(x)), ys = floats(std::forward<Y>(y));
    std::size_t n = xs.size();
    if (ys.size() != n) error("covariance requires that both inputs have same number of data points");
    if (n < 2) error("covariance requires at least two data points");
    double xbar = fsum(xs) / static_cast<double>(n), ybar = fsum(ys) / static_cast<double>(n);
    return sumprod(minus(xs, xbar), minus(ys, ybar)) / static_cast<double>(n - 1);
}

// Ranks with ties averaged, centred on `start` (statistics._rank).
inline std::vector<double> rank(const std::vector<double>& xs, double start) {
    std::vector<std::size_t> order(xs.size());
    for (std::size_t i = 0; i < order.size(); ++i) order[i] = i;
    std::sort(order.begin(), order.end(), [&](std::size_t a, std::size_t b) {
        return xs[a] < xs[b] || (xs[a] == xs[b] && a < b);
    });
    std::vector<double> result(xs.size());
    double i = start - 1;
    for (std::size_t g = 0; g < order.size();) {
        std::size_t e = g;
        while (e < order.size() && xs[order[e]] == xs[order[g]]) ++e;
        double size = static_cast<double>(e - g);
        double r = i + (size + 1) / 2;
        for (std::size_t k = g; k < e; ++k) result[order[k]] = r;
        i += size;
        g = e;
    }
    return result;
}

// sqrt(x * y), more accurately and without overflow or underflow (statistics._sqrtprod).
inline double sqrtprod(double x, double y) {
    double h = std::sqrt(x * y);
    if (!std::isfinite(h)) {
        if (std::isinf(h) && !std::isinf(x) && !std::isinf(y)) {
            double scale = std::ldexp(1.0, -512);
            return sqrtprod(scale * x, scale * y) / scale;
        }
        return h;
    }
    if (h == 0.0) {
        if (x != 0.0 && y != 0.0) {
            double scale = std::ldexp(1.0, 537);
            return sqrtprod(scale * x, scale * y) / scale;
        }
        return h;
    }
    double d = sumprod({x, h}, {y, -h});
    return h + d / (2.0 * h);
}

template <class X, class Y>
double correlation(X&& x, Y&& y, const std::string& method = "linear") {
    auto xs = floats(std::forward<X>(x)), ys = floats(std::forward<Y>(y));
    std::size_t n = xs.size();
    if (ys.size() != n) error("correlation requires that both inputs have same number of data points");
    if (n < 2) error("correlation requires at least two data points");
    if (method != "linear" && method != "ranked") raise("ValueError", "Unknown method: " + repr(method));
    if (method == "ranked") {
        double start = (static_cast<double>(n) - 1) / -2.0;
        xs = rank(xs, start);
        ys = rank(ys, start);
    } else {
        xs = minus(xs, fsum(xs) / static_cast<double>(n));
        ys = minus(ys, fsum(ys) / static_cast<double>(n));
    }
    double sxy = sumprod(xs, ys), sxx = sumprod(xs, xs), syy = sumprod(ys, ys);
    double root = sqrtprod(sxx, syy);
    if (root == 0.0) error("at least one of the inputs is constant");
    return sxy / root;
}

struct LinearRegression {
    double slope_, intercept_;
    double slope() const { return slope_; }
    double intercept() const { return intercept_; }
    bool operator==(const LinearRegression&) const = default;
    std::string sd_repr() const {
        return "LinearRegression(slope=" + float_repr(slope_) + ", intercept=" + float_repr(intercept_) + ")";
    }
};

template <class X, class Y>
LinearRegression linear_regression(X&& x, Y&& y, bool proportional = false) {
    auto xs = floats(std::forward<X>(x)), ys = floats(std::forward<Y>(y));
    std::size_t n = xs.size();
    if (ys.size() != n) error("linear regression requires that both inputs have same number of data points");
    if (n < 2) error("linear regression requires at least two data points");
    double xbar = 0.0, ybar = 0.0;
    if (!proportional) {
        xbar = fsum(xs) / static_cast<double>(n);
        ybar = fsum(ys) / static_cast<double>(n);
        xs = minus(xs, xbar);
        ys = minus(ys, ybar);
    }
    double sxy = sumprod(xs, ys) + 0.0, sxx = sumprod(xs, xs);
    if (sxx == 0.0) error("x is constant");
    double slope = sxy / sxx;
    return {slope, proportional ? 0.0 : ybar - slope * xbar};
}

// Horner's rule, in the order statistics.py writes it out: ((c0 * r + c1) * r + c2)...
inline double horner(double r, std::initializer_list<double> coeffs) {
    auto it = coeffs.begin();
    double acc = *it++;
    for (; it != coeffs.end(); ++it) acc = acc * r + *it;
    return acc;
}

// Wichura's AS241 (statistics._normal_dist_inv_cdf).
inline double normal_dist_inv_cdf(double p, double mu, double sigma) {
    double q = p - 0.5, num, den;
    if (std::fabs(q) <= 0.425) {
        double r = 0.180625 - q * q;
        num = horner(r, {2.5090809287301226727e+3, 3.3430575583588128105e+4, 6.7265770927008700853e+4,
                         4.5921953931549871457e+4, 1.3731693765509461125e+4, 1.9715909503065514427e+3,
                         1.3314166789178437745e+2, 3.3871328727963666080e+0}) * q;
        den = horner(r, {5.2264952788528545610e+3, 2.8729085735721942674e+4, 3.9307895800092710610e+4,
                         2.1213794301586595867e+4, 5.3941960214247511077e+3, 6.8718700749205790830e+2,
                         4.2313330701600911252e+1, 1.0});
        return mu + ((num / den) * sigma);
    }
    double r = q <= 0.0 ? p : 1.0 - p;
    r = std::sqrt(-std::log(r));
    if (r <= 5.0) {
        r = r - 1.6;
        num = horner(r, {7.74545014278341407640e-4, 2.27238449892691845833e-2, 2.41780725177450611770e-1,
                         1.27045825245236838258e+0, 3.64784832476320460504e+0, 5.76949722146069140550e+0,
                         4.63033784615654529590e+0, 1.42343711074968357734e+0});
        den = horner(r, {1.05075007164441684324e-9, 5.47593808499534494600e-4, 1.51986665636164571966e-2,
                         1.48103976427480074590e-1, 6.89767334985100004550e-1, 1.67638483018380384940e+0,
                         2.05319162663775882187e+0, 1.0});
    } else {
        r = r - 5.0;
        num = horner(r, {2.01033439929228813265e-7, 2.71155556874348757815e-5, 1.24266094738807843860e-3,
                         2.65321895265761230930e-2, 2.96560571828504891230e-1, 1.78482653991729133580e+0,
                         5.46378491116411436990e+0, 6.65790464350110377720e+0});
        den = horner(r, {2.04426310338993978564e-15, 1.42151175831644588870e-7, 1.84631831751005468180e-5,
                         7.86869131145613259100e-4, 1.48753612908506148525e-2, 1.36929880922735805310e-1,
                         5.99832206555887937690e-1, 1.0});
    }
    double x = num / den;
    if (q < 0.0) x = -x;
    return mu + (x * sigma);
}

// NormalDist: an immutable value.
struct NormalDist {
    double mu_ = 0.0, sigma_ = 1.0;

    static NormalDist make(double mu = 0.0, double sigma = 1.0) {
        if (sigma < 0.0) error("sigma must be non-negative");
        return NormalDist{mu, sigma};
    }
    template <class It>
    static NormalDist from_samples(It&& data) {
        auto s = sum_squares(floats(std::forward<It>(data)), std::nullopt);
        if (s.n < 2) error("stdev requires at least two data points");
        return make(s.mean, sqrt_ratio(s.ss, static_cast<double>(s.n - 1)));
    }

    double mean() const { return mu_; }
    double median() const { return mu_; }
    double mode() const { return mu_; }
    double stdev() const { return sigma_; }
    double variance() const { return sigma_ * sigma_; }

    list<double> samples(std::int64_t n, std::optional<std::int64_t> seed = std::nullopt) const {
        std::vector<double> out;
        if (seed) {
            random::State own;
            random::seed_state(own, seed);
            for (std::int64_t i = 0; i < n; ++i) {
                std::uint32_t a = own.mt.next() >> 5, b = own.mt.next() >> 6;
                out.push_back(normal_dist_inv_cdf((a * 67108864.0 + b) * (1.0 / 9007199254740992.0), mu_, sigma_));
            }
        } else {
            for (std::int64_t i = 0; i < n; ++i) out.push_back(normal_dist_inv_cdf(random::random(), mu_, sigma_));
        }
        return out;
    }
    double pdf(double x) const {
        double var = sigma_ * sigma_;
        if (var == 0.0) error("pdf() not defined when sigma is zero");
        double diff = x - mu_;
        return std::exp(diff * diff / (-2.0 * var)) / std::sqrt(2.0 * std::numbers::pi * var);
    }
    double cdf(double x) const {
        if (sigma_ == 0.0) error("cdf() not defined when sigma is zero");
        return 0.5 * std::erfc((mu_ - x) / (sigma_ * std::numbers::sqrt2));
    }
    double inv_cdf(double p) const {
        if (p <= 0.0 || p >= 1.0) error("p must be in the range 0.0 < p < 1.0");
        return normal_dist_inv_cdf(p, mu_, sigma_);
    }
    list<double> quantiles(std::int64_t n = 4) const {
        std::vector<double> out;
        for (std::int64_t i = 1; i < n; ++i) out.push_back(inv_cdf(static_cast<double>(i) / static_cast<double>(n)));
        return out;
    }
    double overlap(const NormalDist& other) const {
        NormalDist X = *this, Y = other;
        if (std::make_pair(Y.sigma_, Y.mu_) < std::make_pair(X.sigma_, X.mu_)) std::swap(X, Y);
        double X_var = X.variance(), Y_var = Y.variance();
        if (X_var == 0.0 || Y_var == 0.0) error("overlap() not defined when sigma is zero");
        double dv = Y_var - X_var, dm = std::fabs(Y.mu_ - X.mu_);
        if (dv == 0.0) return std::erfc(dm / (2.0 * X.sigma_ * std::numbers::sqrt2));
        double a = X.mu_ * Y_var - Y.mu_ * X_var;
        double b = X.sigma_ * Y.sigma_ * std::sqrt(dm * dm + dv * std::log(Y_var / X_var));
        double x1 = (a + b) / dv, x2 = (a - b) / dv;
        return 1.0 - (std::fabs(Y.cdf(x1) - X.cdf(x1)) + std::fabs(Y.cdf(x2) - X.cdf(x2)));
    }
    double zscore(double x) const {
        if (sigma_ == 0.0) error("zscore() not defined when sigma is zero");
        return (x - mu_) / sigma_;
    }

    friend NormalDist operator+(const NormalDist& a, const NormalDist& b) { return {a.mu_ + b.mu_, std::hypot(a.sigma_, b.sigma_)}; }
    friend NormalDist operator-(const NormalDist& a, const NormalDist& b) { return {a.mu_ - b.mu_, std::hypot(a.sigma_, b.sigma_)}; }
    friend NormalDist operator+(const NormalDist& a, double c) { return {a.mu_ + c, a.sigma_}; }
    friend NormalDist operator+(double c, const NormalDist& a) { return {a.mu_ + c, a.sigma_}; }
    friend NormalDist operator-(const NormalDist& a, double c) { return {a.mu_ - c, a.sigma_}; }
    friend NormalDist operator-(double c, const NormalDist& a) { return -(a - c); }
    friend NormalDist operator*(const NormalDist& a, double c) { return {a.mu_ * c, a.sigma_ * std::fabs(c)}; }
    friend NormalDist operator*(double c, const NormalDist& a) { return a * c; }
    friend NormalDist operator/(const NormalDist& a, double c) { return {a.mu_ / c, a.sigma_ / std::fabs(c)}; }
    NormalDist operator+() const { return *this; }
    NormalDist operator-() const { return {-mu_, sigma_}; }
    bool operator==(const NormalDist& o) const { return mu_ == o.mu_ && sigma_ == o.sigma_; }

    std::string sd_repr() const { return "NormalDist(mu=" + float_repr(mu_) + ", sigma=" + float_repr(sigma_) + ")"; }
};

}  // namespace sd::statistics
