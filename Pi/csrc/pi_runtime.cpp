// pi_runtime.cpp -- C++ AST interpreter for Pi plans.
//
// The Python frontend parses/checks Pi and writes a plan; this executable owns
// all orchestration at run time: scopes, worker calls, channels/loops (in
// progress), results.  No Python is loaded.
//
// Build:
//   clang++ -O2 -std=c++17 -pthread csrc/pi_runtime.cpp -o pi-runtime
// Run:
//   pi-runtime plan.json

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <memory>
#include <atomic>
#include <unordered_map>
#include <vector>

static constexpr uint64_t MAX_FRAME = 256ull * 1024 * 1024;
/* P5: large but flat programs (a 1,000-term + chain nests ~1 plan level per
   term).  The old 512 cap rejected them.  The runtime runs on a 64 MB stack
   (see main) so this depth is safe to parse and evaluate. */
static constexpr size_t MAX_JSON_DEPTH = 8192;
static constexpr size_t BIG_STACK_BYTES = 64ull * 1024 * 1024;
/* P3: `par for` uses a bounded pool, never one OS thread per element. */
static constexpr size_t MAX_PAR_WORKERS = 64;

#ifdef _WIN32
#define NOMINMAX 1
#include <windows.h>
#else
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <sys/wait.h>
extern char **environ;
#endif

namespace fs = std::filesystem;

// ------------------------------------------------------------------ JSON
[[noreturn]] static void plan_json_fail(std::string msg);

struct J {
    enum Kind { NUL, BOOL, NUM, STR, ARR, OBJ } kind = NUL;
    bool b = false;
    double num = 0;
    std::string str;
    std::vector<J> arr;
    std::map<std::string, J> obj;
    const J &operator[](const std::string &k) const {
        static J nil;
        auto it = obj.find(k);
        return it == obj.end() ? nil : it->second;
    }
    bool has(const std::string &k) const { return obj.count(k) != 0; }
    std::string as_str() const { return str; }
};

static void utf8_append(std::string &out, uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back((char)cp);
    } else if (cp <= 0x7FF) {
        out.push_back((char)(0xC0 | (cp >> 6)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back((char)(0xE0 | (cp >> 12)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        out.push_back((char)(0xF0 | (cp >> 18)));
        out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    }
}

struct JsonParser {
    const char *start, *p, *end;
    explicit JsonParser(const std::string &s) : start(s.data()), p(s.data()), end(s.data() + s.size()) {}
    void ws() { while (p < end && std::isspace((unsigned char)*p)) p++; }
    [[noreturn]] void fail(const char *m) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "plan JSON: %s at %ld near %.40s [%02x]", m,
                      (long)(p - start), p, p < end ? (unsigned char)*p : 0);
        plan_json_fail(buf);
    }
    size_t depth = 0;
    J parse() {
        ws();
        J v = value();
        ws();
        if (p != end) fail("trailing data");
        return v;
    }
    J value() {
        ws();
        if (p >= end) fail("eof");
        char c = *p;
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') { J v; v.kind = J::STR; v.str = string(); return v; }
        if (!std::strncmp(p, "true", 4)) { p += 4; J v; v.kind = J::BOOL; v.b = true; return v; }
        if (!std::strncmp(p, "false", 5)) { p += 5; J v; v.kind = J::BOOL; v.b = false; return v; }
        if (!std::strncmp(p, "null", 4)) { p += 4; return J(); }
        char *e = nullptr;
        double d = std::strtod(p, &e);
        if (e == p) fail("number");
        p = e;
        J v; v.kind = J::NUM; v.num = d; return v;
    }
    std::string string() {
        if (p >= end || *p != '"') fail("string");
        p++;
        std::string out;
        auto hex4 = [&]() -> uint32_t {
            uint32_t v = 0;
            for (int i = 0; i < 4; i++) {
                if (p >= end) fail("truncated \\u escape");
                char c = *p++;
                v <<= 4;
                if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
                else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
                else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
                else fail("bad \\u escape");
            }
            return v;
        };
        while (p < end && *p != '"') {
            unsigned char c = (unsigned char)*p;
            if (c == '\\') {
                p++;
                if (p >= end) fail("bad escape");
                switch (*p) {
                case 'n': out.push_back('\n'); p++; break;
                case 'r': out.push_back('\r'); p++; break;
                case 't': out.push_back('\t'); p++; break;
                case 'b': out.push_back('\b'); p++; break;
                case 'f': out.push_back('\f'); p++; break;
                case '\\': out.push_back('\\'); p++; break;
                case '"': out.push_back('"'); p++; break;
                case '/': out.push_back('/'); p++; break;
                case 'u': {
                    // P4: decode \uXXXX (and surrogate pairs) to UTF-8.  The old
                    // default branch kept the letter and dropped the backslash,
                    // mangling NUL/control characters (Python's json writes a NUL
                    // in a label as \u0000).
                    p++;
                    uint32_t cp = hex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (p + 1 < end && p[0] == '\\' && p[1] == 'u') {
                            p += 2;
                            uint32_t lo = hex4();
                            if (lo < 0xDC00 || lo > 0xDFFF) fail("bad surrogate pair");
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            fail("unpaired high surrogate");
                        }
                    } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                        fail("unpaired low surrogate");
                    }
                    utf8_append(out, cp);
                    break;
                }
                default: fail("bad escape");
                }
            } else if (c < 0x20) {
                fail("control character in string");
            } else {
                out.push_back((char)c);
                p++;
            }
        }
        if (p >= end) fail("unterminated string");
        p++;
        return out;
    }
    J object() {
        if (++depth > MAX_JSON_DEPTH) fail("nesting too deep");
        J v; v.kind = J::OBJ; p++;
        ws();
        if (p < end && *p == '}') { p++; depth--; return v; }
        while (p < end) {
            ws();
            std::string k = string();
            ws();
            if (p >= end || *p != ':') fail("colon");
            p++;
            v.obj[k] = value();
            ws();
            if (p < end && *p == ',') { p++; continue; }
            if (p < end && *p == '}') { p++; break; }
            fail("object");
        }
        depth--;
        return v;
    }
    J array() {
        if (++depth > MAX_JSON_DEPTH) fail("nesting too deep");
        J v; v.kind = J::ARR; p++;
        ws();
        if (p < end && *p == ']') { p++; depth--; return v; }
        while (p < end) {
            v.arr.push_back(value());
            ws();
            if (p < end && *p == ',') { p++; continue; }
            if (p < end && *p == ']') { p++; break; }
            fail("array");
        }
        depth--;
        return v;
    }
};

// ------------------------------------------------------------------ values
struct Worker;
struct Channel;
struct Instance;
struct SystemVal;
struct V {
    enum Kind { UNIT, NUM, STR, PAYLOAD, WORKER, PARAMS, CHAN, TUPLE,
                SYSTEM, FAMILY, INSTANCE, SYSCALL } kind = UNIT;
    double num = 0;
    std::string str;
    std::vector<double> payload;
    Worker *worker = nullptr;
    std::shared_ptr<Channel> chan;
    bool tx = false;
    std::vector<V> tuple;
    std::map<std::string, double> params;
    std::shared_ptr<SystemVal> sys;
    std::shared_ptr<Instance> inst;
    std::string field;
    V() = default;
    explicit V(double d) : kind(NUM), num(d) {}
    explicit V(std::string s) : kind(STR), str(std::move(s)) {}
    static V payload_of(std::vector<double> p) { V v; v.kind = PAYLOAD; v.payload = std::move(p); return v; }
};

// --------------------------------------------------------------- errors
// P6: no `exit()` from worker/timeout/`par for`/controller threads.  Every
// background thread catches (or rethrows) these exceptions and `main` decides
// the exit status after `finish()` has run.
struct PiError : std::exception {
    std::string kind;
    long code = 0;
    Worker *worker = nullptr;
    std::string message;
    PiError(std::string k, long c = 0) : kind(std::move(k)), code(c) {
        message = kind;
        if (code) message += "(" + std::to_string(code) + ")";
    }
    const char *what() const noexcept override { return message.c_str(); }
};

/** A non-recoverable runtime error (bad index, bad conversion, ...).  It is not
    a Pi error constructor, so `on_error` handlers do not swallow it. */
struct PiFatal : std::exception {
    std::string message;
    int exit_code = 1;
    explicit PiFatal(std::string m, int code = 1) : message(std::move(m)), exit_code(code) {}
    const char *what() const noexcept override { return message.c_str(); }
};

[[noreturn]] static void plan_json_fail(std::string msg) {
    throw PiFatal(std::move(msg), 2);
}

/** Internal: the branch holding this token was abandoned by `timeout`. */
struct Cancelled {};

struct CancelToken {
    std::atomic<bool> cancelled{false};
    std::atomic<Worker *> cancelled_worker{nullptr};
    CancelToken *parent = nullptr;
};

static bool cancel_requested(CancelToken *t) {
    for (; t; t = t->parent)
        if (t->cancelled.load()) return true;
    return false;
}

[[noreturn]] static void die(const std::string &m) {
    throw PiFatal(m);
}

// P7: a raw double->long cast is undefined behaviour for NaN, infinities and
// values outside the integer range (`retry(max = 1e30)`, `chan(1e30)`).
static bool double_fits_long(double d) {
    if (!std::isfinite(d)) return false;
    if (d < (double)LONG_MIN || d > (double)LONG_MAX) return false;
    // (double)LONG_MAX rounds up to 2^63 on 64-bit targets, which would still
    // be one past the range; reject it explicitly.
    if (d >= 9223372036854775808.0) return false;
    return true;
}

static long checked_long(double d, const char *what) {
    if (!double_fits_long(d)) throw PiFatal(std::string("integer out of range for ") + what);
    return (long)d;
}

static long as_int(const V &v) {
    if (v.kind != V::NUM) die("expected number");
    return checked_long(v.num, "a number");
}

// ------------------------------------------------------------------ worker
#ifdef _WIN32
struct Worker {
    HANDLE in = nullptr, out = nullptr;
    PROCESS_INFORMATION proc{};
    std::string name, exe;
    uint64_t schema_hi = 0, schema_lo = 0;
    bool gpu = false;
    bool alive = true;
    std::mutex io_mu;
};
#else
struct Worker {
    int in = -1, out = -1;
    pid_t pid = 0;
    std::string name, exe;
    uint64_t schema_hi = 0, schema_lo = 0;
    bool gpu = false;
    bool alive = true;
    std::mutex io_mu;
};
#endif

static std::vector<Worker *> g_workers;
static std::mutex g_workers_mu;
static std::mutex g_spawn_mu;

static void shutdown_worker(Worker *w);

static void reap_and_close(Worker *w) {
    w->alive = false;
    shutdown_worker(w);
}

/** P1: a worker that dies mid-call surfaces as WorkerDied to on_error/restart,
    never as `die()`/`exit()` (which bypassed the recovery ladder). */
[[noreturn]] static void worker_died(Worker *w, const char *) {
    reap_and_close(w);
    PiError e("WorkerDied");
    e.worker = w;
    throw e;
}

/** P2: the branch holding *cancel* was abandoned by `timeout`.  A worker that
    is still blocked in a call cannot be left holding its I/O mutex, so it is
    killed here; the timeout error carries the worker so `| Deadline ⇒ restart`
    can replace it. */
[[noreturn]] static void worker_cancelled(Worker *w, CancelToken *cancel) {
    if (cancel) {
        Worker *expected = nullptr;
        cancel->cancelled_worker.compare_exchange_strong(expected, w);
    }
    reap_and_close(w);
    throw Cancelled{};
}

static void write_all(Worker *w, const std::string &s, CancelToken *cancel = nullptr) {
#ifdef _WIN32
    size_t off = 0;
    while (off < s.size()) {
        if (cancel_requested(cancel)) worker_cancelled(w, cancel);
        DWORD n = 0;
        DWORD chunk = (DWORD)std::min<size_t>(s.size() - off, 1u << 20);
        if (!WriteFile(w->in, s.data() + off, chunk, &n, nullptr) || !n)
            worker_died(w, "worker write");
        off += n;
    }
#else
    size_t off = 0;
    while (off < s.size()) {
        if (cancel_requested(cancel)) worker_cancelled(w, cancel);
        struct pollfd pfd{w->in, POLLOUT, 0};
        int r = ::poll(&pfd, 1, 50);
        if (r < 0) {
            if (errno == EINTR) continue;
            worker_died(w, "worker write");
        }
        if (r == 0) continue;
        ssize_t n = ::write(w->in, s.data() + off, s.size() - off);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) worker_died(w, "worker write");
        off += (size_t)n;
    }
#endif
}

static void read_all(Worker *w, void *buf, size_t n, CancelToken *cancel = nullptr) {
#ifdef _WIN32
    char *p = (char *)buf;
    while (n) {
        if (cancel_requested(cancel)) worker_cancelled(w, cancel);
        DWORD avail = 0;
        if (PeekNamedPipe(w->out, nullptr, 0, nullptr, &avail, nullptr)) {
            if (avail) {
                DWORD got = 0;
                DWORD chunk = (DWORD)std::min<size_t>(n, avail);
                if (!ReadFile(w->out, p, chunk, &got, nullptr) || !got)
                    worker_died(w, "worker read");
                p += got; n -= got;
                continue;
            }
        } else {
            worker_died(w, "worker read");
        }
        /* No bytes ready: wait briefly, then check whether the process is gone. */
        if (w->proc.hProcess) {
            DWORD wr = WaitForSingleObject(w->proc.hProcess, 5);
            if (wr == WAIT_OBJECT_0) {
                /* Drain a final partial buffer before declaring it dead. */
                DWORD left = 0;
                if (!PeekNamedPipe(w->out, nullptr, 0, nullptr, &left, nullptr) || left == 0)
                    worker_died(w, "worker read");
            }
        } else {
            Sleep(5);
        }
    }
#else
    char *p = (char *)buf;
    while (n) {
        if (cancel_requested(cancel)) worker_cancelled(w, cancel);
        struct pollfd pfd{w->out, POLLIN, 0};
        int r = ::poll(&pfd, 1, 50);
        if (r < 0) {
            if (errno == EINTR) continue;
            worker_died(w, "worker read");
        }
        if (r == 0) continue;
        ssize_t got = ::read(w->out, p, n);
        if (got < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (got <= 0) worker_died(w, "worker read");
        p += got; n -= (size_t)got;
    }
#endif
}

static void shutdown_worker(Worker *w) {
    if (!w) return;
#ifdef _WIN32
    if (w->proc.hProcess) { TerminateProcess(w->proc.hProcess, 1); WaitForSingleObject(w->proc.hProcess, 2000); CloseHandle(w->proc.hProcess); CloseHandle(w->proc.hThread); w->proc.hProcess = nullptr; w->proc.hThread = nullptr; }
    if (w->in) { CloseHandle(w->in); w->in = nullptr; }
    if (w->out) { CloseHandle(w->out); w->out = nullptr; }
#else
    if (w->pid > 0) { kill(w->pid, SIGKILL); waitpid(w->pid, nullptr, 0); w->pid = 0; }
    if (w->in >= 0) { close(w->in); w->in = -1; }
    if (w->out >= 0) { close(w->out); w->out = -1; }
#endif
    w->alive = false;
}

static void shutdown_all_workers() {
    std::lock_guard<std::mutex> lk(g_workers_mu);
    for (Worker *w : g_workers) { shutdown_worker(w); delete w; }
    g_workers.clear();
}

/** P2 last resort: kill workers without deleting them (a cancelled branch may
    still hold a pointer).  Deletion happens in shutdown_all_workers. */
static void kill_all_workers() {
    std::lock_guard<std::mutex> lk(g_workers_mu);
    for (Worker *w : g_workers) shutdown_worker(w);
}

#ifdef _WIN32
static std::string windows_env_block(bool gpu) {
    std::string block;
    LPCH env = GetEnvironmentStringsA();
    if (env) {
        for (LPCH p = env; *p; p += std::strlen(p) + 1) {
            std::string entry(p);
            if (entry.rfind("LAMBDA_GPU=", 0) == 0) continue;
            block += entry;
            block.push_back('\0');
        }
        FreeEnvironmentStringsA(env);
    }
    if (gpu) {
        block += "LAMBDA_GPU=1";
        block.push_back('\0');
    }
    block.push_back('\0');
    return block;
}
#else
static void set_cloexec(int fd) {
    int flags = fcntl(fd, F_GETFD);
    if (flags >= 0) fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
}
#endif

static Worker *spawn_worker(const std::string &exe, bool gpu, const std::string &name,
                            uint64_t schema_hi = 0, uint64_t schema_lo = 0,
                            CancelToken *cancel = nullptr) {
    Worker *w = new Worker();
    try {
        w->name = name;
        w->exe = exe;
        w->gpu = gpu;
        w->schema_hi = schema_hi;
        w->schema_lo = schema_lo;
        if (exe.find('"') != std::string::npos) die("worker path contains a quote");
        std::lock_guard<std::mutex> spawn_lock(g_spawn_mu);
#ifdef _WIN32
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE ir, iw, orr, ow;
        if (!CreatePipe(&ir, &iw, &sa, 0) || !CreatePipe(&orr, &ow, &sa, 0)) die("CreatePipe");
        SetHandleInformation(iw, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(orr, HANDLE_FLAG_INHERIT, 0);
        STARTUPINFOA si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = ir;
        si.hStdOutput = ow;
        si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
        std::string cmd = "\"" + exe + "\"";
        /* 3B: pass the child's environment explicitly instead of mutating the
           process environment under a mutex that getenv() readers do not take. */
        std::string env_block = windows_env_block(gpu);
        BOOL ok = CreateProcessA(nullptr, cmd.data(), nullptr, nullptr, TRUE,
                                 CREATE_NO_WINDOW, (LPVOID)env_block.data(), nullptr, &si, &w->proc);
        if (!ok) die("cannot start worker " + exe);
        CloseHandle(ir);
        CloseHandle(ow);
        w->in = iw;
        w->out = orr;
#else
        int pin[2], pout[2];
        if (pipe(pin) || pipe(pout)) die("pipe");
        /* 3B: close-on-exec so later workers do not inherit earlier pipes. */
        set_cloexec(pin[0]); set_cloexec(pin[1]);
        set_cloexec(pout[0]); set_cloexec(pout[1]);
        std::vector<std::string> env_store;
        for (char **e = environ; e && *e; ++e) {
            std::string entry(*e);
            if (entry.rfind("LAMBDA_GPU=", 0) == 0) continue;
            env_store.push_back(std::move(entry));
        }
        if (gpu) env_store.push_back("LAMBDA_GPU=1");
        std::vector<char *> envp;
        envp.reserve(env_store.size() + 1);
        for (auto &entry : env_store) envp.push_back(const_cast<char *>(entry.c_str()));
        envp.push_back(nullptr);
        pid_t pid = fork();
        if (pid < 0) die("fork");
        if (pid == 0) {
            dup2(pin[0], 0); dup2(pout[1], 1);
            close(pin[0]); close(pin[1]); close(pout[0]); close(pout[1]);
            execle(exe.c_str(), exe.c_str(), (char *)nullptr, envp.data());
            _exit(127);
        }
        close(pin[0]); close(pout[1]);
        w->pid = pid; w->in = pin[1]; w->out = pout[0];
#endif
        unsigned char h[16];
        read_all(w, h, 16, cancel);
        if (std::memcmp(h, "LMB1", 4)) die("worker HELLO magic");
        if (h[4] != 2) die("worker HELLO tag");
        uint32_t sid = (uint32_t)h[8] | ((uint32_t)h[9] << 8) | ((uint32_t)h[10] << 16) | ((uint32_t)h[11] << 24);
        uint32_t n = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
        if (sid != 0 || n != 24 || n > MAX_FRAME) die("worker HELLO shape");
        std::vector<unsigned char> body(n);
        read_all(w, body.data(), n, cancel);
        int64_t hdr[3];
        std::memcpy(hdr, body.data(), 24);
        uint64_t got_hi = (uint64_t)hdr[0], got_lo = (uint64_t)hdr[1];
        if (hdr[2] != 2) die("worker schema version");
        if ((schema_hi || schema_lo) && (got_hi != schema_hi || got_lo != schema_lo))
            die("worker schema mismatch");
        {
            std::lock_guard<std::mutex> lk(g_workers_mu);
            g_workers.push_back(w);
        }
        return w;
    } catch (...) {
        shutdown_worker(w);
        delete w;
        throw;
    }
}

static void restart_worker(Worker *w, CancelToken *cancel = nullptr) {
    std::lock_guard<std::mutex> lk(w->io_mu);
    shutdown_worker(w);
    Worker *n = spawn_worker(w->exe, w->gpu, w->name, w->schema_hi, w->schema_lo, cancel);
#ifdef _WIN32
    w->in = n->in; w->out = n->out; w->proc = n->proc;
#else
    w->in = n->in; w->out = n->out; w->pid = n->pid;
#endif
    w->alive = true;
    {
        std::lock_guard<std::mutex> gl(g_workers_mu);
        g_workers.erase(std::remove(g_workers.begin(), g_workers.end(), n), g_workers.end());
    }
    delete n;
}

struct ResumeSignal {};
struct RestartController {};
struct StopSignal {};
struct EscalateSignal { PiError err; explicit EscalateSignal(PiError e) : err(std::move(e)) {} };

static std::atomic<long> g_corr{1};

static V worker_call(Worker *w, long op, const std::vector<V> &args, const std::string &ret = "",
                     CancelToken *cancel = nullptr) {
    std::lock_guard<std::mutex> io_lock(w->io_mu);
    if (!w->alive) {
        PiError e("WorkerDied");
        e.worker = w;
        throw e;
    }
    if (cancel_requested(cancel)) worker_cancelled(w, cancel);
    // flatten
    long nargs = 0;
    for (auto &a : args) {
        if (a.kind == V::NUM) nargs++;
        else if (a.kind == V::PAYLOAD) nargs += 1 + (long)a.payload.size();
        else if (a.kind == V::UNIT) {}
        else if (a.kind == V::PARAMS) nargs += 1 + (long)a.params.size();
        else if (a.kind == V::TUPLE) {
            for (auto &x : a.tuple) {
                if (x.kind == V::NUM) nargs++;
                else if (x.kind == V::PAYLOAD) nargs += 1 + (long)x.payload.size();
                else if (x.kind == V::UNIT) {}
                else die("unsupported nested call argument kind=" + std::to_string((int)x.kind));
            }
        }
        else die("unsupported call argument kind=" + std::to_string((int)a.kind));
    }
    char hdr[128];
    long corr = g_corr.fetch_add(1);
    std::snprintf(hdr, sizeof(hdr), "%ld\n%ld\n%ld\n", corr, op, nargs);
    std::string req = hdr;
    for (auto &a : args) {
        if (a.kind == V::NUM) {
            char b[64]; std::snprintf(b, sizeof(b), "%.17g\n", a.num); req += b;
        } else if (a.kind == V::PAYLOAD) {
            char b[64]; std::snprintf(b, sizeof(b), "%ld\n", (long)a.payload.size()); req += b;
            for (double x : a.payload) { std::snprintf(b, sizeof(b), "%.17g\n", x); req += b; }
        } else if (a.kind == V::PARAMS) {
            char b[64]; std::snprintf(b, sizeof(b), "%ld\n", (long)a.params.size()); req += b;
            for (auto &kv : a.params) { std::snprintf(b, sizeof(b), "%.17g\n", kv.second); req += b; }
        } else if (a.kind == V::TUPLE) {
            for (auto &x : a.tuple) {
                if (x.kind == V::NUM) {
                    char b[64]; std::snprintf(b, sizeof(b), "%.17g\n", x.num); req += b;
                } else if (x.kind == V::PAYLOAD) {
                    char b[64]; std::snprintf(b, sizeof(b), "%ld\n", (long)x.payload.size()); req += b;
                    for (double y : x.payload) { std::snprintf(b, sizeof(b), "%.17g\n", y); req += b; }
                }
            }
        }
    }
    write_all(w, req, cancel);
    unsigned char h[16];
    read_all(w, h, 16, cancel);
    if (std::memcmp(h, "LMB1", 4)) die("reply magic");
    if (h[4] != 1) die("reply tag");
    uint32_t sid = (uint32_t)h[8] | ((uint32_t)h[9] << 8) | ((uint32_t)h[10] << 16) | ((uint32_t)h[11] << 24);
    uint32_t n = (uint32_t)h[12] | ((uint32_t)h[13] << 8) | ((uint32_t)h[14] << 16) | ((uint32_t)h[15] << 24);
    if ((long)sid != corr) die("reply correlation");
    if (n < 8 || (n % 8) != 0 || n > MAX_FRAME) die("reply BadFrame length");
    std::vector<double> body(n / 8);
    read_all(w, body.data(), n, cancel);
    if (body.empty()) die("empty reply");
    long tag = checked_long(body[0], "reply tag");
    if (tag == 2) {
        long code = body.size() > 1 ? checked_long(body[1], "error code") : -1;
        PiError e("WorkerError", code);
        e.worker = w;
        throw e;
    }
    size_t off = 1;
    long stats_k = -1;
    if (tag == 1) {
        if (body.size() < 2) die("reply BadFrame stats header");
        stats_k = checked_long(body[1], "stats length");
        if (stats_k < 0 || (size_t)stats_k > body.size() - 2) die("reply BadFrame stats length");
        off = 2 + (size_t)stats_k;
    } else if (tag != 0) {
        die("reply BadFrame tag");
    }
    if (off > body.size()) die("reply BadFrame payload offset");
    std::vector<double> vals(body.begin() + off, body.end());
    if (ret.find('(') != std::string::npos && tag == 1) {
        long k = stats_k > 0 ? stats_k : 0;
        V stats = V::payload_of(std::vector<double>(body.begin() + 2,
                                                    body.begin() + std::min((size_t)(2 + k), body.size())));
        V out; out.kind = V::TUPLE;
        out.tuple.push_back(V::payload_of(std::move(vals)));
        out.tuple.push_back(stats);
        return out;
    }
    return V::payload_of(std::move(vals));
}

// ------------------------------------------------------------------ channels
struct ChannelClosed {};
struct Channel {
    size_t cap;
    std::deque<V> q;
    bool closed = false;
    std::mutex mu;
    std::condition_variable cv;
    explicit Channel(size_t c) : cap(c) {}
    bool try_recv(V &out) {
        std::lock_guard<std::mutex> lk(mu);
        if (q.empty()) return false;
        out = q.front(); q.pop_front(); cv.notify_all(); return true;
    }
};

static void chan_send(const V &c, const V &v, CancelToken *cancel = nullptr) {
    if (c.kind != V::CHAN || !c.tx) die("send needs a Tx");
    auto ch = c.chan;
    if (!ch) return;
    std::unique_lock<std::mutex> lk(ch->mu);
    while (!(ch->closed || ch->q.size() < ch->cap)) {
        if (cancel_requested(cancel)) throw Cancelled{};
        ch->cv.wait_for(lk, std::chrono::milliseconds(25));
    }
    if (ch->closed) throw ChannelClosed{};
    ch->q.push_back(v);
    ch->cv.notify_all();
}
static bool chan_is_closed(const V &c) {
    if (c.kind != V::CHAN) return true;
    if (!c.chan) return true;
    std::lock_guard<std::mutex> lk(c.chan->mu);
    return c.chan->closed;
}

static V chan_recv(const V &c, CancelToken *cancel = nullptr) {
    if (c.kind != V::CHAN || c.tx) die("recv needs an Rx");
    auto ch = c.chan;
    if (!ch) return V::payload_of({});
    std::unique_lock<std::mutex> lk(ch->mu);
    while (!(ch->closed || !ch->q.empty())) {
        if (cancel_requested(cancel)) throw Cancelled{};
        ch->cv.wait_for(lk, std::chrono::milliseconds(25));
    }
    if (ch->q.empty()) throw ChannelClosed{};
    V v = ch->q.front();
    ch->q.pop_front();
    ch->cv.notify_all();
    return v;
}
static void chan_close(const V &c) {
    if (c.kind != V::CHAN) die("close needs a channel");
    auto ch = c.chan;
    if (!ch) return;
    std::lock_guard<std::mutex> lk(ch->mu);
    ch->closed = true;
    ch->cv.notify_all();
}

// ------------------------------------------------------------------ systems
struct Instance {
    std::string label;
    J on_failure;
    std::unordered_map<std::string, V> env;
    std::unordered_map<std::string, std::vector<V>> rings;
    std::vector<std::thread> tasks;
    std::atomic<long> iterations{0};
    std::atomic<bool> stopped{false};
    std::atomic<bool> failed{false};
    std::mutex mu;
    std::condition_variable cv;
    V tick_tx;
    std::vector<V> tick_rx;
    std::string family;
    std::vector<long> pos;
};

struct SystemVal {
    std::string name;
    std::vector<std::shared_ptr<Instance>> instances;
    std::map<std::string, std::vector<std::shared_ptr<Instance>>> families;
    std::map<std::string, std::map<std::string, std::shared_ptr<Instance>>> indexed;
    J def;
    std::mutex mu;
};
static std::vector<std::shared_ptr<SystemVal>> g_systems;

struct ScopeFrame {
    std::vector<std::shared_ptr<Channel>> chans;
    std::vector<std::shared_ptr<SystemVal>> systems;
};

struct Art {
    std::string kind, name, text;
    long rows = 0, cols = 0;
    std::vector<double> x, y, values;
};

static void bind_pattern(const J &pat, const V &v, std::unordered_map<std::string, V> &env) {
    if (pat["_"].str == "PWild") return;
    if (pat["_"].str == "PTuple") {
        if (v.kind != V::TUPLE || v.tuple.size() != pat["items"].arr.size()) die("tuple bind");
        for (size_t i = 0; i < v.tuple.size(); i++)
            bind_pattern(pat["items"].arr[i], v.tuple[i], env);
        return;
    }
    env[pat["id"].str] = v;
}

static std::string safe_artifact_name(const std::string &name) {
    // NUL truncates filenames at the C API boundary and ':' opens an NTFS
    // alternate data stream on Windows (PR.md 3A "Pi artifact names").
    if (name.empty() || name.size() > 4096 || name == "." || name == ".." ||
        name.find('\0') != std::string::npos || name.find(':') != std::string::npos ||
        name.find('/') != std::string::npos || name.find('\\') != std::string::npos ||
        name.find("..") != std::string::npos)
        die("unsafe artifact name: " + name);
    return name;
}

// ------------------------------------------------------------------ interpreter
struct Interp {
    J plan;
    std::unordered_map<std::string, V> env;
    std::unordered_map<std::string, std::string> workers;
    std::unordered_map<std::string, long> op_index;
    std::vector<std::string> op_rets;
    std::unordered_map<std::string, bool> gpu;
    std::unordered_map<std::string, std::pair<uint64_t, uint64_t>> schema;
    std::string outdir;
    std::string last_name;
    std::vector<double> last_frame;
    long last_rows = 0, last_cols = 0;
    std::ofstream frame_file;
    long frame_count = 0;
    std::string frame_name;
    std::vector<Art> arts;

    explicit Interp(J p) : plan(std::move(p)) {
        for (auto &kv : plan["workers"].obj) workers[kv.first] = kv.second.as_str();
        if (plan["gpu"].kind == J::OBJ) {
            for (auto &kv : plan["gpu"].obj) gpu[kv.first] = kv.second.num != 0;
        }
        if (plan["schemas"].kind == J::OBJ) {
            for (auto &kv : plan["schemas"].obj) {
                std::string h = kv.second.as_str();
                if (h.size() == 32) {
                    try {
                        uint64_t hi = std::stoull(h.substr(0, 16), nullptr, 16);
                        uint64_t lo = std::stoull(h.substr(16), nullptr, 16);
                        schema[kv.first] = {hi, lo};
                    } catch (...) {
                        die("invalid schema hash in plan");
                    }
                }
            }
        }
        auto &prots = plan["protocols"].obj;
        if (prots.size() == 1) {
            auto &ops = prots.begin()->second["ops"].arr;
            for (size_t i = 0; i < ops.size(); i++) op_index[ops[i].as_str()] = (long)i;
            for (auto &r : prots.begin()->second["rets"].arr) op_rets.push_back(r.as_str());
        }
        outdir = plan["out"].as_str();
        if (!outdir.empty()) fs::create_directories(outdir);
    }

    V eval(const J &n);
    void stmt(const J &n);
    void run(const J &body);
    V call(const J &n);
    V instantiate_system(const std::string &name, const std::vector<V> &args);
    V sys_member(const V &obj, const std::string &field);
    V sys_index(const V &obj, const std::vector<long> &pos);
    void system_push(const V &op);
    int handle_instance_failure(const PiError &e);
    std::shared_ptr<Instance> current_inst;
    std::vector<ScopeFrame> scopes;
    Worker *last_error_worker = nullptr;
    std::unordered_map<Worker *, int> restart_counts;
    // P2: set for a branch running under `timeout`; checked at every blocking
    // point so the branch can be cancelled and joined (never detached).
    std::shared_ptr<CancelToken> cancel;

    void check_cancel() const {
        if (cancel_requested(cancel.get())) throw Cancelled{};
    }
    void cancellable_sleep(double seconds) const;
    void parallel_for(size_t n, const std::function<void(size_t)> &body);

    void emit_text(const std::string &s);
    void emit_series(const std::string &name, const V &x, const V &y);
    void emit_points(const std::string &name, const V &x, const V &y);
    void emit_bars(const std::string &name, const V &x, const V &y);
    void emit_grid(const std::string &name, const V &v, long rows, long cols);
    void emit_frame(const std::string &name, const V &v, long rows, long cols);
    void finish();
    V pool_worker(const J &call);
};

void Interp::cancellable_sleep(double seconds) const {
    if (!(seconds > 0)) { check_cancel(); return; }
    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                        std::chrono::duration<double>(seconds));
    for (;;) {
        check_cancel();
        auto now = std::chrono::steady_clock::now();
        if (now >= deadline) return;
        auto step = std::min(std::chrono::duration<double>(0.02),
                             std::chrono::duration<double>(deadline - now));
        std::this_thread::sleep_for(step);
    }
}

/* P3: bounded parallel iteration.  `par for` used to create one std::thread per
   element, which aborts near the OS thread limit (`std::terminate` from
   `~vector<thread>`); a fixed pool of hardware threads is used instead. */
void Interp::parallel_for(size_t n, const std::function<void(size_t)> &body) {
    if (n == 0) return;
    unsigned hw = std::thread::hardware_concurrency();
    size_t pool = std::min<size_t>(n, std::max(1u, hw ? hw : (unsigned)MAX_PAR_WORKERS));
    pool = std::min<size_t>(pool, MAX_PAR_WORKERS);
    std::atomic<size_t> next{0};
    std::atomic<bool> failed{false};
    std::exception_ptr first;
    std::mutex emu;
    auto run = [&]() {
        for (;;) {
            if (failed.load()) return;
            size_t i = next.fetch_add(1);
            if (i >= n) return;
            try {
                body(i);
            } catch (...) {
                {
                    std::lock_guard<std::mutex> lk(emu);
                    if (!first) first = std::current_exception();
                }
                failed.store(true);
                return;
            }
        }
    };
    std::vector<std::thread> ts;
    size_t started = 0;
    try {
        ts.reserve(pool);
        for (size_t k = 0; k < pool; k++) { ts.emplace_back(run); started++; }
    } catch (...) {
        if (started == 0) run();
    }
    for (auto &t : ts) {
        if (t.joinable()) t.join();
    }
    if (first) std::rethrow_exception(first);
}

void Interp::emit_text(const std::string &s) {
    std::printf("%s\n", s.c_str());
    Art a; a.kind = "text"; a.name = "text"; a.text = s; arts.push_back(a);
}

void Interp::emit_series(const std::string &name0, const V &x, const V &y) {
    std::string name = safe_artifact_name(name0);
    Art a; a.kind = "series"; a.name = name; a.x = x.payload; a.y = y.payload;
    arts.push_back(a);
    if (outdir.empty()) { std::printf("series %s\n", name.c_str()); return; }
    std::ofstream f(fs::path(outdir) / (name + ".csv"));
    f << "x,y\n";
    size_t n = std::min(x.payload.size(), y.payload.size());
    for (size_t i = 0; i < n; i++) f << x.payload[i] << "," << y.payload[i] << "\n";
}

void Interp::emit_points(const std::string &name0, const V &x, const V &y) {
    std::string name = safe_artifact_name(name0);
    Art a; a.kind = "points"; a.name = name; a.x = x.payload; a.y = y.payload;
    arts.push_back(a);
    if (outdir.empty()) { std::printf("points %s: %zu\n", name.c_str(), a.x.size()); return; }
    std::ofstream f(fs::path(outdir) / (name + ".csv"));
    f << "x,y\n";
    size_t n = std::min(a.x.size(), a.y.size());
    for (size_t i = 0; i < n; i++) f << a.x[i] << "," << a.y[i] << "\n";
}

void Interp::emit_bars(const std::string &name0, const V &x, const V &y) {
    std::string name = safe_artifact_name(name0);
    Art a; a.kind = "bars"; a.name = name; a.x = x.payload; a.y = y.payload;
    arts.push_back(a);
    if (outdir.empty()) { std::printf("bars %s: %zu\n", name.c_str(), a.x.size()); return; }
    std::ofstream f(fs::path(outdir) / (name + ".csv"));
    f << "x,y\n";
    size_t n = std::min(a.x.size(), a.y.size());
    for (size_t i = 0; i < n; i++) f << a.x[i] << "," << a.y[i] << "\n";
}

void Interp::emit_grid(const std::string &name0, const V &v, long rows, long cols) {
    std::string name = safe_artifact_name(name0);
    if (rows < 0 || cols < 0 || (rows && cols > (long)(1 << 26) / rows)) die("grid dimensions invalid");
    Art a; a.kind = "grid"; a.name = name; a.rows = rows; a.cols = cols; a.values = v.payload;
    arts.push_back(a);
    if (outdir.empty()) { std::printf("grid %s: %ldx%ld\n", name.c_str(), rows, cols); return; }
    std::ofstream f(fs::path(outdir) / (name + ".csv"));
    f << "row,col,value\n";
    for (long r = 0; r < rows; r++)
        for (long c = 0; c < cols; c++) {
            size_t i = (size_t)(r * cols + c);
            if (i < v.payload.size()) f << r << "," << c << "," << v.payload[i] << "\n";
        }
}

void Interp::emit_frame(const std::string &name0, const V &v, long rows, long cols) {
    std::string name = safe_artifact_name(name0);
    if (rows < 0 || cols < 0 || (rows && cols > (long)(1 << 26) / rows)) die("frame dimensions invalid");
    frame_count++;
    if (!outdir.empty() && !frame_file.is_open()) {
        frame_name = name;
        frame_file.open(fs::path(outdir) / (name + ".frames.bin"), std::ios::binary);
        uint32_t hdr[3] = {(uint32_t)cols, (uint32_t)rows, 0};
        frame_file.write((char *)hdr, sizeof(hdr));
    }
    if (frame_file.is_open()) {
        std::vector<float> f;
        f.reserve(v.payload.size());
        for (double x : v.payload) f.push_back((float)x);
        frame_file.write((char *)f.data(), (std::streamsize)(f.size() * sizeof(float)));
    }
    if (frame_count == 1 || frame_count % 10 == 0)
        std::printf("animation %s: frame %ld\n", name.c_str(), frame_count);
    last_name = name; last_frame = v.payload; last_rows = rows; last_cols = cols;
}

static std::string esc(const std::string &s) {
    std::string o;
    for (char c : s) {
        if (c == '&') o += "&amp;";
        else if (c == '<') o += "&lt;";
        else if (c == '>') o += "&gt;";
        else if (c == '"') o += "&quot;";
        else o += c;
    }
    return o;
}

static std::string svg_line(const Art &a) {
    if (a.x.size() < 2) return "";
    double xmin = a.x[0], xmax = a.x[0], ymin = a.y[0], ymax = a.y[0];
    for (double v : a.x) { xmin = std::min(xmin, v); xmax = std::max(xmax, v); }
    for (double v : a.y) { ymin = std::min(ymin, v); ymax = std::max(ymax, v); }
    if (xmax == xmin) xmax = xmin + 1;
    if (ymax == ymin) ymax = ymin + 1;
    const int W = 640, H = 360, P = 40;
    std::ostringstream o;
    o << "<svg xmlns='http://www.w3.org/2000/svg' width='" << W << "' height='" << H
      << "' viewBox='0 0 " << W << " " << H << "'>"
      << "<rect width='100%' height='100%' fill='white'/><polyline fill='none' stroke='#1f77b4' stroke-width='2' points='";
    size_t n = std::min(a.x.size(), a.y.size());
    for (size_t i = 0; i < n; i++) {
        double px = P + (a.x[i] - xmin) / (xmax - xmin) * (W - 2 * P);
        double py = H - P - (a.y[i] - ymin) / (ymax - ymin) * (H - 2 * P);
        o << px << "," << py << " ";
    }
    o << "'/></svg>";
    return o.str();
}

static std::string svg_points(const Art &a) {
    size_t n = std::min(a.x.size(), a.y.size());
    if (!n) return "";
    const int W = 640, H = 360, P = 40;
    double xmin = a.x[0], xmax = a.x[0], ymin = a.y[0], ymax = a.y[0];
    for (size_t i = 1; i < n; i++) {
        xmin = std::min(xmin, a.x[i]); xmax = std::max(xmax, a.x[i]);
        ymin = std::min(ymin, a.y[i]); ymax = std::max(ymax, a.y[i]);
    }
    if (xmax == xmin) xmax = xmin + 1;
    if (ymax == ymin) ymax = ymin + 1;
    std::ostringstream o;
    o << "<svg xmlns='http://www.w3.org/2000/svg' width='" << W << "' height='" << H
      << "' viewBox='0 0 " << W << " " << H << "'>"
      << "<rect width='100%' height='100%' fill='white'/>"
      << "<line x1='" << P << "' y1='" << (H-P) << "' x2='" << (W-P) << "' y2='" << (H-P)
      << "' stroke='#888'/><line x1='" << P << "' y1='" << P << "' x2='" << P << "' y2='"
      << (H-P) << "' stroke='#888'/>";
    size_t stride = std::max<size_t>(1, n / 5000);
    for (size_t i = 0; i < n; i += stride) {
        double px = P + (a.x[i] - xmin) / (xmax - xmin) * (W - 2 * P);
        double py = H - P - (a.y[i] - ymin) / (ymax - ymin) * (H - 2 * P);
        o << "<circle cx='" << px << "' cy='" << py << "' r='1.5' fill='#1f77b4'/>";
    }
    o << "</svg>";
    return o.str();
}

static std::string svg_bars(const Art &a) {
    size_t n = std::min(a.x.size(), a.y.size());
    if (!n) return "";
    const int W = 640, H = 360, P = 40;
    double ymin = 0.0, ymax = 0.0;
    for (size_t i = 0; i < n; i++) ymax = std::max(ymax, a.y[i]);
    if (ymax <= 0) ymax = 1;
    std::ostringstream o;
    o << "<svg xmlns='http://www.w3.org/2000/svg' width='" << W << "' height='" << H
      << "' viewBox='0 0 " << W << " " << H << "'>"
      << "<rect width='100%' height='100%' fill='white'/>"
      << "<line x1='" << P << "' y1='" << (H-P) << "' x2='" << (W-P) << "' y2='" << (H-P)
      << "' stroke='#888'/>"
      << "<line x1='" << P << "' y1='" << P << "' x2='" << P << "' y2='" << (H-P)
      << "' stroke='#888'/>";
    double bw = (double)(W - 2 * P) / (double)n;
    for (size_t i = 0; i < n; i++) {
        double h = (a.y[i] - ymin) / (ymax - ymin) * (H - 2 * P);
        double x = P + i * bw + 1;
        double y = H - P - h;
        o << "<rect x='" << x << "' y='" << y << "' width='" << std::max(1.0, bw - 2)
          << "' height='" << h << "' fill='#1f77b4'/>";
    }
    o << "</svg>";
    return o.str();
}

static std::string svg_heat(const Art &a) {
    if (a.rows <= 0 || a.cols <= 0 || a.values.empty()) return "";
    const int MAX = 160;
    int stride = std::max(1, (int)std::max(a.rows, a.cols) / MAX);
    int rr = (int)((a.rows + stride - 1) / stride), cc = (int)((a.cols + stride - 1) / stride);
    double lo = a.values[0], hi = a.values[0];
    for (double v : a.values) { lo = std::min(lo, v); hi = std::max(hi, v); }
    if (hi <= lo) hi = lo + 1e-9;
    std::ostringstream o;
    o << "<svg xmlns='http://www.w3.org/2000/svg' width='" << cc * 3 << "' height='" << rr * 3
      << "' viewBox='0 0 " << cc << " " << rr << "' shape-rendering='crispEdges'><rect width='100%' height='100%' fill='#0b3d91'/>";
    for (int r = 0; r < rr; r++)
        for (int c = 0; c < cc; c++) {
            long src = (long)(r * stride) * a.cols + (long)(c * stride);
            if (src < 0 || src >= (long)a.values.size()) continue;
            double q = (a.values[(size_t)src] - lo) / (hi - lo);
            int red = (int)(255 * std::max(0.0, std::min(1.0, 1.6 * q - 0.15)));
            int grn = (int)(255 * std::max(0.0, std::min(1.0, 1.5 - 2.0 * std::fabs(q - 0.55))));
            int blu = (int)(255 * std::max(0.0, std::min(1.0, 1.25 - 1.8 * q)));
            o << "<rect x='" << c << "' y='" << r << "' width='1' height='1' fill='rgb("
              << red << "," << grn << "," << blu << ")'/>";
        }
    o << "</svg>";
    return o.str();
}

static void heat_color(double q, int &r, int &g, int &b) {
    q = std::max(0.0, std::min(1.0, q));
    r = (int)(255 * std::max(0.0, std::min(1.0, 1.6 * q - 0.15)));
    g = (int)(255 * std::max(0.0, std::min(1.0, 1.5 - 2.0 * std::fabs(q - 0.55))));
    b = (int)(255 * std::max(0.0, std::min(1.0, 1.25 - 1.8 * q)));
}

static void put_u32(std::ofstream &f, uint32_t v) {
    f.write((const char *)&v, sizeof(v));
}

static void put_u16(std::ofstream &f, uint16_t v) {
    f.write((const char *)&v, sizeof(v));
}

/* Minimal 24-bit BMP writer: no image library, every browser can display it. */
static bool write_bmp(const fs::path &path, int w, int h,
                      const std::vector<float> &vals, double lo, double hi) {
    if (w <= 0 || h <= 0 || vals.size() < (size_t)w * (size_t)h) return false;
    const int row = (w * 3 + 3) & ~3;
    const uint32_t image = (uint32_t)row * (uint32_t)h;
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.put('B'); f.put('M');
    put_u32(f, 54u + image);
    put_u16(f, 0); put_u16(f, 0); put_u32(f, 54);
    put_u32(f, 40); put_u32(f, (uint32_t)w); put_u32(f, (uint32_t)h);
    put_u16(f, 1); put_u16(f, 24); put_u32(f, 0);
    put_u32(f, image); put_u32(f, 0); put_u32(f, 0);
    put_u32(f, 0); put_u32(f, 0);
    std::vector<unsigned char> line((size_t)row, 0);
    for (int y = h - 1; y >= 0; --y) {
        for (int x = 0; x < w; ++x) {
            double v = vals[(size_t)y * (size_t)w + (size_t)x];
            double q = (hi > lo) ? (v - lo) / (hi - lo) : 0.0;
            int r, g, b; heat_color(q, r, g, b);
            line[(size_t)x * 3 + 0] = (unsigned char)b;
            line[(size_t)x * 3 + 1] = (unsigned char)g;
            line[(size_t)x * 3 + 2] = (unsigned char)r;
        }
        f.write((const char *)line.data(), (std::streamsize)line.size());
    }
    return (bool)f;
}

struct PreviewInfo {
    long count = 0;
    long stride = 1;
    long source_count = 0;
    int width = 0, height = 0;
};

/* Convert the binary animation frames into a bounded BMP preview sequence.
   Full per-frame BMPs at 512x512 would be hundreds of megabytes for a long
   run, so the report shows at most 120 frames, downsampled to at most 256px.
   The raw .frames.bin file remains the full-resolution record. */
static PreviewInfo write_frame_images(const fs::path &outdir, const std::string &name,
                                      long rows, long cols, long count) {
    PreviewInfo info;
    fs::path src = outdir / (name + ".frames.bin");
    std::ifstream f(src, std::ios::binary);
    if (!f || count <= 0) return info;
    uint32_t w = 0, h = 0, n = 0;
    f.read((char *)&w, 4); f.read((char *)&h, 4); f.read((char *)&n, 4);
    if (!f || w == 0 || h == 0 || n == 0) return info;
    long use_n = std::min<long>((long)n, count);
    std::vector<float> frame((size_t)w * (size_t)h);
    double lo = 0.0, hi = 0.0;
    bool first = true;
    for (long i = 0; i < use_n; ++i) {
        f.read((char *)frame.data(), (std::streamsize)(frame.size() * sizeof(float)));
        if (!f) return info;
        for (float v : frame) {
            if (first) { lo = hi = v; first = false; }
            else { lo = std::min(lo, (double)v); hi = std::max(hi, (double)v); }
        }
    }
    if (hi <= lo) hi = lo + 1e-9;

    const long MAX_PREVIEW = 120;
    const int MAX_EDGE = 256;
    info.stride = std::max<long>(1, (use_n + MAX_PREVIEW - 1) / MAX_PREVIEW);
    info.count = (use_n + info.stride - 1) / info.stride;
    info.source_count = use_n;
    double scale = std::min(1.0, (double)MAX_EDGE / (double)std::max(w, h));
    info.width = std::max(1, (int)std::lround(w * scale));
    info.height = std::max(1, (int)std::lround(h * scale));

    f.clear(); f.seekg(12, std::ios::beg);
    fs::create_directories(outdir / "frames");
    std::vector<float> preview_px((size_t)info.width * (size_t)info.height);
    long out = 0;
    for (long i = 0; i < use_n; ++i) {
        f.read((char *)frame.data(), (std::streamsize)(frame.size() * sizeof(float)));
        if (!f) return info;
        if (i % info.stride != 0 && i + 1 != use_n) continue;
        for (int y = 0; y < info.height; ++y) {
            long sy = (long)((double)y * h / info.height);
            for (int x = 0; x < info.width; ++x) {
                long sx = (long)((double)x * w / info.width);
                preview_px[(size_t)y * info.width + x] =
                    frame[(size_t)sy * w + (size_t)sx];
            }
        }
        std::ostringstream nm;
        nm << name << "_preview_" << std::setw(6) << std::setfill('0') << out++ << ".bmp";
        if (!write_bmp(outdir / "frames" / nm.str(), info.width, info.height,
                       preview_px, lo, hi))
            return PreviewInfo{};
    }
    info.count = out;
    return info;
}

static std::string js_escape(const std::string &s) {
    std::string o;
    for (char c : s) {
        if (c == '\\' || c == '"') { o += '\\'; o += c; }
        else if (c == '\n') o += "\\n";
        else if (c == '\r') o += "\\r";
        else if (c == '<') o += "\\x3c";
        else if (c == '>') o += "\\x3e";
        else o += c;
    }
    return o;
}

void Interp::finish() {
    // Finalize the raw frame stream before generating the report.  The report
    // can then read it back and turn frames into ordinary images.
    if (frame_file.is_open()) {
        frame_file.seekp((std::streamoff)8);
        uint32_t n = (uint32_t)frame_count;
        frame_file.write((char *)&n, sizeof(n));
        frame_file.close();
        std::printf("animation frames: %ld\n", frame_count);
    }
    PreviewInfo preview;
    if (!outdir.empty() && frame_count > 0 && !frame_name.empty()) {
        preview = write_frame_images(fs::path(outdir), frame_name,
                                     last_rows, last_cols, frame_count);
    }
    bool have_frames = preview.count > 0;

    if (!outdir.empty()) {
        if (!last_name.empty()) {
            Art a; a.kind = "grid"; a.name = last_name + "_final";
            a.rows = last_rows; a.cols = last_cols; a.values = last_frame;
            arts.push_back(a);
        }
        std::vector<std::string> sections;
        std::ostringstream json;
        json << "{\"artifacts\":[";
        bool first = true;
        for (auto &a : arts) {
            if (a.kind == "text") continue;
            if (a.kind == "series") {
                std::ofstream svg(fs::path(outdir) / (a.name + ".svg"));
                svg << svg_line(a);
                if (!first) json << ",";
                first = false;
                json << "{\"kind\":\"series\",\"name\":\"" << esc(a.name) << "\"}";
                sections.push_back("<section><h2>" + esc(a.name) +
                                   " <small>series</small></h2>" +
                                   svg_line(a) + "</section>");
            } else if (a.kind == "points" || a.kind == "bars") {
                std::string svg_text = a.kind == "points" ? svg_points(a) : svg_bars(a);
                std::ofstream svg(fs::path(outdir) / (a.name + ".svg"));
                svg << svg_text;
                if (!first) json << ",";
                first = false;
                json << "{\"kind\":\"" << (a.kind == "points" ? "points" : "bars")
                     << "\",\"name\":\"" << esc(a.name) << "\"}";
                sections.push_back("<section><h2>" + esc(a.name) +
                                   " <small>" + a.kind + "</small></h2>" +
                                   svg_text + "</section>");
            } else if (a.kind == "grid") {
                std::ofstream svg(fs::path(outdir) / (a.name + ".svg"));
                svg << svg_heat(a);
                if (!first) json << ",";
                first = false;
                json << "{\"kind\":\"grid\",\"name\":\"" << esc(a.name) << "\"}";
                sections.push_back("<section><h2>" + esc(a.name) +
                                   " <small>grid</small></h2>" +
                                   svg_heat(a) + "</section>");
            }
        }
        json << "]}";

        std::string player;
        std::string player_js;
        if (have_frames) {
            std::ostringstream frames;
            for (long i = 0; i < preview.count; ++i) {
                if (i) frames << ",";
                std::ostringstream nm;
                nm << frame_name << "_preview_" << std::setw(6) << std::setfill('0')
                   << i << ".bmp";
                frames << "\"frames/" << js_escape(nm.str()) << "\"";
            }
            std::ostringstream note;
            note << "preview " << preview.count << " of " << preview.source_count
                 << " frames, stride " << preview.stride
                 << ", " << preview.width << "x" << preview.height;
            player =
                "<section class='player'><h2>" + esc(frame_name) +
                " <small>animation</small></h2>"
                "<p class='note'>" + note.str() + " (full-resolution sequence: " +
                esc(frame_name) + ".frames.bin)</p>"
                "<img id='frame' alt='animation frame' src='frames/" +
                esc(frame_name) + "_preview_000000.bmp'>"
                "<div class='controls'>"
                "<button id='prev' title='previous frame'>&#9664;</button>"
                "<button id='play'>Play</button>"
                "<button id='next' title='next frame'>&#9654;</button>"
                "<input id='scrub' type='range' min='0' max='" +
                std::to_string(preview.count - 1) + "' value='0'>"
                "<span id='counter'>1/" + std::to_string(preview.count) + "</span>"
                "</div></section>";
            player_js =
                "<script>"
                "(function(){const frames=[" + frames.str() + "];"
                "let i=0,timer=null;const img=document.getElementById('frame');"
                "const scrub=document.getElementById('scrub');"
                "const counter=document.getElementById('counter');"
                "function show(k){if(!frames.length)return;i=(k+frames.length)%frames.length;"
                "img.src=frames[i];scrub.value=i;"
                "counter.textContent=(i+1)+'/'+frames.length;}"
                "function play(){if(timer)return;timer=setInterval(function(){"
                "show(i+1);if(i+1>=frames.length)playStop();},100);"
                "document.getElementById('play').textContent='Pause';}"
                "function playStop(){if(timer){clearInterval(timer);timer=null;}"
                "document.getElementById('play').textContent='Play';}"
                "document.getElementById('play').onclick=function(){timer?playStop():play();};"
                "document.getElementById('prev').onclick=function(){playStop();show(i-1);};"
                "document.getElementById('next').onclick=function(){playStop();show(i+1);};"
                "scrub.oninput=function(){playStop();show(+scrub.value);};"
                "window.addEventListener('load',function(){if(frames.length>1)play();});"
                "})();"
                "</script>";
        }

        std::ofstream rf(fs::path(outdir) / "results.json");
        rf << json.str();

        std::ofstream html(fs::path(outdir) / "report.html");
        html << "<!doctype html><html><head><meta charset='utf-8'>"
             << "<title>Pi report</title>"
             << "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;"
                "max-width:1100px}svg{border:1px solid #ddd;max-width:100%;height:auto}"
                ".player img{max-width:100%;image-rendering:auto;border:1px solid #ddd}"
                ".controls{display:flex;gap:8px;align-items:center;margin-top:8px}"
                ".controls input{flex:1;min-width:160px}"
                "button{padding:6px 12px;font:inherit}"
                ".note{color:#555;font-size:14px;margin:4px 0}"
                "small{color:#666;font-weight:normal}</style></head><body>"
             << "<h1>Pi report (C++ runtime)</h1>";
        for (auto &s : sections) html << s;
        html << player;
        for (auto &a : arts)
            if (a.kind == "text")
                html << "<section><h2>text</h2><pre>" << esc(a.text) << "</pre></section>";
        if (frame_count) html << "<p>animation frames: " << frame_count << "</p>";
        html << player_js << "</body></html>";
        std::printf("pi-cpp: report %s\n", (fs::path(outdir) / "report.html").string().c_str());
    }

    if (outdir.empty() || last_name.empty()) return;
    std::ofstream f(fs::path(outdir) / (last_name + ".final.csv"));
    f << "row,col,value\n";
    for (long r = 0; r < last_rows; r++)
        for (long c = 0; c < last_cols; c++) {
            size_t i = (size_t)(r * last_cols + c);
            if (i < last_frame.size()) f << r << "," << c << "," << last_frame[i] << "\n";
        }
}

V Interp::pool_worker(const J &call) {
    const J &args = call["args"];
    V sr = eval(args.arr.at(0));
    std::string src = sr.str;
    auto it = workers.find(src);
    if (it == workers.end()) die("worker not in plan: " + src);
    bool use_gpu = false;
    // Pool(..., {gpus: 1}) is encoded only when the frontend adds it; default false.
    auto git = gpu.find(src);
    if (git != gpu.end()) use_gpu = git->second;
    long count = 1;
    if (args.arr.size() > 1) count = as_int(eval(args.arr[1]));
    V out; out.kind = V::TUPLE;
    for (long i = 0; i < count; i++) {
        auto sit = schema.find(src);
        Worker *w = spawn_worker(it->second, use_gpu, src,
                                 sit == schema.end() ? 0 : sit->second.first,
                                 sit == schema.end() ? 0 : sit->second.second,
                                 this->cancel.get());
        V v; v.kind = V::WORKER; v.worker = w;
        out.tuple.push_back(v);
    }
    if (count == 1) return out.tuple[0];
    return out;
}

static std::vector<long> as_positions(const V &v) {
    std::vector<long> out;
    if (v.kind == V::TUPLE) {
        for (auto &x : v.tuple) out.push_back(as_int(x));
    } else out.push_back(as_int(v));
    return out;
}
static std::string pos_key(const std::vector<long> &p) {
    std::string s;
    for (size_t i = 0; i < p.size(); i++) {
        if (i) s += ",";
        s += std::to_string(p[i]);
    }
    return s;
}

V Interp::sys_member(const V &obj, const std::string &field) {
    auto sys = obj.sys;
    if (!sys) die("member on null system");
    if (obj.kind == V::INSTANCE) {
        auto it = obj.inst->env.find(field);
        if (it != obj.inst->env.end()) return it->second;
        die("instance has no member " + field);
    }
    if (field == "close") { V v; v.kind = V::SYSCALL; v.sys = sys; v.field = "close"; return v; }
    if (sys->indexed.count(field)) { V v; v.kind = V::FAMILY; v.sys = sys; v.field = field; return v; }
    // exposed op or unknown family
    if (sys->def["expose"].has(field)) { V v; v.kind = V::SYSCALL; v.sys = sys; v.field = field; return v; }
    if (sys->families.count(field)) { V v; v.kind = V::FAMILY; v.sys = sys; v.field = field; return v; }
    die("system has no member " + field);
}

V Interp::sys_index(const V &obj, const std::vector<long> &pos) {
    auto sys = obj.sys;
    auto fit = sys->indexed.find(obj.field);
    if (fit == sys->indexed.end()) die("no indexed family " + obj.field);
    auto it = fit->second.find(pos_key(pos));
    if (it == fit->second.end()) die("no instance " + pos_key(pos));
    V v; v.kind = V::INSTANCE; v.inst = it->second; v.sys = sys; return v;
}

void Interp::system_push(const V &op) {
    auto sys = op.sys;
    if (op.field == "close") {
        for (auto &i : sys->instances) {
            i->stopped.store(true);
            for (auto &kv : i->env)
                if (kv.second.kind == V::WORKER && kv.second.worker) shutdown_worker(kv.second.worker);
            if (i->tick_tx.kind == V::CHAN) chan_close(i->tick_tx);
            if (i->tick_rx.size()) chan_close(i->tick_rx[0]);
            for (auto &r : i->rings) for (auto &e : r.second) if (e.kind == V::CHAN) chan_close(e);
            { std::lock_guard<std::mutex> lk(i->mu); i->cv.notify_all(); }
        }
        for (auto &i : sys->instances)
            for (auto &t : i->tasks) if (t.joinable()) t.join();
        return;
    }
    const J &body = sys->def["expose"][op.field];
    if (body["_"].str != "Call" || body["fn"]["id"].str != "push") die("unsupported expose body");
    const J &mem = body["args"].arr.at(0);
    std::string fam = mem["obj"]["id"].str;
    std::string port = mem["name"].str;
    auto fit = sys->families.find(fam);
    if (fit == sys->families.end()) die("system has no family " + fam);
    auto &insts = fit->second;
    std::vector<long> before;
    for (auto &i : insts) before.push_back(i->iterations.load());
    for (auto &i : insts) {
        if (port == "tick" && i->tick_tx.kind == V::CHAN) chan_send(i->tick_tx, V());
    }
    for (size_t k = 0; k < insts.size(); k++) {
        auto &i = insts[k];
        std::unique_lock<std::mutex> lk(i->mu);
        bool ok = i->cv.wait_for(lk, std::chrono::seconds(30), [&] {
            return i->iterations.load() > before[k] || i->stopped.load() || i->failed.load();
        });
        if (!ok) throw PiError("PushTimeout");
        if (i->failed.load()) throw PiError("WorkerDied");
        if (i->stopped.load() && i->iterations.load() <= before[k]) throw PiError("Stopped");
    }
}

V Interp::instantiate_system(const std::string &name, const std::vector<V> &args) {
    const J &def = plan["systems"][name];
    if (def.kind != J::OBJ) die("unknown system " + name);
    auto sys = std::make_shared<SystemVal>();
    sys->name = name; sys->def = def;
    const J &elems = plan["elems"];
    for (size_t i = 0; i < def["vparams"].arr.size() && i < args.size(); i++)
        env[def["vparams"].arr[i].str] = args[i];
    for (const auto &idecl : def["instances"].arr) {
        std::string ename = idecl["elem"].str;
        const J &edef = elems[ename];
        std::string fam = idecl["name"].str;
        // binding ranges
        std::vector<std::vector<long>> axes;
        std::vector<std::string> names;
        for (auto &b : idecl["bindings"].arr) {
            names.push_back(b.arr[0].str);
            V r = eval(b.arr[1]);
            std::vector<long> vals;
            for (double x : r.payload) vals.push_back(checked_long(x, "instance binding"));
            if (vals.empty()) die("empty instance binding range");
            axes.push_back(vals);
        }
        if (axes.empty()) { axes.push_back({0}); names.push_back(""); }
        std::vector<size_t> idx(axes.size(), 0);
        while (true) {
            auto inst = std::make_shared<Instance>();
            inst->family = fam;
            std::vector<long> pos;
            for (size_t d = 0; d < axes.size(); d++) pos.push_back(axes[d][idx[d]]);
            inst->pos = pos;
            inst->label = fam + "[" + pos_key(pos) + "]";
            inst->on_failure = edef["on_failure"];
            for (size_t d = 0; d < names.size(); d++) if (!names[d].empty()) {
                inst->env[names[d]] = V((double)pos[d]);
                env[names[d]] = V((double)pos[d]);
            }
            std::vector<V> iargs;
            for (auto &a : idecl["args"].arr) iargs.push_back(eval(a));
            for (size_t d = 0; d < edef["vparams"].arr.size() && d < iargs.size(); d++)
                inst->env[edef["vparams"].arr[d].str] = iargs[d];
            for (size_t d = 0; d < edef["tparams"].arr.size() && d < idecl["targs"].arr.size(); d++)
                inst->env[edef["tparams"].arr[d].str] = V((double)idecl["targs"].arr[d].num);
            // workers
            for (auto &w : edef["workers"].arr) {
                std::string wn = w["name"].str;
                std::string src = idecl["using"][wn].str;
                auto wit = workers.find(src);
                if (wit == workers.end()) die("worker not built: " + src);
                auto sit = schema.find(src);
                Worker *wp = spawn_worker(wit->second, false, src,
                                          sit == schema.end() ? 0 : sit->second.first,
                                          sit == schema.end() ? 0 : sit->second.second,
                                          this->cancel.get());
                V wv; wv.kind = V::WORKER; wv.worker = wp;
                inst->env[wn] = wv;
            }
            // ports
            for (auto &p : edef["ports"].arr) {
                std::string pn = p["name"].str;
                bool out = p["dir"].str == "out";
                std::string ty = p["type"].str;
                if (ty.rfind("Ring[", 0) == 0) {
                    auto lb = ty.find('[');
                    auto lb2 = ty.find('[', lb + 1);
                    if (lb2 != std::string::npos) lb = lb2;
                    std::string inner = ty.substr(lb + 1);
                    if (!inner.empty() && inner.back() == ']') inner.pop_back();
                    int n = 0;
                    if (!inner.empty() && std::isdigit((unsigned char)inner[0]))
                        n = std::atoi(inner.c_str());
                    else if (inst->env.count(inner) && inst->env[inner].kind == V::NUM) {
                        double dv = inst->env[inner].num;
                        if (!std::isfinite(dv) || dv < 0 || dv > 1024) die("ring size out of range");
                        n = (int)dv;
                    }
                    std::vector<V> ends;
                    for (int k = 0; k < n; k++) {
                        auto ch = std::make_shared<Channel>(1);
                        V e; e.kind = V::CHAN; e.chan = ch; e.tx = out; ends.push_back(e);
                    }
                    inst->rings[pn] = ends;
                    V t; t.kind = V::TUPLE; t.tuple = ends; inst->env[pn] = t;
                } else {
                    auto ch = std::make_shared<Channel>(1);
                    V e; e.kind = V::CHAN; e.chan = ch; e.tx = out;
                    inst->env[pn] = e;
                    if (pn == "tick" && !out) {
                        inst->tick_rx.push_back(e);
                        V tx = e; tx.tx = true; inst->tick_tx = tx;
                    }
                }
            }
            sys->instances.push_back(inst);
            sys->families[fam].push_back(inst);
            sys->indexed[fam][pos_key(pos)] = inst;
            bool carry = true;
            for (size_t d = axes.size(); d-- > 0;) {
                if (++idx[d] < axes[d].size()) { carry = false; break; }
                idx[d] = 0;
            }
            if (carry) break;
        }
    }
    std::map<Instance *, std::vector<bool>> out_used, in_used;
    for (auto &inst : sys->instances) {
        if (inst->rings.count("halos_out")) out_used[inst.get()] = std::vector<bool>(inst->rings["halos_out"].size(), false);
        if (inst->rings.count("halos_in")) in_used[inst.get()] = std::vector<bool>(inst->rings["halos_in"].size(), false);
    }
    // coupling (grid2d/line)
    for (auto &c : def["couple"].arr) {
        if (c["_"].str != "Call") continue;
        std::string cn = c["fn"]["id"].str;
        int nd = (cn == "couple_grid2d_block") ? 2 : 1;
        if (cn != "couple_grid2d_block" && cn != "couple_line" && cn != "couple_ring_block") continue;
        auto fam_it = sys->families.find("c");
        if (fam_it == sys->families.end() && !sys->families.empty()) fam_it = sys->families.begin();
        if (fam_it == sys->families.end()) continue;
        auto &insts = fam_it->second;
        int dirs = (nd == 2) ? 4 : 2;
        std::vector<int> opp = {1, 0, 3, 2};
        std::vector<long> dims;
        for (auto &arg : c["args"].arr) dims.push_back(as_int(eval(arg)));
        bool periodic = c["kwargs"].has("periodic") ? as_int(eval(c["kwargs"]["periodic"])) != 0 : false;
        if (cn == "couple_ring_block") periodic = true;
        for (auto &a : insts) {
            if ((nd == 2 && (a->pos.size() < 2 || dims.size() < 2)) ||
                (nd == 1 && (a->pos.empty() || dims.empty())))
                die("coupling dimension mismatch");
            for (long d : dims) if (d <= 0) die("coupling dimension must be positive");
            auto oit = a->rings.find("halos_out");
            auto iit = a->rings.find("halos_in");
            if (oit == a->rings.end() || iit == a->rings.end() ||
                oit->second.size() < (size_t)dirs || iit->second.size() < (size_t)dirs)
                die("coupling requires halos_in/halos_out Ring[dirs]");
            for (int k = 0; k < dirs; k++) {
                std::vector<long> q = a->pos;
                if (nd == 2) {
                    static int dx[4] = {-1, 1, 0, 0}, dy[4] = {0, 0, -1, 1};
                    q[0] += dx[k]; q[1] += dy[k];
                } else {
                    q[0] += (k == 0 ? -1 : 1);
                }
                bool outside = false;
                for (size_t d = 0; d < q.size(); d++) {
                    if (d < dims.size()) {
                        if (periodic) { q[d] = (q[d] % dims[d] + dims[d]) % dims[d]; }
                        else if (q[d] < 0 || q[d] >= dims[d]) outside = true;
                    }
                }
                if (outside) continue;
                for (auto &b : insts) if (b->pos == q) {
                    auto &out_ring = a->rings["halos_out"];
                    auto bit = b->rings.find("halos_in");
                    if (bit == b->rings.end() || bit->second.size() < (size_t)dirs)
                        die("neighbour lacks halos_in Ring[dirs]");
                    auto &in_ring = bit->second;
                    auto ch = out_ring[k].chan;
                    V rx; rx.kind = V::CHAN; rx.chan = ch; rx.tx = false;
                    in_ring[opp[k]] = rx;
                    if (out_used.count(a.get())) out_used[a.get()][k] = true;
                    if (in_used.count(b.get())) in_used[b.get()][opp[k]] = true;
                    break; }
            }
        }
    }
    // Unconnected boundary/out ports become discard/null channels.
    for (auto &inst : sys->instances) {
        if (inst->rings.count("halos_out") && out_used.count(inst.get()))
            for (size_t k = 0; k < inst->rings["halos_out"].size(); k++)
                if (!out_used[inst.get()][k]) inst->rings["halos_out"][k].chan = nullptr;
        if (inst->rings.count("halos_in") && in_used.count(inst.get()))
            for (size_t k = 0; k < inst->rings["halos_in"].size(); k++)
                if (!in_used[inst.get()][k]) inst->rings["halos_in"][k].chan = nullptr;
        // Stats sinks without an exposed consumer are discards.
        auto it = inst->env.find("stats");
        if (it != inst->env.end() && it->second.kind == V::CHAN && it->second.tx)
            it->second.chan = nullptr;
    }
    // Refresh port tuples after coupling and start controllers.
    for (auto &inst : sys->instances) {
        for (auto &kv : inst->rings) {
            V t; t.kind = V::TUPLE; t.tuple = kv.second; inst->env[kv.first] = t;
        }
    }
    // controllers: use the element definition associated with the instance's work
    for (auto &inst : sys->instances) {
        // locate by family declaration
        for (const auto &idecl : def["instances"].arr) {
            if (idecl["name"].str != inst->family) continue;
            const J &edef = elems[idecl["elem"].str];
            for (auto &ctrl : edef["ctrls"].arr) {
                auto *self = this;
                inst->tasks.emplace_back([self, ctrl, inst]() mutable {
                    try {
                        Interp branch(self->plan);
                        branch.env = inst->env;
                        branch.workers = self->workers;
                        branch.op_index = self->op_index;
                        branch.gpu = self->gpu;
                        branch.outdir = self->outdir;
                        branch.current_inst = inst;
                        while (true) {
                            try {
                                branch.stmt(ctrl["body"]);
                                break;
                            } catch (const RestartController &) {
                                branch.env = inst->env;
                            }
                        }
                    } catch (...) {
                        inst->failed.store(true);
                        { std::lock_guard<std::mutex> lk(inst->mu); inst->cv.notify_all(); }
                    }
                });
            }
        }
    }
    g_systems.push_back(sys);
    if (!scopes.empty()) scopes.back().systems.push_back(sys);
    V v; v.kind = V::SYSTEM; v.sys = sys; return v;
}

V Interp::call(const J &n) {
    const J &fn = n["fn"];
    std::string fname = fn["id"].str;
    const J &args = n["args"];
    if (fn["_"].str == "Member") {
        V fv = eval(fn);
        if (fv.kind == V::WORKER || fv.kind == V::INSTANCE || fv.kind == V::TUPLE) return fv;
        if (fv.kind == V::SYSCALL) { system_push(fv); return V(); }
        die("unsupported member call");
    }
    if (plan["systems"].has(fname)) {
        std::vector<V> a;
        for (auto &x : args.arr) a.push_back(eval(x));
        return instantiate_system(fname, a);
    }
    if (fname == "call") {
        V w = eval(args.arr.at(0));
        V opv = eval(args.arr.at(1));
        std::string op = opv.str;
        auto oi = op_index.find(op);
        if (oi == op_index.end()) die("unknown op " + op);
        std::vector<V> a;
        for (size_t i = 2; i < args.arr.size(); i++) a.push_back(eval(args.arr[i]));
        if (w.kind != V::WORKER) die("call target is not a worker");
        std::string ret = (oi->second < (long)op_rets.size()) ? op_rets[(size_t)oi->second] : "";
        return worker_call(w.worker, oi->second, a, ret, this->cancel.get());
    }
    if (fname == "chan") {
        long cap = args.arr.empty() ? 1 : as_int(eval(args.arr[0]));
        if (cap < 1) die("channel capacity < 1");
        auto ch = std::make_shared<Channel>((size_t)cap);
        if (!scopes.empty()) scopes.back().chans.push_back(ch);
        V tx; tx.kind = V::CHAN; tx.chan = ch; tx.tx = true;
        V rx; rx.kind = V::CHAN; rx.chan = ch; rx.tx = false;
        V t; t.kind = V::TUPLE; t.tuple = {tx, rx};
        return t;
    }
    if (fname == "send") { chan_send(eval(args.arr[0]), eval(args.arr[1]), this->cancel.get()); return V(); }
    if (fname == "recv") return chan_recv(eval(args.arr[0]), this->cancel.get());
    if (fname == "close") { chan_close(eval(args.arr[0])); return V(); }
    if (fname == "closed") { V c = eval(args.arr[0]); return V(chan_is_closed(c) ? 1.0 : 0.0); }
    if (fname == "restart") {
        if (last_error_worker) {
            int max_attempts = 3;
            if (n["kwargs"].has("max")) max_attempts = (int)as_int(eval(n["kwargs"]["max"]));
            int &count = restart_counts[last_error_worker];
            if (count >= max_attempts) {
                Worker *w = last_error_worker;
                last_error_worker = nullptr;
                PiError e("WorkerDied", 0);
                e.worker = w;
                throw e;
            }
            count++;
            restart_worker(last_error_worker, this->cancel.get());
            last_error_worker = nullptr;
        }
        return V();
    }
    if (fname == "Params") {
        V p; p.kind = V::PARAMS;
        for (auto &kv : n["kwargs"].obj) p.params[kv.first] = eval(kv.second).num;
        return p;
    }
    if (fname == "zip") {
        V a = eval(args.arr.at(0)), b = eval(args.arr.at(1));
        std::vector<V> xs;
        if (a.kind == V::TUPLE) xs = a.tuple;
        else if (a.kind == V::PAYLOAD) for (double x : a.payload) xs.push_back(V(x));
        else die("zip needs lists");
        std::vector<V> ys;
        if (b.kind == V::TUPLE) ys = b.tuple;
        else if (b.kind == V::PAYLOAD) for (double x : b.payload) ys.push_back(V(x));
        else die("zip needs lists");
        V out; out.kind = V::TUPLE;
        size_t n = std::min(xs.size(), ys.size());
        for (size_t i = 0; i < n; i++) {
            V p; p.kind = V::TUPLE; p.tuple = {xs[i], ys[i]};
            out.tuple.push_back(p);
        }
        return out;
    }
    if (fname == "split") {
        V data = eval(args.arr.at(0));
        long n = as_int(eval(args.arr.at(1)));
        if (n < 1) die("split count must be >= 1");
        if (n > 1000000) die("split count is too large");
        std::vector<double> vals = data.payload;
        long size = vals.size() / n, rem = vals.size() % n, at = 0;
        V out; out.kind = V::TUPLE;
        for (long k = 0; k < n; k++) {
            long take = size + (k < rem ? 1 : 0);
            std::vector<double> part(vals.begin() + at, vals.begin() + at + take);
            at += take;
            out.tuple.push_back(V::payload_of(std::move(part)));
        }
        return out;
    }
    if (fname == "allreduce") {
        std::string op = eval(args.arr.at(0)).str;
        V parts = eval(args.arr.at(1));
        std::vector<double> out;
        for (auto &p : parts.tuple) {
            if (p.kind != V::PAYLOAD) die("allreduce needs payloads");
            if (out.size() < p.payload.size()) out.resize(p.payload.size(), 0.0);
            for (size_t i = 0; i < p.payload.size(); i++)
                out[i] = (op == "max") ? std::max(out[i], p.payload[i]) : out[i] + p.payload[i];
        }
        return V::payload_of(std::move(out));
    }
    if (fname == "empty") return V::payload_of({});
    if (fname == "kill_worker") {
        V w = eval(args.arr.at(0));
        if (w.kind == V::WORKER && w.worker) {
            std::lock_guard<std::mutex> lk(w.worker->io_mu);
#ifdef _WIN32
            if (w.worker->proc.hProcess) TerminateProcess(w.worker->proc.hProcess, 1);
#else
            if (w.worker->pid > 0) { kill(w.worker->pid, SIGKILL); waitpid(w.worker->pid, nullptr, 0); w.worker->pid = 0; }
#endif
            w.worker->alive = false;
        }
        return V();
    }
    if (fname == "concat") {
        V xs = eval(args.arr.at(0));
        std::vector<double> out;
        if (xs.kind == V::TUPLE) {
            for (auto &p : xs.tuple) out.insert(out.end(), p.payload.begin(), p.payload.end());
        } else die("concat needs list");
        return V::payload_of(std::move(out));
    }
    if (fname == "halo2") {
        // Extract one edge from a planar two-field tile payload (U then V).
        // Used by the generic GridTile element to hand neighbours only the
        // boundary values, not the whole tile.
        V tile = eval(args.arr.at(0));
        long rows = as_int(eval(args.arr.at(1)));
        long cols = as_int(eval(args.arr.at(2)));
        long side = as_int(eval(args.arr.at(3)));
        if (tile.kind != V::PAYLOAD || rows < 1 || cols < 1 || side < 0 || side > 3)
            die("halo2 expects a two-field tile and side 0..3");
        size_t n = (size_t)rows * (size_t)cols;
        if (tile.payload.size() < 2 * n) die("halo2 tile is too small");
        std::vector<double> out;
        if (side == 0 || side == 1) {
            size_t base = (side == 0) ? 0 : (size_t)(rows - 1) * (size_t)cols;
            for (long c = 0; c < cols; c++) out.push_back(tile.payload[base + (size_t)c]);
            for (long c = 0; c < cols; c++) out.push_back(tile.payload[n + base + (size_t)c]);
        } else {
            size_t col = (side == 2) ? 0 : (size_t)(cols - 1);
            for (long r = 0; r < rows; r++)
                out.push_back(tile.payload[(size_t)r * (size_t)cols + col]);
            for (long r = 0; r < rows; r++)
                out.push_back(tile.payload[n + (size_t)r * (size_t)cols + col]);
        }
        return V::payload_of(std::move(out));
    }
    if (fname == "halo1") {
        // One scalar field tile edge, used by the heat/diffusion element.
        V tile = eval(args.arr.at(0));
        long rows = as_int(eval(args.arr.at(1)));
        long cols = as_int(eval(args.arr.at(2)));
        long side = as_int(eval(args.arr.at(3)));
        if (tile.kind != V::PAYLOAD || rows < 1 || cols < 1 || side < 0 || side > 3)
            die("halo1 expects a tile and side 0..3");
        if (tile.payload.size() < (size_t)rows * (size_t)cols) die("halo1 tile is too small");
        std::vector<double> out;
        if (side == 0 || side == 1) {
            size_t base = (side == 0) ? 0 : (size_t)(rows - 1) * (size_t)cols;
            for (long c = 0; c < cols; c++) out.push_back(tile.payload[base + (size_t)c]);
        } else {
            size_t col = (side == 2) ? 0 : (size_t)(cols - 1);
            for (long r = 0; r < rows; r++)
                out.push_back(tile.payload[(size_t)r * (size_t)cols + col]);
        }
        return V::payload_of(std::move(out));
    }
    if (fname == "partition") {
        // Partition a 2D field into nx*ny tile descriptors.  Each descriptor is
        // [x_origin, y_origin, tile_rows, tile_cols, grid_rows, grid_cols].
        // Descriptors are row-major by tile row (y) then tile column (x).
        long nx = as_int(eval(args.arr.at(0)));
        long ny = as_int(eval(args.arr.at(1)));
        long rows = as_int(eval(args.arr.at(2)));
        long cols = as_int(eval(args.arr.at(3)));
        if (nx < 1 || ny < 1 || rows < 1 || cols < 1)
            die("partition expects positive dimensions");
        if (rows && cols > (long)(1 << 26) / rows)
            die("partition tile is too large");
        V out; out.kind = V::TUPLE;
        for (long ty = 0; ty < ny; ty++) {
            for (long tx = 0; tx < nx; tx++) {
                std::vector<double> d = {
                    (double)(tx * cols), (double)(ty * rows),
                    (double)rows, (double)cols,
                    (double)(nx * rows), (double)(ny * cols)};
                out.tuple.push_back(V::payload_of(std::move(d)));
            }
        }
        return out;
    }
    if (fname == "snapshot_grid") {
        // One-call convenience: collect every worker's snapshot and stitch the
        // tile grid into a single payload.  Equivalent to collect + stitch_grid,
        // but keeps Pi programs focused on the simulation loop.
        V fam = eval(args.arr.at(0));
        long nx = as_int(eval(args.arr.at(1)));
        long ny = as_int(eval(args.arr.at(2)));
        long rows = as_int(eval(args.arr.at(3)));
        long cols = as_int(eval(args.arr.at(4)));
        if (fam.kind != V::FAMILY) die("snapshot_grid expects a system family");
        if (nx < 1 || ny < 1 || rows < 1 || cols < 1)
            die("snapshot_grid dimensions must be positive");
        // Bound the stitched payload before any multiplication/allocation.
        // 100M doubles is 800 MiB; anything larger is a program error here.
        long long total = 1;
        const long long kMaxCells = 100000000LL;
        auto scale = [&](long v) {
            if (v > kMaxCells / total) die("snapshot_grid grid is too large");
            total *= (long long)v;
        };
        scale(nx); scale(ny); scale(rows); scale(cols);
        auto fit = fam.sys->families.find(fam.field);
        if (fit == fam.sys->families.end()) die("snapshot_grid: unknown family");
        std::vector<std::shared_ptr<Instance>> insts = fit->second;
        std::sort(insts.begin(), insts.end(),
                  [](const std::shared_ptr<Instance> &a,
                     const std::shared_ptr<Instance> &b) {
                      size_t n = std::min(a->pos.size(), b->pos.size());
                      for (size_t i = n; i-- > 0;) {
                          if (a->pos[i] != b->pos[i]) return a->pos[i] < b->pos[i];
                      }
                      return false;
                  });
        auto oi = op_index.find("snapshot");
        if (oi == op_index.end()) die("snapshot_grid: protocol has no snapshot op");
        long long tiles_n = (long long)nx * (long long)ny;
        if ((long long)insts.size() < tiles_n)
            die("snapshot_grid: not enough tiles");
        std::vector<V> tiles;
        tiles.reserve((size_t)tiles_n);
        for (long ty = 0; ty < ny; ty++)
            for (long tx = 0; tx < nx; tx++) {
                auto &inst = insts[(size_t)(ty * nx + tx)];
                auto it = inst->env.find("gs");
                if (it == inst->env.end() || it->second.kind != V::WORKER)
                    die("snapshot_grid: instance has no worker 'gs'");
                std::string ret = (oi->second < (long)op_rets.size())
                                      ? op_rets[(size_t)oi->second] : "";
                tiles.push_back(worker_call(it->second.worker, oi->second,
                                            std::vector<V>(), ret, this->cancel.get()));
            }
        size_t tile_n = (size_t)rows * (size_t)cols;
        size_t out_cols = (size_t)nx * (size_t)cols;
        std::vector<double> out((size_t)total);
        for (long ty = 0; ty < ny; ty++)
            for (long tx = 0; tx < nx; tx++) {
                const V &t = tiles[(size_t)(ty * nx + tx)];
                if (t.payload.size() < tile_n) die("snapshot_grid: tile is too small");
                for (long r = 0; r < rows; r++)
                    for (long c = 0; c < cols; c++)
                        out[(size_t)(ty * rows + r) * out_cols +
                            (size_t)(tx * cols + c)] =
                            t.payload[(size_t)r * (size_t)cols + (size_t)c];
            }
        return V::payload_of(std::move(out));
    }
    if (fname == "collect") {
        // Collect one worker operation from every instance of a family, in
        // row-major tile order (y first, then x).  This removes the per-tile
        // snapshot boilerplate from a grid program.
        V fam = eval(args.arr.at(0));
        std::string op = eval(args.arr.at(1)).str;
        if (fam.kind != V::FAMILY) die("collect expects a system family");
        auto fit = fam.sys->families.find(fam.field);
        if (fit == fam.sys->families.end()) die("collect: unknown family");
        std::vector<std::shared_ptr<Instance>> insts = fit->second;
        std::sort(insts.begin(), insts.end(),
                  [](const std::shared_ptr<Instance> &a,
                     const std::shared_ptr<Instance> &b) {
                      size_t n = std::min(a->pos.size(), b->pos.size());
                      for (size_t i = n; i-- > 0;) {
                          if (a->pos[i] != b->pos[i]) return a->pos[i] < b->pos[i];
                      }
                      return false;
                  });
        auto oi = op_index.find(op);
        if (oi == op_index.end()) die("collect: unknown op " + op);
        std::string ret = (oi->second < (long)op_rets.size())
                              ? op_rets[(size_t)oi->second] : "";
        V out; out.kind = V::TUPLE;
        for (auto &inst : insts) {
            auto it = inst->env.find("gs");
            if (it == inst->env.end() || it->second.kind != V::WORKER)
                die("collect: instance has no worker 'gs'");
            out.tuple.push_back(worker_call(it->second.worker, oi->second,
                                            std::vector<V>(), ret, this->cancel.get()));
        }
        return out;
    }
    if (fname == "stitch_grid") {
        // Flat list of tiles, row-major by tile row then tile column.
        V ts = eval(args.arr.at(0));
        long nx = as_int(eval(args.arr.at(1)));
        long ny = as_int(eval(args.arr.at(2)));
        long rows = as_int(eval(args.arr.at(3)));
        long cols = as_int(eval(args.arr.at(4)));
        if (ts.kind != V::TUPLE || nx < 1 || ny < 1 || rows < 1 || cols < 1)
            die("stitch_grid needs a list of tiles and positive dimensions");
        if (nx > 1000000 || ny > 1000000 || rows > 1000000 || cols > 1000000)
            die("stitch_grid dimensions too large");
        if (nx * ny > (long)ts.tuple.size()) die("stitch_grid needs nx*ny tiles");
        long long tile_n_ll = (long long)rows * (long long)cols;
        long long out_n_ll = (long long)nx * (long long)cols * (long long)ny * (long long)rows;
        if (out_n_ll > 100000000LL) die("stitch_grid grid is too large");
        size_t tile_n = (size_t)tile_n_ll;
        std::vector<double> out((size_t)out_n_ll);
        long out_cols = nx * cols;
        for (long ty = 0; ty < ny; ty++) {
            for (long tx = 0; tx < nx; tx++) {
                const V &t = ts.tuple[(size_t)(ty * nx + tx)];
                if (t.payload.size() < tile_n) die("stitch_grid tile is too small");
                for (long r = 0; r < rows; r++) {
                    for (long c = 0; c < cols; c++) {
                        out[(size_t)(ty * rows + r) * (size_t)out_cols + (size_t)(tx * cols + c)] =
                            t.payload[(size_t)r * (size_t)cols + (size_t)c];
                    }
                }
            }
        }
        return V::payload_of(std::move(out));
    }
    if (fname == "hstack") {
        // Tile assembly: [a | b] with each tile rows x cols.
        V av = eval(args.arr.at(0)), bv = eval(args.arr.at(1));
        long rows = as_int(eval(args.arr.at(2)));
        long cols = as_int(eval(args.arr.at(3)));
        if (rows < 0 || cols < 0 || rows > 1000000 || cols > 1000000 ||
            (long long)rows * (long long)cols > 50000000LL)
            die("hstack tile dimensions invalid");
        size_t n = (size_t)rows * (size_t)cols;
        if (av.payload.size() < n || bv.payload.size() < n)
            die("hstack tile shape mismatch");
        std::vector<double> out(n * 2);
        for (long r = 0; r < rows; r++) {
            for (long c = 0; c < cols; c++) {
                out[(size_t)r * 2 * cols + c] = av.payload[(size_t)r * cols + c];
                out[(size_t)r * 2 * cols + cols + c] = bv.payload[(size_t)r * cols + c];
            }
        }
        return V::payload_of(std::move(out));
    }
    if (fname == "vstack") {
        V av = eval(args.arr.at(0)), bv = eval(args.arr.at(1));
        long rows = as_int(eval(args.arr.at(2)));
        long cols = as_int(eval(args.arr.at(3)));
        if (rows < 0 || cols < 0 || rows > 1000000 || cols > 1000000 ||
            (long long)rows * (long long)cols > 50000000LL)
            die("vstack tile dimensions invalid");
        size_t n = (size_t)rows * (size_t)cols;
        if (av.payload.size() < n || bv.payload.size() < n)
            die("vstack tile shape mismatch");
        std::vector<double> out(av.payload.begin(), av.payload.begin() + (std::ptrdiff_t)n);
        out.insert(out.end(), bv.payload.begin(), bv.payload.begin() + (std::ptrdiff_t)n);
        return V::payload_of(std::move(out));
    }
    if (fname == "scalar") {
        V v = eval(args.arr.at(0));
        if (v.kind == V::PAYLOAD && v.payload.size() == 1) return V(v.payload[0]);
        if (v.kind == V::NUM) return v;
        die("scalar");
    }
    if (fname == "payload") return eval(args.arr.at(0));
    if (fname == "range") {
        long lo = as_int(eval(args.arr.at(0)));
        long hi = as_int(eval(args.arr.at(1)));
        if (hi <= lo) return V::payload_of({});
        unsigned long long span = (unsigned long long)hi - (unsigned long long)lo;
        if (span > 100000000ULL) die("range is too large");
        std::vector<double> v;
        v.reserve((size_t)span);
        for (long i = lo; i < hi; i++) v.push_back((double)i);
        return V::payload_of(std::move(v));
    }
    if (fname == "after") {
        double sec = eval(args.arr.at(0)).num;
        if (!std::isfinite(sec) || sec < 0) die("after duration must be finite and non-negative");
        cancellable_sleep(sec);
        return V();
    }
    if (fname == "emit" || fname == "emit_text") {
        std::string line;
        for (size_t i = 0; i < args.arr.size(); i++) {
            V v = eval(args.arr[i]);
            if (i) line += " ";
            if (v.kind == V::NUM) {
                char b[64]; std::snprintf(b, sizeof(b), "%.6g", v.num); line += b;
            } else if (v.kind == V::STR) line += v.str;
            else if (v.kind == V::PAYLOAD) {
                line += "[";
                for (size_t k = 0; k < v.payload.size(); k++) {
                    char b[64]; std::snprintf(b, sizeof(b), "%s%.6g", k ? ", " : "", v.payload[k]); line += b;
                }
                line += "]";
            }
            else line += "()";
        }
        emit_text(line);
        return V();
    }
    if (fname == "emit_series") {
        std::string name = eval(args.arr.at(0)).str;
        V x = eval(args.arr.at(1));
        V y = args.arr.size() > 2 ? eval(args.arr.at(2)) : x;
        emit_series(name, x, y);
        return V();
    }
    if (fname == "emit_points") {
        std::string name = eval(args.arr.at(0)).str;
        V x = eval(args.arr.at(1));
        V y = args.arr.size() > 2 ? eval(args.arr.at(2)) : x;
        emit_points(name, x, y);
        return V();
    }
    if (fname == "emit_bars") {
        std::string name = eval(args.arr.at(0)).str;
        V x = eval(args.arr.at(1));
        V y = args.arr.size() > 2 ? eval(args.arr.at(2)) : x;
        emit_bars(name, x, y);
        return V();
    }
    if (fname == "emit_grid") {
        std::string name = eval(args.arr.at(0)).str;
        V v = eval(args.arr.at(1));
        long r = args.arr.size() > 2 ? as_int(eval(args.arr[2])) : 0;
        long c = args.arr.size() > 3 ? as_int(eval(args.arr[3])) : 0;
        emit_grid(name, v, r, c);
        return V();
    }
    if (fname == "emit_animation_frame") {
        std::string name = eval(args.arr.at(0)).str;
        V v = eval(args.arr.at(1));
        long r = as_int(eval(args.arr[2]));
        long c = as_int(eval(args.arr[3]));
        emit_frame(name, v, r, c);
        return V();
    }
    if (fname == "Pool") die("Pool(...) only valid with .recruit(n)");
    die("unsupported call " + fname);
}

V Interp::eval(const J &n) {
    check_cancel();
    std::string k = n["_"].str;
    if (k.empty()) die("plan node has no kind ('_' field)");
    if (k == "Lit") {
        std::string kind = n["kind"].str;
        if (kind == "string") return V(n["value"].str);
        if (kind == "int" || kind == "float") return V(n["value"].num);
        if (kind == "bool") return V(n["value"].b ? 1.0 : 0.0);
        if (kind == "unit") return V();
        if (kind == "duration") return V(n["value"].num);
        die("unsupported literal " + kind);
    }
    if (k == "Name") {
        auto it = env.find(n["id"].str);
        if (it != env.end()) return it->second;
        if (n["id"].str == "sum" || n["id"].str == "max") return V(n["id"].str);
        if (n["id"].str == "Renormalize") return V(0.0);
        if (n["id"].str == "Report") return V(1.0);
        if (n["id"].str == "Halt") return V(2.0);
        die("unbound name " + n["id"].str);
    }
    if (k == "Await") return eval(n["expr"]);
    if (k == "Index") {
        V o = eval(n["obj"]);
        if (o.kind == V::FAMILY) return sys_index(o, as_positions(eval(n["index"])));
        long i = as_int(eval(n["index"]));
        if (o.kind == V::TUPLE) {
            if (i < 0 || i >= (long)o.tuple.size()) die("tuple index");
            return o.tuple[(size_t)i];
        }
        if (o.kind != V::PAYLOAD || i < 0 || i >= (long)o.payload.size()) die("index");
        return V(o.payload[(size_t)i]);
    }
    if (k == "BinOp") {
        double a = eval(n["left"]).num, b = eval(n["right"]).num;
        std::string op = n["op"].str;
        if (op == "+") return V(a + b);
        if (op == "-") return V(a - b);
        if (op == "*") return V(a * b);
        if (op == "/") return V(a / b);
        if (op == "<") return V(a < b ? 1 : 0);
        if (op == "<=") return V(a <= b ? 1 : 0);
        if (op == ">") return V(a > b ? 1 : 0);
        if (op == ">=") return V(a >= b ? 1 : 0);
        if (op == "==") return V(a == b ? 1 : 0);
        die("unsupported op " + op);
    }
    if (k == "ListE") {
        V v; v.kind = V::TUPLE;
        for (auto &x : n["items"].arr) v.tuple.push_back(eval(x));
        return v;
    }
    if (k == "TupleE") {
        V v; v.kind = V::TUPLE;
        for (auto &x : n["items"].arr) v.tuple.push_back(eval(x));
        return v;
    }
    if (k == "RangeE") {
        long lo = as_int(eval(n["lo"])), hi = as_int(eval(n["hi"]));
        if (hi <= lo) return V::payload_of({});
        unsigned long long span = (unsigned long long)hi - (unsigned long long)lo;
        if (span > 100000000ULL) die("range is too large");
        std::vector<double> v; v.reserve((size_t)span);
        for (long i = lo; i < hi; i++) v.push_back((double)i);
        return V::payload_of(std::move(v));
    }
    if (k == "ParFor") {
        V it = eval(n["iter"]);
        std::vector<V> xs;
        if (it.kind == V::PAYLOAD) for (double x : it.payload) xs.push_back(V(x));
        else if (it.kind == V::TUPLE) xs = it.tuple;
        else die("par for needs payload");
        std::vector<V> results(xs.size());
        auto *self = this;
        parallel_for(xs.size(), [self, &xs, &results, n](size_t i) {
            Interp branch(self->plan);
            branch.env = self->env; branch.workers = self->workers;
            branch.op_index = self->op_index; branch.gpu = self->gpu;
            branch.outdir = self->outdir; branch.scopes = self->scopes;
            branch.current_inst = self->current_inst;
            branch.cancel = self->cancel;
            bind_pattern(n["pat"], xs[i], branch.env);
            results[i] = branch.eval(n["body"]);
        });
        V out; out.kind = V::TUPLE; out.tuple = std::move(results);
        return out;
    }
    if (k == "OnError") {
        try { return eval(n["body"]); }
        catch (const PiError &e) {
            last_error_worker = e.worker;
            for (const auto &arm : n["arms"].arr) {
                const J &pat = arm["pat"];
                bool match = pat["_"].str == "PWild";
                if (pat["_"].str == "PCtor") {
                    if (pat["name"].str == e.kind) match = true;
                    if (pat["name"].str == "WorkerError" && e.kind == "WorkerError"
                        && !pat["args"].arr.empty() && pat["args"].arr[0]["_"].str == "Lit")
                        match = checked_long(pat["args"].arr[0]["value"].num, "handler code") == e.code;
                }
                if (!match) continue;
                const J &act = arm["action"];
                try { return eval(act); }
                catch (const ResumeSignal &) { return V(); }
                catch (const StopSignal &) { return V(); }
                catch (const EscalateSignal &sig) { throw sig.err; }
            }
            throw;
        }
    }
    if (k == "For") {
        V it = eval(n["iter"]);
        V out; out.kind = V::TUPLE;
        std::vector<V> xs;
        if (it.kind == V::PAYLOAD) for (double x : it.payload) xs.push_back(V(x));
        else if (it.kind == V::TUPLE) xs = it.tuple;
        else die("for needs payload/tuple");
        for (auto &x : xs) {
            check_cancel();
            env[n["var"]["id"].str] = x;
            out.tuple.push_back(eval(n["body"]));
        }
        return out;
    }
    if (k == "Block" || k == "ScopeE") {
        V last;
        const J &body = (k == "Block") ? n : n["body"];
        if (body["_"].str == "Block") {
            for (auto &s : body["stmts"].arr) {
                std::string sk = s["_"].str;
                if (sk == "Call" || sk == "Await" || sk == "Lit" || sk == "Name" || sk == "Index" || sk == "BinOp" || sk == "OnError" || sk == "ParFor" || sk == "For" || sk == "If") last = eval(s);
                else stmt(s);
            }
        } else {
            last = eval(body);
        }
        return last;
    }
    if (k == "Call") return call(n);
    if (k == "Member") {
        if (n["name"].str == "recruit") return pool_worker(n["obj"]);
        V o = eval(n["obj"]);
        if (o.kind == V::SYSTEM || o.kind == V::INSTANCE) return sys_member(o, n["name"].str);
        die("unsupported member");
    }
    die("unsupported expression " + k);
}

void Interp::stmt(const J &n) {
    check_cancel();
    std::string k = n["_"].str;
    if (k.empty()) die("plan node has no kind ('_' field)");
    if (k == "Block") { for (auto &s : n["stmts"].arr) stmt(s); return; }
    if (k == "ScopeE") {
        scopes.emplace_back();
        try { stmt(n["body"]); }
        catch (...) {
            for (auto &ch : scopes.back().chans) chan_close(V([&]{ V v; v.kind=V::CHAN; v.chan=ch; return v; }()));
            scopes.pop_back();
            throw;
        }
        for (auto &ch : scopes.back().chans) {
            V v; v.kind = V::CHAN; v.chan = ch; chan_close(v);
        }
        for (auto &sy : scopes.back().systems) {
            V v; v.kind = V::SYSCALL; v.sys = sy; v.field = "close"; system_push(v);
        }
        scopes.pop_back();
        return;
    }
    if (k == "Bind") {
        V v = eval(n["expr"]);
        bind_pattern(n["pat"], v, env);
        return;
    }
    if (k == "Par") {
        std::vector<std::thread> ts;
        std::exception_ptr first;
        std::mutex emu;
        auto *self = this;
        auto run = [self, &first, &emu](const J &b) {
            try {
                Interp branch(self->plan);
                branch.env = self->env;
                branch.workers = self->workers;
                branch.op_index = self->op_index;
                branch.gpu = self->gpu;
                branch.outdir = self->outdir;
                branch.scopes = self->scopes;
                branch.current_inst = self->current_inst;
                branch.cancel = self->cancel;
                branch.stmt(b);
            } catch (...) {
                std::lock_guard<std::mutex> lk(emu);
                if (!first) first = std::current_exception();
            }
        };
        try {
            ts.reserve(n["branches"].arr.size());
            for (auto &b : n["branches"].arr) ts.emplace_back(run, std::cref(b));
        } catch (...) {
            /* Thread creation failed: run what is left serially. */
        }
        for (auto &t : ts) if (t.joinable()) t.join();
        if (first) std::rethrow_exception(first);
        return;
    }
    if (k == "Serve") {
        V ch = eval(n["chan"]);
        while (true) {
            check_cancel();
            V msg;
            try { msg = chan_recv(ch, this->cancel.get()); }
            catch (const ChannelClosed &) { return; }
            catch (const Cancelled &) { throw; }
            catch (const PiFatal &) { throw; }
            catch (...) { return; }
            env[n["pat"]["id"].str] = msg;
            try { stmt(n["body"]); }
            catch (const PiError &e) {
                int action = handle_instance_failure(e);
                if (action == 0) return;
                if (action == 2) {
                    if (current_inst && current_inst->tick_tx.kind == V::CHAN)
                        chan_send(current_inst->tick_tx, msg, this->cancel.get());
                    throw RestartController{};
                }
            }
            catch (const Cancelled &) { throw; }
            catch (const PiFatal &) { throw; }
            catch (...) { return; }
            if (current_inst) {
                current_inst->iterations.fetch_add(1);
                std::lock_guard<std::mutex> lk(current_inst->mu);
                current_inst->cv.notify_all();
            }
        }
    }
    if (k == "For") {
        V it = eval(n["iter"]);
        std::vector<V> xs;
        if (it.kind == V::PAYLOAD) for (double x : it.payload) xs.push_back(V(x));
        else if (it.kind == V::TUPLE) xs = it.tuple;
        else die("for needs payload/tuple");
        for (auto &x : xs) {
            check_cancel();
            env[n["var"]["id"].str] = x;
            stmt(n["body"]);
        }
        return;
    }
    if (k == "Timeout") {
        double sec = eval(n["duration"]).num;
        if (!std::isfinite(sec) || sec < 0) die("timeout duration must be finite and non-negative");
        struct TimeoutState {
            std::mutex mu;
            std::condition_variable cv;
            bool done = false;
            std::exception_ptr ep;
        };
        auto st = std::make_shared<TimeoutState>();
        auto tok = std::make_shared<CancelToken>();
        tok->parent = this->cancel.get();
        J plan = this->plan;
        auto workers = this->workers;
        auto op_index = this->op_index;
        auto gpu = this->gpu;
        std::string outdir = this->outdir;
        auto base_env = this->env;
        auto scopes_copy = this->scopes;
        auto cur_inst = this->current_inst;
        std::thread th([st, tok, plan, workers, op_index, gpu, outdir, base_env, scopes_copy,
                        cur_inst, n]() mutable {
            try {
                Interp branch(plan);
                branch.env = std::move(base_env); branch.workers = std::move(workers);
                branch.op_index = std::move(op_index); branch.gpu = std::move(gpu);
                branch.outdir = std::move(outdir);
                branch.scopes = std::move(scopes_copy);
                branch.current_inst = std::move(cur_inst);
                branch.cancel = tok;
                branch.stmt(n["body"]);
            } catch (...) { st->ep = std::current_exception(); }
            {
                std::lock_guard<std::mutex> lk(st->mu);
                st->done = true;
            }
            st->cv.notify_all();
        });
        std::unique_lock<std::mutex> lk(st->mu);
        bool finished = st->cv.wait_for(lk, std::chrono::duration<double>(sec),
                                        [&] { return st->done; });
        if (!finished) {
            /* P2: cancel the branch and join it; never detach.  Blocking points
               inside the branch poll this token (worker I/O, sleeps, channels). */
            tok->cancelled.store(true);
            bool stopped = st->cv.wait_for(lk, std::chrono::seconds(10),
                                           [&] { return st->done; });
            lk.unlock();
            if (!stopped) {
                /* Cancellation should be prompt; if something unforeseen is
                   still blocked, kill the workers it could be waiting on. */
                kill_all_workers();
                th.join();
            } else {
                th.join();
            }
            PiError e("Deadline");
            e.worker = tok->cancelled_worker.load();
            throw e;
        }
        lk.unlock();
        th.join();
        if (st->ep) std::rethrow_exception(st->ep);
        return;
    }
    if (k == "Retry") {
        int max_attempts = 3;
        double backoff = 0.0;
        if (n["opts"].has("max")) max_attempts = (int)as_int(eval(n["opts"]["max"]));
        if (n["opts"].has("backoff")) backoff = eval(n["opts"]["backoff"]).num;
        if (max_attempts < 1) die("retry max must be >= 1");
        if (!std::isfinite(backoff) || backoff < 0) die("retry backoff must be finite and non-negative");
        for (int attempt = 0; ; attempt++) {
            try { stmt(n["body"]); return; }
            catch (const PiError &e) {
                if (attempt + 1 >= max_attempts) throw;
                if (e.worker && e.kind == "WorkerDied") restart_worker(e.worker, this->cancel.get());
                if (backoff > 0) cancellable_sleep(backoff);
            }
        }
    }
    if (k == "ParFor") {
        V it = eval(n["iter"]);
        std::vector<V> xs;
        if (it.kind == V::PAYLOAD) for (double x : it.payload) xs.push_back(V(x));
        else if (it.kind == V::TUPLE) xs = it.tuple;
        else die("par for needs payload");
        auto *self = this;
        parallel_for(xs.size(), [self, &xs, n](size_t i) {
            Interp branch(self->plan);
            branch.env = self->env; branch.workers = self->workers;
            branch.op_index = self->op_index; branch.gpu = self->gpu;
            branch.outdir = self->outdir;
            branch.cancel = self->cancel;
            branch.env[n["pat"]["id"].str] = xs[i];
            branch.stmt(n["body"]);
        });
        return;
    }
    if (k == "Select") {
        std::map<const J *, std::chrono::steady_clock::time_point> arm_deadlines;
        auto sel_start = std::chrono::steady_clock::now();
        for (auto &arm : n["arms"].arr) {
            const J &g = arm["guard"];
            if (g["_"].str == "Call" && g["fn"]["id"].str == "after") {
                double sec = eval(g["args"].arr[0]).num;
                if (!std::isfinite(sec) || sec < 0) die("select after duration invalid");
                arm_deadlines[&arm] = sel_start + std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::duration<double>(sec));
            }
        }
        for (;;) {
            check_cancel();
            for (auto &arm : n["arms"].arr) {
                const J &g = arm["guard"];
                if (g["_"].str == "Call" && g["fn"]["id"].str == "recv") {
                    V ch = eval(g["args"].arr[0]);
                    if (ch.kind != V::CHAN) die("select recv needs channel");
                    V msg;
                    if (!ch.chan) continue;
                    if (ch.chan->try_recv(msg)) { stmt(arm["body"]); return; }
                } else if (g["_"].str == "Call" && g["fn"]["id"].str == "closed") {
                    if (chan_is_closed(eval(g["args"].arr[0]))) { stmt(arm["body"]); return; }
                } else if (g["_"].str == "Call" && g["fn"]["id"].str == "after") {
                    double sec = eval(g["args"].arr[0]).num;
                    if (!std::isfinite(sec) || sec < 0) die("select after duration invalid");
                    // Only fire once the arm's own deadline has elapsed; otherwise
                    // keep polling the other arms.  Waiting is bounded by the
                    // smallest pending deadline.
                    auto now = std::chrono::steady_clock::now();
                    if (now >= arm_deadlines[&arm]) { stmt(arm["body"]); return; }
                } else {
                    V c = eval(g);
                    if (as_int(c)) { stmt(arm["body"]); return; }
                }
            }
            auto now = std::chrono::steady_clock::now();
            auto next = now + std::chrono::milliseconds(1);
            for (auto &kv : arm_deadlines) if (kv.second < next) next = kv.second;
            std::this_thread::sleep_until(next);
        }
    }
    if (k == "OnError") {
        try {
            stmt(n["body"]);
        } catch (const PiError &e) {
            last_error_worker = e.worker;
            for (const auto &arm : n["arms"].arr) {
                const J &pat = arm["pat"];
                bool match = pat["_"].str == "PWild";
                if (pat["_"].str == "PCtor") {
                    if (pat["name"].str == e.kind) match = true;
                    if (pat["name"].str == "WorkerError" && e.kind == "WorkerError"
                        && !pat["args"].arr.empty() && pat["args"].arr[0]["_"].str == "Lit")
                        match = checked_long(pat["args"].arr[0]["value"].num, "handler code") == e.code;
                }
                if (!match) continue;
                const J &act = arm["action"];
                if (act["_"].str == "Name" && act["id"].str == "resume") return;
                if (act["_"].str == "Name" && act["id"].str == "stop") throw;
                if (act["_"].str == "Name" && act["id"].str == "escalate") throw e;
                try { stmt(act); }
                catch (const ResumeSignal &) { return; }
                catch (const StopSignal &) { return; }
                catch (const EscalateSignal &sig) { throw sig.err; }
                return;
            }
            throw;
        }
        return;
    }
    if (k == "Iterate") {
        long count = as_int(eval(n["n"]));
        for (long i = 0; i < count; i++) {
            check_cancel();
            stmt(n["body"]);
        }
        return;
    }
    if (k == "AfterBlock") {
        double sec = eval(n["duration"]).num;
        if (!std::isfinite(sec) || sec < 0) die("after duration must be finite and non-negative");
        cancellable_sleep(sec);
        stmt(n["body"]);
        return;
    }
    if (k == "If") {
        if (as_int(eval(n["cond"]))) stmt(n["then"]);
        else if (n["orelse"].kind != J::NUL) stmt(n["orelse"]);
        return;
    }
    if (k == "Name") {
        if (n["id"].str == "resume") throw ResumeSignal{};
        if (n["id"].str == "stop") throw StopSignal{};
        if (n["id"].str == "escalate") throw EscalateSignal(PiError("Escalate"));
        (void)eval(n);
        return;
    }
    if (k == "Call") { (void)eval(n); return; }
    if (k == "Await") { (void)eval(n["expr"]); return; }
    if (k == "Lit" || k == "Name" || k == "Index" || k == "BinOp" || k == "Member") {
        (void)eval(n);
        return;
    }
    die("unsupported statement " + k);
}

int Interp::handle_instance_failure(const PiError &e) {
    if (!current_inst || current_inst->on_failure.kind != J::ARR) return 0;
    last_error_worker = e.worker;
    for (const auto &arm : current_inst->on_failure.arr) {
        const J &pat = arm["pat"];
        bool match = pat["_"].str == "PWild";
        if (pat["_"].str == "PCtor") {
            if (pat["name"].str == e.kind) match = true;
            if (pat["name"].str == "WorkerError" && e.kind == "WorkerError"
                && !pat["args"].arr.empty() && pat["args"].arr[0]["_"].str == "Lit")
                match = checked_long(pat["args"].arr[0]["value"].num, "handler code") == e.code;
        }
        if (!match) continue;
        bool restarting = arm["action"]["_"].str == "Call"
            && arm["action"]["fn"]["_"].str == "Name"
            && arm["action"]["fn"]["id"].str == "restart";
        try { stmt(arm["action"]); }
        catch (const ResumeSignal &) { return 1; }
        catch (const StopSignal &) { return 0; }
        catch (const EscalateSignal &sig) { throw sig.err; }
        return restarting ? 2 : 1;
    }
    return 0;
}

void Interp::run(const J &body) {
    stmt(body);
}

static std::string slurp(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) die("cannot open " + path);
    return std::string(std::istreambuf_iterator<char>(f), {});
}

static int run_program(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: pi-runtime plan.json\n");
        return 2;
    }
    int rc = 0;
    {
        std::string text = slurp(argv[1]);
        JsonParser parser(text);
        J plan = parser.parse();
        Interp in(std::move(plan));
        try {
            const auto &progs = in.plan["programs"].obj;
            auto it = progs.find("Main");
            if (it == progs.end()) die("plan has no Main program");
            in.run(it->second["body"]);
        } catch (const PiFatal &e) {
            std::fprintf(stderr, "pi-cpp: %s\n", e.what());
            rc = e.exit_code;
        } catch (const std::exception &e) {
            std::fprintf(stderr, "pi-cpp: %s\n", e.what());
            rc = 1;
        } catch (...) {
            std::fprintf(stderr, "pi-cpp: unknown runtime failure\n");
            rc = 1;
        }
        try { in.finish(); }
        catch (const std::exception &e) { std::fprintf(stderr, "pi-cpp: report: %s\n", e.what()); rc = 1; }
        for (auto &sy : g_systems) {
            V v; v.kind = V::SYSCALL; v.sys = sy; v.field = "close";
            try { in.system_push(v); } catch (...) { rc = 1; }
        }
        g_systems.clear();
    }
    shutdown_all_workers();
    std::printf(rc == 0 ? "pi-cpp: done\n" : "pi-cpp: failed\n");
    return rc;
}

struct MainArgs {
    int argc;
    char **argv;
    int rc;
};

#ifdef _WIN32
static DWORD WINAPI main_thread(LPVOID p) {
    MainArgs *a = (MainArgs *)p;
    a->rc = run_program(a->argc, a->argv);
    return 0;
}
#else
static void *main_thread(void *p) {
    MainArgs *a = (MainArgs *)p;
    a->rc = run_program(a->argc, a->argv);
    return nullptr;
}
#endif

// P5: parse/evaluate on a 64 MB stack so large-but-flat programs (deeply nested
// plan JSON, long left-associated expressions) fit without lowering the limits.
int main(int argc, char **argv) {
#ifndef _WIN32
    ::signal(SIGPIPE, SIG_IGN);  // P1: a write to a dead worker must not kill us
#endif
    MainArgs a{argc, argv, 1};
#if defined(_WIN32)
    HANDLE h = CreateThread(nullptr, BIG_STACK_BYTES, main_thread, &a, 0, nullptr);
    if (!h) return run_program(argc, argv);
    WaitForSingleObject(h, INFINITE);
    CloseHandle(h);
#else
    pthread_attr_t attr;
    pthread_t th;
    if (pthread_attr_init(&attr) != 0 ||
        pthread_attr_setstacksize(&attr, BIG_STACK_BYTES) != 0 ||
        pthread_create(&th, &attr, main_thread, &a) != 0) {
        pthread_attr_destroy(&attr);
        return run_program(argc, argv);
    }
    pthread_join(th, nullptr);
    pthread_attr_destroy(&attr);
#endif
    return a.rc;
}
