"""Benchmark the 5-point periodic heat stencil against native implementations.

Single-threaded Lambda is the apples-to-apples comparison.  A 16-thread
Lambda run is reported separately to show the parallel headroom.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
BUILD = os.path.join(BENCH, "build")
N = 512
STEPS = 50
REPS = 3
EXPECTED = 12853.0
TOL = 1e-6 * max(1.0, abs(EXPECTED))


def build(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit("build failed: " + " ".join(cmd) + "\n" + p.stderr)


def run(exe, env=None):
    # Discard one warm-up process before timing.
    subprocess.run([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={**os.environ, **(env or {})})
    times = []
    out = None
    for _ in range(REPS):
        t = time.perf_counter()
        p = subprocess.run([exe], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, **(env or {})})
        times.append(time.perf_counter() - t)
        if p.returncode != 0:
            raise SystemExit(f"{exe}: exit {p.returncode}\n{p.stderr}")
        out = float(p.stdout.strip().split()[-1])
    if abs(out - EXPECTED) > TOL:
        raise SystemExit(f"{exe}: result {out} != {EXPECTED} (tol {TOL})")
    return statistics.median(times)


def main():
    os.makedirs(BUILD, exist_ok=True)
    c = os.path.join(BUILD, "stencil_c.exe")
    cpp = os.path.join(BUILD, "stencil_cpp.exe")
    cfunc = os.path.join(BUILD, "stencil_cfunc.exe")
    lam = os.path.join(BUILD, "stencil_lambda.exe")
    build(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "stencil_bench.c"), "-o", c])
    build(["clang++", "-O3", "-std=c++20", "-DNDEBUG",
           os.path.join(BENCH, "stencil_bench.cpp"), "-o", cpp])
    build(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "stencil_bench_func.c"), "-o", cfunc])
    build(["python", os.path.join(ROOT, "lambda.py"),
           os.path.join(BENCH, "stencil_bench.lambda"), "-o", lam])
    results = {
        "C": run(c),
        "C++": run(cpp),
        "C functional": run(cfunc),
        "Lambda inline": run(lam, {"LAMBDA_THREADS": "1"}),
    }
    with open(os.path.join(BENCH, "stencil_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(BENCH, "stencil_results.md"), "w", encoding="utf-8") as f:
        f.write("# 5-point periodic heat stencil\n\n")
        f.write(f"512x512, {STEPS} steps, single native thread; "
                "median of 3 after build.\n\n")
        f.write("| implementation | ms |\n| --- | ---: |\n")
        for k, v in results.items():
            f.write(f"| {k} | {v * 1000:.1f} |\n")
        cms = results["C"] * 1000
        f.write(f"\nLambda inline / C (in-place): {results['Lambda inline'] * 1000 / cms:.2f}x\n")
        f.write(f"Lambda inline / C functional: {results['Lambda inline'] / results['C functional']:.2f}x\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
