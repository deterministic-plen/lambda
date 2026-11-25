/* aot_runtime.c -- small native runtime for the Pi AOT subset.
 *
 * It speaks the Lambda-Pi worker protocol directly: spawn one worker process,
 * write text requests, read LMB1 frames, and route payloads to CSV/text output.
 * No Python is involved at run time.
 *
 * PR.md A1-A6 / P7 live here:
 *   A1  no output directory means "print a summary", not files in the root;
 *   A2  artifact labels are sanity-checked (same rule as the C++ runtime);
 *   A3  the worker HELLO schema hash is verified;
 *   A4  replies are validated before indexing/allocating;
 *   A5  strings carry an explicit length (NUL/control characters survive);
 *   P7  double-to-integer conversions are range-checked (no UB).
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>
#include <math.h>
#include <limits.h>

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

#define PI_MAX_FRAME (256ull * 1024 * 1024)
#define PI_MAX_GRID_CELLS 100000000L

typedef enum { PI_UNIT, PI_NUM, PI_STR, PI_PAYLOAD, PI_WORKER } PiKind;
typedef struct PiPayload { long n; double *v; } PiPayload;
typedef struct PiWorker PiWorker;
typedef struct {
    PiKind kind;
    double num;
    PiPayload *payload;
    PiWorker *worker;
    const char *str;      /* PI_STR bytes; not NUL-terminated as a whole (A5) */
    long len;             /* PI_STR byte length */
} PiVal;

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
static long g_anim_len = 0;
static double *g_last = NULL;
static long g_last_n = 0, g_rows = 0, g_cols = 0;
static int g_schema_set = 0;
static uint64_t g_schema_hi = 0, g_schema_lo = 0;

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
static PiVal pi_str(const char *s, long n) {
    PiVal v = pi_unit(); v.kind = PI_STR; v.str = s; v.len = n; return v;
}
static PiVal pi_payload(long n) {
    if (n < 0 || n > PI_MAX_GRID_CELLS) die("AOT: payload length out of range");
    PiVal v = pi_unit(); v.kind = PI_PAYLOAD;
    v.payload = (PiPayload *)calloc(1, sizeof(PiPayload));
    if (!v.payload) die("out of memory");
    v.payload->n = n;
    v.payload->v = (double *)calloc((size_t)(n > 0 ? n : 1), sizeof(double));
    if (!v.payload->v) die("out of memory");
    return v;
}

/* P7: NaN, infinities and out-of-range values are undefined behaviour in a raw
   double->integer cast; reject them (fractional values still truncate). */
static long pi_as_int(PiVal v) {
    double d;
    if (v.kind != PI_NUM) die("AOT: expected a number");
    d = v.num;
    if (!isfinite(d)) die("AOT: integer conversion of a non-finite number");
    if (d < (double)LONG_MIN || d > (double)LONG_MAX) die("AOT: integer out of range");
    return (long)d;
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
    if (ms < 0) ms = 0;
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
        if (!ReadFile(w->out, q, (DWORD)n, &got, NULL) || got == 0) die("worker stdout closed (worker died?)");
        q += got; n -= got;
    }
}
#else
static void write_all(PiWorker *w, const char *p, size_t n) {
    while (n) {
        ssize_t k = write(w->in, p, n);
        if (k <= 0) die("worker stdin write failed (worker died?)");
        p += k; n -= (size_t)k;
    }
}
static void read_all(PiWorker *w, void *p, size_t n) {
    char *q = (char *)p;
    while (n) {
        ssize_t k = read(w->out, q, n);
        if (k <= 0) die("worker stdout closed (worker died?)");
        q += k; n -= (size_t)k;
    }
}
#endif

/* ------------------------------------------------------------- spawn/HELLO */
static PiVal pi_spawn(const char *exe, int gpu) {
    PiWorker *w = (PiWorker *)calloc(1, sizeof(PiWorker));
    unsigned char h[16];
    uint32_t n;
    if (!w) die("out of memory");
    if (!exe || strchr(exe, '"')) die("worker path contains a quote");
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
    /* A3: validate HELLO instead of reading and discarding it. */
    read_all(w, h, 16);
    if (memcmp(h, "LMB1", 4) != 0) die("worker HELLO magic");
    if (h[4] != 2) die("worker HELLO tag");
    if ((h[8] | h[9] | h[10] | h[11]) != 0) die("worker HELLO stream id");
    n = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
    if (n != 24 || (uint64_t)n > PI_MAX_FRAME) die("worker HELLO shape");
    {
        unsigned char body[24];
        uint64_t hi, lo, version;
        read_all(w, body, 24);
        memcpy(&hi, body, 8);
        memcpy(&lo, body + 8, 8);
        memcpy(&version, body + 16, 8);
        if (version != 2) die("worker HELLO version");
        if (g_schema_set && (hi != g_schema_hi || lo != g_schema_lo))
            die("worker schema mismatch");
    }
    {
        PiVal v = pi_unit();
        v.kind = PI_WORKER;
        v.worker = w;
        return v;
    }
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
static void count_arg(long *nargs, PiVal v) {
    if (v.kind == PI_UNIT) return;
    if (v.kind == PI_NUM) { (*nargs)++; return; }
    if (v.kind == PI_PAYLOAD) { *nargs += 1 + v.payload->n; return; }
    die("AOT: only numbers and payloads can cross the seam");
}
static void append_arg(Buf *b, PiVal v) {
    if (v.kind == PI_UNIT) return;
    if (v.kind == PI_NUM) {
        bprintf(b, "%.17g\n", v.num);
        return;
    }
    if (v.kind == PI_PAYLOAD) {
        long n = v.payload->n;
        bprintf(b, "%ld\n", n);
        for (long i = 0; i < n; i++) bprintf(b, "%.17g\n", v.payload->v[i]);
        return;
    }
    die("AOT: only numbers and payloads can cross the seam");
}
static long body_int(double d) {
    if (!isfinite(d) || d < (double)LONG_MIN || d > (double)LONG_MAX)
        die("reply BadFrame value out of range");
    return (long)d;
}

static PiVal pi_call(PiWorker *w, long op, int n, PiVal *args) {
    Buf req; binit(&req);
    long corr = g_corr++;
    long nargs = 0;
    /* A6: count first (cheap, no formatting), then serialise once.  The old
       code formatted every argument into a throw-away buffer first. */
    for (int i = 0; i < n; i++) count_arg(&nargs, args[i]);
    bprintf(&req, "%ld\n%ld\n%ld\n", corr, op, nargs);
    for (int i = 0; i < n; i++) append_arg(&req, args[i]);
    write_all(w, req.p, req.n);
    free(req.p);

    unsigned char h[16];
    read_all(w, h, 16);
    if (memcmp(h, "LMB1", 4) != 0) die("bad reply magic");
    if (h[4] != 1) die("reply is not an f64 frame");
    {
        uint32_t sid = (uint32_t)h[8] | ((uint32_t)h[9] << 8) | ((uint32_t)h[10] << 16) | ((uint32_t)h[11] << 24);
        uint32_t nbytes = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
        double *body;
        long count, kind, off;
        /* A4: never trust the worker's frame length. */
        if ((long)sid != corr) die("reply correlation mismatch");
        if (nbytes < 8 || (nbytes % 8) != 0 || (uint64_t)nbytes > PI_MAX_FRAME)
            die("reply BadFrame length");
        body = (double *)malloc(nbytes);
        if (!body) die("out of memory");
        read_all(w, body, nbytes);
        count = (long)(nbytes / 8);
        kind = body_int(body[0]);
        if (kind == 2) {
            long code = count > 1 ? body_int(body[1]) : -1;
            fprintf(stderr, "pi-aot: worker error %ld\n", code);
            exit(1);
        }
        if (kind == 1) {
            long k;
            if (count < 2) die("reply BadFrame stats header");
            k = body_int(body[1]);
            if (k < 0 || k > count - 2) die("reply BadFrame stats length");
            off = 2 + k;
        } else if (kind == 0) {
            off = 1;
        } else {
            die("reply BadFrame tag");
        }
        if (off < 0 || off > count) die("reply BadFrame payload offset");
        {
            PiVal out = pi_payload(count - off);
            for (long i = off; i < count; i++) out.payload->v[i - off] = body[i];
            free(body);
            return out;
        }
    }
}

/* --------------------------------------------------------------- outputs */
/* A2: the same rule as the C++ runtime's safe_artifact_name, including NUL and
   ':' (NTFS alternate data streams) which the old C++ check missed. */
static void safe_name(PiVal name) {
    const char *s;
    long n;
    if (name.kind != PI_STR) die("AOT: artifact name must be a string");
    s = name.str;
    n = name.len;
    if (n <= 0) die("unsafe artifact name: empty");
    if (n > 4096) die("unsafe artifact name: too long");
    if (memchr(s, '\0', (size_t)n)) die("unsafe artifact name: contains NUL");
    if ((n == 1 && s[0] == '.') || (n == 2 && s[0] == '.' && s[1] == '.'))
        die("unsafe artifact name: '.'/'..'");
    for (long i = 0; i < n; i++) {
        if (s[i] == '/' || s[i] == '\\' || s[i] == ':')
            die("unsafe artifact name: path separator or drive/stream");
    }
    for (long i = 0; i + 1 < n; i++)
        if (s[i] == '.' && s[i + 1] == '.') die("unsafe artifact name: '..'");
}

static FILE *open_out(PiVal name, const char *suffix) {
    char path[4096];
    int n;
    if (!g_out) return NULL;
    n = snprintf(path, sizeof(path), "%s/%.*s%s", g_out, (int)name.len, name.str, suffix);
    if (n < 0 || (size_t)n >= sizeof(path)) die("artifact path too long");
    FILE *f = fopen(path, "w");
    if (!f) die_errno(path);
    return f;
}
static void pi_emit_text(PiVal s) {
    if (s.kind != PI_STR) { printf("()\n"); return; }
    if (s.len > 0) fwrite(s.str, 1, (size_t)s.len, stdout);  /* A5: no NUL truncation */
    printf("\n");
}
static void pi_emit_any(PiVal v) {
    if (v.kind == PI_NUM) printf("%.17g", v.num);
    else if (v.kind == PI_STR) { if (v.len > 0) fwrite(v.str, 1, (size_t)v.len, stdout); }
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
static void pi_emit_series(PiVal name, PiVal x, PiVal y) {
    FILE *f;
    safe_name(name);
    f = open_out(name, ".csv");
    if (!f) { printf("series %.*s: %ld points\n", (int)name.len, name.str, x.payload ? x.payload->n : 0); return; }
    fprintf(f, "x,y\n");
    if (x.kind == PI_PAYLOAD && y.kind == PI_PAYLOAD) {
        long n = x.payload->n < y.payload->n ? x.payload->n : y.payload->n;
        for (long i = 0; i < n; i++) fprintf(f, "%.17g,%.17g\n", x.payload->v[i], y.payload->v[i]);
    }
    fclose(f);
}
static void pi_emit_points(PiVal name, PiVal x, PiVal y) {
    FILE *f;
    safe_name(name);
    f = open_out(name, ".csv");
    if (!f) { printf("points %.*s: %ld\n", (int)name.len, name.str, x.payload ? x.payload->n : 0); return; }
    fprintf(f, "x,y\n");
    if (x.kind == PI_PAYLOAD && y.kind == PI_PAYLOAD) {
        long n = x.payload->n < y.payload->n ? x.payload->n : y.payload->n;
        for (long i = 0; i < n; i++)
            fprintf(f, "%.17g,%.17g\n", x.payload->v[i], y.payload->v[i]);
    }
    fclose(f);
}

static void pi_emit_bars(PiVal name, PiVal x, PiVal y) {
    FILE *f;
    safe_name(name);
    f = open_out(name, ".csv");
    if (!f) { printf("bars %.*s: %ld\n", (int)name.len, name.str, x.payload ? x.payload->n : 0); return; }
    fprintf(f, "x,y\n");
    if (x.kind == PI_PAYLOAD && y.kind == PI_PAYLOAD) {
        long n = x.payload->n < y.payload->n ? x.payload->n : y.payload->n;
        for (long i = 0; i < n; i++)
            fprintf(f, "%.17g,%.17g\n", x.payload->v[i], y.payload->v[i]);
    }
    fclose(f);
}

static void pi_emit_grid(PiVal name, PiVal v, long rows, long cols) {
    FILE *f;
    safe_name(name);
    if (rows < 0 || cols < 0 || (rows && cols > PI_MAX_GRID_CELLS / rows))
        die("grid dimensions invalid");
    f = open_out(name, ".csv");
    if (!f) { printf("grid %.*s: %ldx%ld\n", (int)name.len, name.str, rows, cols); return; }
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
static void pi_emit_animation_frame(PiVal name, PiVal v, long rows, long cols) {
    safe_name(name);
    g_frames++;
    g_anim_len = name.len < (long)sizeof(g_anim_name) - 1 ? name.len : (long)sizeof(g_anim_name) - 1;
    memcpy(g_anim_name, name.str, (size_t)g_anim_len);
    g_anim_name[g_anim_len] = '\0';
    g_rows = rows; g_cols = cols;
    if (v.kind == PI_PAYLOAD) {
        free(g_last);
        g_last_n = v.payload->n;
        g_last = (double *)malloc(sizeof(double) * (size_t)(g_last_n > 0 ? g_last_n : 1));
        if (!g_last) die("out of memory");
        memcpy(g_last, v.payload->v, sizeof(double) * (size_t)g_last_n);
    }
    if (g_frames % 10 == 1) printf("animation %.*s: frame %ld\n", (int)g_anim_len, g_anim_name, g_frames);
}
/* A1: an unset/empty output directory means "no files", not the filesystem root. */
static void pi_set_output(const char *dir) {
    g_out = (dir && *dir) ? dir : NULL;
    if (g_out) PI_MKDIR(dir);
}
static void pi_set_schema(unsigned long long hi, unsigned long long lo) {
    g_schema_set = 1;
    g_schema_hi = (uint64_t)hi;
    g_schema_lo = (uint64_t)lo;
}
static void pi_finish(void) {
    if (g_frames) printf("animation frames: %ld\n", g_frames);
    if (g_last && g_out && g_anim_len) {
        PiVal nm = pi_str(g_anim_name, g_anim_len);
        FILE *f = open_out(nm, ".final.csv");
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
