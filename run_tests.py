"""Compile-and-run regression tests for the Lambda compiler.

Each case compiles a program, optionally pipes stdin, runs it, and compares
stdout exactly.  Every successful run also enables the runtime allocation
statistics and requires ``live=0`` so reference-counting leaks fail the suite.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "tests")

# name, expected stdout, compiler flags, expected compile-failure substring, stdin
CASES = [
    # core / syntax / closures
    ("hello", "3\n", [], None, None),
    ("fib", "75025\n", [], None, None),
    ("map", "[2, 3, 4, 5]\n", [], None, None),
    ("partial", "42\n", [], None, None),
    ("compose", "41\n", [], None, None),
    ("hof", "42\n", [], None, None),
    ("local_fn_value", "25\n", [], None, None),
    ("local_rec_value", "42\n", [], None, None),
    ("capture_chain", "11\n", [], None, None),
    ("mutual", "true\n", [], None, None),
    ("polymorphic", "8\n", [], None, None),
    ("top_values", "7\n", [], None, None),
    ("strings", "hello, world\n", [], None, None),
    ("strings2", "7\n", [], None, None),
    ("let", "6\n", [], None, None),
    ("equality", "true\n", [], None, None),
    ("lists", "[7, 8]\n", [], None, None),
    ("empty", "true\n", [], None, None),
    ("str_conv", "[456, 3, 52]\n", [], None, None),
    ("deeploop", "5000000\n", [], None, None),
    ("zcombinator", "3628800\n", [], None, None),
    ("fix", "3628800\n", [], None, None),
    ("yfix", "5050\n", [], None, None),
    ("fix_inline", "3628800\n", [], None, None),
    ("fix_first_class", "720\n", [], None, None),
    ("cse", "0\n", [], None, None),
    ("cse30", "0\n", [], None, None),
    ("side_effects", "x\nx\n2\n", [], None, None),
    ("churn", "2\n", [], None, None),
    ("listchurn", "false\n", [], None, None),
    ("closure_churn", "1\n", [], None, None),
    # float / scientific
    ("float_add", "3.75\n", [], None, None),
    ("float_sqrt", "1.4142135623730951\n", [], None, None),
    ("float_sum", "6.0\n", [], None, None),
    ("float_sum_annot", "6.0\n", [], None, None),
    ("float_annot", "9.0\n", [], None, None),
    ("float_map", "[2.0, 4.0, 6.0]\n", [], None, None),
    ("float_closure", "[2.0, 4.0, 6.0]\n", [], None, None),
    ("float_partial", "3.5\n", [], None, None),
    ("float_pmap", "[2.0, 4.0, 6.0]\n", [], None, None),
    ("float_firstclass", "[1.0, 2.0, 3.0, 4.0]\n", [], None, None),
    ("float_convert", "4.0\n", [], None, None),
    ("float_str", "6.2831853071795862\n", [], None, None),
    ("float_trig", "1.0\n", [], None, None),
    ("newton", "true\n", [], None, None),
    ("fixed_point", "true\n", [], None, None),
    ("transcendentals", "[true, true, true, true, true]\n", [], None, None),
    ("int_sum_range", "10\n", [], None, None),
    ("range", "[0, 1, 2, 3, 4]\n", [], None, None),
    ("bitwise", "[8, 14, -1, 16, 4, 8]\n", [], None, None),
    # flat arrays
    ("arrayf_basic", "[5.0, 3.5, 1.25]\n", [], None, None),
    ("arraydyn", "[5.0, 3.5, 1.25]\n", ["--dynamic"], None, None),
    ("arrayi_loop", "332833500\n", [], None, None),
    ("arrayb_basic", "32640\n", [], None, None),
    ("array_clone", "[9.0, 1.0]\n", [], None, None),
    # includes
    ("include_basic", "42\n", [], None, None),
    ("include_nested", "40\n", [], None, None),
    ("include_once", "42\n", [], None, None),
    ("list_lib", "[3, 3, 2, 6]\n", [], None, None),
    # parallel
    ("pmap_basic", "[2, 3, 4, 5]\n", [], None, None),
    ("pmap_capture", "[11, 12, 13]\n", [], None, None),
    ("pmap_boxed_capture", "[11, 12, 13]\n", [], None, None),
    ("pmap_dynamic", "[2, 3, 4, 5]\n", ["--dynamic"], None, None),
    ("comp_seq", "[2, 3, 4]\n", [], None, None),
    ("comp_par", "[2, 3, 4]\n", [], None, None),
    ("lambda_p", "42\n", [], None, None),
    ("nth_prime", "10086767\n", [], None, None),
    ("legacy_nth_prime", "10086767\n", [], None, None),
    ("nth_prime_parallel", "10086767\n", [], None, None),
    ("nth_prime_sieve", "10086767\n", [], None, None),
    ("nth_prime_sieve_100k", "1299709\n", [], None, None),
    # modes / diagnostics
    ("hello_dynamic", "3\n", ["--dynamic"], None, None),
    ("map_dynamic", "[2, 3, 4, 5]\n", ["--dynamic"], None, None),
    ("fib_nomt", "75025\n", ["--no-musttail"], None, None),
    # terminal input (off by default)
    ("input_int", "41\n", ["--input"], None, "41\n"),
    ("input_line", "5\n", ["--input"], None, "hello\n"),
    ("input_float", "3.0\n", ["--input"], None, "1.5\n"),
    # expected failures
    ("typeerror", None, ["--strict"], "type error", None),
    ("float_strict", None, ["--strict"], "type error", None),
    ("unbound", None, [], "unbound variable", None),
    ("syntax", None, [], "syntax error", None),
    ("include_cycle_a", None, [], "include cycle", None),
    ("include_body", None, [], "definitions only", None),
    ("pmap_impure", None, [], "pmap requires a provably pure function", None),
    ("array_pmap_impure", None, [], "pmap requires a provably pure function", None),
    ("lambda_p_impure", None, [], "λp function body must be pure", None),
    ("input_disabled", None, [], "terminal input is disabled", None),
]


def run_case(name: str, expected: Optional[str], flags, fail_substr,
             stdin_data: Optional[str], workdir: str):
    src = os.path.join(TESTS, name + ".lambda")
    exe = os.path.join(workdir, name)
    cmd = [sys.executable, os.path.join(ROOT, "lambda.py"), src, "-o", exe] + flags
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          encoding="utf-8", errors="replace")
    compile_time = time.time() - start

    if fail_substr is not None:
        if proc.returncode == 0:
            return False, f"expected compilation to fail ({fail_substr})"
        if fail_substr not in proc.stderr:
            return False, (
                f"expected '{fail_substr}' in compiler stderr, got:\n{proc.stderr}")
        return True, f"compile-fail ok ({compile_time:.2f}s)"

    if proc.returncode != 0:
        return False, f"compilation failed:\n{proc.stderr}"

    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"
    if not os.path.exists(exe):
        return False, "compiler did not produce an executable"

    env = dict(os.environ)
    env["LAMBDA_STATS"] = "1"
    run_start = time.time()
    run = subprocess.run([exe], input=stdin_data, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env, timeout=300,
                         encoding="utf-8", errors="replace")
    run_time = time.time() - run_start
    stdout = run.stdout.replace("\r\n", "\n")
    stderr = run.stderr.replace("\r\n", "\n")

    if run.returncode != 0:
        return False, f"runtime exited {run.returncode}:\n{stderr}"
    if stdout != expected:
        return False, f"stdout mismatch\n  expected: {expected!r}\n  actual:   {stdout!r}"
    m = re.search(r"live=(\d+)", stderr)
    if m is None:
        return False, f"missing allocation statistics in stderr:\n{stderr}"
    live = int(m.group(1))
    if live != 0:
        return False, f"reference-count leak: live={live}\n{stderr}"
    return True, f"ok (compile {compile_time:.2f}s, run {run_time:.2f}s, live=0)"


def main() -> int:
    if not os.path.isdir(TESTS):
        print("tests directory not found", file=sys.stderr)
        return 2
    workdir = tempfile.mkdtemp(prefix="lambda-tests-")
    failures = []
    total_start = time.time()
    for name, expected, flags, fail_substr, stdin_data in CASES:
        ok, message = run_case(name, expected, flags, fail_substr, stdin_data, workdir)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {message}")
        if not ok:
            failures.append((name, message))
    elapsed = time.time() - total_start
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed in {elapsed:.2f}s")
    if failures:
        print("\nFailures:", file=sys.stderr)
        for name, message in failures:
            print(f"  {name}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
