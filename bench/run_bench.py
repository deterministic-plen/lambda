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

    with open(os.path.join(BENCH, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"metadata": metadata, "results": results,
                   "newton": newton_row}, f, indent=2)
    with open(os.path.join(BENCH, "results.md"), "w", encoding="utf-8") as f:
        f.write("# Lambda benchmark results\n\n")
        f.write(f"CPU: {metadata['cpu']} ({metadata['logical_processors']} logical)\n\n")
        f.write(table + "\n\n" + newton_table + "\n")
    print("\nWrote bench/results.json and bench/results.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
