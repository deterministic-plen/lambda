#!/usr/bin/env python3
"""Compile-time microbenchmark for the type-algebra prototype.

This is deliberately a compiler-front-end benchmark: type algebra has no
runtime representation of its own.  Compare generated ADT code with the native
benchmarks in adt_bench/adt_tree_bench; compare this number only against
future type-algebra front-end changes.
"""

from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, ".")
from type_algebra import layout, normalize, parse_type

SOURCES = [
    "mu L. 1 + Int * L",
    "mu T. 1 + Float * T * T",
    "Float * Float * Float",
    "mu E. 1 + Int * E * E + List<E>",
    "Int * 1/(1 - R)",
]


def one_round(n: int = 20000) -> float:
    start = time.perf_counter()
    for i in range(n):
        src = SOURCES[i % len(SOURCES)]
        layout(normalize(parse_type(src)))
    return time.perf_counter() - start


def main() -> int:
    one_round(1000)  # warm-up
    times = [one_round() for _ in range(5)]
    ms = statistics.median(times) * 1000
    print(f"type algebra: 100000 parse+normalize+layout ops in {ms:.2f} ms")
    print(f"per op: {ms / 100000 * 1000:.2f} us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
