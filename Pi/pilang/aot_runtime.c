/* aot_runtime.c -- small native runtime for the Pi AOT subset.
 *
 * It speaks the Lambda-Pi worker protocol directly: spawn one worker process,
 * write text requests, read LMB1 frames, and route payloads to CSV/text output.
 * No Python is involved at run time.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>

#ifdef _WIN32
#include <windows.h>
#include <direct.h>
#define PI_MKDIR(p) _mkdir(p)
#else
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/stat.h>
#define PI_MKDIR(p) mkdir(p, 0777)
#endif

typedef enum { PI_UNIT, PI_NUM, PI_STR, PI_PAYLOAD, PI_WORKER } PiKind;
typedef struct PiPayload { long n; double *v; } PiPayload;
typedef struct PiWorker PiWorker;
typedef struct { PiKind kind; double num; PiPayload *payload; PiWorker *worker; const char *str; } PiVal;

struct PiWorker {
#ifdef _WIN32
    HANDLE in, out;
    PROCESS_INFORMATION proc;
#else
    int in, out;
    pid_t pid;
#endif
};

static long g_corr = 1;
static const char *g_out = NULL;
static long g_frames = 0;
static char g_anim_name[256];
static double *g_last = NULL;
static long g_last_n = 0, g_rows = 0, g_cols = 0;

static void die(const char *msg) {
    fprintf(stderr, "pi-aot: %s\n", msg);
    exit(1);
}
static void die_errno(const char *msg) {
    fprintf(stderr, "pi-aot: %s: %s\n", msg, strerror(errno));
    exit(1);
}

static PiVal pi_unit(void) { PiVal v; memset(&v, 0, sizeof(v)); v.kind = PI_UNIT; return v; }
static PiVal pi_num(double x) { PiVal v = pi_unit(); v.kind = PI_NUM; v.num = x; return v; }
static PiVal pi_str(const char *s) { PiVal v = pi_unit(); v.kind = PI_STR; v.str = s; return v; }
static PiVal pi_payload(long n) {
    PiVal v = pi_unit(); v.kind = PI_PAYLOAD;
    v.payload = (PiPayload *)calloc(1, sizeof(PiPayload));
    v.payload->n = n;
    v.payload->v = (double *)calloc((size_t)(n > 0 ? n : 1), sizeof(double));
    if (!v.payload->v) die("out of memory");
    return v;
}
static long pi_as_int(PiVal v) {
    if (v.kind != PI_NUM) die("AOT: expected a number");
    return (long)v.num;
}
static PiVal pi_index(PiVal v, long i) {
    if (v.kind != PI_PAYLOAD || i < 0 || i >= v.payload->n) die("AOT: payload index out of range");
    return pi_num(v.payload->v[i]);
}
static PiVal pi_range(long lo, long hi) {
    long n = hi > lo ? hi - lo : 0;
    PiVal v = pi_payload(n);
    for (long i = 0; i < n; i++) v.payload->v[i] = (double)(lo + i);
    return v;
}
static PiVal pi_add(PiVal a, PiVal b) { return pi_num(a.num + b.num); }
static PiVal pi_sub(PiVal a, PiVal b) { return pi_num(a.num - b.num); }
static PiVal pi_mul(PiVal a, PiVal b) { return pi_num(a.num * b.num); }
static PiVal pi_div(PiVal a, PiVal b) { return pi_num(a.num / b.num); }
static PiVal pi_lt(PiVal a, PiVal b) { return pi_num(a.num < b.num); }
static PiVal pi_le(PiVal a, PiVal b) { return pi_num(a.num <= b.num); }
static PiVal pi_gt(PiVal a, PiVal b) { return pi_num(a.num > b.num); }
static PiVal pi_ge(PiVal a, PiVal b) { return pi_num(a.num >= b.num); }
static PiVal pi_eq(PiVal a, PiVal b) { return pi_num(a.num == b.num); }

static void pi_sleep_ms(long ms) {
#ifdef _WIN32
    Sleep((DWORD)ms);
#else
    usleep((useconds_t)ms * 1000);
#endif
}

/* ---------------------------------------------------------------- pipes */
#ifdef _WIN32
static void write_all(PiWorker *w, const char *p, size_t n) {
    while (n) {
        DWORD wrote = 0;
        if (!WriteFile(w->in, p, (DWORD)n, &wrote, NULL) || wrote == 0) die("worker stdin write failed");
        p += wrote; n -= wrote;
    }
}
static void read_all(PiWorker *w, void *p, size_t n) {
    char *q = (char *)p;
    while (n) {
        DWORD got = 0;
        if (!ReadFile(w->out, q, (DWORD)n, &got, NULL) || got == 0) die("worker stdout closed");
        q += got; n -= got;
    }
}
#else
static void write_all(PiWorker *w, const char *p, size_t n) {
    while (n) {
        ssize_t k = write(w->in, p, n);
        if (k <= 0) die("worker stdin write failed");
        p += k; n -= (size_t)k;
    }
}
static void read_all(PiWorker *w, void *p, size_t n) {
    char *q = (char *)p;
    while (n) {
        ssize_t k = read(w->out, q, n);
        if (k <= 0) die("worker stdout closed");
        q += k; n -= (size_t)k;
    }
}
#endif

/* ------------------------------------------------------------- spawn/HELLO */
static PiVal pi_spawn(const char *exe, int gpu) {
    PiWorker *w = (PiWorker *)calloc(1, sizeof(PiWorker));
#ifdef _WIN32
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE in_r, in_w, out_r, out_w;
    if (!CreatePipe(&in_r, &in_w, &sa, 0) || !CreatePipe(&out_r, &out_w, &sa, 0))
        die("CreatePipe failed");
    SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOA si;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = in_r;
    si.hStdOutput = out_w;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    char cmd[4096];
    snprintf(cmd, sizeof(cmd), "\"%s\"", exe);
    char oldgpu[16] = {0};
    DWORD oldn = GetEnvironmentVariableA("LAMBDA_GPU", oldgpu, sizeof(oldgpu));
    SetEnvironmentVariableA("LAMBDA_GPU", gpu ? "1" : NULL);
    BOOL started = CreateProcessA(NULL, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW,
                                  NULL, NULL, &si, &w->proc);
    SetEnvironmentVariableA("LAMBDA_GPU", oldn ? oldgpu : NULL);
    if (!started) {
        fprintf(stderr, "pi-aot: cannot start worker %s (error %lu)\n", exe, GetLastError());
        exit(1);
    }
    CloseHandle(in_r); CloseHandle(out_w);
    w->in = in_w; w->out = out_r;
#else
    int in[2], out[2];
    if (pipe(in) || pipe(out)) die_errno("pipe");
    if (gpu) setenv("LAMBDA_GPU", "1", 1);
    else unsetenv("LAMBDA_GPU");
    pid_t pid = fork();
    if (pid < 0) die_errno("fork");
    if (pid == 0) {
        dup2(in[0], 0); dup2(out[1], 1);
        close(in[0]); close(in[1]); close(out[0]); close(out[1]);
        execl(exe, exe, (char *)NULL);
        _exit(127);
    }
    close(in[0]); close(out[1]);
    w->pid = pid; w->in = in[1]; w->out = out[0];
#endif
    /* Read and discard HELLO. */
    unsigned char h[16];
    read_all(w, h, 16);
    if (memcmp(h, "LMB1", 4) != 0) die("worker did not send HELLO");
    uint32_t n = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
    if (n) {
        void *body = malloc(n);
        if (!body) die("out of memory");
        read_all(w, body, n);
        free(body);
    }
    PiVal v = pi_unit();
    v.kind = PI_WORKER;
    v.worker = w;
    return v;
}

/* --------------------------------------------------------------- strings */
typedef struct { char *p; size_t n, cap; } Buf;
static void binit(Buf *b) { b->p = NULL; b->n = b->cap = 0; }
static void bgrow(Buf *b, size_t add) {
    if (b->n + add + 1 > b->cap) {
        size_t c = b->cap ? b->cap * 2 : 256;
        while (c < b->n + add + 1) c *= 2;
        b->p = (char *)realloc(b->p, c);
        if (!b->p) die("out of memory");
        b->cap = c;
    }
}
static void bprintf(Buf *b, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    va_list cp;
    va_copy(cp, ap);
    int n = vsnprintf(NULL, 0, fmt, cp);
    va_end(cp);
    if (n < 0) die("snprintf failed");
    bgrow(b, (size_t)n);
    vsnprintf(b->p + b->n, b->cap - b->n, fmt, ap);
    b->n += (size_t)n;
    va_end(ap);
}

/* --------------------------------------------------------------- worker call */
static void append_arg(Buf *b, long *nargs, PiVal v) {
    if (v.kind == PI_UNIT) return;
    if (v.kind == PI_NUM) {
        bprintf(b, "%.17g\n", v.num);
        (*nargs)++;
    } else if (v.kind == PI_PAYLOAD) {
        bprintf(b, "%ld\n", v.payload->n);
        (*nargs)++;
        for (long i = 0; i < v.payload->n; i++) {
            bprintf(b, "%.17g\n", v.payload->v[i]);
            (*nargs)++;
        }
    } else {
        die("AOT: only numbers and payloads can cross the seam");
    }
}

static PiVal pi_call(PiWorker *w, long op, int n, PiVal *args) {
    Buf b; binit(&b);
    long corr = g_corr++;
    bprintf(&b, "%ld\n%ld\n", corr, op);
    long nargs = 0;
    for (int i = 0; i < n; i++) append_arg(&b, &nargs, args[i]);
    /* nargs must be written before the arguments, so rebuild: header first was wrong. */
    Buf req; binit(&req);
    bprintf(&req, "%ld\n%ld\n%ld\n", corr, op, nargs);
    for (int i = 0; i < n; i++) append_arg(&req, &nargs /* ignored */, args[i]);
    free(b.p);
    write_all(w, req.p, req.n);
    unsigned char h[16];
    read_all(w, h, 16);
    if (memcmp(h, "LMB1", 4) != 0) die("bad reply magic");
    if (h[4] != 1) die("reply is not an f64 frame");
    uint32_t sid = (uint32_t)h[8] | ((uint32_t)h[9] << 8) | ((uint32_t)h[10] << 16) | ((uint32_t)h[11] << 24);
    uint32_t nbytes = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
    if ((long)sid != corr) die("reply correlation mismatch");
    double *body = (double *)malloc(nbytes ? nbytes : 8);
    if (!body) die("out of memory");
    if (nbytes) read_all(w, body, nbytes);
    long count = nbytes / 8;
    free(req.p);
    if (count < 1) die("empty reply");
    long kind = (long)body[0];
    if (kind == 2) {
        fprintf(stderr, "pi-aot: worker error %ld\n", count > 1 ? (long)body[1] : -1);
        exit(1);
    }
    long off = 1;
    if (kind == 1) {
        long k = count > 1 ? (long)body[1] : 0;
        off = 2 + k;
    }
    PiVal out = pi_payload(count - off);
    for (long i = off; i < count; i++) out.payload->v[i - off] = body[i];
    free(body);
    return out;
}

/* --------------------------------------------------------------- outputs */
static FILE *open_out(const char *name, const char *suffix) {
    if (!g_out) return NULL;
    char path[4096];
    snprintf(path, sizeof(path), "%s/%s%s", g_out, name, suffix);
    FILE *f = fopen(path, "w");
    if (!f) die_errno(path);
    return f;
}
static void pi_emit_text(const char *s) { printf("%s\n", s); }
static void pi_emit_any(PiVal v) {
    if (v.kind == PI_NUM) printf("%.17g", v.num);
    else if (v.kind == PI_STR) printf("%s", v.str);
    else if (v.kind == PI_PAYLOAD) {
        long n = v.payload->n;
        if (n > 32) {
            printf("[%ld values: ", n);
            for (long i = 0; i < 6; i++) printf("%s%.6g", i ? ", " : "", v.payload->v[i]);
            printf(", ...]");
        } else {
            printf("[");
            for (long i = 0; i < n; i++) printf("%s%.6g", i ? ", " : "", v.payload->v[i]);
            printf("]");
        }
    }
    else printf("()");
}
static void pi_emit_args(int n, PiVal *v) {
    for (int i = 0; i < n; i++) {
        if (i) printf(" ");
        pi_emit_any(v[i]);
    }
    printf("\n");
}
static void pi_emit_series(const char *name, PiVal x, PiVal y) {
    FILE *f = open_out(name, ".csv");
    if (!f) { printf("series %s: %ld points\n", name, x.payload ? x.payload->n : 0); return; }
    fprintf(f, "x,y\n");
    if (x.kind == PI_PAYLOAD && y.kind == PI_PAYLOAD) {
        long n = x.payload->n < y.payload->n ? x.payload->n : y.payload->n;
        for (long i = 0; i < n; i++) fprintf(f, "%.17g,%.17g\n", x.payload->v[i], y.payload->v[i]);
    }
    fclose(f);
}
static void pi_emit_grid(const char *name, PiVal v, long rows, long cols) {
    FILE *f = open_out(name, ".csv");
    if (!f) { printf("grid %s: %ldx%ld\n", name, rows, cols); return; }
    fprintf(f, "row,col,value\n");
    if (v.kind == PI_PAYLOAD) {
        for (long r = 0; r < rows; r++)
            for (long c = 0; c < cols; c++) {
                long i = r * cols + c;
                if (i < v.payload->n) fprintf(f, "%ld,%ld,%.17g\n", r, c, v.payload->v[i]);
            }
    }
    fclose(f);
}
static void pi_emit_animation_frame(const char *name, PiVal v, long rows, long cols) {
    g_frames++;
    snprintf(g_anim_name, sizeof(g_anim_name), "%s", name);
    g_rows = rows; g_cols = cols;
    if (v.kind == PI_PAYLOAD) {
        free(g_last);
        g_last_n = v.payload->n;
        g_last = (double *)malloc(sizeof(double) * (size_t)(g_last_n > 0 ? g_last_n : 1));
        if (!g_last) die("out of memory");
        memcpy(g_last, v.payload->v, sizeof(double) * (size_t)g_last_n);
    }
    if (g_frames % 10 == 1) printf("animation %s: frame %ld\n", name, g_frames);
}
static void pi_set_output(const char *dir) {
    g_out = dir;
    if (dir && *dir) PI_MKDIR(dir);
}
static void pi_finish(void) {
    if (g_frames) printf("animation frames: %ld\n", g_frames);
    if (g_last && g_out && g_anim_name[0]) {
        FILE *f = open_out(g_anim_name, ".final.csv");
        if (f) {
            fprintf(f, "row,col,value\n");
            for (long r = 0; r < g_rows; r++)
                for (long c = 0; c < g_cols; c++) {
                    long i = r * g_cols + c;
                    if (i < g_last_n) fprintf(f, "%ld,%ld,%.17g\n", r, c, g_last[i]);
                }
            fclose(f);
        }
    }
}
