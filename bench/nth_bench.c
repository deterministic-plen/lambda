// Fair C implementations of the same three nth-prime algorithms used by
// Lambda.  Threads are spawned per call, like Lambda's pmap.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "parallel.h"

#define BLOCK 65536
#define SIEVE_BLOCK 64

static int64_t upper_bound(int64_t n) {
    if (n < 6) return 15;
    double f = (double)n;
    double est = f * (log(f) + log(log(f)));
    return (int64_t)(est * 1.05);
}

static int is_prime_i64(int64_t n) {
    if (n < 2) return 0;
    if (n == 2) return 1;
    if ((n & 1) == 0) return 0;
    for (int64_t d = 3; d * d <= n; d += 2) {
        if (n % d == 0) return 0;
    }
    return 1;
}

/* Fixed-iteration Newton for sqrt(2), matching the Lambda/C++/Rust kernel. */
__attribute__((noinline)) static double newton_solve(double x) {
    for (int i = 0; i < 100; ++i) {
        x -= (x * x - 2.0) / (2.0 * x);
    }
    return x;
}

/* ------------------------------------------------------------------ */
/* 1. Sequential trial division, structurally matching nth_prime.      */
/* ------------------------------------------------------------------ */
static int64_t find_prime(int64_t num, int64_t count, int64_t target);

static int64_t check_prime(int64_t num, int64_t count, int64_t target, int64_t d) {
    if (num < d * d) {
        if (count + 1 == target) return num;
        return find_prime(num + 2, count + 1, target);
    }
    if (num % d == 0) return find_prime(num + 2, count, target);
    return check_prime(num, count, target, d + 2);
}

static int64_t find_prime(int64_t num, int64_t count, int64_t target) {
    return check_prime(num, count, target, 3);
}

__attribute__((noinline)) static int64_t nth_prime_seq(int64_t target) {
    if (target == 1) return 2;
    return find_prime(3, 1, target);
}

/* ------------------------------------------------------------------ */
/* 2. Parallel trial-division block counting.                          */
/* ------------------------------------------------------------------ */
typedef struct {
    int64_t *counts;
} CountCtx;

static void count_work(void *ctx, int64_t b) {
    CountCtx *c = (CountCtx *)ctx;
    int64_t lo = b * BLOCK;
    int64_t hi = lo + BLOCK;
    int64_t count = 0;
    for (int64_t i = (lo < 2 ? 2 : lo); i < hi; ++i) {
        if (is_prime_i64(i)) ++count;
    }
    c->counts[b] = count;
}

__attribute__((noinline)) static int64_t nth_prime_par(int64_t target) {
    int64_t limit = upper_bound(target);
    int64_t blocks = (limit + BLOCK - 1) / BLOCK;
    int64_t *counts = (int64_t *)calloc((size_t)blocks, sizeof(int64_t));
    CountCtx ctx = {counts};
    bench_parallel(blocks, bench_threads(), count_work, &ctx);

    int64_t acc = 0, block = 0;
    for (; block < blocks; ++block) {
        int64_t c = counts[block];
        if (target <= acc + c) break;
        acc += c;
    }
    int64_t k = target - acc;
    int64_t lo = block * BLOCK;
    int64_t hi = lo + BLOCK;
    for (int64_t i = (lo < 2 ? 2 : lo); i < hi; ++i) {
        if (is_prime_i64(i)) {
            if (--k == 0) {
                free(counts);
                return i;
            }
        }
    }
    free(counts);
    return 0;
}

/* ------------------------------------------------------------------ */
/* 3. Segmented 64-bit-mask sieve.                                     */
/* ------------------------------------------------------------------ */
static uint64_t block_mask(int64_t b, const int64_t *base, int64_t nbase) {
    int64_t lo = b * SIEVE_BLOCK;
    uint64_t mask = ~0ULL;
    if (lo == 0) mask &= ~3ULL;
    for (int64_t i = 0; i < nbase; ++i) {
        int64_t p = base[i];
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

#define SIEVE_CHUNK 32

typedef struct {
    int64_t *chunk_counts;
    const int64_t *base;
    int64_t nbase;
    int64_t blocks;
} SieveCtx;

static void sieve_work(void *ctx, int64_t chunk) {
    SieveCtx *c = (SieveCtx *)ctx;
    int64_t start = chunk * SIEVE_CHUNK;
    int64_t stop = start + SIEVE_CHUNK;
    if (stop > c->blocks) stop = c->blocks;
    int64_t total = 0;
    for (int64_t b = start; b < stop; ++b) {
        total += __builtin_popcountll(block_mask(b, c->base, c->nbase));
    }
    c->chunk_counts[chunk] = total;
}

__attribute__((noinline)) static int64_t nth_prime_sieve(int64_t target) {
    int64_t limit = upper_bound(target);
    int64_t root = (int64_t)sqrt((double)limit);
    int64_t *base = (int64_t *)malloc(sizeof(int64_t) * (size_t)(root + 1));
    int64_t nbase = 0;
    for (int64_t i = 2; i <= root; ++i) {
        if (is_prime_i64(i)) base[nbase++] = i;
    }
    int64_t blocks = (limit + SIEVE_BLOCK - 1) / SIEVE_BLOCK;
    int64_t chunks = (blocks + SIEVE_CHUNK - 1) / SIEVE_CHUNK;
    int64_t *chunk_counts = (int64_t *)calloc((size_t)chunks, sizeof(int64_t));
    SieveCtx ctx = {chunk_counts, base, nbase, blocks};
    bench_parallel(chunks, bench_threads(), sieve_work, &ctx);

    int64_t acc = 0, chunk = 0;
    for (; chunk < chunks; ++chunk) {
        int64_t c = chunk_counts[chunk];
        if (target <= acc + c) break;
        acc += c;
    }
    int64_t need = target - acc;
    int64_t start = chunk * SIEVE_CHUNK;
    int64_t stop = start + SIEVE_CHUNK;
    if (stop > blocks) stop = blocks;
    int64_t block = start;
    for (; block < stop; ++block) {
        int64_t c = __builtin_popcountll(block_mask(block, base, nbase));
        if (need <= c) break;
        need -= c;
    }
    int64_t k = need;
    uint64_t m = block_mask(block, base, nbase);
    for (int i = 0; i < 64; ++i) {
        if (m & (1ULL << i)) {
            if (--k == 0) {
                int64_t result = block * SIEVE_BLOCK + i;
                free(base);
                free(chunk_counts);
                return result;
            }
        }
    }
    free(base);
    free(chunk_counts);
    return 0;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "seq";
    int reps = argc > 2 ? atoi(argv[2]) : 3;
    if (strcmp(mode, "array") == 0) {
        volatile int64_t n = 10000000;
        int64_t total = 0;
        for (int r = 0; r < reps; ++r) {
            int64_t *a = (int64_t *)malloc((size_t)n * sizeof(int64_t));
            for (int64_t i = 0; i < n; ++i) a[i] = i;
            int64_t s = 0;
            for (int64_t i = 0; i < n; ++i) s += a[i];
            free(a);
            total += s;
            __asm__ __volatile__("" ::: "memory");
        }
        printf("%lld\n", (long long)total);
        return 0;
    }
    if (strcmp(mode, "newton") == 0) {
        volatile double start = 2.0;
        double sum = 0.0;
        for (int r = 0; r < reps; ++r) {
            double x = start;
            sum += newton_solve(x);
            __asm__ __volatile__("" ::: "memory");
        }
        printf("%.17g\n", sum);
        return 0;
    }
    volatile int64_t target = 670000;
    int64_t sum = 0;
    for (int r = 0; r < reps; ++r) {
        int64_t t = target;
        if (strcmp(mode, "seq") == 0) sum += nth_prime_seq(t);
        else if (strcmp(mode, "par") == 0) sum += nth_prime_par(t);
        else if (strcmp(mode, "sieve") == 0) sum += nth_prime_sieve(t);
        else return 2;
        __asm__ __volatile__("" ::: "memory");
    }
    printf("%lld\n", (long long)sum);
    return 0;
}
