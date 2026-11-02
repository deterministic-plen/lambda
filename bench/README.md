# Fair runtime comparison

This directory compares Lambda against C, C++ and Rust on the same algorithms,
constants, iteration counts and thread counts.

## Protocol

* `n = 670000`, expected result `10086767`.
* Each process performs the algorithm several times so process startup is
  amortized.
* One warm-up run is discarded; the median of three measured wall-clock runs is
  reported. Process startup is included in every number.
* No I/O inside the timed region; only the final sum is printed.
* Parallel runs use 16 threads for all languages, or 1 thread for all
  languages. Every implementation spawns threads per call, matching Lambda's
  `pmap` (no persistent pool).
* Compilers: Lambda uses clang `-O3` (driver default); C `clang -O3
  -DNDEBUG`; C++ `clang++ -O3 -std=c++20 -DNDEBUG`; Rust
  `rustc -C opt-level=3`.
* The sequential C/C++ implementations mirror Lambda's recursive
  `find`/`check` structure. Rust uses the equivalent loop because Rust does not
  guarantee tail-call elimination.
* The sieve uses the same 64-number, one-`uint64_t`-mask block layout and the
  same base-prime set. The parallel versions use the same dynamic work index.
* Newton uses `x -= (x*x - 2.0)/(2.0*x)` for exactly 100 steps, repeated
  1,000,000 times. The output is checked at `1e-9` relative tolerance because
  compilers may contract the expression into an FMA.

## Run

```powershell
cd C:\Users\mumuj\Documents\lambda
clang -O3 -DNDEBUG -D_CRT_SECURE_NO_WARNINGS bench/nth_bench.c -o bench/build/nth_c.exe
clang++ -O3 -std=c++20 -DNDEBUG -D_CRT_SECURE_NO_WARNINGS bench/nth_bench.cpp -o bench/build/nth_cpp.exe
# rustc needs to spawn its linker; use lld-link if MSVC link.exe is unavailable:
#   rustc -C opt-level=3 --edition 2021 bench/nth_bench.rs -o bench/build/nth_rs.exe
#       -C linker="C:\Program Files\LLVM\bin\lld-link.exe"
python bench/run_bench.py
```

`run_bench.py` builds the Lambda variants, runs all configurations, verifies
every result, prints the tables and writes `results.json` / `results.md`.

## Results

See [results.md](results.md) for the latest table. Headline:

* sequential nth-prime: Lambda ~1.01x C; C++/Rust within noise.
* parallel trial division: Lambda ~1.00–1.02x C at 1 and 16 threads.
* Newton f64: Lambda ~1.02x C.
* segmented sieve: Lambda ~1.37x C single-thread, ~1.61x C at 16 threads.
* 10M-element i64 fill+sum: Lambda ~1.13x C.
* F(1,000,000) bigint: Lambda 760 ms vs Python's C-backed bigint 528 ms
  (1.44x); output exactly 208,988 digits and matches.

The sieve gap is now mostly the Lambda-level 64-bit mask loop, bounds checks and
per-task closure application. The implementation uses a flat `arrayi` of base
primes, immortalises it so per-task captures do not contend on the refcount,
counts 32-block chunks with `arrayi-pmap-chunk` into a flat result array, and
materialises only the selected block. The earlier 5–9x gap came from list
masks/counts and per-task RC traffic, not arithmetic. The bigint benchmark is a
pure-Lambda base-1e9 Karatsuba/fast-doubling library; Python's built-in bigint
is the available C-backed reference because GMP/Boost are not installed. A
matrix benchmark is still omitted because Lambda has no 2D array/matrix
primitive; list-of-lists would measure representation overhead rather than
elimination arithmetic.
