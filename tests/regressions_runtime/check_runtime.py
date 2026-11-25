#!/usr/bin/env python3
"""Runtime checks that ``run_tests.py`` manifests cannot express.

A manifest case can only say "this program exits 0 and prints exactly this".
Several PR.md runtime findings are about failure modes instead: a primitive
that must report a type error rather than segfault, a file operation that must
return its error value rather than truncate or abort, an input primitive that
must not split or truncate a line, a heap-size-class/arena invariant, a
compile-time environment switch (``LAMBDA_RAWLOOP``), or allocation statistics.
Those are checked here with exit codes, stderr, filesystem state and small
C-level unit tests compiled from ``RUNTIME_C`` directly.

Usage::

    python tests/regressions_runtime/check_runtime.py [-v]

Exit status is 0 when every check passes.  Checks that need a POSIX-only
facility (``/dev/full``, ``/proc``) are reported as SKIP elsewhere.

Keep this file dependency-light: it must run with the same Python that runs
``run_tests.py`` (3.10) and the same clang that ``lambda.py`` uses.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

# The checks run at import time (decorator application), before main(), and
# their detail text can contain non-ASCII program source.  Reconfigure the
# streams first so a cp1252 console cannot crash a failing check.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LAMBDA = os.path.join(ROOT, "lambda.py")
CLANG = shutil.which("clang")
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

RESULTS = []  # (status, name, detail)


class CheckFailure(Exception):
    pass


class Skipped(Exception):
    pass


def report(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    if status != "PASS" or VERBOSE:
        print(f"[{status}] {name}{': ' + detail if detail else ''}")


def ok(name: str, detail: str = "") -> None:
    report("PASS", name, detail)


def skip(why: str) -> None:
    raise Skipped(why)


class ProcResult:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        self.stderr = stderr.decode("utf-8", "replace").replace("\r\n", "\n")


def build(source: str, name: str, workdir: str, flags=(), env_extra=None) -> str:
    """Compile *source* with lambda.py and return the executable path."""
    src = os.path.join(workdir, name + ".lambda")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)
    exe = os.path.join(workdir, name)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, LAMBDA, src, "-o", exe] + list(flags),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180)
    if proc.returncode != 0:
        raise CheckFailure(f"compilation failed:\n{proc.stderr.strip()}")
    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"
    if not os.path.exists(exe):
        raise CheckFailure("compiler did not produce an executable")
    return exe


def run_exe(exe: str, cwd: str, stdin=None, env_extra=None) -> ProcResult:
    env = dict(os.environ)
    env["LAMBDA_STATS"] = "1"
    if env_extra:
        env.update(env_extra)
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    proc = subprocess.run([exe], input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, cwd=cwd,
                          timeout=180)
    return ProcResult(proc.returncode, proc.stdout, proc.stderr)


def expect_runtime_error(proc: ProcResult, needle: str) -> None:
    if proc.returncode == 0:
        raise CheckFailure(
            f"expected a runtime error containing {needle!r}, but it exited 0 "
            f"with stdout {proc.stdout!r}")
    if needle not in proc.stderr:
        raise CheckFailure(
            f"expected {needle!r} in stderr, got:\n{proc.stderr.strip()}")


def expect_output(proc: ProcResult, expected: str) -> None:
    if proc.returncode != 0:
        raise CheckFailure(
            f"exit {proc.returncode}: {proc.stderr.strip()}")
    if proc.stdout != expected:
        raise CheckFailure(f"stdout {proc.stdout!r}, expected {expected!r}")


def stats(proc: ProcResult):
    match = re.search(
        r"value_allocs=(\d+) value_frees=(\d+) env_allocs=(\d+) "
        r"env_frees=(\d+) live=(\d+) immortal=(\d+)", proc.stderr)
    if not match:
        raise CheckFailure(f"missing allocation statistics: {proc.stderr!r}")
    return dict(zip(
        ("value_allocs", "value_frees", "env_allocs", "env_frees", "live",
         "immortal"), (int(x) for x in match.groups())))


def check(name: str):
    def wrap(fn):
        try:
            fn()
            ok(name)
        except Skipped as exc:
            report("SKIP", name, str(exc))
        except CheckFailure as exc:
            report("FAIL", name, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            report("FAIL", name, f"{type(exc).__name__}: {exc}")
        return fn
    return wrap


# ---------------------------------------------------------------------------
# PR.md S2 / S3: the boxed backend is reachable via --allow-dynamic, so its
# primitives must validate tags instead of trusting them.
# ---------------------------------------------------------------------------

@check("s2_dynamic_str_len_type_error")
def s2_dynamic_str_len():
    with tempfile.TemporaryDirectory(prefix="rt-s2-") as work:
        exe = build("(str-len [1, 2])", "strlen_list", work,
                    ["--allow-dynamic"])
        proc = run_exe(exe, work)
        expect_runtime_error(proc, "runtime error: expected String, got List")


@check("s3_dynamic_sum_type_error")
def s3_dynamic_sum():
    with tempfile.TemporaryDirectory(prefix="rt-s3a-") as work:
        exe = build("(sum 5)", "sum_int", work, ["--allow-dynamic"])
        proc = run_exe(exe, work)
        expect_runtime_error(proc, "runtime error: expected List, got Int")


@check("s3_dynamic_product_type_error")
def s3_dynamic_product():
    with tempfile.TemporaryDirectory(prefix="rt-s3b-") as work:
        exe = build('(product "x")', "product_str", work, ["--allow-dynamic"])
        proc = run_exe(exe, work)
        expect_runtime_error(proc, "runtime error: expected List, got String")


@check("s3_dynamic_sum_improper_list")
def s3_dynamic_sum_improper():
    # (cons 1 2) builds a cons whose tail is an Int: a proper-list check must
    # reject it instead of silently stopping at the broken spine.
    with tempfile.TemporaryDirectory(prefix="rt-s3c-") as work:
        exe = build("(sum (cons 1 2))", "sum_improper", work,
                    ["--allow-dynamic"])
        proc = run_exe(exe, work)
        expect_runtime_error(proc, "runtime error: expected List, got Int")


# ---------------------------------------------------------------------------
# PR.md 2.1 / 2.2 / 2.3: file I/O.
# ---------------------------------------------------------------------------

@check("2_3_write_file_nul_path")
def write_file_nul_path():
    with tempfile.TemporaryDirectory(prefix="rt-23w-") as work:
        exe = build(r'(write-file "nul_target\u0000.tmp" (arrayb 2 65))',
                    "nul_write", work)
        proc = run_exe(exe, work)
        expect_output(proc, "-1\n")
        # The C-string prefix must not have been created either.
        if os.path.exists(os.path.join(work, "nul_target")):
            raise CheckFailure("wrote the truncated prefix path 'nul_target'")


@check("2_3_read_file_nul_path")
def read_file_nul_path():
    with tempfile.TemporaryDirectory(prefix="rt-23r-") as work:
        with open(os.path.join(work, "nul_prefix"), "w", encoding="utf-8") as f:
            f.write("ABCDE")
        exe = build(
            r'[(str-len (read-file "nul_prefix\u0000ignored")), '
            r'(arrayb-length (read-file-bytes "nul_prefix\u0000ignored"))]',
            "nul_read", work)
        proc = run_exe(exe, work)
        expect_output(proc, "[0, 0]\n")


@check("2_2_read_file_directory")
def read_file_directory():
    with tempfile.TemporaryDirectory(prefix="rt-22d-") as work:
        exe = build("[(str-len (read-file \".\")), "
                    "(arrayb-length (read-file-bytes \".\"))]", "read_dir", work)
        proc = run_exe(exe, work)
        expect_output(proc, "[0, 0]\n")


@check("2_2_read_file_nonregular_posix")
def read_file_nonregular_posix():
    if not os.path.exists("/proc/self/status"):
        skip("no /proc on this platform")
    with tempfile.TemporaryDirectory(prefix="rt-22p-") as work:
        # /proc/self/status is a regular file with st_size 0: ftell sizing
        # returned an empty string; the growing read must return the content.
        exe = build('(str-len (read-file "/proc/self/status"))',
                    "read_proc", work)
        proc = run_exe(exe, work)
        if proc.returncode != 0:
            raise CheckFailure(f"exit {proc.returncode}: {proc.stderr.strip()}")
        try:
            n = int(proc.stdout.strip())
        except ValueError:
            raise CheckFailure(f"unexpected stdout {proc.stdout!r}")
        if n <= 0:
            raise CheckFailure(
                f"read-file returned {n} bytes for /proc/self/status")


@check("2_1_write_file_close_failure_posix")
def write_file_close_failure_posix():
    if not os.path.exists("/dev/full"):
        skip("no /dev/full on this platform")
    with tempfile.TemporaryDirectory(prefix="rt-21-") as work:
        exe = build('(write-file "/dev/full" (arrayb 100 65))', "write_full",
                    work)
        proc = run_exe(exe, work)
        expect_output(proc, "-1\n")


# ---------------------------------------------------------------------------
# PR.md S5 / S6 / S7: terminal input must not split long lines, add phantom
# lines, truncate at NUL, or silently saturate on overflow.
# ---------------------------------------------------------------------------

@check("s5_read_line_long_and_phantom")
def s5_read_line_long():
    with tempfile.TemporaryDirectory(prefix="rt-s5-") as work:
        # 5000-byte line: the old 4096-byte fgets buffer split it and the
        # program desynchronised (second read saw the rest of the first line).
        exe = build('[(str-len (read-line "")), (str-len (read-line ""))]',
                    "long_line", work, ["--input"])
        proc = run_exe(exe, work, stdin="x" * 5000 + "\nab\n")
        expect_output(proc, "[5000, 2]\n")
        # Exactly 4095 bytes used to be followed by a phantom empty line.
        exe = build('[(str-len (read-line "")), (str-len (read-line ""))]',
                    "phantom_line", work, ["--input"])
        proc = run_exe(exe, work, stdin="x" * 4095 + "\ny\n")
        expect_output(proc, "[4095, 1]\n")


@check("s7_read_line_nul")
def s7_read_line_nul():
    with tempfile.TemporaryDirectory(prefix="rt-s7-") as work:
        exe = build(
            '[(str-byte-length (read-line "")), (str-byte-length (read-line ""))]',
            "nul_line", work, ["--input"])
        proc = run_exe(exe, work, stdin="a\x00b\nc\n")
        expect_output(proc, "[3, 1]\n")


@check("s6_read_int_overflow")
def s6_read_int_overflow():
    with tempfile.TemporaryDirectory(prefix="rt-s6-") as work:
        exe = build('(read-int "")', "int_overflow", work, ["--input"])
        proc = run_exe(exe, work, stdin="99999999999999999999\n")
        expect_runtime_error(proc, "read-int: integer out of range")


@check("s7_read_int_nul")
def s7_read_int_nul():
    with tempfile.TemporaryDirectory(prefix="rt-s7i-") as work:
        exe = build('(read-int "")', "int_nul", work, ["--input"])
        proc = run_exe(exe, work, stdin="42\x00junk\n")
        expect_runtime_error(proc, "read-int: invalid integer")


@check("input_normal_paths")
def input_normal_paths():
    with tempfile.TemporaryDirectory(prefix="rt-in-") as work:
        exe = build('[(read-int ""), (str-len (read-line ""))]', "in_normal",
                    work, ["--input"])
        proc = run_exe(exe, work, stdin="  42  \nhello\n")
        expect_output(proc, "[42, 5]\n")
        exe = build('(read-float "")', "in_float", work, ["--input"])
        proc = run_exe(exe, work, stdin="1.5\r\n")
        expect_output(proc, "1.5\n")


# ---------------------------------------------------------------------------
# PR.md F1 / F3: C-level invariants of the array pool and the data arenas.
# ---------------------------------------------------------------------------

C_RUNTIME_CHECKS = r"""
#include <stdio.h>

static int c_failures = 0;

static void expect_new_pool_block(const char *what, void *old_block,
                                  void *new_block) {
    if (old_block == new_block) {
        fprintf(stderr,
                "FAIL F1: %s recycled the empty array's 1-byte pool block for "
                "a larger size class (heap overflow)\n", what);
        c_failures++;
    }
}

static void check_pool_size_classes(void) {
    pool_init();

    /* Empty ArrayF (1-byte class 0 block) must not be reused for ArrayI(1)
       (8-byte class 3 block). */
    Value *empty_f = lambda_arrayf_new(0, 0.0);
    void *block_f = empty_f->p;
    lambda_release(empty_f);
    Value *one_i = lambda_arrayi_new(1, 7);
    expect_new_pool_block("empty ArrayF -> ArrayI(1)", block_f, one_i->p);
    if (lambda_arrayi_get(one_i, 0) != 7) {
        fprintf(stderr, "FAIL F1: ArrayI(1) value corrupted\n");
        c_failures++;
    }
    lambda_release(one_i);

    /* Empty ArrayI (1-byte class 0 block) must not be reused for ArrayB(5)
       (8-byte class 3 block). */
    Value *empty_i = lambda_arrayi_new(0, 0);
    void *block_i = empty_i->p;
    lambda_release(empty_i);
    Value *five_b = lambda_arrayb_new(5, 65);
    expect_new_pool_block("empty ArrayI -> ArrayB(5)", block_i, five_b->p);
    if (lambda_arrayb_get(five_b, 4) != 65) {
        fprintf(stderr, "FAIL F1: ArrayB(5) value corrupted\n");
        c_failures++;
    }
    lambda_release(five_b);

    /* Empty -> empty reuse is fine and must keep working. */
    Value *empty2 = lambda_arrayf_new(0, 0.0);
    lambda_release(empty2);
    Value *empty3 = lambda_arrayf_new(0, 0.0);
    if (lambda_arrayf_length(empty3) != 0) {
        fprintf(stderr, "FAIL F1: empty ArrayF length corrupted\n");
        c_failures++;
    }
    lambda_release(empty3);
    printf("F1: pool size classes ok\n");
}

static void check_arena_reservation(void) {
    /* A fresh 64-column arena used to reserve 2,097,152 rows up front: about
       1 GiB per arena before the program did anything.  The reservation must
       stay small and growth must still work. */
    AdtArena *a = lambda_arena_new(64);
    size_t reserved = (size_t)a->cap * (size_t)(a->width + 1) * sizeof(uint64_t);
    if (reserved > ((size_t)8 << 20)) {
        fprintf(stderr, "FAIL F3: fresh arena reserves %zu bytes\n", reserved);
        c_failures++;
    }
    if ((size_t)a->edge_cap * sizeof(uint64_t) > ((size_t)1 << 20)) {
        fprintf(stderr, "FAIL F3: fresh arena reserves %ld edge slots\n",
                (long)a->edge_cap);
        c_failures++;
    }
    for (int64_t i = 0; i < 100000; i++) {
        int64_t id = lambda_arena_alloc1(a, 1, (uint64_t)i);
        if (id != i) {
            fprintf(stderr, "FAIL F3: arena growth returned wrong id\n");
            c_failures++;
            break;
        }
    }
    if (lambda_arena_get(a, 99999, 0) != 99999) {
        fprintf(stderr, "FAIL F3: arena growth corrupted a row\n");
        c_failures++;
    }
    lambda_arena_free(a);
    printf("F3: arena reservation ok\n");
}

static int64_t arrayg_elem_int(Value *a, int64_t i) {
    Value *e = lambda_arrayg_get(a, i);
    int64_t x = lambda_get_int(e);
    lambda_release(e);
    return x;
}

static void check_arrayg(void) {
    int64_t base = live_values;

    /* Build [1, 2, 3] the way generated code would, then convert it.  The
       conversion moves the element references and frees only the cons cells,
       so no element may be released or retained twice. */
    Value *list = lambda_nil();
    for (int i = 3; i >= 1; i--) list = lambda_cons(lambda_box_int(i), list);
    Value *arr = lambda_arrayg_of(list);
    if (lambda_arrayg_length(arr) != 3) {
        fprintf(stderr, "FAIL arrayg: length after of()\n");
        c_failures++;
    }
    if (arrayg_elem_int(arr, 1) != 2) {
        fprintf(stderr, "FAIL arrayg: get() after of()\n");
        c_failures++;
    }

    /* rc == 1: set() replaces the slot in place. */
    arr = lambda_arrayg_set(arr, 1, lambda_box_int(9));
    if (arrayg_elem_int(arr, 1) != 9) {
        fprintf(stderr, "FAIL arrayg: set() in place\n");
        c_failures++;
    }

    /* rc > 1: set() must copy, so the other handle keeps the old buffer. */
    lambda_retain(arr); /* second handle */
    Value *copy = lambda_arrayg_set(arr, 0, lambda_box_int(7));
    if (arrayg_elem_int(arr, 0) != 1 || arrayg_elem_int(arr, 1) != 9) {
        fprintf(stderr, "FAIL arrayg: copy-on-write mutated the shared buffer\n");
        c_failures++;
    }
    if (arrayg_elem_int(copy, 0) != 7 || arrayg_elem_int(copy, 1) != 9 ||
        arrayg_elem_int(copy, 2) != 3) {
        fprintf(stderr, "FAIL arrayg: copy-on-write content\n");
        c_failures++;
    }
    lambda_print_ln(copy); /* printed as a list: [7, 9, 3] */
    lambda_flush();
    lambda_release(copy);
    lambda_release(arr);

    /* Empty array and Array(n, init). */
    Value *empty = lambda_arrayg_of(lambda_nil());
    if (lambda_arrayg_length(empty) != 0) {
        fprintf(stderr, "FAIL arrayg: empty length\n");
        c_failures++;
    }
    lambda_print_ln(empty); /* [] */
    lambda_flush();
    lambda_release(empty);

    Value *init = lambda_box_int(5);
    Value *four = lambda_arrayg_new(4, init);
    lambda_release(init); /* the array holds its own four references */
    if (arrayg_elem_int(four, 3) != 5) {
        fprintf(stderr, "FAIL arrayg: new() content\n");
        c_failures++;
    }
    Value *clone = lambda_arrayg_clone(four);
    if (arrayg_elem_int(clone, 0) != 5 || arrayg_elem_int(clone, 3) != 5) {
        fprintf(stderr, "FAIL arrayg: clone() content\n");
        c_failures++;
    }
    lambda_release(clone);
    lambda_release(four);

    if (live_values != base) {
        fprintf(stderr, "FAIL arrayg: live=%lld, baseline=%lld\n",
                (long long)live_values, (long long)base);
        c_failures++;
    }
    printf("arrayg: ok\n");
}

int main(void) {
    check_pool_size_classes();
    check_arena_reservation();
    check_arrayg();
    return c_failures ? 1 : 0;
}
"""


def compile_and_run_c_checks(work: str, atomic_rc: bool) -> None:
    sys.path.insert(0, ROOT)
    from runtime import RUNTIME_C  # noqa: E402  (import after path fix)
    variant = "atomic" if atomic_rc else "plain"
    src = os.path.join(work, f"runtime_checks_{variant}.c")
    exe = os.path.join(work, f"runtime_checks_{variant}")
    with open(src, "w", encoding="utf-8") as f:
        f.write(RUNTIME_C)
        f.write(C_RUNTIME_CHECKS)
    cmd = [CLANG, "-O1", "-Wno-override-module"]
    if atomic_rc:
        cmd.append("-DLAMBDA_ATOMIC_RC=1")
    cmd.extend([src, "-o", exe])
    if os.name != "nt":
        cmd.extend(["-lm", "-pthread"])
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode != 0:
        raise CheckFailure(
            f"C checks ({variant}) failed to compile:\n{proc.stderr.strip()}")
    run = subprocess.run([exe], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=120)
    out = run.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    err = run.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    if run.returncode != 0:
        raise CheckFailure(err.strip() or f"C checks ({variant}) failed")
    for marker in ("F1: pool size classes ok", "F3: arena reservation ok",
                   "arrayg: ok", "[7, 9, 3]", "[]"):
        if marker not in out:
            raise CheckFailure(f"C checks ({variant}): missing {marker!r} "
                               f"in stdout {out!r}")


@check("c_runtime_invariants_plain_and_atomic")
def c_runtime_invariants():
    if CLANG is None:
        skip("clang not on PATH")
    with tempfile.TemporaryDirectory(prefix="rt-c-") as work:
        compile_and_run_c_checks(work, atomic_rc=False)
        compile_and_run_c_checks(work, atomic_rc=True)


# ---------------------------------------------------------------------------
# PR.md C7: LAMBDA_RAWLOOP=1 used to free the pmap environment twice (the
# generated destructor already calls lambda_free_env).
# ---------------------------------------------------------------------------

@check("c7_rawloop_pmap_env")
def c7_rawloop_pmap_env():
    source = ("(float->int (arrayf-get "
              "(arrayf-pmap (λi: Int. (sin (int->float i))) 64) 0))")
    with tempfile.TemporaryDirectory(prefix="rt-c7-") as work:
        exe = build(source, "rawloop_pmap", work,
                    env_extra={"LAMBDA_RAWLOOP": "1"})
        proc = run_exe(exe, work, env_extra={"LAMBDA_THREADS": "4"})
        expect_output(proc, "0\n")
        st = stats(proc)
        if st["env_allocs"] != st["env_frees"] or st["live"] != 0:
            raise CheckFailure(
                f"env leak or value leak: {st}")


# ---------------------------------------------------------------------------
# PR.md S13: immortalised values used to be invisible to the leak report.
# ---------------------------------------------------------------------------

@check("s13_immortal_visible_in_stats")
def s13_immortal_visible():
    with tempfile.TemporaryDirectory(prefix="rt-s13-") as work:
        exe = build("a → (immortalize (arrayb 2 65))\n0", "immortal", work)
        proc = run_exe(exe, work)
        expect_output(proc, "0\n")
        st = stats(proc)
        if st["immortal"] != 1:
            raise CheckFailure(
                f"expected immortal=1 in statistics, got {st}")
        # live= still excludes intentionally immortal values, so ordinary
        # leak checking keeps working.
        if st["live"] != 0:
            raise CheckFailure(f"expected live=0, got {st}")


# ---------------------------------------------------------------------------
# PR.md float printing: shortest round-trip, normalized NaN sign.
# ---------------------------------------------------------------------------

@check("float_shortest_roundtrip_and_nan_sign")
def float_shortest_roundtrip():
    with tempfile.TemporaryDirectory(prefix="rt-f-") as work:
        exe = build("[(0.1), (1e-7), (1000.0), (1e16), (1e15), (-0.0)]",
                    "float_shortest", work)
        proc = run_exe(exe, work)
        expect_output(proc, "[0.1, 1e-07, 1000.0, 1e+16, "
                            "1000000000000000.0, -0.0]\n")
        exe = build("(- (/ 1.0 0.0) (/ 1.0 0.0))", "float_nan", work)
        proc = run_exe(exe, work)
        expect_output(proc, "nan\n")
        exe = build('[(float->str 0.1), (float->str (* pi 2.0))]', "float_str",
                    work)
        proc = run_exe(exe, work)
        expect_output(proc, '["0.1", "6.283185307179586"]\n')


def main() -> int:
    print("runtime regression checks (PR.md runtime findings)")
    failures = [r for r in RESULTS if r[0] == "FAIL"]
    skipped = [r for r in RESULTS if r[0] == "SKIP"]
    passes = [r for r in RESULTS if r[0] == "PASS"]
    print(f"\n{len(passes)} passed, {len(failures)} failed, "
          f"{len(skipped)} skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
