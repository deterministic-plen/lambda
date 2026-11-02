// Fair C++ implementations of the same three nth-prime algorithms used by
// Lambda.  Threads are spawned per call, like Lambda's pmap.
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

namespace {

constexpr int64_t BLOCK = 65536;
constexpr int64_t SIEVE_BLOCK = 64;

int bench_threads() {
    const char *s = std::getenv("BENCH_THREADS");
    if (s && *s) {
        char *end = nullptr;
        long long n = std::strtoll(s, &end, 10);
        if (end != s && n > 0) return static_cast<int>(n);
    }
    unsigned hw = std::thread::hardware_concurrency();
    return hw ? static_cast<int>(hw) : 1;
}

__attribute__((noinline)) bool is_prime(int64_t n) {
    if (n < 2) return false;
    if (n == 2) return true;
    if ((n & 1) == 0) return false;
    for (int64_t d = 3; d * d <= n; d += 2) {
        if (n % d == 0) return false;
    }
    return true;
}

// Fixed-iteration Newton for sqrt(2), matching the Lambda/C/Rust kernel.
__attribute__((noinline)) double newton_solve(double x) {
    for (int i = 0; i < 100; ++i) {
        x -= (x * x - 2.0) / (2.0 * x);
    }
    return x;
}

// ---------------------------------------------------------------------------
// 1. Sequential trial division, structurally matching nth_prime.lambda.
// ---------------------------------------------------------------------------
int64_t find_prime(int64_t num, int64_t count, int64_t target);

int64_t check_prime(int64_t num, int64_t count, int64_t target, int64_t d) {
    if (num < d * d) {
        if (count + 1 == target) return num;
        return find_prime(num + 2, count + 1, target);
    }
    if (num % d == 0) return find_prime(num + 2, count, target);
    return check_prime(num, count, target, d + 2);
}

int64_t find_prime(int64_t num, int64_t count, int64_t target) {
    return check_prime(num, count, target, 3);
}

__attribute__((noinline)) int64_t nth_prime_seq(int64_t target) {
    if (target == 1) return 2;
    return find_prime(3, 1, target);
}

// ---------------------------------------------------------------------------
// 2. Parallel trial-division block counting, matching nth_prime_parallel.
// ---------------------------------------------------------------------------
int64_t upper_bound(int64_t n) {
    if (n < 6) return 15;
    double f = static_cast<double>(n);
    double est = f * (std::log(f) + std::log(std::log(f)));
    return static_cast<int64_t>(est * 1.05);
}

__attribute__((noinline)) int64_t count_block(int64_t b) {
    int64_t lo = b * BLOCK;
    int64_t hi = lo + BLOCK;
    int64_t count = 0;
    for (int64_t i = (lo < 2 ? 2 : lo); i < hi; ++i) {
        if (is_prime(i)) ++count;
    }
    return count;
}

__attribute__((noinline)) int64_t nth_prime_par(int64_t target) {
    int64_t limit = upper_bound(target);
    int64_t blocks = (limit + BLOCK - 1) / BLOCK;
    std::vector<int64_t> counts(static_cast<size_t>(blocks));
    std::atomic<int64_t> next{0};
    auto worker = [&]() {
        for (;;) {
            int64_t i = next.fetch_add(1, std::memory_order_relaxed);
            if (i >= blocks) break;
            counts[static_cast<size_t>(i)] = count_block(i);
        }
    };
    int threads = bench_threads();
    if (threads > blocks) threads = static_cast<int>(blocks);
    std::vector<std::thread> pool;
    for (int i = 1; i < threads; ++i) pool.emplace_back(worker);
    worker();
    for (auto &t : pool) t.join();

    int64_t acc = 0, block = 0;
    for (; block < blocks; ++block) {
        int64_t c = counts[static_cast<size_t>(block)];
        if (target <= acc + c) break;
        acc += c;
    }
    int64_t k = target - acc;
    int64_t lo = block * BLOCK;
    int64_t hi = lo + BLOCK;
    for (int64_t i = (lo < 2 ? 2 : lo); i < hi; ++i) {
        if (is_prime(i)) {
            if (--k == 0) return i;
        }
    }
    return 0;
}

// ---------------------------------------------------------------------------
// 3. Segmented 64-bit-mask sieve, matching nth_prime_sieve.
// ---------------------------------------------------------------------------
uint64_t block_mask(int64_t b, const std::vector<int64_t> &base) {
    int64_t lo = b * SIEVE_BLOCK;
    uint64_t mask = ~0ULL;
    if (lo == 0) mask &= ~3ULL; // 0 and 1 are not prime
    for (int64_t p : base) {
        int64_t p2 = p * p;
        int64_t rem = lo % p;
        int64_t first0 = (rem == 0) ? lo : lo + (p - rem);
        int64_t first = first0 < p2 ? p2 : first0;
        for (int64_t off = first - lo; off < 64; off += p) {
            mask &= ~(1ULL << off);
        }
    }
    return mask;
}

__attribute__((noinline)) int64_t nth_prime_sieve(int64_t target) {
    int64_t limit = upper_bound(target);
    int64_t root = static_cast<int64_t>(std::sqrt(static_cast<double>(limit)));
    std::vector<int64_t> base;
    for (int64_t i = 2; i <= root; ++i) {
        if (is_prime(i)) base.push_back(i);
    }
    int64_t blocks = (limit + SIEVE_BLOCK - 1) / SIEVE_BLOCK;
    constexpr int64_t CHUNK = 32;
    int64_t chunks = (blocks + CHUNK - 1) / CHUNK;
    std::vector<int64_t> chunk_counts(static_cast<size_t>(chunks));
    std::atomic<int64_t> next{0};
    auto worker = [&]() {
        for (;;) {
            int64_t c = next.fetch_add(1, std::memory_order_relaxed);
            if (c >= chunks) break;
            int64_t start = c * CHUNK;
            int64_t stop = std::min(start + CHUNK, blocks);
            int64_t total = 0;
            for (int64_t b = start; b < stop; ++b) {
                total += __builtin_popcountll(block_mask(b, base));
            }
            chunk_counts[static_cast<size_t>(c)] = total;
        }
    };
    int threads = bench_threads();
    if (threads > chunks) threads = static_cast<int>(chunks);
    std::vector<std::thread> pool;
    for (int i = 1; i < threads; ++i) pool.emplace_back(worker);
    worker();
    for (auto &t : pool) t.join();

    int64_t acc = 0, chunk = 0;
    for (; chunk < chunks; ++chunk) {
        int64_t c = chunk_counts[static_cast<size_t>(chunk)];
        if (target <= acc + c) break;
        acc += c;
    }
    int64_t need = target - acc;
    int64_t start = chunk * CHUNK;
    int64_t stop = std::min(start + CHUNK, blocks);
    int64_t block = start;
    for (; block < stop; ++block) {
        int64_t c = __builtin_popcountll(block_mask(block, base));
        if (need <= c) break;
        need -= c;
    }
    int64_t k = need;
    uint64_t m = block_mask(block, base);
    for (int i = 0; i < 64; ++i) {
        if (m & (1ULL << i)) {
            if (--k == 0) return block * SIEVE_BLOCK + i;
        }
    }
    return 0;
}

} // namespace

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "seq";
    int reps = argc > 2 ? std::atoi(argv[2]) : 3;
    if (std::strcmp(mode, "array") == 0) {
        volatile int64_t n = 10000000;
        int64_t total = 0;
        for (int r = 0; r < reps; ++r) {
            std::vector<int64_t> a(static_cast<size_t>(n));
            for (int64_t i = 0; i < n; ++i) a[static_cast<size_t>(i)] = i;
            int64_t s = 0;
            for (int64_t i = 0; i < n; ++i) s += a[static_cast<size_t>(i)];
            total += s;
            asm volatile("" ::: "memory");
        }
        std::printf("%lld\n", static_cast<long long>(total));
        return 0;
    }
    if (std::strcmp(mode, "newton") == 0) {
        volatile double start = 2.0;
        double sum = 0.0;
        for (int r = 0; r < reps; ++r) {
            double x = start;
            sum += newton_solve(x);
            asm volatile("" ::: "memory");
        }
        std::printf("%.17g\n", sum);
        return 0;
    }
    volatile int64_t target = 670000;
    int64_t sum = 0;
    for (int r = 0; r < reps; ++r) {
        int64_t t = target;
        if (std::strcmp(mode, "seq") == 0) sum += nth_prime_seq(t);
        else if (std::strcmp(mode, "par") == 0) sum += nth_prime_par(t);
        else if (std::strcmp(mode, "sieve") == 0) sum += nth_prime_sieve(t);
        else return 2;
        asm volatile("" ::: "memory");
    }
    std::printf("%lld\n", static_cast<long long>(sum));
    return 0;
}
