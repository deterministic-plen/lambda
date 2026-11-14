#!/usr/bin/env python3
"""Benchmark the flat-arena tree prototype against native implementations.

The Lambda program uses an explicit integer node stack (a prototype for a
recursive-ADT arena).  It is not yet generated ADT code; the point is to expose
layout/codegen costs before the recursive ADT implementation lands.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
BUILD = os.path.join(BENCH, "build")
os.makedirs(BUILD, exist_ok=True)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd} failed:\n{p.stderr}")
    return p


def build():
    run(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "adt_tree_bench.c"),
         "-o", os.path.join(BUILD, "adt_tree_c.exe")])
    run(["clang++", "-O3", "-std=c++20", "-DNDEBUG",
         os.path.join(BENCH, "adt_tree_bench.cpp"),
         "-o", os.path.join(BUILD, "adt_tree_cpp.exe")])
    rust = ["rustc", "-C", "opt-level=3", "--edition", "2021",
            os.path.join(BENCH, "adt_tree_bench.rs"),
            "-o", os.path.join(BUILD, "adt_tree_rs.exe")]
    if os.name == "nt":
        lld = r"C:\Program Files\LLVM\bin\lld-link.exe"
        if os.path.exists(lld):
            rust[1:1] = ["-C", f"linker={lld}"]
    try:
        run(rust)
    except RuntimeError:
        pass
    run([sys.executable, os.path.join(ROOT, "lambda.py"),
         os.path.join(BENCH, "adt_tree_bench.lambda"),
         "-o", os.path.join(BUILD, "adt_tree_lambda")])


def measure(exe, runs=5):
    times, value = [], None
    for i in range(runs + 1):
        start = time.perf_counter()
        p = run([exe])
        elapsed = time.perf_counter() - start
        if i == 0:
            value = int(p.stdout.strip())
        else:
            times.append(elapsed)
    return statistics.median(times), value


def main():
    build()
    files = {
        "Lambda": os.path.join(BUILD, "adt_tree_lambda.exe"),
        "C": os.path.join(BUILD, "adt_tree_c.exe"),
        "C++": os.path.join(BUILD, "adt_tree_cpp.exe"),
    }
    rust = os.path.join(BUILD, "adt_tree_rs.exe")
    if os.path.exists(rust):
        files["Rust"] = rust
    results, reference = {}, None
    for name, exe in files.items():
        med, value = measure(exe)
        results[name] = {"seconds": med, "value": value}
        if reference is None:
            reference = value
        elif value != reference:
            raise RuntimeError(f"{name}: result {value} != {reference}")
    base = results["C"]["seconds"]
    lines = ["# Recursive tree / flat-arena benchmark", "",
             "1048576-node implicit complete binary tree, 50 traversals; "
             "median of 5 measured runs.", "",
             "| implementation | time | vs C |", "| --- | ---: | ---: |"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['seconds']*1000:.1f} ms | {r['seconds']/base:.2f}x |")
    text = "\n".join(lines) + "\n"
    print(text)
    open(os.path.join(BENCH, "adt_tree_results.md"), "w", encoding="utf-8").write(text)
    json.dump(results, open(os.path.join(BENCH, "adt_tree_results.json"), "w", encoding="utf-8"), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
