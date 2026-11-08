"""Compute-only F(1,000,000) reference with Python ints or gmpy2.

Timing is measured inside the process so it excludes interpreter startup and
decimal conversion.  This is the number reported next to Lambda's native
compute time.  Pass ``gmp`` to use gmpy2 when it is installed.
"""

import json
import sys
import time


def fib(n, zero, one):
    if n == 0:
        return (zero, one)
    a, b = fib(n >> 1, zero, one)
    c = a * ((b << 1) - a)
    d = a * a + b * b
    if n & 1:
        return (d, c + d)
    return (c, d)


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "python"
    if backend == "gmp":
        try:
            import gmpy2
        except ImportError:
            print(json.dumps({"error": "gmpy2 is not installed"}))
            return 2
        zero, one = gmpy2.mpz(0), gmpy2.mpz(1)
    else:
        zero, one = 0, 1

    fib(1000, zero, one)  # warm-up
    samples = []
    value = zero
    for _ in range(5):
        start = time.perf_counter()
        value = fib(1_000_000, zero, one)[0]
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    print(json.dumps({
        "backend": backend,
        "compute_ms": samples[len(samples) // 2],
        "samples_ms": samples,
        "bits": int(value).bit_length() if backend == "python" else value.bit_length(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
