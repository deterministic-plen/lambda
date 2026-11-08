"""Fair runtime comparison: Lambda vs C vs C++ vs Rust.

All four implementations use the same algorithms, constants, iteration counts
and thread counts.  Every process runs the algorithm several times so process
startup is amortized; the reported number is the median per-call wall time of
three measured runs after one discarded warm-up.
"""

from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
BUILD = os.path.join(BENCH, "build")
NTH = 670000
PRIME = 10086767
NEWTON_REPS = 1_000_000
NEWTON_STEPS = 100
ARRAY_N = 10_000_000
ARRAY_SUM = 49_999_995_000_000
ARRAY_REPS = 3

# mode, threads, repetitions per process
CONFIGS = [
    ("seq", 1, 3),
    ("par", 1, 3),
    ("par", 16, 12),
    ("sieve", 1, 3),
    ("sieve", 16, 20),
]

IMPLEMENTATIONS = ["lambda", "c", "cpp", "rust"]
LABELS = {"lambda": "Lambda", "c": "C", "cpp": "C++", "rust": "Rust"}


def tool_version(cmd) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def cpu_name() -> str:
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-ItemProperty 'HKLM:\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0').ProcessorNameString"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        name = p.stdout.strip()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    return platform.processor() or "unknown CPU"


def newton_expected(reps: int) -> float:
    x = 2.0
    for _ in range(NEWTON_STEPS):
        x -= (x * x - 2.0) / (2.0 * x)
    return x * reps


def build_lambda(mode: str, reps: int) -> str:
    template = os.path.join(BENCH, f"lambda_{mode}.lambda")
    with open(template, "r", encoding="utf-8") as f:
        source = f.read()
    if mode == "newton":
        source = source.replace("(run 1000000 0.0)", f"(run {reps} 0.0)")
        if f"(run {reps} 0.0)" not in source:
            raise RuntimeError(f"could not set repetitions in {template}")
    elif mode == "array":
        source = source.replace("(bench 3 0)", f"(bench {reps} 0)")
        if f"(bench {reps} 0)" not in source:
            raise RuntimeError(f"could not set repetitions in {template}")
    else:
        source = source.replace("(run 3)", f"(run {reps})")
        if f"(run {reps})" not in source:
            raise RuntimeError(f"could not set repetitions in {template}")
    src = os.path.join(BUILD, f"lambda_{mode}_r{reps}.lambda")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)
    exe = os.path.join(BUILD, f"lam_{mode}_r{reps}")
    p = subprocess.run(
        [sys.executable, os.path.join(ROOT, "lambda.py"), src, "-o", exe],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"Lambda build failed for {mode}:\n{p.stderr}")
    if os.name == "nt" and not exe.lower().endswith(".exe"):
        exe += ".exe"
    return exe


def native_exe(name: str) -> str:
    exe = os.path.join(BUILD, name)
    if os.name == "nt":
        exe += ".exe"
    if not os.path.exists(exe):
        raise RuntimeError(f"missing benchmark executable: {exe}")
    return exe


def run_once(exe: str, mode: str, reps: int, threads: int, lambda_impl: bool) -> float:
    env = dict(os.environ)
    if lambda_impl:
        env["LAMBDA_THREADS"] = str(threads)
        env.pop("LAMBDA_STATS", None)
        cmd = [exe]
    else:
        env["BENCH_THREADS"] = str(threads)
        cmd = [exe, mode, str(reps)]
    start = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=600)
    elapsed = time.perf_counter() - start
    if p.returncode != 0:
        raise RuntimeError(f"{exe} exited {p.returncode}:\n{p.stderr}")
    text = p.stdout.strip()
    if mode == "newton":
        try:
            value = float(text)
        except ValueError as exc:
            raise RuntimeError(f"{exe} gave non-float output {text!r}") from exc
        expected = newton_expected(reps)
        # Different compilers may contract x*x - 2.0 into an FMA, changing the
        # last bits; compare at 1e-9 relative rather than bit-for-bit.
        if abs(value - expected) > max(1e-3, abs(expected) * 1e-9):
            raise RuntimeError(
                f"{exe} gave {value!r}, expected about {expected!r}")
    elif mode == "array":
        if text != str(reps * ARRAY_SUM):
            raise RuntimeError(
                f"{exe} gave {text!r}, expected {reps * ARRAY_SUM}")
    else:
        if text != str(reps * PRIME):
            raise RuntimeError(f"{exe} gave {text!r}, expected {reps * PRIME}")
    return elapsed


def median_time(exe: str, mode: str, reps: int, threads: int,
                lambda_impl: bool) -> dict:
    # One discarded warm-up, then three measured runs.
    run_once(exe, mode, reps, threads, lambda_impl)
    samples = [run_once(exe, mode, reps, threads, lambda_impl)
               for _ in range(3)]
    median = statistics.median(samples)
    return {
        "median_total_s": median,
        "median_ms_per_call": median / reps * 1000.0,
        "min_ms_per_call": min(samples) / reps * 1000.0,
        "samples_s": samples,
    }


def run_capture(cmd, timeout: int = 600):
    start = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    elapsed = time.perf_counter() - start
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {p.returncode}:\n{p.stderr}")
    return p.stdout, elapsed


def main() -> int:
    os.makedirs(BUILD, exist_ok=True)
    # Check that native builds exist; Lambda variants are built per config.
    natives = {
        "c": native_exe("nth_c"),
        "cpp": native_exe("nth_cpp"),
        "rust": native_exe("nth_rs"),
    }
    metadata = {
        "cpu": cpu_name(),
        "logical_processors": os.cpu_count(),
        "platform": platform.platform(),
        "clang": tool_version(["clang", "--version"]),
        "clangxx": tool_version(["clang++", "--version"]),
        "rustc": tool_version(["rustc", "--version"]),
        "python": sys.version.split()[0],
        "nth": NTH,
        "prime": PRIME,
        "compiler_flags": {
            "lambda": "clang -O3 (driver default)",
            "c": "clang -O3 -DNDEBUG",
            "cpp": "clang++ -O3 -std=c++20 -DNDEBUG",
            "rust": "rustc -C opt-level=3",
        },
    }
    print(f"CPU: {metadata['cpu']} ({metadata['logical_processors']} logical)")
    print(f"clang: {metadata['clang']}")
    print(f"rustc: {metadata['rustc']}")
    print("Median per call, three runs after one warm-up; process startup included.\n")

    results = []
    for mode, threads, reps in CONFIGS:
        lam_exe = build_lambda(mode, reps)
        row = {"mode": mode, "threads": threads, "reps": reps, "times_ms": {}}
        for impl in IMPLEMENTATIONS:
            exe = lam_exe if impl == "lambda" else natives[impl]
            time_info = median_time(exe, mode, reps, threads, impl == "lambda")
            row["times_ms"][impl] = time_info["median_ms_per_call"]
            row.setdefault("details", {})[impl] = time_info
            print(f"  {mode} threads={threads} reps={reps} {LABELS[impl]}: "
                  f"{time_info['median_ms_per_call']:.3f} ms/call")
        results.append(row)
        print()

    # Markdown table
    header = ("| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | "
              "Lambda/C | Lambda/C++ | Lambda/Rust |")
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, sep]
    for row in results:
        t = row["times_ms"]
        lam = t["lambda"]
        lines.append(
            f"| {row['mode']} | {row['threads']} | {lam:.3f} | {t['c']:.3f} | "
            f"{t['cpp']:.3f} | {t['rust']:.3f} | {lam / t['c']:.2f}x | "
            f"{lam / t['cpp']:.2f}x | {lam / t['rust']:.2f}x |")
    table = "\n".join(lines)
    print(table)

    # f64 Newton kernel: report microseconds per 100-step solve.
    print()
    newton_row = {"mode": "newton", "threads": 1, "reps": NEWTON_REPS,
                  "times_us": {}}
    newton_exe = build_lambda("newton", NEWTON_REPS)
    for impl in IMPLEMENTATIONS:
        exe = newton_exe if impl == "lambda" else natives[impl]
        info = median_time(exe, "newton", NEWTON_REPS, 1, impl == "lambda")
        us = info["median_total_s"] / NEWTON_REPS * 1e6
        newton_row["times_us"][impl] = us
        newton_row.setdefault("details", {})[impl] = info
        print(f"  newton reps={NEWTON_REPS} {LABELS[impl]}: {us:.4f} us/solve")
    nt = newton_row["times_us"]
    nl = nt["lambda"]
    newton_header = ("| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | "
                     "Lambda/C | Lambda/C++ | Lambda/Rust |")
    newton_sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    newton_table = "\n".join([
        newton_header,
        newton_sep,
        f"| per solve | {nl:.4f} | {nt['c']:.4f} | {nt['cpp']:.4f} | "
        f"{nt['rust']:.4f} | {nl / nt['c']:.2f}x | {nl / nt['cpp']:.2f}x | "
        f"{nl / nt['rust']:.2f}x |",
    ])
    print(newton_table)

    # Large flat array: allocate, fill, and sum 10M i64 values.
    print()
    array_row = {"mode": "array", "threads": 1, "reps": ARRAY_REPS,
                 "n": ARRAY_N, "times_ms": {}}
    array_exe = build_lambda("array", ARRAY_REPS)
    for impl in IMPLEMENTATIONS:
        exe = array_exe if impl == "lambda" else natives[impl]
        info = median_time(exe, "array", ARRAY_REPS, 1, impl == "lambda")
        array_row["times_ms"][impl] = info["median_ms_per_call"]
        array_row.setdefault("details", {})[impl] = info
        print(f"  array n={ARRAY_N} {LABELS[impl]}: "
              f"{info['median_ms_per_call']:.3f} ms/pass")
    at = array_row["times_ms"]
    al = at["lambda"]
    array_table = "\n".join([
        "| large array (10M i64 fill+sum) | Lambda ms | C ms | C++ ms | Rust ms | "
        "Lambda/C | Lambda/C++ | Lambda/Rust |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| per pass | {al:.3f} | {at['c']:.3f} | {at['cpp']:.3f} | "
        f"{at['rust']:.3f} | {al / at['c']:.2f}x | {al / at['cpp']:.2f}x | "
        f"{al / at['rust']:.2f}x |",
    ])
    print(array_table)

    # Pure-Lambda bigint: F(1,000,000).  Python's built-in bigint is the
    # available big-number reference on this machine (GMP/Boost not installed).
    print()
    bigint_exe = os.path.join(BUILD, "lam_bigint")
    build = subprocess.run(
        [sys.executable, os.path.join(ROOT, "lambda.py"),
         os.path.join(ROOT, "examples", "bigint_fib1m.lambda"),
         "-o", bigint_exe],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    if build.returncode != 0:
        raise RuntimeError("bigint example build failed:\n" + build.stderr)
    if os.name == "nt" and not bigint_exe.lower().endswith(".exe"):
        bigint_exe += ".exe"
    py_ref = [sys.executable, os.path.join(BENCH, "fib1m_ref.py")]

    lam_out, _ = run_capture([bigint_exe])
    py_out, _ = run_capture(py_ref)
    if lam_out != py_out:
        raise RuntimeError("F(1,000,000) output differs from Python reference")
    ref_digits = len(lam_out.strip())
    run_capture([bigint_exe])  # warm-up
    run_capture(py_ref)
    lam_samples = [run_capture([bigint_exe])[1] for _ in range(3)]
    py_samples = [run_capture(py_ref)[1] for _ in range(3)]
    lam_t = statistics.median(lam_samples)
    py_t = statistics.median(py_samples)
    bigint_row = {
        "n": 1_000_000,
        "digits": ref_digits,
        "lambda_s": lam_t,
        "python_s": py_t,
        "lambda_samples_s": lam_samples,
        "python_samples_s": py_samples,
    }
    bigint_table = "\n".join([
        "| bigint | Lambda | Python (C bigint) | Lambda / Python |",
        "|---|---:|---:|---:|",
        f"| F(1,000,000), {ref_digits} digits (compute + output) | "
        f"{lam_t * 1000:.1f} ms | {py_t * 1000:.1f} ms | "
        f"{lam_t / py_t:.2f}x |",
    ])
    print(bigint_table)

    # Compute-only comparison.  Lambda's executable is timed as a process
    # (startup included); Python and gmpy2 report their own in-process compute
    # time so decimal conversion and interpreter startup do not hide the gap.
    compute_lam_exe = os.path.join(BUILD, "lam_bigint_compute")
    build = subprocess.run(
        [sys.executable, os.path.join(ROOT, "lambda.py"),
         os.path.join(BENCH, "bigint_fib1m_compute.lambda"),
         "-o", compute_lam_exe],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    if build.returncode != 0:
        raise RuntimeError("bigint compute build failed:\n" + build.stderr)
    if os.name == "nt" and not compute_lam_exe.lower().endswith(".exe"):
        compute_lam_exe += ".exe"
    run_capture([compute_lam_exe])
    compute_lam_samples = [run_capture([compute_lam_exe])[1] for _ in range(3)]
    compute_lam_t = statistics.median(compute_lam_samples) * 1000.0

    def bigint_compute_ref(backend):
        try:
            out, _ = run_capture(
                [sys.executable, os.path.join(BENCH, "bigint_compute_ref.py"),
                 backend], timeout=180)
        except RuntimeError:
            return None
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    py_compute = bigint_compute_ref("python")
    gmp_compute = bigint_compute_ref("gmp")
    py_compute_ms = py_compute["compute_ms"] if py_compute else float("nan")
    gmp_compute_ms = (gmp_compute["compute_ms"]
                      if gmp_compute else float("nan"))
    gmp_cell = f"{gmp_compute_ms:.2f} ms" if gmp_compute else "n/a"
    gmp_ratio = (f"{compute_lam_t / gmp_compute_ms:.1f}x"
                 if gmp_compute else "n/a")
    compute_table = "\n".join([
        "| bigint compute | Lambda | Python int | gmpy2 (GMP) | "
        "Lambda / Python | Lambda / gmpy2 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| F(1,000,000) compute only | {compute_lam_t:.1f} ms | "
        f"{py_compute_ms:.1f} ms | {gmp_cell} | "
        f"{compute_lam_t / py_compute_ms:.2f}x | {gmp_ratio} |",
    ])
    print()
    print(compute_table)
    bigint_compute_row = {
        "n": 1_000_000,
        "lambda_ms": compute_lam_t,
        "python_ms": py_compute_ms,
        "gmpy2_ms": gmp_compute_ms if gmp_compute else None,
        "python_samples_ms": py_compute["samples_ms"] if py_compute else [],
        "gmpy2_samples_ms": gmp_compute["samples_ms"] if gmp_compute else [],
        "lambda_samples_s": compute_lam_samples,
        "bits": py_compute["bits"] if py_compute else None,
    }

    with open(os.path.join(BENCH, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"metadata": metadata, "results": results,
                   "newton": newton_row, "array": array_row,
                   "bigint": bigint_row,
                   "bigint_compute": bigint_compute_row}, f, indent=2)
    with open(os.path.join(BENCH, "results.md"), "w", encoding="utf-8") as f:
        f.write("# Lambda benchmark results\n\n")
        f.write(f"CPU: {metadata['cpu']} ({metadata['logical_processors']} logical)\n\n")
        f.write(table + "\n\n" + newton_table + "\n\n" + array_table + "\n\n"
                + bigint_table + "\n\n" + compute_table + "\n")
    print("\nWrote bench/results.json and bench/results.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
