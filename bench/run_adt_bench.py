#!/usr/bin/env python3
"""Build and run the product/sum ADT benchmark in four implementations."""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
BUILD = os.path.join(BENCH, "build")
os.makedirs(BUILD, exist_ok=True)


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd} failed:\n{p.stderr}")
    return p


def build():
    run(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "adt_bench.c"),
         "-o", os.path.join(BUILD, "adt_c.exe")])
    run(["clang++", "-O3", "-std=c++20", "-DNDEBUG",
         os.path.join(BENCH, "adt_bench.cpp"),
         "-o", os.path.join(BUILD, "adt_cpp.exe")])
    rust = ["rustc", "-C", "opt-level=3", "--edition", "2021",
            os.path.join(BENCH, "adt_bench.rs"),
            "-o", os.path.join(BUILD, "adt_rs.exe")]
    lld = r"C:\Program Files\LLVM\bin\lld-link.exe"
    if os.name == "nt" and os.path.exists(lld):
        rust[1:1] = ["-C", f"linker={lld}"]
    run(rust)
    run([sys.executable, os.path.join(ROOT, "lambda.py"),
         os.path.join(BENCH, "adt_bench.lambda"),
         "-o", os.path.join(BUILD, "adt_lambda")])


def as_float(text: str) -> float:
    return float(text.strip())


def measure(exe: str, runs: int = 5):
    times = []
    value = None
    for i in range(runs + 1):
        start = time.perf_counter()
        p = run([exe])
        elapsed = time.perf_counter() - start
        if i == 0:
            value = as_float(p.stdout)
        else:
            times.append(elapsed)
    return statistics.median(times), value


def main() -> int:
    build()
    files = {
        "Lambda": os.path.join(BUILD, "adt_lambda.exe"),
        "C": os.path.join(BUILD, "adt_c.exe"),
        "C++": os.path.join(BUILD, "adt_cpp.exe"),
        "Rust": os.path.join(BUILD, "adt_rs.exe"),
    }
    results = {}
    reference = None
    for name, exe in files.items():
        median, value = measure(exe)
        results[name] = {"seconds": median, "value": value}
        if reference is None:
            reference = value
        elif abs(value - reference) > abs(reference) * 1e-12:
            raise RuntimeError(f"{name} result mismatch: {value} vs {reference}")
    base = results["C"]["seconds"]
    lines = ["# ADT benchmark", "",
             "20M iterations x 3 repetitions; median of 5 measured runs.", "",
             "| implementation | time | vs C |", "| --- | ---: | ---: |"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['seconds']*1000:.1f} ms | {r['seconds']/base:.2f}x |")
    text = "\n".join(lines) + "\n"
    print(text)
    with open(os.path.join(BENCH, "adt_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(BENCH, "adt_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
