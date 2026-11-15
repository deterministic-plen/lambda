"""Benchmark the generic SoA Vec2 map against C."""
from __future__ import annotations
import json, os, statistics, subprocess, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
BUILD = os.path.join(BENCH, "build")
EXPECTED = -2999225.0
TOL = 1e-6 * max(1.0, abs(EXPECTED))

def build(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode: raise SystemExit(p.stderr)

def run(exe):
    subprocess.run([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ts = []
    out = None
    for _ in range(3):
        t = time.perf_counter()
        p = subprocess.run([exe], capture_output=True, text=True, encoding="utf-8", errors="replace")
        ts.append(time.perf_counter() - t)
        out = float(p.stdout.strip().split()[-1])
        if p.returncode: raise SystemExit(p.stderr)
    if abs(out - EXPECTED) > TOL: raise SystemExit(f"result {out} != {EXPECTED}")
    return statistics.median(ts)

def main():
    os.makedirs(BUILD, exist_ok=True)
    c = os.path.join(BUILD, "vec2_c.exe")
    lam = os.path.join(BUILD, "vec2_lambda.exe")
    cfunc = os.path.join(BUILD, "vec2_cfunc.exe")
    build(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "vec2_bench.c"), "-o", c])
    build(["python", os.path.join(ROOT, "lambda.py"), os.path.join(BENCH, "vec2_bench.lambda"), "-o", lam])
    build(["clang", "-O3", "-DNDEBUG", os.path.join(BENCH, "vec2_bench_func.c"), "-o", cfunc])
    results = {"C": run(c), "C functional": run(cfunc), "Lambda": run(lam)}
    with open(os.path.join(BENCH, "vec2_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(BENCH, "vec2_results.md"), "w", encoding="utf-8") as f:
        f.write("# SoA Vec2 rotation\n\n1,000,000 vectors, 3 rotations, plus reduction.\n\n")
        f.write("| implementation | ms |\n| --- | ---: |\n")
        for k, v in results.items(): f.write(f"| {k} | {v*1000:.1f} |\n")
        f.write(f"\nLambda / C (in-place): {results['Lambda']/results['C']:.2f}x\n")
        f.write(f"Lambda / C functional: {results['Lambda']/results['C functional']:.2f}x\n")
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
