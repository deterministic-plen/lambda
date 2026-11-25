"""C runtime and LLVM declarations for generated programs.

Boxed values are reference counted.  The generated code follows a simple
ownership convention:

* every boxed expression result is an owned reference;
* a function owns its boxed parameters and releases them before returning;
* containers (environments, cons cells) own their contents;
* immortal values (literals, ``nil``, primitive closures) have ``rc == -1``.

That makes the hot path allocation-free once inference has chosen unboxed
``i64`` values, while boxed data is reclaimed deterministically.
"""

RUNTIME_C = r"""
#define _CRT_SECURE_NO_WARNINGS
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef _WIN32
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

typedef struct Value Value;
typedef struct EnvHeader EnvHeader;
typedef Value *(*Code)(void *env, Value *arg);
typedef Value *(*UnaryFn)(Value *);

/* Keep this layout in sync with %Value in the generated IR. */
struct Value {
    int32_t tag;
    int32_t rc;      /* -1 == immortal */
    int64_t i;       /* int/bool payload, env pointer, or cons head */
    void   *p;       /* string bytes, code pointer, or cons tail */
};

struct EnvHeader {
    void (*dtor)(void *);
};

enum {
    TAG_INT = 0,
    TAG_BOOL = 1,
    TAG_CLOSURE = 2,
    TAG_STRING = 3,
    TAG_CONS = 4,
    TAG_NIL = 5,
    TAG_FLOAT = 6,
    TAG_ARRAYF = 7,
    TAG_ARRAYI = 8,
    TAG_ARRAYB = 9,
    TAG_ARRAYG = 10   /* boxed generic Array<T>: slots are owned %Value* */
};

#ifdef LAMBDA_ATOMIC_RC
#define STAT_ADD(x, n) __atomic_add_fetch(&(x), (n), __ATOMIC_RELAXED)
#define STAT_LOAD(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#else
#define STAT_ADD(x, n) ((x) += (n))
#define STAT_LOAD(x) (x)
#endif

static int64_t value_allocs = 0;
static int64_t value_frees = 0;
static int64_t env_allocs = 0;
static int64_t env_frees = 0;
static int64_t live_values = 0;
/* Values made immortal by `immortalize`.  They are excluded from live_values
   (they are intentionally never freed), which used to make a leak of an
   immortalised allocation invisible to the harness; the separate counter
   keeps that leak visible without turning intentional immortalisation into a
   test failure. */
static int64_t immortal_values = 0;

static Value NIL_VALUE = { TAG_NIL, -1, 0, NULL };

static void fail(const char *msg);
static void fail_type(const char *expected, Value *got);
static Value *apply_borrowed(Value *fn, Value *arg);
static Value *apply_owned(Value *fn, Value *arg);

/* ------------------------------------------------------------------ */
/* allocation                                                          */
/* ------------------------------------------------------------------ */
static void *xmalloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) {
        fflush(stdout);
        fprintf(stderr, "lambda: out of memory\n");
        exit(1);
    }
    return p;
}

/* Bounded array-data pool.  Bigint fast doubling creates hundreds of
   thousands of small limb arrays; recycling their data blocks avoids most
   malloc/free traffic while keeping the RC Value headers normal. */
#define POOL_CLASSES 32
#define POOL_PER_CLASS 16
#define POOL_MAX_BYTES (1u << 20)
static void *pool_data[POOL_CLASSES][POOL_PER_CLASS];
static int pool_count[POOL_CLASSES];
static int pool_ready = 0;
#ifdef _WIN32
static CRITICAL_SECTION pool_lock;
#else
static pthread_mutex_t pool_lock;
#endif

static void pool_init(void) {
    if (pool_ready) return;
#ifdef _WIN32
    InitializeCriticalSection(&pool_lock);
#else
    pthread_mutex_init(&pool_lock, NULL);
#endif
    pool_ready = 1;
}

static void pool_enter(void) {
#ifdef LAMBDA_ATOMIC_RC
#ifdef _WIN32
    EnterCriticalSection(&pool_lock);
#else
    pthread_mutex_lock(&pool_lock);
#endif
#endif
}

static void pool_leave(void) {
#ifdef LAMBDA_ATOMIC_RC
#ifdef _WIN32
    LeaveCriticalSection(&pool_lock);
#else
    pthread_mutex_unlock(&pool_lock);
#endif
#endif
}

static int pool_class(size_t n) {
    int c = 0;
    size_t s = 1;
    while (s < n && c < POOL_CLASSES - 1) { s <<= 1; ++c; }
    return c;
}

static void *pool_take(size_t n, size_t *capacity) {
    int c = pool_class(n);
    if (n > POOL_MAX_BYTES) {
        *capacity = n;
        return xmalloc(n);
    }
    pool_enter();
    void *p = NULL;
    if (pool_count[c] > 0) {
        p = pool_data[c][--pool_count[c]];
    }
    pool_leave();
    *capacity = (size_t)1 << c;
    if (p) return p;
    return xmalloc(*capacity);
}

static void pool_put(void *p, size_t n) {
    if (!p) return;
    if (n > POOL_MAX_BYTES) { free(p); return; }
    int c = pool_class(n);
    pool_enter();
    if (pool_count[c] < POOL_PER_CLASS) {
        pool_data[c][pool_count[c]++] = p;
        p = NULL;
    }
    pool_leave();
    if (p) free(p);
}

static Value *value_alloc(int32_t tag) {
    Value *v = (Value *)xmalloc(sizeof(Value));
    v->tag = tag;
    v->rc = 1;
    v->i = 0;
    v->p = NULL;
    STAT_ADD(value_allocs, 1);
    STAT_ADD(live_values, 1);
    return v;
}

static Value *value_alloc_immortal(int32_t tag) {
    Value *v = (Value *)xmalloc(sizeof(Value));
    v->tag = tag;
    v->rc = -1;
    v->i = 0;
    v->p = NULL;
    return v;
}

void *lambda_alloc_env(int64_t size) {
    void *p = xmalloc((size_t)size);
    STAT_ADD(env_allocs, 1);
    return p;
}

void lambda_free_env(void *p) {
    if (p) {
        STAT_ADD(env_frees, 1);
        free(p);
    }
}

void lambda_retain(Value *v) {
    if (!v) return;
#ifdef LAMBDA_ATOMIC_RC
    int32_t rc = __atomic_load_n(&v->rc, __ATOMIC_RELAXED);
    if (rc > 0) __atomic_add_fetch(&v->rc, 1, __ATOMIC_RELAXED);
#else
    if (v->rc > 0) v->rc++;
#endif
}

/* Return 1 when this call dropped the last reference. */
static int rc_release_once(Value *v) {
#ifdef LAMBDA_ATOMIC_RC
    int32_t old = __atomic_load_n(&v->rc, __ATOMIC_RELAXED);
    while (old > 0) {
        if (__atomic_compare_exchange_n(&v->rc, &old, old - 1, 0,
                                        __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
            return old == 1;
        }
    }
    return 0;
#else
    if (v->rc < 0) return 0;
    return --v->rc == 0;
#endif
}

void lambda_release(Value *v);

/* Iterative teardown worklist.

   Values used to be destroyed recursively: a cons chain freed its head with
   lambda_release and a closure's generated environment destructor released
   the closures it captured.  A chain of ~150k nested closures (or a
   left-nested list in the boxed backend) then overflowed the 8 MB native
   stack.  Now a value whose reference count just reached zero is only pushed
   here; the outermost release call drains the worklist in a loop.  Nested
   releases (from a destructor or a cons head) just push, so the C stack depth
   no longer grows with the data structure.  The worklist is thread local, so
   pmap workers cannot race on it. */
typedef struct ReleaseWork {
    Value **items;
    size_t count;
    size_t cap;
    int busy;
} ReleaseWork;

#ifdef LAMBDA_ATOMIC_RC
/* pmap workers release values concurrently, so each thread needs its own
   worklist.  Static `__declspec(thread)` gave every Windows executable a
   load-time TLS directory, which made the first launch of each freshly built
   binary take ~14 s on a machine with real-time scanning.  Dynamic TLS keeps
   the hot single-threaded runtime free of a TLS section while still giving
   atomic builds a per-thread list. */
#ifdef _WIN32
static ReleaseWork *release_work_slot(void) {
    static volatile LONG key = -1;
    if (key == -1) {
        DWORD fresh = TlsAlloc();
        if (fresh == TLS_OUT_OF_INDEXES) fail("out of memory: TLS slot");
        if (InterlockedCompareExchange(&key, (LONG)fresh, -1) != -1) {
            TlsFree(fresh); /* another thread installed first */
        }
    }
    ReleaseWork *w = (ReleaseWork *)TlsGetValue((DWORD)key);
    if (!w) {
        w = (ReleaseWork *)calloc(1, sizeof(ReleaseWork));
        if (!w) fail("out of memory: release worklist");
        TlsSetValue((DWORD)key, w);
    }
    return w;
}
#else
static pthread_key_t release_work_key;
static pthread_once_t release_work_once = PTHREAD_ONCE_INIT;
static void release_work_make(void) {
    if (pthread_key_create(&release_work_key, NULL) != 0)
        fail("out of memory: release worklist key");
}
static ReleaseWork *release_work_slot(void) {
    pthread_once(&release_work_once, release_work_make);
    ReleaseWork *w = (ReleaseWork *)pthread_getspecific(release_work_key);
    if (!w) {
        w = (ReleaseWork *)calloc(1, sizeof(ReleaseWork));
        if (!w) fail("out of memory: release worklist");
        pthread_setspecific(release_work_key, w);
    }
    return w;
}
#endif
#else
/* The non-atomic runtime is single-threaded; a plain static keeps the hot
   path branch-free and avoids a TLS section in every executable. */
static ReleaseWork g_release_work;
static ReleaseWork *release_work_slot(void) {
    return &g_release_work;
}
#endif

static void release_push(Value *v) {
    ReleaseWork *w = release_work_slot();
    if (w->count == w->cap) {
        size_t cap = w->cap ? w->cap * 2 : 32;
        Value **items = (Value **)realloc(w->items, cap * sizeof(Value *));
        if (!items) fail("out of memory growing the release worklist");
        w->items = items;
        w->cap = cap;
    }
    w->items[w->count++] = v;
}

/* Tear down one value whose reference count is already zero. */
static void release_teardown(Value *v) {
    switch (v->tag) {
    case TAG_CONS: {
        /* Queue the tail before the head: the LIFO worklist then visits the
           head's subtree first, which keeps the worklist small for the common
           right-nested list shape. */
        Value *head = (Value *)(intptr_t)v->i;
        Value *tail = (Value *)v->p;
        free(v);
        STAT_ADD(value_frees, 1);
        STAT_ADD(live_values, -1);
        lambda_release(tail);
        lambda_release(head);
        return;
    }
    case TAG_CLOSURE:
        if (v->i) {
            EnvHeader *env = (EnvHeader *)(intptr_t)v->i;
            if (env->dtor) env->dtor(env);
        }
        break;
    case TAG_STRING:
        free(v->p);
        break;
    case TAG_ARRAYF:
    case TAG_ARRAYI:
        /* Must use the same size rule as pool_take (checked_array_bytes):
           an empty array's block was taken as a 1-byte, class-0 block, so
           filing it under the 8-byte class made the next one-element array
           write 8 bytes into it. */
        pool_put(v->p, v->i > 0 ? sizeof(double) * (size_t)v->i : 1);
        v->p = NULL;
        break;
    case TAG_ARRAYB:
        pool_put(v->p, v->i > 0 ? (size_t)v->i : 1);
        v->p = NULL;
        break;
    case TAG_ARRAYG:
        /* Each slot owns one reference.  Releasing the elements may push
           them onto this worklist; that is handled by the drain loop. */
        if (v->p) {
            Value **slots = (Value **)v->p;
            int64_t n = v->i;
            for (int64_t i = 0; i < n; i++) lambda_release(slots[i]);
            pool_put(v->p, n > 0 ? sizeof(Value *) * (size_t)n : 1);
            v->p = NULL;
        }
        break;
    default:
        break;
    }
    free(v);
    STAT_ADD(value_frees, 1);
    STAT_ADD(live_values, -1);
}

static void release_drain(void) {
    ReleaseWork *w = release_work_slot();
    if (w->busy) return; /* a nested release: the outermost caller drains */
    w->busy = 1;
    while (w->count) {
        Value *v = w->items[--w->count];
        release_teardown(v);
    }
    w->busy = 0;
}

/* Called with v->rc already at zero. */
void lambda_release_zero(Value *v) {
    if (!v) return;
    release_push(v);
    release_drain();
}

void lambda_release(Value *v) {
    if (!v) return;
    if (!rc_release_once(v)) return;
    lambda_release_zero(v);
}

/* ------------------------------------------------------------------ */
/* constructors                                                        */
/* ------------------------------------------------------------------ */
Value *lambda_box_int(int64_t x) {
    Value *v = value_alloc(TAG_INT);
    v->i = x;
    return v;
}

/* Immutable index boxes for parallel map.  The pmap path used to allocate and
   free one Value per element, which serialized the allocator and dominated the
   stencil timings.  Reuse immortal boxes for small/medium index ranges. */
#define LAMBDA_INT_CACHE_MAX ((int64_t)1 << 20)
static Value **g_int_cache = NULL;
static int64_t g_int_cache_n = 0;

static void int_cache_ensure(int64_t n) {
    if (n <= g_int_cache_n) return;
    if (n > LAMBDA_INT_CACHE_MAX) return;
    int64_t old = g_int_cache_n;
    Value **p = (Value **)realloc(g_int_cache, (size_t)n * sizeof(Value *));
    if (!p) return;
    g_int_cache = p;
    for (int64_t i = old; i < n; i++) {
        Value *v = value_alloc_immortal(TAG_INT);
        v->i = i;
        g_int_cache[i] = v;
    }
    g_int_cache_n = n;
}

static Value *int_immortal(int64_t x) {
    if (x >= 0 && x < g_int_cache_n) return g_int_cache[x];
    return lambda_box_int(x);
}

Value *lambda_box_bool(int64_t x) {
    Value *v = value_alloc(TAG_BOOL);
    v->i = x ? 1 : 0;
    return v;
}

Value *lambda_box_float(double x) {
    Value *v = value_alloc(TAG_FLOAT);
    memcpy(&v->i, &x, sizeof(double));
    return v;
}

double lambda_get_float(Value *v) {
    if (!v || v->tag != TAG_FLOAT) fail_type("Float", v);
    double x;
    memcpy(&x, &v->i, sizeof(double));
    return x;
}

Value *lambda_nil(void) { return &NIL_VALUE; }

Value *lambda_make_closure(void *code, void *env) {
    Value *v = value_alloc(TAG_CLOSURE);
    v->i = (int64_t)(intptr_t)env;
    v->p = code;
    return v;
}

static Value *make_immortal_closure(void *code, void *env) {
    Value *v = value_alloc_immortal(TAG_CLOSURE);
    v->i = (int64_t)(intptr_t)env;
    v->p = code;
    return v;
}

/* Takes ownership of head and tail. */
Value *lambda_cons(Value *head, Value *tail) {
    Value *v = value_alloc(TAG_CONS);
    v->i = (int64_t)(intptr_t)head;
    v->p = tail;
    return v;
}

Value *lambda_string_new_len(const char *bytes, int64_t len) {
    Value *v = value_alloc(TAG_STRING);
    char *buf = (char *)xmalloc((size_t)len + 1);
    if (len) memcpy(buf, bytes, (size_t)len);
    buf[len] = 0;
    v->i = len;
    v->p = buf;
    return v;
}

Value *lambda_string_new(const char *bytes) {
    return lambda_string_new_len(bytes, (int64_t)strlen(bytes));
}

/* ------------------------------------------------------------------ */
/* inspection / errors                                                 */
/* ------------------------------------------------------------------ */
static const char *tag_name(int32_t tag) {
    switch (tag) {
    case TAG_INT: return "Int";
    case TAG_BOOL: return "Bool";
    case TAG_CLOSURE: return "Function";
    case TAG_STRING: return "String";
    case TAG_CONS: return "List";
    case TAG_NIL: return "List";
    case TAG_FLOAT: return "Float";
    case TAG_ARRAYF: return "ArrayF";
    case TAG_ARRAYI: return "ArrayI";
    case TAG_ARRAYB: return "ArrayB";
    case TAG_ARRAYG: return "Array";
    default: return "?";
    }
}

static void fail(const char *msg) {
    fflush(stdout);
    fprintf(stderr, "lambda: runtime error: %s\n", msg);
    exit(1);
}

static void fail_type(const char *expected, Value *got) {
    char buf[160];
    snprintf(buf, sizeof(buf), "expected %s, got %s", expected,
             got ? tag_name(got->tag) : "null");
    fail(buf);
}

void lambda_check_function(Value *v) {
    if (!v || v->tag != TAG_CLOSURE) fail_type("Function", v);
}

void lambda_bad_enum(void) {
    fail("invalid enum tag");
}

int64_t lambda_get_int(Value *v) {
    if (!v || v->tag != TAG_INT) fail_type("Int", v);
    return v->i;
}

/* First-class `and`/`or`: short-circuit semantics are preserved for direct
   applications; as values they take both operands already evaluated. */
int64_t lambda_truthy(Value *v);

Value *lambda_op_and(Value *a, Value *b) {
    if (lambda_truthy(a)) { lambda_release(a); return b; }
    lambda_release(b); return a;
}

Value *lambda_op_or(Value *a, Value *b) {
    if (lambda_truthy(a)) { lambda_release(b); return a; }
    lambda_release(a); return b;
}

int64_t lambda_get_bool(Value *v) {
    if (!v || v->tag != TAG_BOOL) fail_type("Bool", v);
    return v->i ? 1 : 0;
}

int64_t lambda_truthy(Value *v) {
    if (!v) return 0;
    switch (v->tag) {
    case TAG_BOOL:
    case TAG_INT: return v->i != 0;
    case TAG_FLOAT: {
        double x;
        memcpy(&x, &v->i, sizeof(double));
        return x != 0.0;
    }
    case TAG_NIL: return 0;
    default: return 1;
    }
}

static const char *string_data(Value *v) {
    if (!v || v->tag != TAG_STRING) fail_type("String", v);
    return (const char *)v->p;
}

int64_t lambda_div_i64(int64_t a, int64_t b) {
    if (b == 0) fail("division by zero");
    if (b == -1 && a == INT64_MIN) return INT64_MIN;
    return a / b;
}

int64_t lambda_ftoi(double x) {
    if (isnan(x)) return 0;
    if (x >= 9223372036854775807.0) return INT64_MAX;
    if (x <= -9223372036854775808.0) return INT64_MIN;
    return (int64_t)x;
}

int64_t lambda_mod_i64(int64_t a, int64_t b) {
    if (b == 0) fail("division by zero");
    if (b == -1) return 0;
    return a % b;
}

/* ------------------------------------------------------------------ */
/* output                                                              */
/* ------------------------------------------------------------------ */
static char out_buf[65536];
static size_t out_len = 0;
static int data_mode = 0;

static void out_flush(void) {
    if (out_len) {
        fwrite(out_buf, 1, out_len, data_mode ? stderr : stdout);
        out_len = 0;
    }
}

static void out_write(const char *s, size_t n) {
    while (n) {
        size_t room = sizeof(out_buf) - out_len;
        if (room == 0) { out_flush(); room = sizeof(out_buf); }
        size_t take = n < room ? n : room;
        memcpy(out_buf + out_len, s, take);
        out_len += take;
        s += take;
        n -= take;
    }
}

static void out_puts(const char *s) { out_write(s, strlen(s)); }
static void out_ch(char c) { out_write(&c, 1); }

/* Binary data mode: stdout carries length-framed bytes, not terminal text.
   On Windows the CRT must be switched out of text mode or \n is translated. */
void lambda_enable_binary_stdout(void) {
    out_flush();
    data_mode = 1;
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
#endif
}

static int64_t write_raw(const void *data, size_t n) {
    out_flush();
    if (n && fwrite(data, 1, n, stdout) != n) {
        fail("write failed");
    }
    fflush(stdout);
    return (int64_t)n;
}

int64_t lambda_op_write_bytes(Value *v) {
    if (!v || v->tag != TAG_ARRAYB) fail_type("ArrayB", v);
    return write_raw(v->p, (size_t)v->i);
}

int64_t lambda_op_write_f64s(Value *v) {
    if (!v || v->tag != TAG_ARRAYF) fail_type("ArrayF", v);
    return write_raw(v->p, (size_t)v->i * sizeof(double));
}

int64_t lambda_op_write_i64s(Value *v) {
    if (!v || v->tag != TAG_ARRAYI) fail_type("ArrayI", v);
    return write_raw(v->p, (size_t)v->i * sizeof(int64_t));
}

static void print_string(Value *v, int quoted) {
    const char *s = (const char *)v->p;
    int64_t len = v->i;
    if (!quoted) {
        out_write(s, (size_t)len);
        return;
    }
    out_ch('"');
    for (int64_t k = 0; k < len; k++) {
        unsigned char c = (unsigned char)s[k];
        switch (c) {
        case '\\': out_puts("\\\\"); break;
        case '"': out_puts("\\\""); break;
        case '\n': out_puts("\\n"); break;
        case '\t': out_puts("\\t"); break;
        case '\r': out_puts("\\r"); break;
        default:
            if (c < 32) {
                char tmp[8];
                snprintf(tmp, sizeof(tmp), "\\x%02x", c);
                out_puts(tmp);
            } else {
                out_ch((char)c);
            }
        }
    }
    out_ch('"');
}

static void print_int(int64_t x) {
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%" PRId64, x);
    out_write(tmp, (size_t)n);
}

/* Shortest decimal representation that round-trips to the same double.

   "%.17g" exposed the binary noise of a value (0.1 printed as
   0.10000000000000001 and 1e-7 as 9.9999999999999995e-08).  Try increasing
   significant-digit counts until strtod() gives back the same bits, then
   render that digit string in fixed or scientific notation the way Python's
   repr does (fixed when the decimal point position is in (-4, 16]).
   NaN is normalised to "nan" (a NaN can carry a sign bit, which made the
   boxed backend print "-nan" while the typed one printed "nan"); infinities
   print "inf"/"-inf".  The result always looks like a float ("6" -> "6.0")
   unless it is in scientific notation. */
static int format_double(char *buf, size_t cap, double x) {
    if (isnan(x)) {
        if (cap < 4) return snprintf(buf, cap, "%g", x);
        memcpy(buf, "nan", 4);
        return 3;
    }
    if (isinf(x)) {
        const char *s = x < 0 ? "-inf" : "inf";
        size_t n = strlen(s);
        if (n + 1 > cap) return snprintf(buf, cap, "%g", x);
        memcpy(buf, s, n + 1);
        return (int)n;
    }
    char cand[64];
    int precision;
    for (precision = 1; precision <= 17; precision++) {
        snprintf(cand, sizeof(cand), "%.*e", precision - 1, x);
        if (strtod(cand, NULL) == x) break;
    }
    if (precision > 17) precision = 17;
    /* cand is "-d.dddddde[+-]dd": split it into digits and exponent. */
    const char *p = cand;
    int neg = 0;
    if (*p == '-') { neg = 1; p++; }
    char digits[32];
    size_t nd = 0;
    int exp10 = 0;
    for (; *p && *p != 'e' && *p != 'E'; p++) {
        if (*p == '.' || *p == '+') continue;
        if (nd < sizeof(digits) - 1) digits[nd++] = *p;
    }
    if (*p == 'e' || *p == 'E') exp10 = atoi(p + 1);
    while (nd > 1 && digits[nd - 1] == '0') nd--;
    digits[nd] = 0;
    int decpt = exp10 + 1; /* value = 0.digits * 10^decpt */
    size_t o = 0;
#define FD_PUT(ch) do { if (o + 1 < cap) buf[o] = (char)(ch); o++; } while (0)
    if (neg) FD_PUT('-');
    if (decpt > -4 && decpt <= 16) {
        if (decpt <= 0) {
            FD_PUT('0'); FD_PUT('.');
            for (int i = 0; i < -decpt; i++) FD_PUT('0');
            for (size_t i = 0; i < nd; i++) FD_PUT(digits[i]);
        } else if ((size_t)decpt >= nd) {
            for (size_t i = 0; i < nd; i++) FD_PUT(digits[i]);
            for (size_t i = nd; i < (size_t)decpt; i++) FD_PUT('0');
            FD_PUT('.'); FD_PUT('0');
        } else {
            for (int i = 0; i < decpt; i++) FD_PUT(digits[i]);
            FD_PUT('.');
            for (size_t i = (size_t)decpt; i < nd; i++) FD_PUT(digits[i]);
        }
    } else {
        FD_PUT(digits[0]);
        if (nd > 1) {
            FD_PUT('.');
            for (size_t i = 1; i < nd; i++) FD_PUT(digits[i]);
        }
        FD_PUT('e');
        int e = decpt - 1;
        FD_PUT(e < 0 ? '-' : '+');
        if (e < 0) e = -e;
        char ebuf[8];
        int en = snprintf(ebuf, sizeof(ebuf), "%d", e);
        if (en < 2) FD_PUT('0');
        for (int i = 0; i < en; i++) FD_PUT(ebuf[i]);
    }
#undef FD_PUT
    if (o + 1 > cap) return snprintf(buf, cap, "%.17g", x);
    buf[o] = 0;
    return (int)o;
}

static void print_double(double x) {
    char tmp[64];
    int n = format_double(tmp, sizeof(tmp), x);
    out_write(tmp, (size_t)n);
}

static void print_value_inner(Value *v, int nested) {
    if (!v) { out_puts("<null>"); return; }
    switch (v->tag) {
    case TAG_INT: print_int(v->i); break;
    case TAG_BOOL: out_puts(v->i ? "true" : "false"); break;
    case TAG_FLOAT: {
        double x;
        memcpy(&x, &v->i, sizeof(double));
        print_double(x);
        break;
    }
    case TAG_STRING: print_string(v, nested); break;
    case TAG_CLOSURE: out_puts("<function>"); break;
    case TAG_NIL: out_puts("[]"); break;
    case TAG_ARRAYF:
    case TAG_ARRAYI:
    case TAG_ARRAYB: {
        char buf[80];
        const char *kind = v->tag == TAG_ARRAYF ? "arrayf"
                         : v->tag == TAG_ARRAYI ? "arrayi" : "arrayb";
        snprintf(buf, sizeof(buf), "[%s %lld]", kind, (long long)v->i);
        out_puts(buf);
        break;
    }
    case TAG_ARRAYG: {
        /* Boxed generic arrays print as a list-like sequence of elements. */
        Value **slots = (Value **)v->p;
        out_ch('[');
        for (int64_t i = 0; i < v->i; i++) {
            if (i) out_puts(", ");
            print_value_inner(slots[i], 1);
        }
        out_ch(']');
        break;
    }
    case TAG_CONS: {
        Value *cur = v;
        out_ch('[');
        int first = 1;
        while (cur && cur->tag == TAG_CONS) {
            if (!first) out_puts(", ");
            first = 0;
            print_value_inner((Value *)(intptr_t)cur->i, 1);
            cur = (Value *)cur->p;
        }
        if (cur && cur->tag != TAG_NIL) {
            out_puts(" . ");
            print_value_inner(cur, 1);
        }
        out_ch(']');
        break;
    }
    default: out_puts("<?>"); break;
    }
}

void lambda_print_ln(Value *v) { print_value_inner(v, 0); out_ch('\n'); }
void lambda_print_i64(int64_t x) { print_int(x); out_ch('\n'); }
void lambda_print_bool(int64_t x) { out_puts(x ? "true\n" : "false\n"); }
void lambda_print_float(double x) { print_double(x); out_ch('\n'); }
void lambda_flush(void) { out_flush(); }

void lambda_stats(void) {
    if (!getenv("LAMBDA_STATS")) return;
    out_flush();
    fprintf(stderr,
            "lambda-stats: value_allocs=%" PRId64 " value_frees=%" PRId64
            " env_allocs=%" PRId64 " env_frees=%" PRId64 " live=%" PRId64
            " immortal=%" PRId64 "\n",
            STAT_LOAD(value_allocs), STAT_LOAD(value_frees),
            STAT_LOAD(env_allocs), STAT_LOAD(env_frees), STAT_LOAD(live_values),
            STAT_LOAD(immortal_values));
}

/* ------------------------------------------------------------------ */
/* structural equality                                                 */
/* ------------------------------------------------------------------ */
static int value_equal(Value *a, Value *b) {
    while (1) {
        if (!a || !b) return 0;
        if (a == b) {
            /* Same boxed object is usually equal, but IEEE NaN is not equal
               to itself; ``(== n n)`` must agree between the typed and boxed
               backends. */
            if (a->tag == TAG_FLOAT) {
                double x;
                memcpy(&x, &a->i, sizeof(double));
                return x == x;
            }
            return 1;
        }
        if (a->tag != b->tag) return 0;
        switch (a->tag) {
        case TAG_INT:
        case TAG_BOOL: return a->i == b->i;
        case TAG_FLOAT: {
            double x, y;
            memcpy(&x, &a->i, sizeof(double));
            memcpy(&y, &b->i, sizeof(double));
            return x == y;
        }
        case TAG_STRING:
            return a->i == b->i &&
                   memcmp(a->p, b->p, (size_t)a->i) == 0;
        case TAG_NIL: return 1;
        case TAG_CLOSURE: return a->p == b->p && a->i == b->i;
        case TAG_ARRAYF:
        case TAG_ARRAYI:
        case TAG_ARRAYB:
        case TAG_ARRAYG:
            /* Arrays are mutable identity objects, not structural values. */
            return a == b;
        case TAG_CONS:
            if (!value_equal((Value *)(intptr_t)a->i,
                             (Value *)(intptr_t)b->i)) return 0;
            a = (Value *)a->p;
            b = (Value *)b->p;
            continue;
        default: return 0;
        }
    }
}

/* ------------------------------------------------------------------ */
/* boxed operations (ownership: arguments are consumed)                */
/* ------------------------------------------------------------------ */
static int64_t as_int(Value *v) {
    if (!v || v->tag != TAG_INT) fail_type("Int", v);
    return v->i;
}

static double as_float(Value *v) {
    if (!v || v->tag != TAG_FLOAT) fail_type("Float", v);
    double x;
    memcpy(&x, &v->i, sizeof(double));
    return x;
}

static int both(Value *a, Value *b, int32_t tag) {
    return a && b && a->tag == tag && b->tag == tag;
}

/* ------------------------------------------------------------------ */
/* flat arrays (one RC header, contiguous unboxed element storage)     */
/* ------------------------------------------------------------------ */
static int64_t array_index(Value *a, int32_t tag, int64_t idx) {
    if (!a || a->tag != tag) fail_type(tag_name(tag), a);
    if (idx < 0 || idx >= a->i) fail("array index out of bounds");
    return idx;
}

void lambda_array_bounds_fail(void) {
    fail("array index out of bounds");
}

static size_t checked_array_bytes(int64_t n, size_t elem) {
    if (n < 0) fail("array size must be non-negative");
    if ((uint64_t)n > (uint64_t)(SIZE_MAX / elem))
        fail("array size is too large");
    size_t bytes = (size_t)n * elem;
    return bytes ? bytes : 1;
}

Value *lambda_arrayf_new(int64_t n, double init) {
    Value *a = value_alloc(TAG_ARRAYF);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(double)), &cap);
    double *d = (double *)a->p;
    for (int64_t i = 0; i < n; ++i) d[i] = init;
    return a;
}

/* pmap fully overwrites every element, so skip the zero fill. */
Value *lambda_arrayf_new_uninit(int64_t n) {
    Value *a = value_alloc(TAG_ARRAYF);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(double)), &cap);
    return a;
}

Value *lambda_arrayi_new_uninit(int64_t n) {
    Value *a = value_alloc(TAG_ARRAYI);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(int64_t)), &cap);
    return a;
}

double lambda_arrayf_get_unchecked(Value *a, int64_t idx) {
    if (!a || a->tag != TAG_ARRAYF) fail_type("ArrayF", a);
    return ((double *)a->p)[idx];
}

double lambda_arrayf_get(Value *a, int64_t idx) {
    idx = array_index(a, TAG_ARRAYF, idx);
    return ((double *)a->p)[idx];
}

void lambda_arrayf_set(Value *a, int64_t idx, double value) {
    idx = array_index(a, TAG_ARRAYF, idx);
    ((double *)a->p)[idx] = value;
}

int64_t lambda_arrayf_length(Value *a) {
    if (!a || a->tag != TAG_ARRAYF) fail_type("ArrayF", a);
    return a->i;
}

Value *lambda_arrayf_clone(Value *a) {
    if (!a || a->tag != TAG_ARRAYF) fail_type("ArrayF", a);
    Value *b = lambda_arrayf_new(a->i, 0.0);
    memcpy(b->p, a->p, sizeof(double) * (size_t)a->i);
    return b;
}

Value *lambda_arrayi_new(int64_t n, int64_t init) {
    Value *a = value_alloc(TAG_ARRAYI);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(int64_t)), &cap);
    int64_t *d = (int64_t *)a->p;
    for (int64_t i = 0; i < n; ++i) d[i] = init;
    return a;
}

int64_t lambda_arrayi_get(Value *a, int64_t idx) {
    idx = array_index(a, TAG_ARRAYI, idx);
    return ((int64_t *)a->p)[idx];
}

void lambda_arrayi_set(Value *a, int64_t idx, int64_t value) {
    idx = array_index(a, TAG_ARRAYI, idx);
    ((int64_t *)a->p)[idx] = value;
}

int64_t lambda_arrayi_length(Value *a) {
    if (!a || a->tag != TAG_ARRAYI) fail_type("ArrayI", a);
    return a->i;
}

Value *lambda_arrayi_clone(Value *a) {
    if (!a || a->tag != TAG_ARRAYI) fail_type("ArrayI", a);
    Value *b = lambda_arrayi_new(a->i, 0);
    memcpy(b->p, a->p, sizeof(int64_t) * (size_t)a->i);
    return b;
}

static int64_t byte_value(int64_t x) {
    if (x < 0 || x > 255) fail("byte value must be in 0..255");
    return x;
}

Value *lambda_arrayb_new(int64_t n, int64_t init) {
    Value *a = value_alloc(TAG_ARRAYB);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, 1), &cap);
    uint8_t *d = (uint8_t *)a->p;
    uint8_t b = (uint8_t)byte_value(init);
    for (int64_t i = 0; i < n; ++i) d[i] = b;
    return a;
}

int64_t lambda_arrayb_get(Value *a, int64_t idx) {
    idx = array_index(a, TAG_ARRAYB, idx);
    return (int64_t)((uint8_t *)a->p)[idx];
}

void lambda_arrayb_set(Value *a, int64_t idx, int64_t value) {
    idx = array_index(a, TAG_ARRAYB, idx);
    ((uint8_t *)a->p)[idx] = (uint8_t)byte_value(value);
}

int64_t lambda_arrayb_length(Value *a) {
    if (!a || a->tag != TAG_ARRAYB) fail_type("ArrayB", a);
    return a->i;
}

Value *lambda_arrayb_clone(Value *a) {
    if (!a || a->tag != TAG_ARRAYB) fail_type("ArrayB", a);
    Value *b = lambda_arrayb_new(a->i, 0);
    memcpy(b->p, a->p, (size_t)a->i);
    return b;
}

/* ------------------------------------------------------------------ */
/* boxed generic arrays (Array<T>)                                     */
/*                                                                     */
/* One RC header plus a contiguous array of owned %Value* slots: the   */
/* generic counterpart of ArrayI/ArrayF for arbitrary element types.   */
/* Every slot holds exactly one reference, so retains/releases follow  */
/* the normal rules and the array itself counts as one live value.     */
/* ------------------------------------------------------------------ */

static int32_t value_rc_load(Value *v) {
#ifdef LAMBDA_ATOMIC_RC
    return __atomic_load_n(&v->rc, __ATOMIC_RELAXED);
#else
    return v->rc;
#endif
}

/* Takes ownership of the list.  The element references are moved out of the
   cons cells into the array (no retain/release per element); only the cons
   cells are freed, exactly as a list release would do. */
Value *lambda_arrayg_of(Value *list) {
    if (!list) fail("array of null");
    int64_t n = 0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        n++;
        cur = (Value *)cur->p;
    }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", cur);
    Value *a = value_alloc(TAG_ARRAYG);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(Value *)), &cap);
    Value **slots = (Value **)a->p;
    cur = list;
    for (int64_t i = 0; i < n; i++) {
        slots[i] = (Value *)(intptr_t)cur->i; /* ownership moves to the slot */
        Value *next = (Value *)cur->p;
        free(cur);
        STAT_ADD(value_frees, 1);
        STAT_ADD(live_values, -1);
        cur = next;
    }
    return a;
}

/* Retains `init` once per slot; the caller keeps its own reference. */
Value *lambda_arrayg_new(int64_t n, Value *init) {
    if (n < 0) fail("array size must be non-negative");
    Value *a = value_alloc(TAG_ARRAYG);
    a->i = n;
    size_t cap = 0;
    a->p = pool_take(checked_array_bytes(n, sizeof(Value *)), &cap);
    Value **slots = (Value **)a->p;
    for (int64_t i = 0; i < n; i++) {
        lambda_retain(init);
        slots[i] = init;
    }
    return a;
}

int64_t lambda_arrayg_length(Value *a) {
    if (!a || a->tag != TAG_ARRAYG) fail_type("Array", a);
    return a->i;
}

/* Returns a retained reference to the element. */
Value *lambda_arrayg_get(Value *a, int64_t idx) {
    idx = array_index(a, TAG_ARRAYG, idx);
    Value *e = ((Value **)a->p)[idx];
    lambda_retain(e);
    return e;
}

Value *lambda_arrayg_clone(Value *a) {
    if (!a || a->tag != TAG_ARRAYG) fail_type("Array", a);
    Value *b = value_alloc(TAG_ARRAYG);
    b->i = a->i;
    size_t cap = 0;
    b->p = pool_take(checked_array_bytes(a->i, sizeof(Value *)), &cap);
    Value **src = (Value **)a->p;
    Value **dst = (Value **)b->p;
    for (int64_t i = 0; i < a->i; i++) {
        lambda_retain(src[i]);
        dst[i] = src[i];
    }
    return b;
}

/* Bounds-checked write.  Takes ownership of both `a` (the caller's
   reference) and `v`.  Copy-on-write when the buffer is shared: a copy is
   made, the caller's reference to the old array is released, and the new
   array is returned. */
Value *lambda_arrayg_set(Value *a, int64_t idx, Value *v) {
    idx = array_index(a, TAG_ARRAYG, idx);
    if (value_rc_load(a) > 1) {
        Value *b = lambda_arrayg_clone(a);
        lambda_release(a); /* caller's reference to the shared buffer */
        Value **slots = (Value **)b->p;
        lambda_release(slots[idx]);
        slots[idx] = v;
        return b;
    }
    Value **slots = (Value **)a->p;
    lambda_release(slots[idx]);
    slots[idx] = v;
    return a;
}

int64_t lambda_arrayi_sum(Value *a) {
    if (!a || a->tag != TAG_ARRAYI) fail_type("ArrayI", a);
    const int64_t *restrict p = (const int64_t *)a->p;
    int64_t n = a->i;
    uint64_t s = 0;
#pragma clang loop vectorize(enable) interleave(enable)
    for (int64_t i = 0; i < n; ++i) s += (uint64_t)p[i];
    return (int64_t)s;
}

double lambda_arrayf_sum(Value *a) {
    if (!a || a->tag != TAG_ARRAYF) fail_type("ArrayF", a);
    const double *restrict p = (const double *)a->p;
    int64_t n = a->i;
    double s = 0.0;
#pragma clang loop vectorize(enable) interleave(enable)
    for (int64_t i = 0; i < n; ++i) s += p[i];
    return s;
}

/* Convenience constructors from lists.  They consume the list; the list
   elements are copied while the list is still owned, then the whole spine is
   released once. */
static int64_t list_length(Value *list) {
    int64_t n = 0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) { n++; cur = (Value *)cur->p; }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", list);
    return n;
}

Value *lambda_arrayf_of(Value *list) {
    int64_t n = list_length(list);
    Value *a = lambda_arrayf_new(n, 0.0);
    double *d = (double *)a->p;
    Value *cur = list;
    for (int64_t i = 0; i < n; ++i) {
        Value *head = (Value *)(intptr_t)cur->i;
        if (!head || head->tag != TAG_FLOAT) fail_type("Float", head);
        double x; memcpy(&x, &head->i, sizeof(double));
        d[i] = x;
        cur = (Value *)cur->p;
    }
    lambda_release(list);
    return a;
}

Value *lambda_arrayi_of(Value *list) {
    int64_t n = list_length(list);
    Value *a = lambda_arrayi_new(n, 0);
    int64_t *d = (int64_t *)a->p;
    Value *cur = list;
    for (int64_t i = 0; i < n; ++i) {
        Value *head = (Value *)(intptr_t)cur->i;
        if (!head || head->tag != TAG_INT) fail_type("Int", head);
        d[i] = head->i;
        cur = (Value *)cur->p;
    }
    lambda_release(list);
    return a;
}

Value *lambda_arrayb_of(Value *list) {
    int64_t n = list_length(list);
    Value *a = lambda_arrayb_new(n, 0);
    uint8_t *d = (uint8_t *)a->p;
    Value *cur = list;
    for (int64_t i = 0; i < n; ++i) {
        Value *head = (Value *)(intptr_t)cur->i;
        if (!head || head->tag != TAG_INT) fail_type("Int", head);
        d[i] = (uint8_t)byte_value(head->i);
        cur = (Value *)cur->p;
    }
    lambda_release(list);
    return a;
}

Value *lambda_op_arrayf_of(Value *list) { return lambda_arrayf_of(list); }
Value *lambda_op_arrayi_of(Value *list) { return lambda_arrayi_of(list); }
Value *lambda_op_arrayb_of(Value *list) { return lambda_arrayb_of(list); }

/* Boxed entry points for the dynamic backend.  Arguments are consumed; the
   array returned by `set` is the same buffer, so its ownership passes on. */
Value *lambda_op_arrayf_new(Value *n, Value *init) {
    int64_t len = as_int(n);
    double x = as_float(init);
    lambda_release(n);
    lambda_release(init);
    return lambda_arrayf_new(len, x);
}

Value *lambda_op_arrayf_get(Value *a, Value *idx) {
    int64_t i = as_int(idx);
    double x = lambda_arrayf_get(a, i);
    lambda_release(idx);
    lambda_release(a);
    return lambda_box_float(x);
}

Value *lambda_op_arrayf_set(Value *a, Value *idx, Value *value) {
    int64_t i = as_int(idx);
    double x = as_float(value);
    lambda_arrayf_set(a, i, x);
    lambda_release(idx);
    lambda_release(value);
    return a;
}

Value *lambda_op_arrayf_length(Value *a) {
    int64_t n = lambda_arrayf_length(a);
    lambda_release(a);
    return lambda_box_int(n);
}

Value *lambda_op_arrayf_clone(Value *a) {
    Value *b = lambda_arrayf_clone(a);
    lambda_release(a);
    return b;
}

Value *lambda_op_arrayi_new(Value *n, Value *init) {
    int64_t len = as_int(n);
    int64_t x = as_int(init);
    lambda_release(n);
    lambda_release(init);
    return lambda_arrayi_new(len, x);
}

Value *lambda_op_arrayi_get(Value *a, Value *idx) {
    int64_t i = as_int(idx);
    int64_t x = lambda_arrayi_get(a, i);
    lambda_release(idx);
    lambda_release(a);
    return lambda_box_int(x);
}

Value *lambda_op_arrayi_set(Value *a, Value *idx, Value *value) {
    int64_t i = as_int(idx);
    int64_t x = as_int(value);
    lambda_arrayi_set(a, i, x);
    lambda_release(idx);
    lambda_release(value);
    return a;
}

Value *lambda_op_arrayi_length(Value *a) {
    int64_t n = lambda_arrayi_length(a);
    lambda_release(a);
    return lambda_box_int(n);
}

Value *lambda_op_arrayi_clone(Value *a) {
    Value *b = lambda_arrayi_clone(a);
    lambda_release(a);
    return b;
}

Value *lambda_op_arrayb_new(Value *n, Value *init) {
    int64_t len = as_int(n);
    int64_t x = as_int(init);
    lambda_release(n);
    lambda_release(init);
    return lambda_arrayb_new(len, x);
}

Value *lambda_op_arrayb_get(Value *a, Value *idx) {
    int64_t i = as_int(idx);
    int64_t x = lambda_arrayb_get(a, i);
    lambda_release(idx);
    lambda_release(a);
    return lambda_box_int(x);
}

Value *lambda_op_arrayb_set(Value *a, Value *idx, Value *value) {
    int64_t i = as_int(idx);
    int64_t x = as_int(value);
    lambda_arrayb_set(a, i, x);
    lambda_release(idx);
    lambda_release(value);
    return a;
}

Value *lambda_op_arrayb_length(Value *a) {
    int64_t n = lambda_arrayb_length(a);
    lambda_release(a);
    return lambda_box_int(n);
}

Value *lambda_op_arrayb_clone(Value *a) {
    Value *b = lambda_arrayb_clone(a);
    lambda_release(a);
    return b;
}

Value *lambda_op_add(Value *a, Value *b) {
    if (both(a, b, TAG_INT)) {
        int64_t x = a->i, y = b->i;
        lambda_release(a); lambda_release(b);
        return lambda_box_int((int64_t)((uint64_t)x + (uint64_t)y));
    }
    if (both(a, b, TAG_FLOAT)) {
        double x = as_float(a), y = as_float(b);
        lambda_release(a); lambda_release(b);
        return lambda_box_float(x + y);
    }
    fail("+ expects two Ints or two Floats");
    return NULL;
}

Value *lambda_op_sub(Value *a, Value *b) {
    if (both(a, b, TAG_INT)) {
        int64_t x = a->i, y = b->i;
        lambda_release(a); lambda_release(b);
        return lambda_box_int((int64_t)((uint64_t)x - (uint64_t)y));
    }
    if (both(a, b, TAG_FLOAT)) {
        double x = as_float(a), y = as_float(b);
        lambda_release(a); lambda_release(b);
        return lambda_box_float(x - y);
    }
    fail("- expects two Ints or two Floats");
    return NULL;
}

Value *lambda_op_mul(Value *a, Value *b) {
    if (both(a, b, TAG_INT)) {
        int64_t x = a->i, y = b->i;
        lambda_release(a); lambda_release(b);
        return lambda_box_int((int64_t)((uint64_t)x * (uint64_t)y));
    }
    if (both(a, b, TAG_FLOAT)) {
        double x = as_float(a), y = as_float(b);
        lambda_release(a); lambda_release(b);
        return lambda_box_float(x * y);
    }
    fail("* expects two Ints or two Floats");
    return NULL;
}

Value *lambda_op_div(Value *a, Value *b) {
    if (both(a, b, TAG_INT)) {
        int64_t x = a->i, y = b->i;
        lambda_release(a); lambda_release(b);
        return lambda_box_int(lambda_div_i64(x, y));
    }
    if (both(a, b, TAG_FLOAT)) {
        double x = as_float(a), y = as_float(b);
        lambda_release(a); lambda_release(b);
        return lambda_box_float(x / y);
    }
    fail("/ expects two Ints or two Floats");
    return NULL;
}

Value *lambda_op_mod(Value *a, Value *b) {
    if (both(a, b, TAG_INT)) {
        int64_t x = a->i, y = b->i;
        lambda_release(a); lambda_release(b);
        return lambda_box_int(lambda_mod_i64(x, y));
    }
    if (both(a, b, TAG_FLOAT)) {
        double x = as_float(a), y = as_float(b);
        lambda_release(a); lambda_release(b);
        return lambda_box_float(fmod(x, y));
    }
    fail("% expects two Ints or two Floats");
    return NULL;
}

Value *lambda_op_eq(Value *a, Value *b) {
    int r = value_equal(a, b);
    lambda_release(a); lambda_release(b);
    return lambda_box_bool(r);
}

#define CMP_OP(NAME, OP)                                                   \
    Value *lambda_op_##NAME(Value *a, Value *b) {                          \
        if (both(a, b, TAG_INT)) {                                         \
            int64_t x = a->i, y = b->i;                                    \
            lambda_release(a); lambda_release(b);                          \
            return lambda_box_bool(x OP y);                                \
        }                                                                  \
        if (both(a, b, TAG_FLOAT)) {                                       \
            double x = as_float(a), y = as_float(b);                       \
            lambda_release(a); lambda_release(b);                          \
            return lambda_box_bool(x OP y);                                \
        }                                                                  \
        fail("comparison expects two Ints or two Floats");                 \
        return NULL;                                                       \
    }

CMP_OP(lt, <)
CMP_OP(le, <=)
CMP_OP(gt, >)
CMP_OP(ge, >=)

typedef struct AdtArena {
    int64_t cap;
    int64_t count;
    int64_t width;
    uint64_t *words;
    int64_t edge_cap;
    int64_t edge_count;
    uint64_t *edges;
} AdtArena;

/* Data arenas grow on demand.  They used to reserve 2,097,152 rows up front
   (16 MiB per word of row width, so about 176 MiB for a 10-field type and
   1 GiB for the 64-field maximum) per arena before the program did anything;
   that fails under a hard address-space limit even though most programs use a
   few rows.  Start small and double. */
#define LAMBDA_ARENA_INITIAL_ROWS 1024
#define LAMBDA_ARENA_INITIAL_EDGES 1024

AdtArena *lambda_arena_new(int64_t width) {
    if (width < 0 || width > 64) fail("ADT arena width out of range");
    AdtArena *a = (AdtArena *)xmalloc(sizeof(AdtArena));
    a->cap = LAMBDA_ARENA_INITIAL_ROWS;
    a->count = 0;
    a->width = width;
    a->words = (uint64_t *)xmalloc((size_t)a->cap * (size_t)(width + 1) * sizeof(uint64_t));
    a->edge_cap = LAMBDA_ARENA_INITIAL_EDGES;
    a->edge_count = 0;
    a->edges = (uint64_t *)xmalloc((size_t)a->edge_cap * sizeof(uint64_t));
    return a;
}

static void arena_grow(AdtArena *a) {
    if (a->cap > INT64_MAX / 2) fail("ADT arena is too large");
    int64_t cap = a->cap * 2;
    size_t row_words = (size_t)(a->width + 1);
    if ((uint64_t)cap > (uint64_t)SIZE_MAX / (row_words * sizeof(uint64_t)))
        fail("out of memory growing ADT arena");
    a->words = (uint64_t *)realloc(a->words, (size_t)cap * row_words * sizeof(uint64_t));
    if (!a->words) fail("out of memory growing ADT arena");
    a->cap = cap;
}

static inline int64_t arena_reserve(AdtArena *a, int64_t tag) {
    if (!a) fail("null ADT arena");
    if (a->count >= a->cap) arena_grow(a);
    int64_t id = a->count++;
    uint64_t *row = a->words + (size_t)id * (size_t)(a->width + 1);
    row[0] = (uint64_t)tag;
    return id;
}

int64_t lambda_arena_alloc(AdtArena *a, int64_t tag) {
    return arena_reserve(a, tag);
}

void lambda_arena_set(AdtArena *a, int64_t id, int64_t slot, uint64_t value);

int64_t lambda_arena_alloc1(AdtArena *a, int64_t tag, uint64_t v0) {
    int64_t id = arena_reserve(a, tag);
    uint64_t *row = a->words + (size_t)id * (size_t)(a->width + 1);
    row[1] = v0;
    return id;
}
int64_t lambda_arena_alloc2(AdtArena *a, int64_t tag, uint64_t v0, uint64_t v1) {
    int64_t id = arena_reserve(a, tag);
    uint64_t *row = a->words + (size_t)id * (size_t)(a->width + 1);
    row[1] = v0; row[2] = v1;
    return id;
}
int64_t lambda_arena_alloc3(AdtArena *a, int64_t tag, uint64_t v0, uint64_t v1, uint64_t v2) {
    int64_t id = arena_reserve(a, tag);
    uint64_t *row = a->words + (size_t)id * (size_t)(a->width + 1);
    row[1] = v0; row[2] = v1; row[3] = v2;
    return id;
}
int64_t lambda_arena_alloc4(AdtArena *a, int64_t tag, uint64_t v0, uint64_t v1, uint64_t v2, uint64_t v3) {
    int64_t id = arena_reserve(a, tag);
    uint64_t *row = a->words + (size_t)id * (size_t)(a->width + 1);
    row[1] = v0; row[2] = v1; row[3] = v2; row[4] = v3;
    return id;
}


void lambda_arena_set(AdtArena *a, int64_t id, int64_t slot, uint64_t value) {
    if (!a || id < 0 || id >= a->count || slot < 0 || slot >= a->width) fail("ADT arena set out of bounds");
    a->words[(size_t)id * (size_t)(a->width + 1) + (size_t)slot + 1] = value;
}

uint64_t lambda_arena_get(AdtArena *a, int64_t id, int64_t slot) {
    if (!a || id < 0 || id >= a->count || slot < 0 || slot >= a->width) fail("ADT arena get out of bounds");
    return a->words[(size_t)id * (size_t)(a->width + 1) + (size_t)slot + 1];
}

uint64_t *lambda_arena_row(AdtArena *a, int64_t id) {
    if (!a || id < 0 || id >= a->count) fail("ADT arena row out of bounds");
    return a->words + (size_t)id * (size_t)(a->width + 1) + 1;
}

int64_t lambda_arena_tag(AdtArena *a, int64_t id) {
    if (!a || id < 0 || id >= a->count) fail("ADT arena tag out of bounds");
    return (int64_t)a->words[(size_t)id * (size_t)(a->width + 1)];
}

static void arena_edge_grow(AdtArena *a) {
    if (a->edge_cap > INT64_MAX / 2) fail("ADT edge arena is too large");
    int64_t cap = a->edge_cap * 2;
    if ((uint64_t)cap > (uint64_t)SIZE_MAX / sizeof(uint64_t))
        fail("out of memory growing ADT edge arena");
    a->edges = (uint64_t *)realloc(a->edges, (size_t)cap * sizeof(uint64_t));
    if (!a->edges) fail("out of memory growing ADT edge arena");
    a->edge_cap = cap;
}

int64_t lambda_arena_attach_list(AdtArena *a, Value *list) {
    if (!a) fail("null ADT arena");
    int64_t count = 0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        count++;
        cur = (Value *)cur->p;
    }
    if (cur && cur->tag != TAG_NIL) fail("ADT child list is not a proper list");
    if (a->edge_count + count + 1 > a->edge_cap) {
        while (a->edge_count + count + 1 > a->edge_cap) arena_edge_grow(a);
    }
    int64_t start = a->edge_count;
    a->edges[start] = (uint64_t)count;
    a->edge_count += count + 1;
    cur = list;
    for (int64_t i = 0; i < count; i++) {
        if (!cur || cur->tag != TAG_CONS) fail("ADT child list changed during attach");
        Value *head = (Value *)(intptr_t)cur->i;
        Value *tail = (Value *)cur->p;
        a->edges[start + 1 + i] = (uint64_t)lambda_get_int(head);
        cur = tail;
    }
    lambda_release(list);
    return start;
}

Value *lambda_arena_get_list(AdtArena *a, int64_t start) {
    if (!a || start < 0 || start >= a->edge_count) fail("ADT child list out of bounds");
    int64_t count = (int64_t)a->edges[start];
    Value *out = lambda_nil();
    for (int64_t i = count - 1; i >= 0; i--) {
        out = lambda_cons(lambda_box_int((int64_t)a->edges[start + 1 + i]), out);
    }
    return out;
}

void lambda_arena_free(AdtArena *a) {
    if (!a) return;
    free(a->words);
    free(a->edges);
    free(a);
}

int64_t lambda_adt_tag(Value *v) {
    if (!v || v->tag != TAG_CONS) fail_type("ADT", v);
    Value *tag = (Value *)(intptr_t)v->i;
    return lambda_get_int(tag);
}

Value *lambda_adt_field(Value *v, int64_t index) {
    if (index < 0) fail("ADT field index must be non-negative");
    Value *cur = v;
    lambda_retain(cur);
    for (int64_t i = 0; i <= index; i++) {
        if (!cur || cur->tag != TAG_CONS) {
            lambda_release(cur);
            fail("ADT field index out of bounds");
        }
        Value *tail = (Value *)cur->p;
        lambda_retain(tail);
        lambda_release(cur);
        cur = tail;
    }
    Value *field = (Value *)(intptr_t)cur->i;
    lambda_retain(field);
    lambda_release(cur);
    return field;
}

Value *lambda_op_cons(Value *a, Value *b) { return lambda_cons(a, b); }

Value *lambda_op_head(Value *lst) {
    if (!lst || lst->tag != TAG_CONS) {
        lambda_release(lst);
        fail("head of empty list");
    }
    Value *head = (Value *)(intptr_t)lst->i;
    lambda_retain(head);
    lambda_release(lst);
    return head;
}

Value *lambda_op_tail(Value *lst) {
    if (!lst || lst->tag != TAG_CONS) {
        lambda_release(lst);
        fail("tail of empty list");
    }
    Value *tail = (Value *)lst->p;
    lambda_retain(tail);
    lambda_release(lst);
    return tail;
}

Value *lambda_op_empty(Value *lst) {
    if (!lst) fail("empty? of null");
    int r = (lst->tag == TAG_NIL);
    lambda_release(lst);
    return lambda_box_bool(r);
}

Value *lambda_op_not(Value *v) {
    if (!v || v->tag != TAG_BOOL) fail_type("Bool", v);
    int64_t x = v->i;
    lambda_release(v);
    return lambda_box_bool(!x);
}

Value *lambda_op_int_to_str(Value *v) {
    int64_t x = as_int(v);
    lambda_release(v);
    char tmp[32];
    int n = snprintf(tmp, sizeof(tmp), "%" PRId64, x);
    return lambda_string_new_len(tmp, n);
}

/* ------------------------------------------------------------------ */
/* lossless / Unicode string inspection                                */
/* ------------------------------------------------------------------ */

/* Decode one UTF-8 scalar at byte offset.  Returns -1 for invalid or
   truncated input and advances next by one byte in that case. */
static int64_t utf8_decode_one(const unsigned char *s, int64_t len,
                               int64_t off, int64_t *next) {
    if (off < 0 || off >= len) { if (next) *next = off + 1; return -1; }
    unsigned char b0 = s[off];
    if (b0 < 0x80) { if (next) *next = off + 1; return b0; }
    if (b0 >= 0xC2 && b0 <= 0xDF) {
        if (off + 1 >= len || (s[off+1] & 0xC0) != 0x80) {
            if (next) *next = off + 1; return -1;
        }
        if (next) *next = off + 2;
        return ((int64_t)(b0 & 0x1F) << 6) | (s[off+1] & 0x3F);
    }
    if (b0 >= 0xE0 && b0 <= 0xEF) {
        if (off + 2 >= len) { if (next) *next = off + 1; return -1; }
        unsigned char b1 = s[off+1], b2 = s[off+2];
        if ((b1 & 0xC0) != 0x80 || (b2 & 0xC0) != 0x80) {
            if (next) *next = off + 1; return -1;
        }
        if ((b0 == 0xE0 && b1 < 0xA0) || (b0 == 0xED && b1 >= 0xA0)) {
            if (next) *next = off + 1; return -1;
        }
        if (next) *next = off + 3;
        return ((int64_t)(b0 & 0x0F) << 12) |
               ((int64_t)(b1 & 0x3F) << 6) | (b2 & 0x3F);
    }
    if (b0 >= 0xF0 && b0 <= 0xF4) {
        if (off + 3 >= len) { if (next) *next = off + 1; return -1; }
        unsigned char b1 = s[off+1], b2 = s[off+2], b3 = s[off+3];
        if ((b1 & 0xC0) != 0x80 || (b2 & 0xC0) != 0x80 ||
            (b3 & 0xC0) != 0x80) {
            if (next) *next = off + 1; return -1;
        }
        if ((b0 == 0xF0 && b1 < 0x90) || (b0 == 0xF4 && b1 >= 0x90)) {
            if (next) *next = off + 1; return -1;
        }
        if (next) *next = off + 4;
        return ((int64_t)(b0 & 0x07) << 18) |
               ((int64_t)(b1 & 0x3F) << 12) |
               ((int64_t)(b2 & 0x3F) << 6) | (b3 & 0x3F);
    }
    if (next) *next = off + 1;
    return -1;
}

int64_t lambda_str_byte_length(Value *s) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    return s->i;
}

int64_t lambda_str_byte_at(Value *s, int64_t i) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    if (i < 0 || i >= s->i) return -1;
    return ((const unsigned char *)s->p)[i];
}

int64_t lambda_str_utf8_at(Value *s, int64_t byte_off) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    int64_t next = 0;
    return utf8_decode_one((const unsigned char *)s->p, s->i, byte_off, &next);
}

int64_t lambda_str_codepoint_at(Value *s, int64_t index) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    if (index < 0) return -1;
    int64_t off = 0, count = 0;
    while (off < s->i) {
        int64_t next = 0;
        int64_t cp = utf8_decode_one((const unsigned char *)s->p, s->i, off, &next);
        if (count == index) return cp;
        count++;
        off = next > off ? next : off + 1;
    }
    return -1;
}

int64_t lambda_str_codepoint_length(Value *s) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    int64_t off = 0, count = 0;
    while (off < s->i) {
        int64_t next = 0;
        (void)utf8_decode_one((const unsigned char *)s->p, s->i, off, &next);
        count++;
        off = next > off ? next : off + 1;
    }
    return count;
}

int64_t lambda_str_valid_utf8(Value *s) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    int64_t off = 0;
    while (off < s->i) {
        int64_t next = 0;
        int64_t cp = utf8_decode_one((const unsigned char *)s->p, s->i, off, &next);
        if (cp < 0) return 0;
        off = next > off ? next : off + 1;
    }
    return 1;
}

Value *lambda_str_to_utf16le(Value *s) {
    if (!s || s->tag != TAG_STRING) fail_type("String", s);
    int64_t units = 0, off = 0;
    while (off < s->i) {
        int64_t next = 0;
        int64_t cp = utf8_decode_one((const unsigned char *)s->p, s->i, off, &next);
        if (cp < 0) cp = 0xFFFD;
        units += cp > 0xFFFF ? 2 : 1;
        off = next > off ? next : off + 1;
    }
    Value *out = lambda_arrayi_new(units, 0);
    int64_t *d = (int64_t *)out->p, k = 0;
    off = 0;
    while (off < s->i) {
        int64_t next = 0;
        int64_t cp = utf8_decode_one((const unsigned char *)s->p, s->i, off, &next);
        if (cp < 0) cp = 0xFFFD;
        if (cp > 0xFFFF) {
            cp -= 0x10000;
            d[k++] = 0xD800 + (cp >> 10);
            d[k++] = 0xDC00 + (cp & 0x3FF);
        } else {
            d[k++] = cp;
        }
        off = next > off ? next : off + 1;
    }
    return out;
}

Value *lambda_utf16le_to_str(Value *v) {
    if (!v || v->tag != TAG_ARRAYI) fail_type("ArrayI", v);
    int64_t *u = (int64_t *)v->p, n = v->i;
    char *buf = (char *)xmalloc((size_t)n * 4 + 1);
    int64_t out = 0;
    for (int64_t i = 0; i < n; i++) {
        int64_t unit = u[i] & 0xFFFF;
        int64_t cp;
        if (unit >= 0xD800 && unit <= 0xDBFF && i + 1 < n) {
            int64_t lo = u[i+1] & 0xFFFF;
            if (lo >= 0xDC00 && lo <= 0xDFFF) {
                cp = 0x10000 + ((unit - 0xD800) << 10) + (lo - 0xDC00);
                i++;
            } else cp = 0xFFFD;
        } else if (unit >= 0xD800 && unit <= 0xDFFF) cp = 0xFFFD;
        else cp = unit;
        if (cp <= 0x7F) buf[out++] = (char)cp;
        else if (cp <= 0x7FF) {
            buf[out++] = (char)(0xC0 | (cp >> 6));
            buf[out++] = (char)(0x80 | (cp & 0x3F));
        } else if (cp <= 0xFFFF) {
            buf[out++] = (char)(0xE0 | (cp >> 12));
            buf[out++] = (char)(0x80 | ((cp >> 6) & 0x3F));
            buf[out++] = (char)(0x80 | (cp & 0x3F));
        } else {
            buf[out++] = (char)(0xF0 | (cp >> 18));
            buf[out++] = (char)(0x80 | ((cp >> 12) & 0x3F));
            buf[out++] = (char)(0x80 | ((cp >> 6) & 0x3F));
            buf[out++] = (char)(0x80 | (cp & 0x3F));
        }
    }
    Value *r = lambda_string_new_len(buf, out);
    free(buf);
    lambda_release(v);
    return r;
}

/* UTF-16LE byte-buffer conversions, useful for non-UTF-8 files. */
Value *lambda_str_to_utf16le_bytes(Value *s) {
    Value *u = lambda_str_to_utf16le(s);
    Value *b = lambda_arrayb_new(u->i * 2, 0);
    unsigned char *d = (unsigned char *)b->p;
    int64_t *units = (int64_t *)u->p;
    for (int64_t i = 0; i < u->i; i++) {
        d[2*i] = (unsigned char)(units[i] & 0xFF);
        d[2*i+1] = (unsigned char)((units[i] >> 8) & 0xFF);
    }
    lambda_release(u);
    return b;
}

Value *lambda_utf16le_bytes_to_str(Value *b) {
    if (!b || b->tag != TAG_ARRAYB) fail_type("ArrayB", b);
    int64_t n = b->i / 2;
    Value *u = lambda_arrayi_new(n, 0);
    int64_t *units = (int64_t *)u->p;
    const unsigned char *d = (const unsigned char *)b->p;
    for (int64_t i = 0; i < n; i++) units[i] = (int64_t)d[2*i] | ((int64_t)d[2*i+1] << 8);
    lambda_release(b);
    return lambda_utf16le_to_str(u);
}

Value *lambda_op_str_to_utf16le_bytes(Value *s) {
    Value *b = lambda_str_to_utf16le_bytes(s);
    lambda_release(s);
    return b;
}

Value *lambda_op_utf16le_bytes_to_str(Value *b) {
    return lambda_utf16le_bytes_to_str(b);
}

/* Read a whole regular file into a malloc'd, NUL-terminated buffer.

   Sizing the buffer with fseek(SEEK_END)/ftell aborted with "out of memory"
   on directories, silently returned empty data for pipes and other unseekable
   files, and mis-sized files over 2 GiB because `long` is 32-bit on Windows.
   fstat plus a growing read handles all of those; non-regular files are
   rejected (NULL) and callers return their normal "empty on error" value. */
static char *read_regular_file(const char *path, size_t *out_len) {
    *out_len = 0;
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
#ifdef _WIN32
    struct _stat st;
    int regular = _fstat(_fileno(f), &st) == 0 && (st.st_mode & _S_IFREG) != 0;
#else
    struct stat st;
    int regular = fstat(fileno(f), &st) == 0 && S_ISREG(st.st_mode);
#endif
    if (!regular) {
        fclose(f);
        return NULL;
    }
    /* st_size is only a capacity hint (it is 0 for proc files and may be
       truncated on Windows); the growing loop below always reads to EOF. */
    size_t cap = 4096;
    {
        int64_t want = (int64_t)st.st_size;
        if (want > 0 && (uint64_t)want <= (uint64_t)(1u << 20)) cap = (size_t)want;
    }
    char *buf = (char *)xmalloc(cap + 1);
    size_t len = 0;
    for (;;) {
        if (len == cap) {
            if (cap > SIZE_MAX / 2) { free(buf); fclose(f); return NULL; }
            cap *= 2;
            char *nb = (char *)realloc(buf, cap + 1);
            if (!nb) { free(buf); fclose(f); return NULL; }
            buf = nb;
        }
        size_t n = fread(buf + len, 1, cap - len, f);
        len += n;
        if (n == 0) break;
    }
    int read_error = ferror(f);
    if (fclose(f) != 0 || read_error) {
        free(buf);
        return NULL;
    }
    buf[len] = 0;
    *out_len = len;
    return buf;
}

/* Raw file bytes without any Unicode interpretation. */
Value *lambda_read_file_bytes_raw(Value *pathv) {
    if (!pathv || pathv->tag != TAG_STRING) fail_type("String", pathv);
    const char *path = (const char *)pathv->p;
    /* Lambda strings are length-tagged but fopen takes a C string: an
       embedded NUL silently truncated the path to its prefix file. */
    if (memchr(path, 0, (size_t)pathv->i) != NULL) {
        lambda_release(pathv);
        return lambda_arrayb_new(0, 0);
    }
    size_t len = 0;
    char *buf = read_regular_file(path, &len);
    lambda_release(pathv);
    if (!buf) return lambda_arrayb_new(0, 0);
    Value *a = lambda_arrayb_new((int64_t)len, 0);
    if (len) memcpy(a->p, buf, len);
    free(buf);
    return a;
}

Value *lambda_op_str_byte_at(Value *s, Value *i) {
    int64_t x = lambda_str_byte_at(s, as_int(i));
    lambda_release(s); lambda_release(i);
    return lambda_box_int(x);
}
Value *lambda_op_str_utf8_at(Value *s, Value *i) {
    int64_t x = lambda_str_utf8_at(s, as_int(i));
    lambda_release(s); lambda_release(i);
    return lambda_box_int(x);
}
Value *lambda_op_str_codepoint_at(Value *s, Value *i) {
    int64_t x = lambda_str_codepoint_at(s, as_int(i));
    lambda_release(s); lambda_release(i);
    return lambda_box_int(x);
}
Value *lambda_op_str_byte_length(Value *s) {
    int64_t x = lambda_str_byte_length(s);
    lambda_release(s);
    return lambda_box_int(x);
}
Value *lambda_op_str_codepoint_length(Value *s) {
    int64_t x = lambda_str_codepoint_length(s);
    lambda_release(s);
    return lambda_box_int(x);
}
Value *lambda_op_str_valid_utf8(Value *s) {
    int64_t x = lambda_str_valid_utf8(s);
    lambda_release(s);
    return lambda_box_bool(x);
}
Value *lambda_op_str_to_utf16le(Value *s) {
    Value *r = lambda_str_to_utf16le(s);
    lambda_release(s);
    return r;
}
Value *lambda_op_utf16le_to_str(Value *v) {
    return lambda_utf16le_to_str(v);
}
Value *lambda_op_read_file_bytes(Value *path) {
    return lambda_read_file_bytes_raw(path);
}

Value *lambda_op_str_to_int(Value *v) {
    const char *s = string_data(v);
    errno = 0;
    char *end = NULL;
    long long x = strtoll(s, &end, 10);
    if (end == s) x = 0;
    lambda_release(v);
    return lambda_box_int((int64_t)x);
}

Value *lambda_op_str_to_list(Value *v) {
    if (!v || v->tag != TAG_STRING) fail_type("String", v);
    int64_t len = v->i;
    const unsigned char *s = (const unsigned char *)v->p;
    Value *list = lambda_nil();
    for (int64_t k = len - 1; k >= 0; k--) {
        list = lambda_cons(lambda_box_int((int64_t)s[k]), list);
    }
    lambda_release(v);
    return list;
}

Value *lambda_op_str_len(Value *v) {
    /* The boxed backend can be reached by any program that fails type
       inference, so primitives must not trust their argument's tag: reading
       v->i from a non-string leaked a heap pointer to the program. */
    int64_t len = lambda_str_byte_length(v);
    lambda_release(v);
    return lambda_box_int(len);
}

Value *lambda_op_str_append(Value *a, Value *b) {
    if (!a || a->tag != TAG_STRING) fail_type("String", a);
    if (!b || b->tag != TAG_STRING) fail_type("String", b);
    int64_t la = a->i, lb = b->i;
    char *buf = (char *)xmalloc((size_t)(la + lb) + 1);
    memcpy(buf, a->p, (size_t)la);
    memcpy(buf + la, b->p, (size_t)lb);
    buf[la + lb] = 0;
    Value *r = value_alloc(TAG_STRING);
    r->i = la + lb;
    r->p = buf;
    lambda_release(a); lambda_release(b);
    return r;
}

Value *lambda_op_read_file(Value *pathv) {
    if (!pathv || pathv->tag != TAG_STRING) fail_type("String", pathv);
    const char *path = (const char *)pathv->p;
    /* Reject embedded NULs rather than silently truncating the path. */
    if (memchr(path, 0, (size_t)pathv->i) != NULL) {
        lambda_release(pathv);
        return lambda_string_new_len("", 0);
    }
    size_t len = 0;
    char *buf = read_regular_file(path, &len);
    lambda_release(pathv);
    if (!buf) return lambda_string_new_len("", 0);
    /* String payloads are free()d by the destructor, so hand the buffer
       over directly (it is already NUL-terminated) instead of copying. */
    Value *r = value_alloc(TAG_STRING);
    r->i = (int64_t)len;
    r->p = buf;
    return r;
}

/* Copy a string's bytes into a flat ArrayB (no boxed per-byte list). */
Value *lambda_op_str_to_arrayb(Value *v) {
    if (!v || v->tag != TAG_STRING) fail_type("String", v);
    int64_t len = v->i;
    Value *arr = lambda_arrayb_new(len, 0);
    if (len > 0) memcpy(arr->p, v->p, (size_t)len);
    lambda_release(v);
    return arr;
}

/* Copy an ArrayB's bytes into a String (inverse of str->arrayb). */
Value *lambda_op_arrayb_to_str(Value *v) {
    if (!v || v->tag != TAG_ARRAYB) fail_type("ArrayB", v);
    Value *r = lambda_string_new_len((const char *)v->p, v->i);
    lambda_release(v);
    return r;
}

/* Command-line arguments captured by main().  argv(0) is the executable.
   (lambda_prim_argc is defined with the other primitive globals below.) */
int64_t lambda_argc = 0;
char **lambda_argv = NULL;

void lambda_refresh_argc_prim(int64_t argc);

void lambda_set_args(int64_t argc, char **argv) {
    lambda_argc = argc;
    lambda_argv = argv;
    lambda_refresh_argc_prim(argc);
}

int64_t lambda_op_argc(void) {
    return lambda_argc;
}

/* `argc` is a value, not a function: boxed references see the live cast. */
Value *lambda_op_argc_prim(void) {
    return lambda_box_int(lambda_argc);
}

/* Returns "" for an out-of-range index rather than failing. */
Value *lambda_op_argv(Value *v) {
    if (!v || v->tag != TAG_INT) fail_type("Int", v);
    int64_t idx = v->i;
    lambda_release(v);
    if (!lambda_argv || idx < 0 || idx >= lambda_argc) {
        return lambda_string_new_len("", 0);
    }
    return lambda_string_new_len(lambda_argv[idx], (int64_t)strlen(lambda_argv[idx]));
}

/* Write the bytes of an ArrayB to a file.  Returns bytes written, or -1. */
int64_t lambda_op_write_file(Value *pathv, Value *data) {
    if (!pathv || pathv->tag != TAG_STRING) fail_type("String", pathv);
    if (!data || data->tag != TAG_ARRAYB) fail_type("ArrayB", data);
    /* Reject embedded NULs: fopen would silently write the prefix path. */
    if (memchr(pathv->p, 0, (size_t)pathv->i) != NULL) {
        lambda_release(pathv);
        lambda_release(data);
        return -1;
    }
    FILE *f = fopen((const char *)pathv->p, "wb");
    if (!f) {
        lambda_release(pathv);
        lambda_release(data);
        return -1;
    }
    size_t n = fwrite(data->p, 1, (size_t)data->i, f);
    int64_t ok = (n == (size_t)data->i) ? (int64_t)n : -1;
    /* Buffered bytes are only flushed by fclose, so a close/flush failure
       (full disk, closed pipe) must fail the call too. */
    if (fclose(f) != 0) ok = -1;
    lambda_release(pathv);
    lambda_release(data);
    return ok;
}

/* ------------------------------------------------------------------ */
/* optional terminal input (compiled only with --input)                */
/* ------------------------------------------------------------------ */

/* Read one line of arbitrary length with arbitrary bytes (NUL included).

   fgets with a fixed 4096-byte buffer split longer lines into several reads
   (and a line of exactly 4095 bytes was followed by a phantom empty read),
   and both read-line and read-int went through strlen, so an embedded NUL
   silently cut the line.  A dynamic fgetc loop has neither problem.

   Returns NULL on end of input before any byte; otherwise a malloc'd buffer
   with its exact length (the trailing newline and CR are not included).  The
   buffer is NUL-terminated for convenience, but callers use the length. */
static char *read_prompt_line(Value *prompt, size_t *len_out) {
    *len_out = 0;
    if (prompt) {
        if (prompt->tag != TAG_STRING) fail_type("String", prompt);
        out_write((const char *)prompt->p, (size_t)prompt->i);
    }
    lambda_release(prompt);
    out_flush();
    size_t cap = 128, len = 0;
    char *buf = (char *)xmalloc(cap + 1);
    int c = EOF;
    while ((c = fgetc(stdin)) != EOF) {
        if (c == '\n') break;
        if (len == cap) {
            if (cap > SIZE_MAX / 2) {
                free(buf);
                fail("input line is too long");
            }
            cap *= 2;
            char *nb = (char *)realloc(buf, cap + 1);
            if (!nb) {
                free(buf);
                fail("out of memory reading input");
            }
            buf = nb;
        }
        buf[len++] = (char)c;
    }
    if (c == EOF && len == 0) {
        free(buf);
        return NULL;
    }
    while (len > 0 && buf[len - 1] == '\r') len--;
    buf[len] = 0;
    *len_out = len;
    return buf;
}

Value *lambda_op_read_line(Value *prompt) {
    size_t len = 0;
    char *line = read_prompt_line(prompt, &len);
    if (!line) return lambda_string_new_len("", 0);
    Value *r = lambda_string_new_len(line, (int64_t)len);
    free(line);
    return r;
}

Value *lambda_op_read_int(Value *prompt) {
    size_t len = 0;
    char *line = read_prompt_line(prompt, &len);
    if (!line) fail("read-int: end of input");
    char *end = NULL;
    errno = 0;
    long long x = strtoll(line, &end, 10);
    /* strtoll saturates to LLONG_MAX/LLONG_MIN and sets ERANGE; that used to
       be accepted silently, turning a too-large number into INT64_MAX. */
    if (errno == ERANGE) {
        free(line);
        fail("read-int: integer out of range");
    }
    while (end && *end && (*end == ' ' || *end == '\t')) end++;
    /* Comparing against the length (not *end) rejects embedded NULs and
       trailing junk instead of silently truncating the number. */
    if (end == line || (end && (size_t)(end - line) != len)) {
        free(line);
        fail("read-int: invalid integer");
    }
    free(line);
    return lambda_box_int((int64_t)x);
}

Value *lambda_op_read_float(Value *prompt) {
    size_t len = 0;
    char *line = read_prompt_line(prompt, &len);
    if (!line) fail("read-float: end of input");
    char *end = NULL;
    double x = strtod(line, &end);
    while (end && *end && (*end == ' ' || *end == '\t')) end++;
    if (end == line || (end && (size_t)(end - line) != len)) {
        free(line);
        fail("read-float: invalid number");
    }
    free(line);
    return lambda_box_float(x);
}

Value *lambda_op_print(Value *v) {
    print_value_inner(v, 0);
    return v; /* ownership passes through */
}

Value *lambda_op_println(Value *v) {
    print_value_inner(v, 0);
    out_ch('\n');
    return v;
}

/* ------------------------------------------------------------------ */
/* float conversions and mathematics                                   */
/* ------------------------------------------------------------------ */
Value *lambda_op_int_to_float(Value *v) {
    int64_t x = as_int(v);
    lambda_release(v);
    return lambda_box_float((double)x);
}

Value *lambda_op_float_to_int(Value *v) {
    double x = as_float(v);
    lambda_release(v);
    int64_t r;
    if (isnan(x)) r = 0;
    else if (x >= 9223372036854775807.0) r = INT64_MAX;
    else if (x <= -9223372036854775808.0) r = INT64_MIN;
    else r = (int64_t)x;
    return lambda_box_int(r);
}

Value *lambda_op_str_to_float(Value *v) {
    const char *s = string_data(v);
    char *end = NULL;
    double x = strtod(s, &end);
    if (end == s) x = 0.0;
    lambda_release(v);
    return lambda_box_float(x);
}

Value *lambda_op_float_to_str(Value *v) {
    double x = as_float(v);
    lambda_release(v);
    char tmp[64];
    int n = format_double(tmp, sizeof(tmp), x);
    return lambda_string_new_len(tmp, n);
}

Value *lambda_op_abs(Value *v) {
    if (!v) fail_type("number", v);
    if (v->tag == TAG_INT) {
        int64_t x = v->i;
        lambda_release(v);
        if (x == INT64_MIN) return lambda_box_int(INT64_MIN);
        return lambda_box_int(x < 0 ? -x : x);
    }
    if (v->tag == TAG_FLOAT) {
        double x = as_float(v);
        lambda_release(v);
        return lambda_box_float(fabs(x));
    }
    fail_type("number", v);
    return NULL;
}

static Value *float_unary(Value *v, double (*fn)(double)) {
    double x = as_float(v);
    lambda_release(v);
    return lambda_box_float(fn(x));
}

Value *lambda_op_sqrt(Value *v) { return float_unary(v, sqrt); }
Value *lambda_op_sin(Value *v) { return float_unary(v, sin); }
Value *lambda_op_cos(Value *v) { return float_unary(v, cos); }
Value *lambda_op_tan(Value *v) { return float_unary(v, tan); }
Value *lambda_op_asin(Value *v) { return float_unary(v, asin); }
Value *lambda_op_acos(Value *v) { return float_unary(v, acos); }
Value *lambda_op_atan(Value *v) { return float_unary(v, atan); }
Value *lambda_op_exp(Value *v) { return float_unary(v, exp); }
Value *lambda_op_ln(Value *v) { return float_unary(v, log); }
Value *lambda_op_log10(Value *v) { return float_unary(v, log10); }
Value *lambda_op_log2(Value *v) { return float_unary(v, log2); }
Value *lambda_op_floor(Value *v) { return float_unary(v, floor); }
Value *lambda_op_ceil(Value *v) { return float_unary(v, ceil); }
Value *lambda_op_round(Value *v) { return float_unary(v, round); }
Value *lambda_op_trunc(Value *v) { return float_unary(v, trunc); }

Value *lambda_op_pow(Value *a, Value *b) {
    double x = as_float(a), y = as_float(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_float(pow(x, y));
}

Value *lambda_op_atan2(Value *a, Value *b) {
    double x = as_float(a), y = as_float(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_float(atan2(x, y));
}

/* ------------------------------------------------------------------ */
/* bit-level integer operations                                        */
/* ------------------------------------------------------------------ */
Value *lambda_op_bit_and(Value *a, Value *b) {
    int64_t x = as_int(a), y = as_int(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_int(x & y);
}

Value *lambda_op_bit_or(Value *a, Value *b) {
    int64_t x = as_int(a), y = as_int(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_int(x | y);
}

Value *lambda_op_bit_xor(Value *a, Value *b) {
    int64_t x = as_int(a), y = as_int(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_int(x ^ y);
}

Value *lambda_op_bit_not(Value *v) {
    int64_t x = as_int(v);
    lambda_release(v);
    return lambda_box_int(~x);
}

Value *lambda_op_shl(Value *a, Value *b) {
    int64_t x = as_int(a), y = as_int(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_int((int64_t)((uint64_t)x << (y & 63)));
}

Value *lambda_op_shr(Value *a, Value *b) {
    int64_t x = as_int(a), y = as_int(b);
    lambda_release(a); lambda_release(b);
    return lambda_box_int((int64_t)((uint64_t)x >> (y & 63)));
}

Value *lambda_op_popcount(Value *v) {
    int64_t x = as_int(v);
    lambda_release(v);
    return lambda_box_int((int64_t)__builtin_popcountll((unsigned long long)x));
}

/* ------------------------------------------------------------------ */
/* list reductions, ranges, map/pmap                                   */
/* ------------------------------------------------------------------ */
int64_t lambda_sum_i64(Value *list) {
    int64_t acc = 0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        Value *h = (Value *)(intptr_t)cur->i;
        acc = (int64_t)((uint64_t)acc + (uint64_t)as_int(h));
        cur = (Value *)cur->p;
    }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", cur);
    lambda_release(list);
    return acc;
}

double lambda_sum_f64(Value *list) {
    double acc = 0.0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        Value *h = (Value *)(intptr_t)cur->i;
        acc += as_float(h);
        cur = (Value *)cur->p;
    }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", cur);
    lambda_release(list);
    return acc;
}

int64_t lambda_product_i64(Value *list) {
    int64_t acc = 1;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        Value *h = (Value *)(intptr_t)cur->i;
        acc = (int64_t)((uint64_t)acc * (uint64_t)as_int(h));
        cur = (Value *)cur->p;
    }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", cur);
    lambda_release(list);
    return acc;
}

double lambda_product_f64(Value *list) {
    double acc = 1.0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        Value *h = (Value *)(intptr_t)cur->i;
        acc *= as_float(h);
        cur = (Value *)cur->p;
    }
    if (!cur || cur->tag != TAG_NIL) fail_type("List", cur);
    lambda_release(list);
    return acc;
}

/* Ranges, map/pmap and reductions.  A reduction must verify that the spine is
   a proper list: the boxed backend is reachable through the type-error
   fallback, so `sum` can be handed a non-list, and reading it as a list used
   to dereference v->i as a pointer and crash instead of reporting a type
   error. */
Value *lambda_op_sum(Value *list) {
    if (!list) fail("sum of null");
    if (list->tag == TAG_NIL) {
        lambda_release(list);
        return lambda_box_int(0);
    }
    if (list->tag != TAG_CONS) {
        lambda_release(list);
        fail_type("List", list);
        return NULL;
    }
    Value *h = (Value *)(intptr_t)list->i;
    if (h && h->tag == TAG_FLOAT) return lambda_box_float(lambda_sum_f64(list));
    return lambda_box_int(lambda_sum_i64(list));
}

Value *lambda_op_product(Value *list) {
    if (!list) fail("product of null");
    if (list->tag == TAG_NIL) {
        lambda_release(list);
        return lambda_box_int(1);
    }
    if (list->tag != TAG_CONS) {
        lambda_release(list);
        fail_type("List", list);
        return NULL;
    }
    Value *h = (Value *)(intptr_t)list->i;
    if (h && h->tag == TAG_FLOAT) return lambda_box_float(lambda_product_f64(list));
    return lambda_box_int(lambda_product_i64(list));
}

Value *lambda_range(int64_t lo, int64_t hi) {
    if (hi <= lo) return lambda_nil();
    Value *head = NULL;
    Value *last = NULL;
    for (int64_t i = lo; i < hi; i++) {
        Value *cell = lambda_cons(lambda_box_int(i), lambda_nil());
        if (head == NULL) {
            head = cell;
        } else {
            last->p = cell; /* local list under construction */
        }
        last = cell;
    }
    return head;
}

Value *lambda_op_range(Value *lo, Value *hi) {
    int64_t a = as_int(lo), b = as_int(hi);
    lambda_release(lo); lambda_release(hi);
    return lambda_range(a, b);
}

/* Mark a read-only value graph immortal so worker threads can share it
   without atomic reference-count traffic.  It intentionally leaks the graph
   until process exit; use only for shared immutable constants. */
static void immortalize_graph(Value *v) {
    while (v) {
        if (v->rc > 0) {
            STAT_ADD(live_values, -1);
            STAT_ADD(immortal_values, 1);
        }
        v->rc = -1;
        if (v->tag != TAG_CONS) return;
        immortalize_graph((Value *)(intptr_t)v->i);
        v = (Value *)v->p;
    }
}

Value *lambda_op_immortalize(Value *v) {
    immortalize_graph(v);
    return v;
}

Value *lambda_op_map(Value *f, Value *list) {
    Value *head = NULL;
    Value *last = NULL;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        Value *elem = (Value *)(intptr_t)cur->i;
        Value *r = apply_borrowed(f, elem);
        Value *cell = lambda_cons(r, lambda_nil());
        if (head == NULL) {
            head = cell;
        } else {
            last->p = cell; /* local list under construction */
        }
        last = cell;
        cur = (Value *)cur->p;
    }
    lambda_release(list);
    lambda_release(f);
    return head ? head : lambda_nil();
}

#define LAMBDA_MAX_THREADS 64

typedef struct PMapJob {
    Value *f;
    Value **items;
    Value **results;
    int64_t n;
    int64_t next;
} PMapJob;

static int64_t pmap_default_threads(void) {
#ifdef _WIN32
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    return si.dwNumberOfProcessors > 0 ? (int64_t)si.dwNumberOfProcessors : 1;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int64_t)n : 1;
#endif
}

static int64_t pmap_requested_threads(void) {
    const char *s = getenv("LAMBDA_THREADS");
    if (s && *s) {
        char *end = NULL;
        long long n = strtoll(s, &end, 10);
        if (end != s && n > 0) return (int64_t)n;
    }
    return pmap_default_threads();
}

static void pmap_run_range(PMapJob *job) {
    for (;;) {
        int64_t i = __atomic_fetch_add(&job->next, 1, __ATOMIC_RELAXED);
        if (i >= job->n) break;
        job->results[i] = apply_owned(job->f, job->items[i]);
    }
}

#ifdef _WIN32
static DWORD WINAPI pmap_thread_main(LPVOID p) {
    pmap_run_range((PMapJob *)p);
    return 0;
}
#else
static void *pmap_thread_main(void *p) {
    pmap_run_range((PMapJob *)p);
    return NULL;
}
#endif

Value *lambda_op_pmap(Value *f, Value *list) {
    int64_t n = 0;
    Value *cur = list;
    while (cur && cur->tag == TAG_CONS) {
        n++;
        cur = (Value *)cur->p;
    }
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (n <= 1 || threads <= 1) {
        return lambda_op_map(f, list);
    }
    if (threads > n) threads = n;

    Value **items = (Value **)xmalloc(sizeof(Value *) * (size_t)n);
    Value **results = (Value **)xmalloc(sizeof(Value *) * (size_t)n);
    cur = list;
    for (int64_t i = 0; i < n; i++) {
        Value *elem = (Value *)(intptr_t)cur->i;
        lambda_retain(elem); /* ownership for the worker that consumes it */
        items[i] = elem;
        results[i] = NULL;
        cur = (Value *)cur->p;
    }

    PMapJob job;
    job.f = f;
    job.items = items;
    job.results = results;
    job.n = n;
    job.next = 0;

#ifdef _WIN32
    HANDLE handles[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        HANDLE h = CreateThread(NULL, 0, pmap_thread_main, &job, 0, NULL);
        if (h) handles[created++] = h;
    }
    pmap_run_range(&job);
    if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
    for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
    pthread_t th[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        if (pthread_create(&th[created], NULL, pmap_thread_main, &job) == 0) {
            created++;
        }
    }
    pmap_run_range(&job);
    for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif

    Value *head = NULL;
    Value *last = NULL;
    for (int64_t i = 0; i < n; i++) {
        Value *cell = lambda_cons(results[i], lambda_nil());
        if (head == NULL) head = cell;
        else last->p = cell; /* local list under construction */
        last = cell;
    }
    lambda_release(list);
    lambda_release(f);
    free(items);
    free(results);
    return head ? head : lambda_nil();
}

/* Parallel Int -> Int map into a flat Int array.  The result buffer is
   disjoint per index, so workers never communicate. */
typedef struct ArrayPMapJob {
    Value *f;
    Value *out;
    int64_t n;
    int64_t next;
} ArrayPMapJob;

static void array_pmap_run(ArrayPMapJob *job) {
    int64_t *data = (int64_t *)job->out->p;
    for (;;) {
        int64_t i = __atomic_fetch_add(&job->next, 1, __ATOMIC_RELAXED);
        if (i >= job->n) break;
        Value *arg = int_immortal(i);
        Value *r = apply_owned(job->f, arg);
        int64_t value = lambda_get_int(r);
        lambda_release(r);
        data[i] = value;
    }
}

#ifdef _WIN32
static DWORD WINAPI array_pmap_thread_main(LPVOID p) {
    array_pmap_run((ArrayPMapJob *)p);
    return 0;
}
#else
static void *array_pmap_thread_main(void *p) {
    array_pmap_run((ArrayPMapJob *)p);
    return NULL;
}
#endif

Value *lambda_arrayi_pmap(Value *f, int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    int_cache_ensure(n);
    Value *out = lambda_arrayi_new(n, 0);
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (threads > n) threads = n;
    if (threads <= 1 || n <= 1) {
        int64_t *data = (int64_t *)out->p;
        for (int64_t i = 0; i < n; i++) {
            Value *r = apply_owned(f, int_immortal(i));
            data[i] = lambda_get_int(r);
            lambda_release(r);
        }
        lambda_release(f);
        return out;
    }

    ArrayPMapJob job;
    job.f = f;
    job.out = out;
    job.n = n;
    job.next = 0;

#ifdef _WIN32
    HANDLE handles[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        HANDLE h = CreateThread(NULL, 0, array_pmap_thread_main, &job, 0, NULL);
        if (h) handles[created++] = h;
    }
    array_pmap_run(&job);
    if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
    for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
    pthread_t th[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        if (pthread_create(&th[created], NULL, array_pmap_thread_main, &job) == 0) {
            created++;
        }
    }
    array_pmap_run(&job);
    for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
    lambda_release(f);
    return out;
}

Value *lambda_arrayf_require_len(Value *a, int64_t n) {
    if (!a || a->tag != TAG_ARRAYF) fail_type("ArrayF", a);
    if (a->i < n) fail("arrayf-pmap-into destination is too short");
    return a;
}

/* Map into an existing ArrayF.  The caller owns the destination; pmap writes
   every element, so no output allocation is needed. */
Value *lambda_arrayf_pmap_into(Value *f, Value *dst, int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    if (!dst || dst->tag != TAG_ARRAYF) fail_type("ArrayF", dst);
    if (dst->i < n) fail("arrayf-pmap-into destination is too short");
    int_cache_ensure(n);
    double *data = (double *)dst->p;
    for (int64_t i = 0; i < n; i++) {
        Value *r = apply_owned(f, int_immortal(i));
        data[i] = lambda_get_float(r);
        lambda_release(r);
    }
    lambda_release(f);
    return dst;
}

/* Parallel Int -> Float map into a flat Float array. */
typedef struct ArrayFPMapJob {
    Value *f;
    Value *out;
    int64_t n;
    int64_t next;
} ArrayFPMapJob;

static void arrayf_pmap_run(ArrayFPMapJob *job) {
    double *data = (double *)job->out->p;
    for (;;) {
        int64_t i = __atomic_fetch_add(&job->next, 1, __ATOMIC_RELAXED);
        if (i >= job->n) break;
        Value *r = apply_owned(job->f, int_immortal(i));
        data[i] = lambda_get_float(r);
        lambda_release(r);
    }
}

#ifdef _WIN32
static DWORD WINAPI arrayf_pmap_thread_main(LPVOID p) {
    arrayf_pmap_run((ArrayFPMapJob *)p);
    return 0;
}
#else
static void *arrayf_pmap_thread_main(void *p) {
    arrayf_pmap_run((ArrayFPMapJob *)p);
    return NULL;
}
#endif

/* Raw parallel pmap: the compiler supplies an environment and a callback
   taking (env, i64) and returning double.  No Value boxing or closure call is
   needed per element.  The environment's dtor releases captured values after
   all workers finish. */
typedef struct RawFPMapJob {
    void *env;
    double (*fn)(void *, int64_t);
    Value *out;
    int64_t n;
    int64_t next;
} RawFPMapJob;

static void rawf_pmap_run(RawFPMapJob *job) {
    double *data = (double *)job->out->p;
    for (;;) {
        int64_t i = __atomic_fetch_add(&job->next, 1, __ATOMIC_RELAXED);
        if (i >= job->n) break;
        data[i] = job->fn(job->env, i);
    }
}

#ifdef _WIN32
static DWORD WINAPI rawf_pmap_thread_main(LPVOID p) {
    rawf_pmap_run((RawFPMapJob *)p);
    return 0;
}
#else
static void *rawf_pmap_thread_main(void *p) {
    rawf_pmap_run((RawFPMapJob *)p);
    return NULL;
}
#endif

/* Raw parallel range loop: each worker runs the compiled body over a contiguous
   [lo, hi) range.  This is the fast parallel path; there is no per-element
   callback or boxing. */
typedef void (*RawLoopFn)(void *, double *, int64_t, int64_t);

typedef struct RawLoopArg {
    RawLoopFn fn;
    void *env;
    double *data;
} RawLoopArg;

static void rawloop_run(RawLoopArg *a, int64_t lo, int64_t hi) {
    if (hi > lo) a->fn(a->env, a->data, lo, hi);
}

#ifdef _WIN32
typedef struct RawLoopWin { RawLoopArg *a; int64_t lo, hi; } RawLoopWin;
static DWORD WINAPI rawloop_thread_main(LPVOID p) {
    RawLoopWin *w = (RawLoopWin *)p;
    rawloop_run(w->a, w->lo, w->hi);
    return 0;
}
#else
typedef struct RawLoopPosix { RawLoopArg *a; int64_t lo, hi; } RawLoopPosix;
static void *rawloop_thread_main(void *p) {
    RawLoopPosix *w = (RawLoopPosix *)p;
    rawloop_run(w->a, w->lo, w->hi);
    return NULL;
}
#endif

Value *lambda_arrayf_pmap_raw_loop(void *env, RawLoopFn fn, int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    Value *out = lambda_arrayf_new(n, 0.0);
    RawLoopArg arg;
    arg.fn = fn;
    arg.env = env;
    arg.data = (double *)out->p;
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (threads > n) threads = n;
    if (threads < 1) threads = 1;
    if (threads > 1 && n > 1) {
#ifdef _WIN32
        HANDLE handles[LAMBDA_MAX_THREADS];
        RawLoopWin args[LAMBDA_MAX_THREADS];
        int created = 0;
        for (int64_t t = 1; t < threads; t++) {
            args[t].a = &arg;
            args[t].lo = t * n / threads;
            args[t].hi = (t + 1) * n / threads;
            HANDLE h = CreateThread(NULL, 0, rawloop_thread_main, &args[t], 0, NULL);
            if (h) {
                handles[created++] = h;
            } else {
                /* A failed thread creation must not leave its range
                   uncomputed: run that chunk on this thread instead. */
                rawloop_run(&arg, args[t].lo, args[t].hi);
            }
        }
        rawloop_run(&arg, 0, n / threads);
        if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
        for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
        pthread_t th[LAMBDA_MAX_THREADS];
        RawLoopPosix args[LAMBDA_MAX_THREADS];
        int created = 0;
        for (int64_t t = 1; t < threads; t++) {
            args[t].a = &arg;
            args[t].lo = t * n / threads;
            args[t].hi = (t + 1) * n / threads;
            if (pthread_create(&th[created], NULL, rawloop_thread_main, &args[t]) == 0) {
                created++;
            } else {
                /* A failed thread creation must not leave its range
                   uncomputed: run that chunk on this thread instead. */
                rawloop_run(&arg, args[t].lo, args[t].hi);
            }
        }
        rawloop_run(&arg, 0, n / threads);
        for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
    } else {
        rawloop_run(&arg, 0, n);
    }
    EnvHeader *h = (EnvHeader *)env;
    if (h) {
        /* The generated environment destructor already calls
           lambda_free_env, so freeing it again here was a double free in the
           LAMBDA_RAWLOOP path.  Keep a direct free only for a destructor-less
           environment. */
        if (h->dtor) h->dtor(env);
        else lambda_free_env(env);
    }
    return out;
}

Value *lambda_arrayf_pmap_raw(void *env, double (*fn)(void *, int64_t), int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    Value *out = lambda_arrayf_new(n, 0.0);
    if (n > 0) {
        int64_t threads = pmap_requested_threads();
        if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
        if (threads > n) threads = n;
        RawFPMapJob job;
        job.env = env; job.fn = fn; job.out = out; job.n = n; job.next = 0;
        if (threads <= 1) {
            rawf_pmap_run(&job);
        } else {
#ifdef _WIN32
            HANDLE handles[LAMBDA_MAX_THREADS];
            int created = 0;
            for (int64_t i = 1; i < threads; i++) {
                HANDLE h = CreateThread(NULL, 0, rawf_pmap_thread_main, &job, 0, NULL);
                if (h) handles[created++] = h;
            }
            rawf_pmap_run(&job);
            if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
            for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
            pthread_t th[LAMBDA_MAX_THREADS];
            int created = 0;
            for (int64_t i = 1; i < threads; i++) {
                if (pthread_create(&th[created], NULL, rawf_pmap_thread_main, &job) == 0)
                    created++;
            }
            rawf_pmap_run(&job);
            for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
        }
    }
    EnvHeader *h = (EnvHeader *)env;
    if (h) {
        /* The generated environment destructor already calls
           lambda_free_env; freeing it again here was a double free. */
        if (h->dtor) h->dtor(env);
        else lambda_free_env(env);
    }
    return out;
}

/* Serial fallback for arrayf-pmap when the function value cannot be proved
   pure.  This keeps higher-order array code expressive without running an
   unknown (possibly effectful) closure on worker threads. */
Value *lambda_arrayf_map(Value *f, int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    int_cache_ensure(n);
    Value *out = lambda_arrayf_new(n, 0.0);
    double *data = (double *)out->p;
    for (int64_t i = 0; i < n; i++) {
        Value *r = apply_owned(f, int_immortal(i));
        data[i] = lambda_get_float(r);
        lambda_release(r);
    }
    lambda_release(f);
    return out;
}

Value *lambda_arrayf_pmap(Value *f, int64_t n) {
    if (n < 0) fail("array size must be non-negative");
    int_cache_ensure(n);
    Value *out = lambda_arrayf_new(n, 0.0);
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (threads > n) threads = n;
    if (threads <= 1 || n <= 1) {
        double *data = (double *)out->p;
        for (int64_t i = 0; i < n; i++) {
            Value *r = apply_owned(f, int_immortal(i));
            data[i] = lambda_get_float(r);
            lambda_release(r);
        }
        lambda_release(f);
        return out;
    }
    ArrayFPMapJob job;
    job.f = f;
    job.out = out;
    job.n = n;
    job.next = 0;
#ifdef _WIN32
    HANDLE handles[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        HANDLE h = CreateThread(NULL, 0, arrayf_pmap_thread_main, &job, 0, NULL);
        if (h) handles[created++] = h;
    }
    arrayf_pmap_run(&job);
    if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
    for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
    pthread_t th[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        if (pthread_create(&th[created], NULL, arrayf_pmap_thread_main, &job) == 0) created++;
    }
    arrayf_pmap_run(&job);
    for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
    lambda_release(f);
    return out;
}

/* ------------------------------------------------------------------ */
/* optional CUDA backend for arrayf-pmap (NVRTC + CUDA Driver API)      */
/*                                                                     */
/* The compiler embeds one kernel source per translatable lambda.  The */
/* runtime compiles it with NVRTC to compute_80 PTX, JITs it through   */
/* the driver, and writes directly into the ArrayF.  Any failure (no   */
/* GPU, no NVRTC, unsupported arch) falls back to the CPU thread pool. */
/* Only one GPU is used; multi-GPU would need a device pool.           */
/* ------------------------------------------------------------------ */

#ifdef _WIN32
#include <windows.h>
#define GPU_LIB_HANDLE HMODULE
#else
#include <dlfcn.h>
#define GPU_LIB_HANDLE void *
#endif

typedef int gpu_result;
typedef void *gpu_program;
typedef int gpu_device;
typedef void *gpu_context;
typedef void *gpu_module;
typedef void *gpu_function;
typedef unsigned long long gpu_ptr;

typedef struct GpuKernel {
    char *source;
    char entry[32];
    gpu_module module;
    gpu_function fn;
    struct GpuKernel *next;
} GpuKernel;

static struct {
    int tried;
    int ok;
    gpu_context ctx;
    GpuKernel *kernels;
    gpu_result (*cuInit)(unsigned int);
    gpu_result (*cuDeviceGet)(gpu_device *, int);
    gpu_result (*cuCtxCreate)(gpu_context *, unsigned int, gpu_device);
    gpu_result (*cuCtxSetCurrent)(gpu_context);
    gpu_result (*cuModuleLoadData)(gpu_module *, const void *);
    gpu_result (*cuModuleGetFunction)(gpu_function *, gpu_module, const char *);
    gpu_result (*cuMemAlloc)(gpu_ptr *, size_t);
    gpu_result (*cuMemFree)(gpu_ptr);
    gpu_result (*cuMemcpyDtoH)(void *, gpu_ptr, size_t);
    gpu_result (*cuMemcpyHtoD)(gpu_ptr, const void *, size_t);
    gpu_result (*cuLaunchKernel)(gpu_function, unsigned, unsigned, unsigned,
                                 unsigned, unsigned, unsigned, unsigned,
                                 void *, void **, void **);
    gpu_result (*cuCtxSynchronize)(void);
} gpu;

typedef gpu_result (*nvrtc_create)(gpu_program *, const char *, const char *,
                                   int, const char **, const char **);
typedef gpu_result (*nvrtc_compile)(gpu_program, int, const char **);
typedef gpu_result (*nvrtc_ptx_size)(gpu_program, size_t *);
typedef gpu_result (*nvrtc_ptx)(gpu_program, char *);
typedef gpu_result (*nvrtc_log_size)(gpu_program, size_t *);
typedef gpu_result (*nvrtc_log)(gpu_program, char *);
typedef gpu_result (*nvrtc_destroy)(gpu_program);

static void *gpu_symbol(GPU_LIB_HANDLE lib, const char *name) {
#ifdef _WIN32
    return (void *)GetProcAddress(lib, name);
#else
    return dlsym(lib, name);
#endif
}

static GPU_LIB_HANDLE gpu_open(const char *name) {
#ifdef _WIN32
    return LoadLibraryA(name);
#else
    return dlopen(name, RTLD_NOW | RTLD_LOCAL);
#endif
}

static int gpu_init(void) {
    if (gpu.tried) return gpu.ok;
    gpu.tried = 1;
    const char *src = getenv("LAMBDA_GPU");
    if (!src || strcmp(src, "1") != 0) return 0;
    GPU_LIB_HANDLE nv = gpu_open("nvrtc64_112_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_120_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_130_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_140_0.dll");
    if (!nv) nv = gpu_open("libnvrtc.so");
    if (!nv) return 0;
    GPU_LIB_HANDLE cu = gpu_open("nvcuda.dll");
    if (!cu) cu = gpu_open("libcuda.so.1");
    if (!cu) return 0;
#define GPU_LOAD(field, lib, name) \
    gpu.field = (void *)gpu_symbol(lib, name); if (!gpu.field) return 0
    GPU_LOAD(cuInit, cu, "cuInit");
    GPU_LOAD(cuDeviceGet, cu, "cuDeviceGet");
    GPU_LOAD(cuCtxCreate, cu, "cuCtxCreate_v2");
    if (!gpu.cuCtxCreate) gpu.cuCtxCreate = (void *)gpu_symbol(cu, "cuCtxCreate");
    GPU_LOAD(cuCtxSetCurrent, cu, "cuCtxSetCurrent");
    GPU_LOAD(cuModuleLoadData, cu, "cuModuleLoadData");
    GPU_LOAD(cuModuleGetFunction, cu, "cuModuleGetFunction");
    GPU_LOAD(cuMemAlloc, cu, "cuMemAlloc_v2");
    GPU_LOAD(cuMemFree, cu, "cuMemFree_v2");
    GPU_LOAD(cuMemcpyDtoH, cu, "cuMemcpyDtoH_v2");
    GPU_LOAD(cuMemcpyHtoD, cu, "cuMemcpyHtoD_v2");
    GPU_LOAD(cuLaunchKernel, cu, "cuLaunchKernel");
    GPU_LOAD(cuCtxSynchronize, cu, "cuCtxSynchronize");
#undef GPU_LOAD
    if (!gpu.cuCtxCreate || !gpu.cuCtxSetCurrent) return 0;
    gpu_device dev;
    if (gpu.cuInit(0) != 0 || gpu.cuDeviceGet(&dev, 0) != 0) return 0;
    if (gpu.cuCtxCreate(&gpu.ctx, 0, dev) != 0) return 0;
    if (gpu.cuCtxSetCurrent(gpu.ctx) != 0) return 0;
    gpu.ok = 1;
    return 1;
}

static GpuKernel *gpu_kernel_named(const char *source, const char *entry) {
    for (GpuKernel *k = gpu.kernels; k; k = k->next) {
        if (strcmp(k->source, source) == 0 && strcmp(k->entry, entry) == 0) return k;
    }
    GPU_LIB_HANDLE nv = gpu_open("nvrtc64_112_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_120_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_130_0.dll");
    if (!nv) nv = gpu_open("nvrtc64_140_0.dll");
    if (!nv) nv = gpu_open("libnvrtc.so");
    if (!nv) return NULL;
    nvrtc_create create = (nvrtc_create)gpu_symbol(nv, "nvrtcCreateProgram");
    nvrtc_compile compile = (nvrtc_compile)gpu_symbol(nv, "nvrtcCompileProgram");
    nvrtc_ptx_size ptx_size = (nvrtc_ptx_size)gpu_symbol(nv, "nvrtcGetPTXSize");
    nvrtc_ptx ptx_get = (nvrtc_ptx)gpu_symbol(nv, "nvrtcGetPTX");
    if (!create || !compile || !ptx_size || !ptx_get) return NULL;
    gpu_program prog = NULL;
    if (create(&prog, source, "lambda_pmap.cu", 0, NULL, NULL) != 0) return NULL;
    const char *opts[] = {"--gpu-architecture=compute_80", "--std=c++14"};
    gpu_result rc = compile(prog, 2, opts);
    if (rc != 0) return NULL;
    size_t n = 0;
    if (ptx_size(prog, &n) != 0 || n < 2) return NULL;
    char *ptx = (char *)malloc(n);
    if (!ptx) return NULL;
    if (ptx_get(prog, ptx) != 0) { free(ptx); return NULL; }
    GpuKernel *k = (GpuKernel *)xmalloc(sizeof(GpuKernel));
    k->source = (char *)xmalloc(strlen(source) + 1);
    memcpy(k->source, source, strlen(source) + 1);
    strncpy(k->entry, entry, sizeof(k->entry) - 1);
    k->entry[sizeof(k->entry) - 1] = 0;
    k->module = NULL;
    k->fn = NULL;
    k->next = gpu.kernels;
    gpu.kernels = k;
    if (gpu.cuModuleLoadData(&k->module, ptx) != 0 ||
        gpu.cuModuleGetFunction(&k->fn, k->module, entry) != 0) {
        k->fn = NULL;
    }
    free(ptx);
    return k->fn ? k : NULL;
}

Value *lambda_gpu_arrayf_pmap(Value *f, int64_t n, const char *source) {
    if (n < 1000000 || !gpu_init()) return lambda_arrayf_pmap(f, n);
    GpuKernel *k = gpu_kernel_named(source, "pmap");
    if (!k || !k->fn) return lambda_arrayf_pmap(f, n);
    Value *out = lambda_arrayf_new(n, 0.0);
    gpu_ptr dout = 0;
    if (gpu.cuMemAlloc(&dout, sizeof(double) * (size_t)n) != 0) {
        lambda_release(out);
        return lambda_arrayf_pmap(f, n);
    }
    long long nn = (long long)n;
    void *args[] = {&dout, &nn};
    unsigned threads = 256;
    unsigned blocks = (unsigned)((n + threads - 1) / threads);
    gpu_result rc = gpu.cuLaunchKernel(k->fn, blocks, 1, 1, threads, 1, 1,
                                       0, NULL, args, NULL);
    if (rc == 0) rc = gpu.cuCtxSynchronize();
    if (rc == 0) rc = gpu.cuMemcpyDtoH(out->p, dout, sizeof(double) * (size_t)n);
    gpu.cuMemFree(dout);
    if (rc != 0) {
        lambda_release(out);
        return lambda_arrayf_pmap(f, n);
    }
    lambda_release(f);
    return out;
}

static GpuKernel *gpu_kernel(const char *source) {
    return gpu_kernel_named(source, "pmap");
}

Value *lambda_gpu_arrayf_pmap1(Value *f, int64_t n, const char *source,
                                  Value *in0) {
    if (n < 100000 || !gpu_init() || !in0 || in0->tag != TAG_ARRAYF ||
        in0->i < n) {
        return lambda_arrayf_pmap(f, n);
    }
    GpuKernel *k = gpu_kernel_named(source, "pmap");
    if (!k || !k->fn) return lambda_arrayf_pmap(f, n);
    gpu_ptr din = 0, dout = 0;
    if (gpu.cuMemAlloc(&din, sizeof(double) * (size_t)n) != 0 ||
        gpu.cuMemAlloc(&dout, sizeof(double) * (size_t)n) != 0) {
        if (din) gpu.cuMemFree(din);
        if (dout) gpu.cuMemFree(dout);
        return lambda_arrayf_pmap(f, n);
    }
    gpu_result rc = gpu.cuMemcpyHtoD(din, in0->p, sizeof(double) * (size_t)n);
    if (rc != 0) {
        gpu.cuMemFree(din); gpu.cuMemFree(dout);
        return lambda_arrayf_pmap(f, n);
    }
    Value *out = lambda_arrayf_new(n, 0.0);
    long long nn = (long long)n;
    void *args[] = {&dout, &din, &nn};
    unsigned threads = 256;
    unsigned blocks = (unsigned)((n + threads - 1) / threads);
    if (rc == 0) rc = gpu.cuLaunchKernel(k->fn, blocks, 1, 1, threads, 1, 1,
                                        0, NULL, args, NULL);
    if (rc == 0) rc = gpu.cuCtxSynchronize();
    if (rc == 0) rc = gpu.cuMemcpyDtoH(out->p, dout, sizeof(double) * (size_t)n);
    gpu.cuMemFree(din); gpu.cuMemFree(dout);
    if (rc != 0) {
        lambda_release(out);
        return lambda_arrayf_pmap(f, n);
    }
    lambda_release(f);
    return out;
}

Value *lambda_op_arrayf_pmap(Value *f, Value *nv) {
    int64_t n = as_int(nv);
    lambda_release(nv);
    return lambda_arrayf_pmap(f, n);
}

Value *lambda_op_arrayi_pmap(Value *f, Value *nv) {
    int64_t n = as_int(nv);
    lambda_release(nv);
    return lambda_arrayi_pmap(f, n);
}

Value *lambda_arrayi_pmap_chunk(Value *f, int64_t n, int64_t chunk) {
    if (n < 0) fail("array size must be non-negative");
    if (chunk <= 0) fail("arrayi-pmap-chunk chunk size must be positive");
    int64_t chunks = (n + chunk - 1) / chunk;
    Value *out = lambda_arrayi_new(chunks, 0);
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (threads > chunks) threads = chunks;
    if (threads <= 1 || chunks <= 1) {
        int64_t *data = (int64_t *)out->p;
        for (int64_t i = 0; i < chunks; i++) {
            Value *r = apply_owned(f, lambda_box_int(i));
            data[i] = lambda_get_int(r);
            lambda_release(r);
        }
        lambda_release(f);
        return out;
    }

    ArrayPMapJob job;
    job.f = f;
    job.out = out;
    job.n = chunks;
    job.next = 0;

#ifdef _WIN32
    HANDLE handles[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        HANDLE h = CreateThread(NULL, 0, array_pmap_thread_main, &job, 0, NULL);
        if (h) handles[created++] = h;
    }
    array_pmap_run(&job);
    if (created > 0) WaitForMultipleObjects((DWORD)created, handles, TRUE, INFINITE);
    for (int i = 0; i < created; i++) CloseHandle(handles[i]);
#else
    pthread_t th[LAMBDA_MAX_THREADS];
    int created = 0;
    for (int64_t i = 1; i < threads; i++) {
        if (pthread_create(&th[created], NULL, array_pmap_thread_main, &job) == 0) {
            created++;
        }
    }
    array_pmap_run(&job);
    for (int i = 0; i < created; i++) pthread_join(th[i], NULL);
#endif
    lambda_release(f);
    return out;
}

Value *lambda_op_arrayi_pmap_chunk(Value *f, Value *nv, Value *cv) {
    int64_t n = as_int(nv);
    int64_t chunk = as_int(cv);
    lambda_release(nv);
    lambda_release(cv);
    return lambda_arrayi_pmap_chunk(f, n, chunk);
}

/* ------------------------------------------------------------------ */
/* curried primitive closures                                          */
/* ------------------------------------------------------------------ */
typedef Value *(*BinFn)(Value *, Value *);

typedef struct BinRoot {
    EnvHeader hdr; /* dtor = NULL */
    BinFn fn;
} BinRoot;

typedef struct BinPartial {
    EnvHeader hdr; /* dtor = partial_dtor */
    BinFn fn;
    Value *a;
} BinPartial;

static void partial_dtor(void *p) {
    BinPartial *part = (BinPartial *)p;
    lambda_release(part->a);
    lambda_free_env(part);
}

static Value *bin_stage2(void *env, Value *b);

static Value *bin_stage1(void *env, Value *a) {
    BinRoot *root = (BinRoot *)env;
    BinPartial *part = (BinPartial *)lambda_alloc_env(sizeof(BinPartial));
    part->hdr.dtor = partial_dtor;
    part->fn = root->fn;
    part->a = a; /* ownership moves into the partial closure */
    return lambda_make_closure((void *)bin_stage2, part);
}

static Value *bin_stage2(void *env, Value *b) {
    BinPartial *part = (BinPartial *)env;
    Value *a = part->a;
    lambda_retain(a); /* the env keeps its own reference */
    return part->fn(a, b); /* consumes both */
}

static Value *unary_stage(void *env, Value *a) {
    UnaryFn fn = (UnaryFn)env;
    return fn(a);
}

typedef Value *(*LogicBinFn)(Value *, Value *);

typedef struct LogicRoot {
    EnvHeader hdr; /* dtor = NULL */
    LogicBinFn fn;
} LogicRoot;

typedef struct LogicHeld {
    EnvHeader hdr; /* dtor = logic_dtor */
    LogicBinFn fn;
    Value *a;
} LogicHeld;

static void logic_dtor(void *p) {
    LogicHeld *held = (LogicHeld *)p;
    lambda_release(held->a);
    lambda_free_env(held);
}

static Value *logic_stage2(void *env, Value *b) {
    LogicHeld *held = (LogicHeld *)env;
    Value *a = held->a;
    lambda_retain(a); /* the environment keeps its own reference */
    return held->fn(a, b);
}

static Value *logic_stage1(void *env, Value *a) {
    LogicRoot *root = (LogicRoot *)env;
    LogicHeld *held = (LogicHeld *)lambda_alloc_env(sizeof(LogicHeld));
    held->hdr.dtor = logic_dtor;
    held->fn = root->fn;
    held->a = a; /* ownership moves into the partial closure */
    return lambda_make_closure((void *)logic_stage2, held);
}

/* ------------------------------------------------------------------ */
/* call-by-value fixed-point combinator (fix / Y)                      */
/* ------------------------------------------------------------------ */
typedef struct FixPartial {
    EnvHeader hdr;
    Value *f;
} FixPartial;

static void fix_partial_dtor(void *p) {
    FixPartial *part = (FixPartial *)p;
    lambda_release(part->f);
    lambda_free_env(part);
}

/* Call a closure without consuming the caller's references. */
static Value *apply_borrowed(Value *fn, Value *arg) {
    if (!fn || fn->tag != TAG_CLOSURE) fail_type("Function", fn);
    Code code = (Code)fn->p;
    EnvHeader *env = (EnvHeader *)(intptr_t)fn->i;
    lambda_retain(arg);
    return code(env, arg);
}

/* Caller transfers ownership of arg; the closure wrapper consumes it. */
static Value *apply_owned(Value *fn, Value *arg) {
    if (!fn || fn->tag != TAG_CLOSURE) fail_type("Function", fn);
    Code code = (Code)fn->p;
    EnvHeader *env = (EnvHeader *)(intptr_t)fn->i;
    return code(env, arg);
}

static Value *fix_apply(void *env, Value *x);

Value *lambda_fix(Value *f) {
    FixPartial *part = (FixPartial *)lambda_alloc_env(sizeof(FixPartial));
    part->hdr.dtor = fix_partial_dtor;
    part->f = f; /* ownership moves into the recursive closure */
    return lambda_make_closure((void *)fix_apply, part);
}

static Value *fix_apply(void *env, Value *x) {
    FixPartial *part = (FixPartial *)env;
    /* A fresh recursive-function closure is passed to f.  It owns its own
       reference to f and is captured by f's result when needed; no reference
       cycle is created. */
    FixPartial *rp = (FixPartial *)lambda_alloc_env(sizeof(FixPartial));
    rp->hdr.dtor = fix_partial_dtor;
    rp->f = part->f;
    lambda_retain(rp->f);
    Value *rec = lambda_make_closure((void *)fix_apply, rp);
    Value *g = apply_borrowed(part->f, rec);
    Value *res = apply_borrowed(g, x);
    lambda_release(g);
    lambda_release(rec);
    lambda_release(x);
    return res;
}

static Value *fix_stage(void *env, Value *f) {
    (void)env;
    return lambda_fix(f);
}

Value *lambda_prim_add = NULL;
Value *lambda_prim_sub = NULL;
Value *lambda_prim_mul = NULL;
Value *lambda_prim_div = NULL;
Value *lambda_prim_mod = NULL;
Value *lambda_prim_eq = NULL;
Value *lambda_prim_lt = NULL;
Value *lambda_prim_le = NULL;
Value *lambda_prim_gt = NULL;
Value *lambda_prim_ge = NULL;
Value *lambda_prim_cons = NULL;
Value *lambda_prim_head = NULL;
Value *lambda_prim_tail = NULL;
Value *lambda_prim_empty = NULL;
Value *lambda_prim_not = NULL;
Value *lambda_prim_and = NULL;
Value *lambda_prim_or = NULL;
Value *lambda_prim_str_to_int = NULL;
Value *lambda_prim_int_to_str = NULL;
Value *lambda_prim_str_to_list = NULL;
Value *lambda_prim_str_to_arrayb = NULL;
Value *lambda_prim_arrayb_to_str = NULL;
Value *lambda_prim_argv = NULL;
Value *lambda_prim_argc = NULL;

void lambda_refresh_argc_prim(int64_t argc) {
    /* argc is materialised per use (no boxed global to leak). */
    (void)argc;
}
Value *lambda_prim_str_len = NULL;
Value *lambda_prim_str_append = NULL;
Value *lambda_prim_read_file = NULL;
Value *lambda_prim_print = NULL;
Value *lambda_prim_println = NULL;
Value *lambda_prim_fix = NULL;
Value *lambda_prim_int_to_float = NULL;
Value *lambda_prim_float_to_int = NULL;
Value *lambda_prim_str_to_float = NULL;
Value *lambda_prim_float_to_str = NULL;
Value *lambda_prim_sqrt = NULL;
Value *lambda_prim_sin = NULL;
Value *lambda_prim_cos = NULL;
Value *lambda_prim_tan = NULL;
Value *lambda_prim_asin = NULL;
Value *lambda_prim_acos = NULL;
Value *lambda_prim_atan = NULL;
Value *lambda_prim_atan2 = NULL;
Value *lambda_prim_exp = NULL;
Value *lambda_prim_ln = NULL;
Value *lambda_prim_log10 = NULL;
Value *lambda_prim_log2 = NULL;
Value *lambda_prim_floor = NULL;
Value *lambda_prim_ceil = NULL;
Value *lambda_prim_round = NULL;
Value *lambda_prim_trunc = NULL;
Value *lambda_prim_pow = NULL;
Value *lambda_prim_abs = NULL;
Value *lambda_prim_sum = NULL;
Value *lambda_prim_product = NULL;
Value *lambda_prim_bit_and = NULL;
Value *lambda_prim_bit_or = NULL;
Value *lambda_prim_bit_xor = NULL;
Value *lambda_prim_bit_not = NULL;
Value *lambda_prim_shl = NULL;
Value *lambda_prim_shr = NULL;
Value *lambda_prim_popcount = NULL;
Value *lambda_prim_range = NULL;
Value *lambda_prim_map = NULL;
Value *lambda_prim_pmap = NULL;
Value *lambda_prim_immortalize = NULL;
Value *lambda_prim_read_line = NULL;
Value *lambda_prim_read_int = NULL;
Value *lambda_prim_read_float = NULL;

static BinRoot root_add = { { NULL }, lambda_op_add };
static BinRoot root_sub = { { NULL }, lambda_op_sub };
static BinRoot root_mul = { { NULL }, lambda_op_mul };
static BinRoot root_div = { { NULL }, lambda_op_div };
static BinRoot root_mod = { { NULL }, lambda_op_mod };
static BinRoot root_eq = { { NULL }, lambda_op_eq };
static BinRoot root_lt = { { NULL }, lambda_op_lt };
static BinRoot root_le = { { NULL }, lambda_op_le };
static BinRoot root_gt = { { NULL }, lambda_op_gt };
static BinRoot root_ge = { { NULL }, lambda_op_ge };
static BinRoot root_cons = { { NULL }, lambda_op_cons };
static BinRoot root_str_append = { { NULL }, lambda_op_str_append };
static BinRoot root_pow = { { NULL }, lambda_op_pow };
static BinRoot root_atan2 = { { NULL }, lambda_op_atan2 };
static BinRoot root_bit_and = { { NULL }, lambda_op_bit_and };
static LogicRoot root_and = { { NULL }, lambda_op_and };
static LogicRoot root_or = { { NULL }, lambda_op_or };
static BinRoot root_bit_or = { { NULL }, lambda_op_bit_or };
static BinRoot root_bit_xor = { { NULL }, lambda_op_bit_xor };
static BinRoot root_shl = { { NULL }, lambda_op_shl };
static BinRoot root_shr = { { NULL }, lambda_op_shr };
static BinRoot root_range = { { NULL }, lambda_op_range };
static BinRoot root_map = { { NULL }, lambda_op_map };
static BinRoot root_pmap = { { NULL }, lambda_op_pmap };

void lambda_runtime_init(void) {
    pool_init();

    lambda_prim_add = make_immortal_closure((void *)bin_stage1, &root_add);
    lambda_prim_sub = make_immortal_closure((void *)bin_stage1, &root_sub);
    lambda_prim_mul = make_immortal_closure((void *)bin_stage1, &root_mul);
    lambda_prim_div = make_immortal_closure((void *)bin_stage1, &root_div);
    lambda_prim_mod = make_immortal_closure((void *)bin_stage1, &root_mod);
    lambda_prim_eq = make_immortal_closure((void *)bin_stage1, &root_eq);
    lambda_prim_lt = make_immortal_closure((void *)bin_stage1, &root_lt);
    lambda_prim_le = make_immortal_closure((void *)bin_stage1, &root_le);
    lambda_prim_gt = make_immortal_closure((void *)bin_stage1, &root_gt);
    lambda_prim_ge = make_immortal_closure((void *)bin_stage1, &root_ge);
    lambda_prim_cons = make_immortal_closure((void *)bin_stage1, &root_cons);
    lambda_prim_head = make_immortal_closure((void *)unary_stage, (void *)lambda_op_head);
    lambda_prim_tail = make_immortal_closure((void *)unary_stage, (void *)lambda_op_tail);
    lambda_prim_empty = make_immortal_closure((void *)unary_stage, (void *)lambda_op_empty);
    lambda_prim_not = make_immortal_closure((void *)unary_stage, (void *)lambda_op_not);
    lambda_prim_and = make_immortal_closure((void *)logic_stage1, (void *)&root_and);
    lambda_prim_or = make_immortal_closure((void *)logic_stage1, (void *)&root_or);
    lambda_prim_str_to_int = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_int);
    lambda_prim_int_to_str = make_immortal_closure((void *)unary_stage, (void *)lambda_op_int_to_str);
    lambda_prim_str_to_list = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_list);
    lambda_prim_str_len = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_len);
    lambda_prim_str_append = make_immortal_closure((void *)bin_stage1, &root_str_append);
    lambda_prim_str_to_arrayb = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_arrayb);
    lambda_prim_arrayb_to_str = make_immortal_closure((void *)unary_stage, (void *)lambda_op_arrayb_to_str);
    lambda_prim_argv = make_immortal_closure((void *)unary_stage, (void *)lambda_op_argv);
    lambda_prim_read_file = make_immortal_closure((void *)unary_stage, (void *)lambda_op_read_file);
    lambda_prim_print = make_immortal_closure((void *)unary_stage, (void *)lambda_op_print);
    lambda_prim_println = make_immortal_closure((void *)unary_stage, (void *)lambda_op_println);
    lambda_prim_fix = make_immortal_closure((void *)fix_stage, NULL);
    lambda_prim_int_to_float = make_immortal_closure((void *)unary_stage, (void *)lambda_op_int_to_float);
    lambda_prim_float_to_int = make_immortal_closure((void *)unary_stage, (void *)lambda_op_float_to_int);
    lambda_prim_str_to_float = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_float);
    lambda_prim_float_to_str = make_immortal_closure((void *)unary_stage, (void *)lambda_op_float_to_str);
    lambda_prim_sqrt = make_immortal_closure((void *)unary_stage, (void *)lambda_op_sqrt);
    lambda_prim_sin = make_immortal_closure((void *)unary_stage, (void *)lambda_op_sin);
    lambda_prim_cos = make_immortal_closure((void *)unary_stage, (void *)lambda_op_cos);
    lambda_prim_tan = make_immortal_closure((void *)unary_stage, (void *)lambda_op_tan);
    lambda_prim_asin = make_immortal_closure((void *)unary_stage, (void *)lambda_op_asin);
    lambda_prim_acos = make_immortal_closure((void *)unary_stage, (void *)lambda_op_acos);
    lambda_prim_atan = make_immortal_closure((void *)unary_stage, (void *)lambda_op_atan);
    lambda_prim_atan2 = make_immortal_closure((void *)bin_stage1, &root_atan2);
    lambda_prim_exp = make_immortal_closure((void *)unary_stage, (void *)lambda_op_exp);
    lambda_prim_ln = make_immortal_closure((void *)unary_stage, (void *)lambda_op_ln);
    lambda_prim_log10 = make_immortal_closure((void *)unary_stage, (void *)lambda_op_log10);
    lambda_prim_log2 = make_immortal_closure((void *)unary_stage, (void *)lambda_op_log2);
    lambda_prim_floor = make_immortal_closure((void *)unary_stage, (void *)lambda_op_floor);
    lambda_prim_ceil = make_immortal_closure((void *)unary_stage, (void *)lambda_op_ceil);
    lambda_prim_round = make_immortal_closure((void *)unary_stage, (void *)lambda_op_round);
    lambda_prim_trunc = make_immortal_closure((void *)unary_stage, (void *)lambda_op_trunc);
    lambda_prim_pow = make_immortal_closure((void *)bin_stage1, &root_pow);
    lambda_prim_abs = make_immortal_closure((void *)unary_stage, (void *)lambda_op_abs);
    lambda_prim_sum = make_immortal_closure((void *)unary_stage, (void *)lambda_op_sum);
    lambda_prim_product = make_immortal_closure((void *)unary_stage, (void *)lambda_op_product);
    lambda_prim_bit_and = make_immortal_closure((void *)bin_stage1, &root_bit_and);
    lambda_prim_bit_or = make_immortal_closure((void *)bin_stage1, &root_bit_or);
    lambda_prim_bit_xor = make_immortal_closure((void *)bin_stage1, &root_bit_xor);
    lambda_prim_bit_not = make_immortal_closure((void *)unary_stage, (void *)lambda_op_bit_not);
    lambda_prim_shl = make_immortal_closure((void *)bin_stage1, &root_shl);
    lambda_prim_shr = make_immortal_closure((void *)bin_stage1, &root_shr);
    lambda_prim_popcount = make_immortal_closure((void *)unary_stage, (void *)lambda_op_popcount);
    lambda_prim_range = make_immortal_closure((void *)bin_stage1, &root_range);
    lambda_prim_map = make_immortal_closure((void *)bin_stage1, &root_map);
    lambda_prim_pmap = make_immortal_closure((void *)bin_stage1, &root_pmap);
    lambda_prim_immortalize = make_immortal_closure((void *)unary_stage, (void *)lambda_op_immortalize);
    lambda_prim_read_line = make_immortal_closure((void *)unary_stage, (void *)lambda_op_read_line);
    lambda_prim_read_int = make_immortal_closure((void *)unary_stage, (void *)lambda_op_read_int);
    lambda_prim_read_float = make_immortal_closure((void *)unary_stage, (void *)lambda_op_read_float);
}
"""

# Declarations shared by every generated module.  Keep %Value in sync with the
# C struct above.
RUNTIME_IR_DECLS = r"""
%Value = type { i32, i32, i64, i8* }

declare i8* @malloc(i64)
declare void @free(i8*)
declare i8* @getenv(i8*)
declare i32 @printf(i8*, ...)

declare void @lambda_runtime_init()
declare void @lambda_enable_binary_stdout()
declare i64 @lambda_op_write_bytes(%Value*)
declare i64 @lambda_op_write_f64s(%Value*)
declare i64 @lambda_op_write_i64s(%Value*)
declare void @lambda_stats()
declare void @lambda_flush()
declare i8* @lambda_alloc_env(i64)
declare void @lambda_free_env(i8*)

declare %Value* @lambda_box_int(i64)
declare %Value* @lambda_box_bool(i64)
declare %Value* @lambda_box_float(double)
declare %Value* @lambda_nil()
declare %Value* @lambda_cons(%Value*, %Value*)
declare %Value* @lambda_make_closure(i8*, i8*)

declare void @lambda_retain(%Value*)
declare void @lambda_release(%Value*)
declare void @lambda_release_zero(%Value*)
declare void @lambda_check_function(%Value*)
declare void @lambda_bad_enum()
declare i64 @lambda_get_int(%Value*)
declare i64 @lambda_get_bool(%Value*)
declare double @lambda_get_float(%Value*)
declare i64 @lambda_truthy(%Value*)

declare i64 @lambda_div_i64(i64, i64)
declare i64 @lambda_mod_i64(i64, i64)
declare i64 @lambda_ftoi(double)

declare %Value* @lambda_op_add(%Value*, %Value*)
declare %Value* @lambda_op_sub(%Value*, %Value*)
declare %Value* @lambda_op_mul(%Value*, %Value*)
declare %Value* @lambda_op_div(%Value*, %Value*)
declare %Value* @lambda_op_mod(%Value*, %Value*)
declare %Value* @lambda_op_eq(%Value*, %Value*)
declare %Value* @lambda_op_lt(%Value*, %Value*)
declare %Value* @lambda_op_le(%Value*, %Value*)
declare %Value* @lambda_op_gt(%Value*, %Value*)
declare %Value* @lambda_op_ge(%Value*, %Value*)
declare %Value* @lambda_op_cons(%Value*, %Value*)
declare %Value* @lambda_op_head(%Value*)
declare %AdtArena* @lambda_arena_new(i64)
declare i64 @lambda_arena_alloc(%AdtArena*, i64)
declare i64 @lambda_arena_alloc1(%AdtArena*, i64, i64)
declare i64 @lambda_arena_alloc2(%AdtArena*, i64, i64, i64)
declare i64 @lambda_arena_alloc3(%AdtArena*, i64, i64, i64, i64)
declare i64 @lambda_arena_alloc4(%AdtArena*, i64, i64, i64, i64, i64)
declare void @lambda_arena_set(%AdtArena*, i64, i64, i64)
declare i64 @lambda_arena_get(%AdtArena*, i64, i64)
declare i64* @lambda_arena_row(%AdtArena*, i64)
declare i64 @lambda_arena_tag(%AdtArena*, i64)
declare i64 @lambda_arena_attach_list(%AdtArena*, %Value*)
declare %Value* @lambda_arena_get_list(%AdtArena*, i64)
declare void @lambda_arena_free(%AdtArena*)
declare i64 @lambda_adt_tag(%Value*)
declare %Value* @lambda_adt_field(%Value*, i64)
declare %Value* @lambda_op_tail(%Value*)
declare %Value* @lambda_op_empty(%Value*)
declare %Value* @lambda_op_not(%Value*)
declare %Value* @lambda_op_and(%Value*, %Value*)
declare %Value* @lambda_op_or(%Value*, %Value*)
declare %Value* @lambda_op_int_to_str(%Value*)
declare %Value* @lambda_op_str_to_int(%Value*)
declare %Value* @lambda_op_str_to_list(%Value*)
declare %Value* @lambda_op_str_len(%Value*)
declare %Value* @lambda_op_str_append(%Value*, %Value*)
declare %Value* @lambda_op_read_file(%Value*)
declare %Value* @lambda_op_str_to_arrayb(%Value*)
declare i64 @lambda_str_byte_length(%Value*)
declare i64 @lambda_str_byte_at(%Value*, i64)
declare i64 @lambda_str_utf8_at(%Value*, i64)
declare i64 @lambda_str_codepoint_at(%Value*, i64)
declare i64 @lambda_str_codepoint_length(%Value*)
declare i64 @lambda_str_valid_utf8(%Value*)
declare %Value* @lambda_str_to_utf16le(%Value*)
declare %Value* @lambda_utf16le_to_str(%Value*)
declare %Value* @lambda_str_to_utf16le_bytes(%Value*)
declare %Value* @lambda_utf16le_bytes_to_str(%Value*)
declare %Value* @lambda_op_str_to_utf16le_bytes(%Value*)
declare %Value* @lambda_op_utf16le_bytes_to_str(%Value*)
declare %Value* @lambda_op_read_file_bytes(%Value*)
declare %Value* @lambda_op_str_byte_at(%Value*, %Value*)
declare %Value* @lambda_op_str_utf8_at(%Value*, %Value*)
declare %Value* @lambda_op_str_codepoint_at(%Value*, %Value*)
declare %Value* @lambda_op_str_byte_length(%Value*)
declare %Value* @lambda_op_str_codepoint_length(%Value*)
declare %Value* @lambda_op_str_valid_utf8(%Value*)
declare %Value* @lambda_op_str_to_utf16le(%Value*)
declare %Value* @lambda_op_utf16le_to_str(%Value*)
declare %Value* @lambda_op_arrayb_to_str(%Value*)
declare i64 @lambda_op_argc()
declare %Value* @lambda_op_argc_prim()
declare %Value* @lambda_op_argv(%Value*)
declare void @lambda_set_args(i64, i8**)
declare %Value* @lambda_op_print(%Value*)
declare %Value* @lambda_op_println(%Value*)
declare %Value* @lambda_fix(%Value*)

declare %Value* @lambda_arrayf_new(i64, double)
declare noalias %Value* @lambda_arrayf_new_uninit(i64)
declare %Value* @lambda_arrayi_new_uninit(i64)
declare void @lambda_array_bounds_fail()
declare double @lambda_arrayf_get_unchecked(%Value*, i64)
declare double @lambda_arrayf_get(%Value*, i64)
declare void @lambda_arrayf_set(%Value*, i64, double)
declare i64 @lambda_arrayf_length(%Value*)
declare %Value* @lambda_arrayf_clone(%Value*)
declare %Value* @lambda_arrayf_of(%Value*)
declare %Value* @lambda_arrayi_of(%Value*)
declare %Value* @lambda_arrayb_of(%Value*)
declare %Value* @lambda_arrayi_new(i64, i64)
declare i64 @lambda_arrayi_get(%Value*, i64)
declare void @lambda_arrayi_set(%Value*, i64, i64)
declare i64 @lambda_arrayi_length(%Value*)
declare %Value* @lambda_arrayi_clone(%Value*)
declare %Value* @lambda_arrayb_new(i64, i64)
declare i64 @lambda_arrayb_get(%Value*, i64)
declare void @lambda_arrayb_set(%Value*, i64, i64)
declare i64 @lambda_arrayi_sum(%Value*)
declare double @lambda_arrayf_sum(%Value*)
declare i64 @lambda_arrayb_length(%Value*)
declare %Value* @lambda_arrayb_clone(%Value*)
declare %Value* @lambda_arrayg_of(%Value*)
declare %Value* @lambda_arrayg_new(i64, %Value*)
declare %Value* @lambda_arrayg_get(%Value*, i64)
declare %Value* @lambda_arrayg_set(%Value*, i64, %Value*)
declare i64 @lambda_arrayg_length(%Value*)
declare %Value* @lambda_arrayg_clone(%Value*)
declare %Value* @lambda_arrayi_pmap(%Value*, i64)
declare %Value* @lambda_arrayf_pmap(%Value*, i64)
declare %Value* @lambda_arrayf_map(%Value*, i64)
declare %Value* @lambda_arrayf_pmap_into(%Value*, %Value*, i64)
declare %Value* @lambda_arrayf_require_len(%Value*, i64)
declare %Value* @lambda_arrayf_pmap_raw(i8*, ptr, i64)
declare %Value* @lambda_arrayf_pmap_raw_loop(i8*, ptr, i64)
declare %Value* @lambda_gpu_arrayf_pmap(%Value*, i64, i8*)
declare %Value* @lambda_gpu_arrayf_pmap1(%Value*, i64, i8*, %Value*)
declare %Value* @lambda_op_arrayi_pmap(%Value*, %Value*)
declare %Value* @lambda_op_arrayf_pmap(%Value*, %Value*)
declare i64 @lambda_op_write_file(%Value*, %Value*)
declare %Value* @lambda_arrayi_pmap_chunk(%Value*, i64, i64)
declare %Value* @lambda_op_arrayi_pmap_chunk(%Value*, %Value*, %Value*)

declare %Value* @lambda_op_arrayf_new(%Value*, %Value*)
declare %Value* @lambda_op_arrayf_of(%Value*)
declare %Value* @lambda_op_arrayi_of(%Value*)
declare %Value* @lambda_op_arrayb_of(%Value*)
declare %Value* @lambda_op_arrayf_get(%Value*, %Value*)
declare %Value* @lambda_op_arrayf_set(%Value*, %Value*, %Value*)
declare %Value* @lambda_op_arrayf_length(%Value*)
declare %Value* @lambda_op_arrayf_clone(%Value*)
declare %Value* @lambda_op_arrayi_new(%Value*, %Value*)
declare %Value* @lambda_op_arrayi_get(%Value*, %Value*)
declare %Value* @lambda_op_arrayi_set(%Value*, %Value*, %Value*)
declare %Value* @lambda_op_arrayi_length(%Value*)
declare %Value* @lambda_op_arrayi_clone(%Value*)
declare %Value* @lambda_op_arrayb_new(%Value*, %Value*)
declare %Value* @lambda_op_arrayb_get(%Value*, %Value*)
declare %Value* @lambda_op_arrayb_set(%Value*, %Value*, %Value*)
declare %Value* @lambda_op_arrayb_length(%Value*)
declare %Value* @lambda_op_arrayb_clone(%Value*)

declare %Value* @lambda_op_int_to_float(%Value*)
declare %Value* @lambda_op_float_to_int(%Value*)
declare %Value* @lambda_op_str_to_float(%Value*)
declare %Value* @lambda_op_float_to_str(%Value*)
declare %Value* @lambda_op_sqrt(%Value*)
declare %Value* @lambda_op_sin(%Value*)
declare %Value* @lambda_op_cos(%Value*)
declare %Value* @lambda_op_tan(%Value*)
declare %Value* @lambda_op_asin(%Value*)
declare %Value* @lambda_op_acos(%Value*)
declare %Value* @lambda_op_atan(%Value*)
declare %Value* @lambda_op_atan2(%Value*, %Value*)
declare %Value* @lambda_op_exp(%Value*)
declare %Value* @lambda_op_ln(%Value*)
declare %Value* @lambda_op_log10(%Value*)
declare %Value* @lambda_op_log2(%Value*)
declare %Value* @lambda_op_floor(%Value*)
declare %Value* @lambda_op_ceil(%Value*)
declare %Value* @lambda_op_round(%Value*)
declare %Value* @lambda_op_trunc(%Value*)
declare %Value* @lambda_op_pow(%Value*, %Value*)
declare %Value* @lambda_op_abs(%Value*)
declare %Value* @lambda_op_sum(%Value*)
declare %Value* @lambda_op_product(%Value*)
declare %Value* @lambda_op_bit_and(%Value*, %Value*)
declare %Value* @lambda_op_bit_or(%Value*, %Value*)
declare %Value* @lambda_op_bit_xor(%Value*, %Value*)
declare %Value* @lambda_op_bit_not(%Value*)
declare %Value* @lambda_op_shl(%Value*, %Value*)
declare %Value* @lambda_op_shr(%Value*, %Value*)
declare %Value* @lambda_op_popcount(%Value*)
declare %Value* @lambda_op_range(%Value*, %Value*)
declare %Value* @lambda_op_map(%Value*, %Value*)
declare %Value* @lambda_op_pmap(%Value*, %Value*)
declare %Value* @lambda_op_immortalize(%Value*)
declare %Value* @lambda_op_read_line(%Value*)
declare %Value* @lambda_op_read_int(%Value*)
declare %Value* @lambda_op_read_float(%Value*)

declare i64 @lambda_sum_i64(%Value*)
declare double @lambda_sum_f64(%Value*)
declare i64 @lambda_product_i64(%Value*)
declare double @lambda_product_f64(%Value*)
declare %Value* @lambda_range(i64, i64)

declare double @llvm.sqrt.f64(double)
declare double @llvm.fabs.f64(double)
declare double @llvm.floor.f64(double)
declare double @llvm.ceil.f64(double)
declare double @llvm.round.f64(double)
declare double @llvm.trunc.f64(double)
declare double @llvm.pow.f64(double, double)
declare i64 @llvm.ctpop.i64(i64)

declare double @sin(double)
declare double @cos(double)
declare double @tan(double)
declare double @asin(double)
declare double @acos(double)
declare double @atan(double)
declare double @atan2(double, double)
declare double @exp(double)
declare double @log(double)
declare double @log10(double)
declare double @log2(double)
declare double @floor(double)
declare double @ceil(double)
declare double @round(double)
declare double @trunc(double)
declare double @fmod(double, double)

declare void @lambda_print_ln(%Value*)
declare void @lambda_print_i64(i64)
declare void @lambda_print_bool(i64)
declare void @lambda_print_float(double)

declare %Value* @lambda_string_new(i8*)
"""
