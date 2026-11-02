#ifndef BENCH_PARALLEL_H
#define BENCH_PARALLEL_H

#include <stdint.h>
#include <stdlib.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

typedef void (*bench_work_fn)(void *ctx, int64_t index);

typedef struct {
    bench_work_fn fn;
    void *ctx;
    int64_t n;
    int64_t next;
} BenchJob;

static void bench_worker(BenchJob *job) {
    for (;;) {
#ifdef _WIN32
        int64_t i = (int64_t)InterlockedIncrement64((volatile LONG64 *)&job->next) - 1;
#else
        int64_t i = __atomic_fetch_add(&job->next, 1, __ATOMIC_RELAXED);
#endif
        if (i >= job->n) return;
        job->fn(job->ctx, i);
    }
}

#ifdef _WIN32
static DWORD WINAPI bench_thread_main(LPVOID p) {
    bench_worker((BenchJob *)p);
    return 0;
}
#else
static void *bench_thread_main(void *p) {
    bench_worker((BenchJob *)p);
    return NULL;
}
#endif

static int bench_threads(void) {
    const char *s = getenv("BENCH_THREADS");
    if (s && *s) {
        char *end = NULL;
        long long n = strtoll(s, &end, 10);
        if (end != s && n > 0) return (int)n;
    }
#ifdef _WIN32
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    return si.dwNumberOfProcessors > 0 ? (int)si.dwNumberOfProcessors : 1;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 1;
#endif
}

static void bench_parallel(int64_t n, int threads, bench_work_fn fn, void *ctx) {
    BenchJob job;
    job.fn = fn;
    job.ctx = ctx;
    job.n = n;
    job.next = 0;
    if (threads < 1) threads = 1;
    if (threads > 64) threads = 64;
    if (n > 0 && threads > n) threads = (int)n;
    if (threads <= 1 || n <= 0) {
        bench_worker(&job);
        return;
    }
#ifdef _WIN32
    HANDLE hs[64];
    int created = 0;
    for (int i = 1; i < threads; i++) {
        HANDLE h = CreateThread(NULL, 0, bench_thread_main, &job, 0, NULL);
        if (h) hs[created++] = h;
    }
    bench_worker(&job);
    if (created > 0) WaitForMultipleObjects((DWORD)created, hs, TRUE, INFINITE);
    for (int i = 0; i < created; i++) CloseHandle(hs[i]);
#else
    pthread_t th[64];
    int created = 0;
    for (int i = 1; i < threads; i++) {
        if (pthread_create(&th[created], NULL, bench_thread_main, &job) == 0) created++;
    }
    bench_worker(&job);
    for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
}

#endif
