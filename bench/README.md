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
* segmented sieve: Lambda ~1.09x C single-thread, ~1.45x C at 16 threads.
* 10M-element i64 fill+sum: Lambda ~1.21x C.
* F(1,000,000) bigint: Lambda ~103 ms compute+output vs Python's C-backed
  bigint ~512 ms; output exactly 208,988 digits and matches.
* Bigint compute-only: Lambda ~100 ms, Python ~36 ms, gmpy2/GMP ~2.8 ms
  (Lambda is ~2.8x Python and ~36x GMP).
* unboxed product/sum ADT loop: Lambda ~1.11x C; identical checksum, zero
  allocations.
* generated recursive ADT arena (1,048,576-node tree, 10 traversals):
  Lambda ~1.9–2.4x C, ~1.2–2.0x C++, ~1.6x Rust across runs. The <=2x-C
  target is marginal and not consistently met. It is generated code from
  `data Tree = Leaf | Node (...)`, not the old explicit-stack prototype; see
  `adt_recursive_results.md`.
* old explicit-stack arena prototype: ~6x C. Kept only as a historical
  baseline; it is superseded by the generated arena benchmark.

The product/sum benchmark is separate:

```powershell
python bench/run_adt_bench.py
```

It builds [adt_bench.c](adt_bench.c), [adt_bench.cpp](adt_bench.cpp),
[adt_bench.rs](adt_bench.rs) and [adt_bench.lambda](adt_bench.lambda), verifies
the checksum, and writes `bench/adt_results.md` / `.json`.

The generated recursive-ADT arena benchmark is separate:

```powershell
python bench/run_adt_tree_bench.py
```

```powershell
python bench/run_adt_recursive_bench.py
```

It builds a 1,048,576-node recursive `data` tree through the generated arena
backend and traverses it ten times. The Lambda program is
`bench/adt_recursive_bench.lambda`; the native implementations use equivalent
node arrays. Results are in `bench/adt_recursive_results.md` / `.json`.

The older explicit-stack prototype (`adt_tree_bench.*`, `run_adt_tree_bench.py`)
is retained as a historical baseline only; it measures ~6x C. The generated
arena path is the performance gate.

The type-algebra prototype is compile-time-only:

```powershell
python bench/type_algebra_bench.py
```

Current rate is roughly 3.3 microseconds per parse+normalize+layout iteration
on the reference machine; this has no runtime cost and is tracked separately
from generated ADT code.

The sieve gap dropped sharply after adding the `p >= 64` fast path: such a
prime can mark at most one bit in a 64-bit block, so Lambda no longer calls the
recursive mark loop for the vast majority of base primes. The remaining
single-thread gap is the Lambda-level mask loop and bounds checks; the parallel
gap is mostly task dispatch/closure application across 32-block chunks. The
implementation uses a flat `arrayi` of base primes, immortalises it so per-task
captures do not contend on the refcount, counts 32-block chunks with
`arrayi-pmap-chunk` into a flat result array, and materialises only the selected
block. The earlier 5–9x gap came from list masks/counts and per-task RC traffic,
not arithmetic. The bigint benchmark stays entirely in Lambda: the library
uses the base-1e9 `arrayi` representation, fast doubling, streamed decimal
output, direct `arrayi` limb operations and Karatsuba squaring.  The compiler
contributes inline reference counts, borrowed call arguments, tail ownership
moves, cached array metadata and lock-free pooling.
`bench/bigint_compute_ref.py` measures Python and optional gmpy2 compute
in-process; the Lambda compute-only executable is timed as a process.
A matrix benchmark is still omitted because Lambda has no 2D array/matrix
primitive; list-of-lists would measure representation overhead rather than
elimination arithmetic.
