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
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
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
    TAG_ARRAYB = 9
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

void lambda_release(Value *v) {
    /* Cons tails are walked iteratively: releasing a long list must not
       consume C stack proportional to its length. */
    while (v) {
        if (!rc_release_once(v)) return;
        if (v->tag == TAG_CONS) {
            Value *head = (Value *)(intptr_t)v->i;
            Value *tail = (Value *)v->p;
            lambda_release(head);
            free(v);
            STAT_ADD(value_frees, 1);
            STAT_ADD(live_values, -1);
            v = tail;
            continue;
        }
        switch (v->tag) {
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
        case TAG_ARRAYB:
            free(v->p);
            v->p = NULL;
            break;
        default:
            break;
        }
        free(v);
        STAT_ADD(value_frees, 1);
        STAT_ADD(live_values, -1);
        return;
    }
}

/* ------------------------------------------------------------------ */
/* constructors                                                        */
/* ------------------------------------------------------------------ */
Value *lambda_box_int(int64_t x) {
    Value *v = value_alloc(TAG_INT);
    v->i = x;
    return v;
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

int64_t lambda_get_int(Value *v) {
    if (!v || v->tag != TAG_INT) fail_type("Int", v);
    return v->i;
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

static void out_flush(void) {
    if (out_len) {
        fwrite(out_buf, 1, out_len, stdout);
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

static void print_double(double x) {
    char tmp[64];
    int n = snprintf(tmp, sizeof(tmp), "%.17g", x);
    /* Make floats visually distinct from integers (6 -> 6.0). */
    if (isfinite(x) && strpbrk(tmp, ".eE") == NULL && n < (int)sizeof(tmp) - 2) {
        tmp[n++] = '.';
        tmp[n++] = '0';
        tmp[n] = 0;
    }
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

void lambda_print_value(Value *v) { print_value_inner(v, 0); }
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
            " env_allocs=%" PRId64 " env_frees=%" PRId64 " live=%" PRId64 "\n",
            STAT_LOAD(value_allocs), STAT_LOAD(value_frees),
            STAT_LOAD(env_allocs), STAT_LOAD(env_frees), STAT_LOAD(live_values));
}

/* ------------------------------------------------------------------ */
/* structural equality                                                 */
/* ------------------------------------------------------------------ */
static int value_equal(Value *a, Value *b) {
    while (1) {
        if (a == b) return 1;
        if (!a || !b) return 0;
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

Value *lambda_arrayf_new(int64_t n, double init) {
    if (n < 0) fail("array size must be non-negative");
    Value *a = value_alloc(TAG_ARRAYF);
    a->i = n;
    a->p = xmalloc(sizeof(double) * (size_t)(n > 0 ? n : 1));
    double *d = (double *)a->p;
    for (int64_t i = 0; i < n; ++i) d[i] = init;
    return a;
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
    if (n < 0) fail("array size must be non-negative");
    Value *a = value_alloc(TAG_ARRAYI);
    a->i = n;
    a->p = xmalloc(sizeof(int64_t) * (size_t)(n > 0 ? n : 1));
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
    if (n < 0) fail("array size must be non-negative");
    Value *a = value_alloc(TAG_ARRAYB);
    a->i = n;
    a->p = xmalloc((size_t)(n > 0 ? n : 1));
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
    int64_t len = v->i;
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
    const char *path = string_data(pathv);
    FILE *f = fopen(path, "rb");
    if (!f) {
        lambda_release(pathv);
        return lambda_string_new_len("", 0);
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    if (size < 0) size = 0;
    fseek(f, 0, SEEK_SET);
    char *buf = (char *)xmalloc((size_t)size + 1);
    size_t got = fread(buf, 1, (size_t)size, f);
    fclose(f);
    buf[got] = 0;
    Value *r = value_alloc(TAG_STRING);
    r->i = (int64_t)got;
    r->p = buf;
    lambda_release(pathv);
    return r;
}

/* ------------------------------------------------------------------ */
/* optional terminal input (compiled only with --input)                */
/* ------------------------------------------------------------------ */
static char *read_prompt_line(Value *prompt) {
    if (prompt) {
        if (prompt->tag != TAG_STRING) fail_type("String", prompt);
        out_write((const char *)prompt->p, (size_t)prompt->i);
    }
    lambda_release(prompt);
    out_flush();
    char buf[4096];
    if (!fgets(buf, sizeof(buf), stdin)) return NULL;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = 0;
    char *copy = (char *)xmalloc(n + 1);
    memcpy(copy, buf, n + 1);
    return copy;
}

Value *lambda_op_read_line(Value *prompt) {
    char *line = read_prompt_line(prompt);
    if (!line) return lambda_string_new_len("", 0);
    Value *r = lambda_string_new(line);
    free(line);
    return r;
}

Value *lambda_op_read_int(Value *prompt) {
    char *line = read_prompt_line(prompt);
    if (!line) fail("read-int: end of input");
    char *end = NULL;
    errno = 0;
    long long x = strtoll(line, &end, 10);
    while (end && *end && (*end == ' ' || *end == '\t' || *end == '\r')) end++;
    if (end == line || (end && *end)) {
        free(line);
        fail("read-int: invalid integer");
    }
    free(line);
    return lambda_box_int((int64_t)x);
}

Value *lambda_op_read_float(Value *prompt) {
    char *line = read_prompt_line(prompt);
    if (!line) fail("read-float: end of input");
    char *end = NULL;
    double x = strtod(line, &end);
    while (end && *end && (*end == ' ' || *end == '\t' || *end == '\r')) end++;
    if (end == line || (end && *end)) {
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

static int format_double(char *buf, size_t cap, double x) {
    int n = snprintf(buf, cap, "%.17g", x);
    if (isfinite(x) && strpbrk(buf, ".eE") == NULL && n < (int)cap - 2) {
        buf[n++] = '.';
        buf[n++] = '0';
        buf[n] = 0;
    }
    return n;
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
    lambda_release(list);
    return acc;
}

Value *lambda_op_sum(Value *list) {
    if (!list) fail("sum of null");
    if (list->tag == TAG_NIL) {
        lambda_release(list);
        return lambda_box_int(0);
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
    if (!v) return;
    if (v->rc > 0) STAT_ADD(live_values, -1);
    v->rc = -1;
    if (v->tag == TAG_CONS) {
        immortalize_graph((Value *)(intptr_t)v->i);
        immortalize_graph((Value *)v->p);
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
        Value *arg = lambda_box_int(i);
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
    Value *out = lambda_arrayi_new(n, 0);
    int64_t threads = pmap_requested_threads();
    if (threads > LAMBDA_MAX_THREADS) threads = LAMBDA_MAX_THREADS;
    if (threads > n) threads = n;
    if (threads <= 1 || n <= 1) {
        int64_t *data = (int64_t *)out->p;
        for (int64_t i = 0; i < n; i++) {
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

Value *lambda_op_arrayi_pmap(Value *f, Value *nv) {
    int64_t n = as_int(nv);
    lambda_release(nv);
    return lambda_arrayi_pmap(f, n);
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
Value *lambda_prim_str_to_int = NULL;
Value *lambda_prim_int_to_str = NULL;
Value *lambda_prim_str_to_list = NULL;
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
static BinRoot root_bit_or = { { NULL }, lambda_op_bit_or };
static BinRoot root_bit_xor = { { NULL }, lambda_op_bit_xor };
static BinRoot root_shl = { { NULL }, lambda_op_shl };
static BinRoot root_shr = { { NULL }, lambda_op_shr };
static BinRoot root_range = { { NULL }, lambda_op_range };
static BinRoot root_map = { { NULL }, lambda_op_map };
static BinRoot root_pmap = { { NULL }, lambda_op_pmap };

void lambda_runtime_init(void) {
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
    lambda_prim_str_to_int = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_int);
    lambda_prim_int_to_str = make_immortal_closure((void *)unary_stage, (void *)lambda_op_int_to_str);
    lambda_prim_str_to_list = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_to_list);
    lambda_prim_str_len = make_immortal_closure((void *)unary_stage, (void *)lambda_op_str_len);
    lambda_prim_str_append = make_immortal_closure((void *)bin_stage1, &root_str_append);
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
declare i64 @lambda_get_int(%Value*)
declare i64 @lambda_get_bool(%Value*)
declare double @lambda_get_float(%Value*)
declare i64 @lambda_truthy(%Value*)

declare i64 @lambda_div_i64(i64, i64)
declare i64 @lambda_mod_i64(i64, i64)

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
declare %Value* @lambda_op_tail(%Value*)
declare %Value* @lambda_op_empty(%Value*)
declare %Value* @lambda_op_not(%Value*)
declare %Value* @lambda_op_int_to_str(%Value*)
declare %Value* @lambda_op_str_to_int(%Value*)
declare %Value* @lambda_op_str_to_list(%Value*)
declare %Value* @lambda_op_str_len(%Value*)
declare %Value* @lambda_op_str_append(%Value*, %Value*)
declare %Value* @lambda_op_read_file(%Value*)
declare %Value* @lambda_op_print(%Value*)
declare %Value* @lambda_op_println(%Value*)
declare %Value* @lambda_fix(%Value*)

declare %Value* @lambda_arrayf_new(i64, double)
declare void @lambda_array_bounds_fail()
declare double @lambda_arrayf_get(%Value*, i64)
declare void @lambda_arrayf_set(%Value*, i64, double)
declare i64 @lambda_arrayf_length(%Value*)
declare %Value* @lambda_arrayf_clone(%Value*)
declare %Value* @lambda_arrayi_new(i64, i64)
declare i64 @lambda_arrayi_get(%Value*, i64)
declare void @lambda_arrayi_set(%Value*, i64, i64)
declare i64 @lambda_arrayi_length(%Value*)
declare %Value* @lambda_arrayi_clone(%Value*)
declare %Value* @lambda_arrayb_new(i64, i64)
declare i64 @lambda_arrayb_get(%Value*, i64)
declare void @lambda_arrayb_set(%Value*, i64, i64)
declare i64 @lambda_arrayb_length(%Value*)
declare %Value* @lambda_arrayb_clone(%Value*)
declare %Value* @lambda_arrayi_pmap(%Value*, i64)
declare %Value* @lambda_op_arrayi_pmap(%Value*, %Value*)

declare %Value* @lambda_op_arrayf_new(%Value*, %Value*)
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

declare void @lambda_print_value(%Value*)
declare void @lambda_print_ln(%Value*)
declare void @lambda_print_i64(i64)
declare void @lambda_print_bool(i64)
declare void @lambda_print_float(double)

declare %Value* @lambda_string_new(i8*)
"""
