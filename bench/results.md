# Lambda benchmark results

CPU: AMD Ryzen 7 9700X 8-Core Processor (16 logical)

| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| seq | 1 | 1001.068 | 991.403 | 991.902 | 1007.801 | 1.01x | 1.01x | 0.99x |
| par | 1 | 1189.946 | 1168.031 | 1212.832 | 1188.788 | 1.02x | 0.98x | 1.00x |
| par | 16 | 168.120 | 166.481 | 171.798 | 168.360 | 1.01x | 0.98x | 1.00x |
| sieve | 1 | 173.680 | 159.518 | 157.500 | 159.544 | 1.09x | 1.10x | 1.09x |
| sieve | 16 | 27.692 | 19.136 | 19.346 | 21.722 | 1.45x | 1.43x | 1.27x |

| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per solve | 0.3653 | 0.3573 | 0.3565 | 0.3577 | 1.02x | 1.02x | 1.02x |

| large array (10M i64 fill+sum) | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per pass | 16.064 | 13.269 | 13.457 | 13.831 | 1.21x | 1.19x | 1.16x |

| bigint | Lambda | Python (C bigint) | Lambda / Python |
|---|---:|---:|---:|
| F(1,000,000), 208,988 digits (compute + output) | 92.8 ms | 512.1 ms | 0.18x |

| bigint compute | Lambda | Python int | gmpy2 (GMP) | Lambda / Python | Lambda / gmpy2 |
|---|---:|---:|---:|---:|---:|
| F(1,000,000) compute only | 87.8 ms | 35.8 ms | 2.80 ms | 2.47x | 31.6x |

Bigint protocol: both the library and the compiler optimizations are pure
Lambda/LLVM work — no bigint primitives. `bench/bigint_fib1m_compute.lambda`
compiles the same fast-doubling recurrence as `bench/bigint_compute_ref.py`;
Lambda is timed as a process (median of five after a discarded warm-up), while
Python/gmpy2 report in-process compute time so interpreter startup and decimal
conversion do not hide the arithmetic gap.  The 208,988-digit total output is
verified against Python on every full run.  The Lambda result is at the
~88 ms target and on the same order as Python's C-backed bigint; GMP remains
a different class because it uses FFT multiplication and hand-tuned assembly.

| ADT (20M product/sum iterations × 3) | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| unboxed Vec2 + Shape match | 41.8 | 39.5 | 39.8 | 40.0 | 1.06x | 1.05x | 1.05x |
