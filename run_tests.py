"""Compile-and-run regression tests for the Lambda compiler.

Each case compiles a program, optionally pipes stdin, runs it, and compares
stdout exactly.  Every successful run also enables the runtime allocation
statistics and requires ``live=0`` so reference-counting leaks fail the suite.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "tests")


def stream_frame(tag: int, stream_id: int, payload: bytes) -> bytes:
    return (b"LMB1" + struct.pack("<BBHII", tag, 0, 0, stream_id, len(payload))
            + payload)


STREAM_F64 = stream_frame(1, 1, struct.pack("<2d", 1.0, -2.0))

# name, expected stdout (str or bytes), compiler flags, expected compile-failure substring, stdin
CASES = [
    # core / syntax / closures
    ("hello", "3\n", [], None, None),
    ("array_of", "[3.0, 2.5, 3.0, 255.0, 0.0]\n", [], None, None),
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
    ("logic", "[true, false, false, true, true, false, false, true]\n", [], None, None),
    ("logic_shortcircuit", "[false, true, false, false]\n", [], None, None),
    ("logic_equiv", "[false, false, false, false, true, true]\n", [], None, None),
    ("bitwise_and", "[8, 8]\n", [], None, None),
    # namespacing and borrow inspection
    ("namespace", "4.0\n", [], None, None),
    ("borrow_inspect", "[7, 35, 7]\n", [], None, None),
    ("borrow_get", "9\n", [], None, None),
    ("borrow_consume", "", [], "borrowed inspection", None),
    # flat arrays
    ("arrayf_basic", "[5.0, 3.5, 1.25]\n", [], None, None),
    ("arraydyn", "[5.0, 3.5, 1.25]\n", ["--dynamic"], None, None),
    ("arrayi_loop", "332833500\n", [], None, None),
    ("arrayb_basic", "32640\n", [], None, None),
    ("array_clone", "[9.0, 1.0]\n", [], None, None),
    ("array_rebind", "7\n", [], None, None),
    ("stream_f64", STREAM_F64, ["--data"], None, None),
    ("enum_turnstile", "0\n", [], None, None),
    ("enum_coffee", "Idle\n", [], None, None),
    ("enum_match_turnstile", "0\n", [], None, None),
    ("enum_match_deep", "0\n", [], None, None),
    ("record_complex", "[4.0, 6.0]\n", [], None, None),
    ("record_if", "3.0\n", [], None, None),
    ("record_vector", "[4.0, 6.0, 52.0]\n", [], None, None),
    ("record_suffix", "[1.0, 2.0]\n", [], None, None),
    ("record_suffix_last", "[1.0, 2.0]\n", [], None, None),
    ("sum_option", "3.5\n", [], None, None),
    ("sum_shape", "[12.0, 6.0, 0.0]\n", [], None, None),
    ("borrow_read", "6\n", [], None, None),
    ("array_sum_i", "15\n", [], None, None),
    ("array_sum_f", "6.0\n", [], None, None),
    ("arrayi_pmap", "[0, 2, 6, 12, 20]\n", [], None, None),
    ("arrayi_pmap_chunk", "[0, 10, 20, 30]\n", [], None, None),
    ("bigint_fib_100", "354224848179261915075\n", [], None, None),
    ("bigint_fib_1000", "43466557686937456435688527675040625802564660517371780402481729089536555417949051890403879840079255169295922593080322634775209689623239873322471161642996440906533187938298969649928516003704476137795166849228875\n", [], None, None),
    ("bigint_fib_1000_dynamic", "43466557686937456435688527675040625802564660517371780402481729089536555417949051890403879840079255169295922593080322634775209689623239873322471161642996440906533187938298969649928516003704476137795166849228875\n", ["--dynamic"], None, None),
    ("bigint_fib_100000_sum", "1168320704920\n", [], None, None),
    ("bigint_ops", "3\n9999999999999999999999999991000000000000000000000000000999999999999999999999999998999999999999999999999999998000000000000000000000000001999999998000000001\n", [], None, None),
    # includes
    ("include_basic", "42\n", [], None, None),
    ("include_nested", "40\n", [], None, None),
    ("include_once", "42\n", [], None, None),
    ("include_stdlib", "1024\n", [], None, None),
    ("complex_safe_div", "[1.0, 2.0, 1.0]\n", [], None, None),
    ("adt_tree", "3\n", [], None, None),
    ("adt_boxed", "hi\n", [], None, None),
    ("adt_listfield", "[1, 2, 3]\n", [], None, None),
    ("adt_generic", "1\n", [], None, None),
    ("ast_interp", "14\n", [], None, None),
    ("ast_compiler", "10\n", [], None, None),
    ("adt_color", "2\n", [], None, None),
    ("adt_float", "4.0\n", [], None, None),
    ("adt_five", "15\n", [], None, None),
    ("adt_rose", "4\n", [], None, None),
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
    ("arrayf_pmap", "[1000, 295, 700]\n", [], None, None),
    ("typeerror", None, ["--strict"], "type error", None),
    ("float_strict", None, ["--strict"], "type error", None),
    ("unbound", None, [], "unbound variable", None),
    ("syntax", None, [], "syntax error", None),
    ("syntax_defs_only", None, [], "program ends after its definitions", None),
    ("syntax_unclosed", None, [], "while parsing parenthesized expression", None),
    ("syntax_list_unclosed", None, [], "while parsing list or comprehension", None),
    ("syntax_lambda_eof", None, [], "while parsing lambda expression", None),
    ("infix_warn", None, ["--strict"], "Lambda has no infix operators", None),
    ("include_cycle_a", None, [], "include cycle", None),
    ("include_body", None, [], "definitions only", None),
    ("pmap_impure", None, [], "pmap requires a provably pure function", None),
    ("array_pmap_impure", None, [], "pmap requires a provably pure function", None),
    ("array_consume_reuse", None, [], "consumed by a destructive array update", None),
    ("lambda_p_impure", None, [], "λp function body must be pure", None),
    ("input_disabled", None, [], "terminal input is disabled", None),
    ("stream_no_data", None, [], "binary output is disabled", None),
    ("enum_mix", None, ["--strict"], "numeric operator expects Int or Float", None),
    ("enum_match_missing", None, ["--strict"], "non-exhaustive match", None),
    ("record_firstclass", None, [], "product-typed functions cannot be captured", None),
    ("record_mix", None, ["--strict"], "numeric operator expects Int or Float", None),
    ("sum_binder_arity", None, ["--strict"], "payload binders", None),
    ("borrow_mutate", None, [], "cannot mutate a borrowed", None),
    ("borrow_return", None, [], "borrowed parameter cannot be returned", None),
]


def run_case(name: str, expected, flags, fail_substr,
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
    binary = isinstance(expected, (bytes, bytearray))
    if binary:
        run = subprocess.run([exe], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, timeout=300)
        stdout = run.stdout
        stderr = run.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
    else:
        run = subprocess.run([exe], input=stdin_data, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env, timeout=300,
                             encoding="utf-8", errors="replace")
        stdout = run.stdout.replace("\r\n", "\n")
        stderr = run.stderr.replace("\r\n", "\n")
    run_time = time.time() - run_start

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
