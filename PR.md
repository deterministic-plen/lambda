# Review findings for Lambda and Pi: refcount race, Pi fault handling, silent type fallback, stdlib and docs defects

> Draft PR description based on a review of `deterministic-plen/lambda` at commit `4a89a95`.
> Nothing here has been pushed, and **no fix code has been written except the one-line patch in 1.1** (tested in
> scratch only). This file is an issue list; the fixes are to be written afterwards. Evidence labels used throughout:
> **VERIFIED** = reproduced by running it, **READING** = confirmed by reading the code, not run.

## Summary

The existing suite is healthy (170/170 compile-run-leak cases, 17/17 Pi static tests), but it only checks stdout and
`live=0`, so it cannot see concurrency bugs, silent I/O failures, checks that the type system quietly skips, or
anything outside `tests/` (six shipped example programs do not compile, and the Pi AOT path cannot build at all).

The review found **7 high-severity** issues and several dozen medium and low ones (the sixth pass, 3F, adds a heap overflow in the array pool, an unbounded `data` arena and a deep-nesting stack overflow; the fifth pass, 3E, adds a local file-exposure issue in the viewer, two type-confusion bugs in the fallback backend, a closure-destructor stack overflow and input-handling defects) (the index has one row per group)
across the compiler, runtime, standard library, docs and Pi. The sections are in the order the work was done (1 to 3 first pass, 3A second,
3B third, 3C fourth, 3D, 3E and 3F last), not strictly by severity, so use the index below to find things. A single PR for all of
this would be unreviewable; section "Suggested split" proposes five.

### Findings index

Severity is my judgement: **High** = memory corruption, wrong results by default, or a feature that does not work as
documented; **Medium** = wrong or silent behaviour that needs a specific input or setup; **Low** = hygiene, docs,
diagnostics. "Where" points to the section that has the detail and evidence.

| severity | where | issue | status |
| --- | --- | --- | --- |
| High | 1.1 | data race in `lambda_release_zero` corrupts memory in pure `pmap` (tested one-line fix) | VERIFIED |
| High | 1.2 | Pi runs binaries from predictable `/tmp` paths (cache poisoning) | READING |
| High | 1.3, C1, 3C | type errors silently become runtime errors by default; the fallback backend cannot compile a quarter of the suite and can be 300x slower | VERIFIED |
| High | P1 | a Pi worker crash mid-call kills the whole runtime and bypasses `on_error`/`restart`/`retry` | VERIFIED |
| High | P2 | Pi `timeout`: caught `Deadline` still exits 1; documented `Deadline ⇒ restart` hangs forever | VERIFIED |
| High | A1 | Pi AOT writes CSV artifacts to `/` when `--out` is omitted | VERIFIED |
| High | AOT build | `pi aot` cannot compile its own output (`-std=c99`) and no test covers it | VERIFIED |
| Medium | 2.1 | `write-file` ignores `fclose` failure | VERIFIED |
| Medium | 2.2 | `read-file`/`read-file-bytes` abort or return empty on directories and unseekable files | VERIFIED |
| Medium | 2.3 | NUL in a path silently truncates it | VERIFIED |
| Medium | 2.4, C9 | use after consume is not diagnosed; same-array-twice, alias and double-`set` aliasing | VERIFIED |
| Medium | 2.5, 2.6, P8 | Pi worker cache key incomplete, non-atomic builds, shared schema file, cache poisoning | READING |
| Medium | C7 | `LAMBDA_RAWLOOP=1` double free | VERIFIED |
| Medium | GPU path | CUDA path unreachable; `LAMBDA_GPU` and `{gpus: 1}` are no-ops; docs contradict | VERIFIED |
| Medium | P3 | `par for` makes one OS thread per element; 100,000 elements abort | VERIFIED |
| Medium | P4 | plan JSON parser does not decode `\uXXXX` | VERIFIED |
| Medium | P5 | flat Pi programs hit a recursion limit (about 1,000 terms) and a nesting cap (about 600) | VERIFIED |
| Medium | A2 | Pi AOT does not sanitise artifact labels (`../` escapes the output directory) | VERIFIED |
| Medium | A3, A4 | Pi AOT skips the schema check and trusts worker frame lengths (heap under-read) | READING |
| Medium | L10 | `Complex::exp` is wrong whenever the imaginary part is non-zero | VERIFIED |
| Medium | L11 | `Complex::modulus` of zero is NaN | VERIFIED |
| Medium | L13, L14 | `matrix` and `field` libraries claim bounds safety but do not check shapes or indices | VERIFIED |
| Medium | L1, L2 | `is-prime` never terminates near INT64_MAX; `append`/`filter` segfault above about 100k elements | VERIFIED |
| Medium | L4, L5, L6 | `arena-alloc`/`arena-reserve` and `vec-get` defects | VERIFIED |
| Medium | 3A generalization | block definitions are not generalized across each other | VERIFIED |
| Medium | 3A list literals | list literals fail above about 3,500 elements; compile time is quadratic (structural options given) | VERIFIED |
| Medium | C10, C11 | multi-parameter generics unparseable; misleading error for nested `type` | VERIFIED |
| Medium | 3C shipped programs | six shipped programs do not compile, none covered by CI | VERIFIED |
| Medium | P6, P7 | `die()` calls `exit` from any thread; unchecked double-to-integer casts | READING |
| Medium | 3A docs | the README Quick Start, sum-type and Strings examples do not compile or fall back | VERIFIED |
| Low | 2.4a, shifts | integer wrap, shift masking and logical `shr` undocumented | VERIFIED |
| Low | L3, L7, L8, L9, L12, L15, L16 | `iexp`, `base-primes`, `minf`, `bigint` sub/from-int, `max`/`min` clash, `stream`, `vec`/`kalman` notes | VERIFIED / READING |
| Low | C2, C3, C4, C5, C6, C8, C12, C13 | name mangling, temp-dir leak, `lld`, diagnostics, `include` paths, thread-create failure, uninhabited `data`, rebind idiom | mixed |
| Low | 3 | float printing, NaN sign, GPU loader, dead doc links, empty `SECURITY.md`, test counts, clutter, module names, CI coverage | mixed |
| Medium-High | S1 | `tools/viewer.py` serves the whole parent directory (dot-files included) and accepts any `Host` (DNS rebinding) | VERIFIED |
| Medium | S2, S3 | dynamic backend: `str-len` leaks heap pointers, `sum`/`product` segfault on non-lists (reachable through the default fallback) | VERIFIED |
| Medium | S4 | freeing about 150k nested closures overflows the native stack | VERIFIED |
| Medium | S5 | `read-line` splits lines over 4095 bytes and adds a phantom empty line at exactly 4095 | VERIFIED |
| Low | S6 to S13 | `read-int` overflow saturates, NUL truncation in input, viewer NUL traceback, misleading `data`-in-`pmap` message, silent stack-overflow segfault, `argv` out of range, `int32` rc wrap, immortal values hidden from `live=` | VERIFIED / READING |
| Medium | F1 | freeing an empty `ArrayI`/`ArrayF` files a 1-byte block under the 8-byte pool class; the next one-element array writes 8 bytes into it (valgrind: invalid write; masked on glibc) | VERIFIED |
| Medium | F2 | `data` values are never reclaimed: 20M dropped nodes cost about 300 MB, 200M exhaust a 2 GB limit; `live=0` and valgrind cannot see it | VERIFIED |
| Medium | F4 | dropping a deeply left-nested list in the fallback backend segfaults at about 400k depth (same cause as S4) | VERIFIED |
| Low | F3, F5, F6 | 16 MiB-per-word arena reservation up front; top-level record value gives a raw clang error; top-level `data` value prints its arena index | mixed |
| Low | 3A, 3B, 3D | built-ins redefinable, native stack overflow, `str->int`, lexer diagnostics, missing executable bits, Pi fork/`setenv`/NUL/`_cstr`, test-harness and driver nits, per-build runtime recompilation | mixed |

### Suggested split

1. **Runtime safety:** 1.1, F1, F2 and F4 (with S4), C7, 2.1, 2.2, 2.3, C8, S2 to S7 (each small, each testable; S4 needs an iterative closure free).
2. **Typing and diagnostics:** 1.3/C1 (`--strict` by default), 3A generalization, C10, C11, C2, 2.4/C9 (decision needed:
   enforce linearity, copy-on-write, or reword the docs).
3. **Pi runtime and tools:** S1 and S8 (viewer), P1, P2, P3, P4, P5, P6, P7, 1.2, 2.5, 2.6, P8, fork/`setenv` fixes.
4. **Pi AOT:** the `-std` flag, A1 to A6 (and a CI test, since nothing runs it).
5. **Library, docs and repo hygiene:** L1 to L16, shipped programs, README, `.gitignore`/`docs/`, executable bits, CI
   (compile every example, run Pi tests, add a TSan or valgrind job).

## 1. High severity

### 1.1 Data race in `lambda_release_zero` corrupts memory in pure `pmap` programs (VERIFIED)

`runtime.py:249` (C function `lambda_release_zero`). When a freed cons chain is walked iteratively, every node after
the first is decremented with a plain read-modify-write:

```c
int32_t rc = v->rc;          // non-atomic read
if (rc < 0) return;
if (--rc != 0) { v->rc = rc; return; }   // non-atomic write
v->rc = 0;
```

The first node uses the atomic `rc_release_once`, but tail nodes do not, even in `LAMBDA_ATOMIC_RC` builds. Two workers
that free lists sharing a tail lose updates. `SECURITY_REVIEW.md` lists this as a "latent race ... not reproduced";
it reproduces easily.

**Repro** (accepted by the compiler as pure, no warnings):

```lambda
mk → (λn.
  t → (range 0 50)
  f → (λi. (sum (cons i t)))
  (sum (pmap f (range 0 4000))))
(println (mk 0))
```

Expected output `12898000`. Run with `LAMBDA_THREADS=8`, 150 runs each, on a 2-core machine:

| runtime | correct | hang (5 s timeout) | crash |
| --- | ---: | ---: | ---: |
| current, first build | 118 | 19 | 13 (segfault, or `expected Int, got null` / `got ?`) |
| current, second build | 15 | 82 | 53 |
| one-line fix below | 150 | 0 | 0 |

The failure rate varies with scheduling; the two "current" rows are two separate builds, not a trend. The patched
runtime was linked against the same `--keep-ir` output as the unpatched one, so only the runtime differs.

**Fix:**

```diff
--- a/runtime.py
+++ b/runtime.py
@@ lambda_release_zero
         if (!first) {
-            int32_t rc = v->rc;
-            if (rc < 0) return;
-            if (--rc != 0) { v->rc = rc; return; }
-            v->rc = 0;
+            if (!rc_release_once(v)) return;
         }
```

`rc_release_once` returns 0 for immortal (`rc < 0`) values and returns 1 only when this call dropped the last
reference, which is exactly what the removed lines computed. In non-atomic builds it falls back to the old behaviour.

**Test to add:** the repro above as `tests/parallel/pmap_shared_tail.lambda`, run in a loop (for example 50 iterations)
by `run_tests.py`, since a single run usually passes.

### 1.2 Pi executes binaries from predictable `/tmp` paths (READING)

- `Pi/pilang/cpp.py:25` builds the C++ runtime at `<tmp>/pi-cpp-runtime` and reuses it whenever
  `mtime(out) >= mtime(src)`.
- `Pi/pilang/worker.py:64` caches compiled workers in `<tmp>/pi-build/<name>-<hash>` and trusts any existing file
  (`if os.path.exists(exe): return exe`).

On a shared machine another local user can pre-create either path with a future mtime and get code executed as the
victim. `SECURITY_REVIEW.md` says the predictable `/tmp` cache was replaced, but that was done only for the Lambda
driver (`lambda.py` now uses `mkdtemp`); Pi still has the same trust problem.

**Proposed fix:** use a per-user cache directory created with mode `0700` (`$XDG_CACHE_HOME/pi`, `~/.cache/pi`, or
`%LOCALAPPDATA%\pi`), refuse to use it if it is not owned by the current user, and build to a temp file in that
directory followed by `os.replace`. This also removes the concurrent-build hazard in 3.3.

### 1.3 Type errors silently become runtime errors by default (VERIFIED)

`lambda.py` catches `TypeError_` and recompiles in dynamic mode unless `--strict` is passed. Observed:

```
$ cat t.lambda
(println (+ 1 2.0))
$ python lambda.py t.lambda -o t
warning: type inference failed (expected Int, got Float); compiling in dynamic (boxed) mode
Success! Run ./t
$ ./t
lambda: runtime error: + expects two Ints or two Floats
```

The same happens for `f → (λx. (* x 2))` used at both `Int` and `Float`. The README presents Lambda as "statically
typed", but by default a type error ships as a program that fails when run. `run_tests.py` already treats the fallback
as a failure for every case unless `strict: false`.

**Proposed fix:** make `--strict` the default and add an explicit `--allow-dynamic` flag for the fallback. At minimum,
print `Success!` only when the build is fully typed. Also decide whether numeric operators should be polymorphic across
`Int`/`Float` (the second example above); if not, document it, because the error message
(`numeric operator expects Int or Float, got String -> Int`) does not point at the cause.

## 2. Medium severity

### 2.1 `write-file` reports success when the write failed (VERIFIED)

`runtime.py:1775`: `fclose(f)` is unchecked and buffered data is only flushed there.

```
(write-file "/dev/full" (arrayb 100 65))   →   100
```

```diff
-    size_t n = fwrite(data->p, 1, (size_t)data->i, f);
-    int64_t ok = (n == (size_t)data->i) ? (int64_t)n : -1;
-    fclose(f);
+    size_t n = fwrite(data->p, 1, (size_t)data->i, f);
+    int64_t ok = (n == (size_t)data->i) ? (int64_t)n : -1;
+    if (fclose(f) != 0) ok = -1;
```

Untested diff.

### 2.2 `read-file` and `read-file-bytes` mis-handle non-regular files (VERIFIED)

`runtime.py:1599` and `:1703` size the buffer with `fseek(SEEK_END)`/`ftell`:

- a directory: `(read-file "/")` aborts with `lambda: out of memory` (`ftell` returns a huge value);
- `/proc/self/status`, pipes and other unseekable files: silently return an empty string/array;
- `long` is 32-bit on Windows, so files over 2 GB are mis-sized (READING).

**Proposed fix:** `fstat` and require a regular file, then read in a growing loop; return the existing "empty on error"
value for anything else.

### 2.3 Path strings containing NUL are silently truncated (VERIFIED)

Lambda strings are length-tagged, but `fopen` receives `(const char *)p`.
`(write-file "x\0.txt" ...)` created a file named `x`. Reject paths with embedded NUL in `read-file`,
`read-file-bytes` and `write-file`.

### 2.4 Use after consume is not diagnosed; `arrayi-set` mutates a buffer that other handles still see (VERIFIED)

```lambda
main → (λn.
  a → (arrayi 4 3)
  b → (arrayi-set a 0 9)
  c → (arrayi-set a 1 8)
  (+ (arrayi-get b 0) (arrayi-get c 1)))     ; prints 17
```

`a`, `b` and `c` are one buffer. Valgrind reports no errors in any variant I tried (the compiler retains correctly, so
this is memory-safe), and the README does say "returns a new handle to the same buffer". But it also says arrays are
consumed, and the checker never rejects reading `a` after `(arrayi-set a ...)` or after passing it to a function that
drops it. Either enforce linearity in typed mode, or copy-on-write when `rc > 1`, or reword the README so "consume" is
not implied.

### 2.4a Integer semantics are undefined in the docs (VERIFIED, no UB observed)

| expression | result |
| --- | --- |
| `(+ 9223372036854775807 1)`, `(sum [INT64_MAX, 1])` | wraps to `-9223372036854775808` |
| `(/ INT64_MIN -1)` | `INT64_MIN` (no trap) |
| `(shl 1 64)` | `1` (count masked to 6 bits) |
| `(shl 1 -1)` | `-9223372036854775808` |
| `(shr -8 200)` | `72057594037927935` (logical shift, count masked) |

These are consistent, but none of it is in the README. Document wrapping and shift-count masking, and state that `shr`
is logical.

### 2.5 Pi worker cache key is incomplete (READING)

`Pi/pilang/worker.py` hashes only `lambda.py`, `compiler.py`, `runtime.py`, `typecheck.py` plus the worker source and
`Pi/pilang/lib`. Changes to `analysis.py`, `parser.py`, `lexer.py`, `resolve.py`, `loader.py`, `type_algebra.py`, the
top-level `lib/*.lambda`, or any file a worker includes do not invalidate the cache, so a stale worker binary can be
reused. Hash every `*.py` next to `lambda.py` and the transitive include set, or key on the compiler's git/tree hash.

### 2.6 Pi runtime build is not atomic (READING, partially checked)

`cpp.py` compiles directly to the final path and later trusts it by mtime. Two concurrent `pi run` invocations write
the same file, and an interrupted build can leave a newer-than-source partial binary. Covered by the fix in 1.2
(build to a temp file, then `os.replace`).

## 3. Low severity and hygiene

- **Float printing is not shortest round-trip** (VERIFIED): `0.1` prints `0.10000000000000001`, `1e-7` prints
  `9.9999999999999995e-08` (looks like `%.17g`). Use shortest round-trip formatting.
- **Typed and dynamic builds disagree on NaN** (VERIFIED): `inf - inf` prints `nan` typed and `-nan` dynamic.
- **GPU library loading** (READING, `runtime.py:2690`, `:2728`): `libnvrtc.so` is normally only a development symlink
  (the runtime package ships `libnvrtc.so.12`), so the GPU path may silently fall back on machines that have CUDA but
  not its dev packages. On Windows, `LoadLibraryA` with a bare name searches the application directory and current
  directory first (DLL planting) when `LAMBDA_GPU=1`. Load by full system path or pass
  `LOAD_LIBRARY_SEARCH_SYSTEM32`.
- **Docs are broken** (VERIFIED by link check): `docs/` is listed in `.gitignore`, so every `docs/...` link in
  `README.md` and `Pi/README.md` (about 17 links, including `docs/safety.md`, `docs/strings.md`,
  `Pi/docs/viewer.md`) is dead. README link `pi/` has the wrong case (`Pi/`), and `Pi/README.md` links `../../README.md`
  which is outside the repo. Remove `docs/` from `.gitignore` and commit the docs, or drop the links.
- **`SECURITY.md` is empty** (1 byte). Add a reporting contact.
- **Test counts disagree**: README says 144, `SECURITY_REVIEW.md` says 132, `run_tests.py --list` says 170.
  Drop the numbers or generate them.
- **Build dependencies undocumented**: `lambda.py:152` hardcodes `-fuse-ld=lld`, so `lld` is required but the README
  only mentions clang. Either detect it or document it.
- **Repository clutter**: `bench/build/*.pdb` (about 3.5 MB of Windows Rust debug symbols, with CI-runner paths
  embedded) and an empty `logs.txt` are tracked; add them to `.gitignore` and remove them. `lambda.py` has an unused
  `import hashlib`. The directory `boostrap/` is misspelt.
- **Misleading success message**: `lambda.py:297` prints `Run ./<basename>` even when `-o` points elsewhere.
- **Module names**: top-level `parser.py`, `lexer.py`, `errors.py`, `compiler.py`, `runtime.py` can shadow
  stdlib/third-party modules and there is no packaging metadata (`pyproject.toml`); the entry point is a script named
  `lambda.py`, which cannot be imported.
- **Pi artifact names** (READING): `safe_artifact_name` (`pi_runtime.cpp:571`) correctly rejects `/`, `\` and `..`, but
  does not reject NUL or `:` (NTFS alternate data streams on Windows).
- **One compiler warning** with `-Wall -Wextra`: unused function `gpu_kernel` (`runtime.c:2775` in the extracted
  source).
- **CI coverage**: `ci.yml` only runs `run_tests.py` on Linux. Not run: Pi pytest (17 tests), editor tests, Pi C++
  examples, other platforms, valgrind/sanitizers, any linter. At least add the Pi and editor tests, plus a valgrind or
  ASan job over a subset of `tests/` (including the new `pmap` race test run repeatedly under TSan).

## 3A. Second-pass findings (language, standard library, docs, Pi)

### Language and compiler

- **Definitions in a block are not generalized relative to each other** (VERIFIED). A helper used at two types from two
  different definitions is rejected, although the same helper is fine when both uses are in the block's final
  expression:

  ```lambda
  ident → (λx. x)
  a → (ident 1)
  b → (ident "s")          ; type error: expected Int, got String
  ```

  The same happens for the library's own generic `length` (`a → (length [1,2,3])`, `b → (length ["x","y"])`) and for
  local blocks inside a function. The README says "Hindley–Milner inference", but a block's definitions are one
  recursive group with no dependency analysis, so none of them is generalized for the others. In the default
  (non-strict) mode this does not even surface as an error: the whole program silently drops to the boxed dynamic
  backend (see 1.3). Fix: split each block into strongly connected components in dependency order and generalize
  each component after inferring it.
- **List literals are limited to roughly 3,500 elements, and compile time is quadratic** (VERIFIED). This corrects an
  earlier draft of this file that said 5,000 elements compile; they do not.

  *The limit.* `lambda.py:176` sets `COMPILER_RECURSION_LIMIT = 20000` (a deliberate safety net, with a 64 MB compiler
  thread). `parser.py:780` desugars `[a, b, c]` into `cons a (cons b (cons c nil))`, an AST whose depth equals the
  element count, and every later pass recurses over it at about 5 Python frames per element. Measured with a
  `[1, 1, ..., 1]` literal: 3,000 and 3,500 elements compile, 4,000 and everything larger fail with
  `compiler stack overflow while lowering this program (deeply nested expressions, ...)`. The message is misleading
  because the list is flat.

  *The cost.* Before any limit is reached, compile time grows quadratically (clean machine, `--strict`):

  | elements | 500 | 1,000 | 2,000 | 3,000 |
  | --- | ---: | ---: | ---: | ---: |
  | compile time | 4.1 s | 4.5 s | 18.9 s | 51.6 s |

  The same happens for plain nested expressions: `(+ 1 (+ 1 ...))` at depth 1,500 takes 9.3 s. A profile of a
  1,500-element literal (40.8 s under `cProfile`) shows two independent hotspots:
  1. `share_common_subexpressions` (`analysis.py:643`) is about 26 s. `count()` and `rewrite()` call
     `simple_pure(n)` and `key(n)` on every `App` node. Both traverse the node's whole subtree, and `key` builds a
     string as long as the subtree, with no caching, so a depth-N chain costs O(N²) time and memory.
  2. `walk()` (`lambda_ast.py:392`) is about 14.5 s. It is a recursive generator using `yield from`, so on a depth-N
     tree every yielded node passes through all its ancestors' generator frames, which is O(N·depth) per traversal. Nine
     call sites traverse the whole program with it (`analysis.py`, `resolve.py`, `typecheck.py`, `compiler.py`).

  *Prototype (scratch only, repo untouched).* An iterative `walk` (verified to yield the identical pre-order sequence on
  three full-program traversals) plus interned, memoized `key`/`simple_pure` gives the same output and:

  | case | current | prototype |
  | --- | ---: | ---: |
  | 2,000-element list, re-measured | 17.5 s | 1.6 s |
  | 3,000-element list | 51.6 s | 1.9 s |
  | nested `(+ 1 ...)`, depth 1,500 | 9.3 s | 1.3 s |

  With the quadratic cost gone, the recursion cap is the only remaining limit (3,500 elements compile, 4,000 fail).

  *Options for the limit, smallest first.*
  1. **Fix the two hotspots** (above): local to `lambda_ast.py` and `analysis.py`, no behaviour change, helps every
     long expression. Do this regardless.
  2. **Raise `COMPILER_RECURSION_LIMIT`.** On Python 3.13 with the 64 MB stack, a limit of 400,000 compiled 20,000
     elements (20 s) and 60,000 elements (150 s) without crashing; 100,000 was not enough. But `lambda.py` documents
     CPython 3.10, where Python frames use the C stack and 64 MB would not hold, and the README states no minimum
     Python version. A stopgap only, unless the minimum version is stated and tested.
  3. **Add an n-ary `ListLit` node**, like `RecordLit`/`SumLit`, so a literal is one node whose passes loop over
     `elements` instead of recursing. `RecordLit` is handled in about 26 places across six files (`lambda_ast.py` 4,
     `parser.py` 2, `resolve.py` 3, `typecheck.py` 3, `analysis.py` 9, `compiler.py` 5), so this is mechanical but
     touches every pass; code generation can then build the cons chain from the end in a loop.
  4. **A constant-table fast path** for literals of numbers or strings: emit one global constant array and call a
     runtime builder, instead of 20,000 nested calls in one LLVM function. At 20,000 elements, about 17 of 20 s is
     `clang -O3` on a single 2.4 MB function (measured separately), so after options 1 to 3 the cost moves from Python
     to LLVM.

  Until then, make the error message say "list literal too long" when that is the cause.
- **Built-in names can be redefined without any diagnostic** (VERIFIED). `+ → (λa b. 99)` makes `(+ 1 2)` return 99
  and `println → (λx. 5)` replaces printing. This may be intentional, but it deserves a warning.
- **Stack overflow is an unexplained segfault** (VERIFIED): see `append`/`filter` below. The runtime installs no guard
  or message for native stack exhaustion.

### Standard library (`lib/`)

| id | problem | status |
| --- | --- | --- |
| L1 | `is-prime` never terminates for primes above about 9.223372031e18. `d*d` overflows, so `(< n (* d d))` never becomes true. `is-prime 9000000000000000041` returned `true` in 6 s; `is-prime 9223372036854775783` (largest prime below 2^63) was still running after 110 s. Compare `d` against `n / d` instead. | VERIFIED |
| L2 | `append` and `filter` are not tail recursive. A 100,000-element list works; 300,000 and 3,000,000 segfault with no message. Rewrite with an accumulator and `reverse`. | VERIFIED |
| L3 | `iexp` with a negative exponent silently returns 1 (`(iexp 2 -1)` and `(iexp 2 -3)` both give 1); `(iexp 2 64)` wraps to 0. Reject `p < 0`. | VERIFIED |
| L4 | `arena-alloc` on overflow returns `[-1, 0]`: the returned cursor is 0, not the unchanged cursor, so a caller that stores `(arena-used r)` rewinds the arena and overwrites live data. A negative size is accepted and moves the cursor backwards (`(arena-alloc 10 -5 100)` gives `[10, 5]`). | VERIFIED |
| L5 | `arena-reserve` does not grow an arena that is exactly full (`(arena-reserve (arena-new 4) 4 4)` still has length 4). It only grows when `used > cap`, i.e. after the overrun, and takes no requested size. Likely inverted or incomplete. | VERIFIED (behaviour); intent is a guess |
| L6 | `vec-get` checks nothing against the vector's own length: `(vec-get v 5)` on a 2-element vector returns stale `0.0`, and `(vec-get v -1)` / `(vec-get v -2)` return the capacity (8.0) and length (2.0) header slots. | VERIFIED |
| L7 | `base-primes 0` and `base-primes 1` both return `[2]`. | VERIFIED |
| L8 | `minf` is order-dependent with NaN (`[NaN,3,1]` gives `nan`, `[3,NaN,1]` gives `1.0`), and `minf`/`maxf` on an empty array fail with out-of-bounds while `varp` returns `0.0`. | VERIFIED |
| L9 | `bi-sub` ignores the final borrow: `5 - 7` yields a one-limb value `999999998` with no error. `bi-from-int` does not normalize or validate: `x = 2000000000` stores limb `2000000000`, `x = -5` stores limb `-5`. Undersized `cap` is caught safely as an out-of-bounds error. | VERIFIED |

### Documentation and packaging

- **The README's first example does not compile** (VERIFIED). The Quick Start file starts with `-- hello.lambda`;
  comments are `;`, so it fails with `syntax error: unexpected token '.' after program` (and the bare
  `println "hello"` line needs parentheses). Change it to `; hello.lambda` and `(println "hello")`.
- **The README's sum-type example does not parse** (VERIFIED). `type Shape (Circle (r: Float) | ...)` with
  `Circle r ⇒ ..., ` fails with `expected ':' or '→', got '('`. The syntax the tests actually use is
  `Circle r: ...,` (see `tests/enums_sums/sum_shape.lambda`) and, for enums, `enum State (A, B)` with `A → ... | B → ...`.
- **The README's Strings example triggers the dynamic fallback** (VERIFIED): it builds a list mixing `Int` and `Bool`
  values, so the compiler prints `type inference failed (expected Int, got Bool); compiling in dynamic (boxed) mode`.
- **Executable bit missing** (VERIFIED): `Pi/pi` and `editors/install_vscode.sh` are committed as mode 100644, so the
  documented `./pi run examples/heat.pi` fails with `Permission denied` on Linux/macOS. `git update-index --chmod=+x`.

### Pi

- Confirmed in practice that a Pi run creates the fixed paths `/tmp/pi-cpp-runtime` and `/tmp/pi-build/` (see 1.2).
- `"body": []` in a plan gives `pi-cpp: unsupported statement ` with an empty detail; the message should name the
  offending node (nit).

## 3B. Third-pass findings (compiler driver, GPU path, standard library, Pi)

### Compiler and driver

| id | problem | status |
| --- | --- | --- |
| C1 | **The type-error fallback hides the real diagnostic and sometimes produces a worse one.** For a non-exhaustive *enum* match the compiler warns, then builds a boxed program that stops with `runtime error: invalid enum tag` (exit 1). For a non-exhaustive *sum-type* match (`Some x: x` with no `None`) the fallback fails with `product values cannot be boxed/first-class yet`, which says nothing about the match. `--dynamic` on any program using sum types gives `unknown sum type 'Option'`. A duplicate arm (`A → 1 \| A → 5`) falls back and then dies in clang: `duplicate case value in switch`. `--strict` gives the correct messages in every case. This is the same root cause as 1.3, with concrete examples. | VERIFIED |
| C2 | **Type names are not mangled safely in the IR.** `@arena_{name}` / `@arena_words_{name}` (compiler.py ~523) use the raw type name, so non-ASCII or punctuated names break the IR with a raw clang error. The `%rec_` sanitiser (~751) replaces every non-alphanumeric character with `_`, which is lossy: `T?` and `T_`, or `A-b` and `A_b`, collide ("redefinition of type"). The lexer rejects quotes and whitespace, so I found no injection. Fix: mangle with a numeric id or hex escape, as is already done for `@fn_`/`@gval_`. | VERIFIED |
| C3 | **`build()` leaks its temp directory on early failure.** `tempfile.mkdtemp` (lambda.py:134) runs before the `try/finally`, so a missing `clang` (or a failure writing the IR) leaves `/tmp/lambda-build-*/program.ll` behind. Reproduced with `PATH=/nonexistent`. Move `mkdtemp` inside the `try`, or use `TemporaryDirectory`. | VERIFIED |
| C4 | `-fuse-ld=lld` is hard-coded (lambda.py:152). Machines with clang but no lld fail with a linker error and no hint. Probe for lld, or fall back to the default linker. | READING (lld is installed here) |
| C5 | `RecursionError` is handled only around `build()`; `--layout` (`data_layouts`) is not covered. A missing file name in the message for invalid UTF-8 source (bare codec error). Both nits. | VERIFIED (UTF-8), READING (layout) |
| C6 | `include` accepts absolute and `../` paths, and a failed parse prints the first token of the file: `include "/etc/hostname"` reports `found 'vm'`, `/etc/passwd` reports `found 'root'`. Only matters if untrusted `.lambda` sources are compiled; then it is a one-token read primitive. Special files (devices, FIFOs) are rejected. | VERIFIED |
| C7 | **`LAMBDA_RAWLOOP=1` (compile-time, undocumented) gives a deterministic double free.** `lambda_arrayf_pmap_raw_loop` and `lambda_arrayf_pmap_raw` end with `if (h->dtor) h->dtor(env); free(env);`, but the generated `env_dtor` already calls `lambda_free_env`. The repo's own `tests/arrays/arrayf_pmap.lambda` fails under it. Either delete the debug switch or remove the extra `free`. | VERIFIED |
| C8 | When a worker thread cannot be created, its chunk is skipped silently and the result array keeps stale values (no error path). | READING |

### GPU path

The CUDA/NVRTC code (compiler.py 3215-3393) is **unreachable**. `arrayf-pmap` is lowered to the inline serial loop first, so `LAMBDA_GPU=1` and Pi's `{gpus: 1}` change nothing (`cpu/wall = 1.00` on a 4096² field). The docs disagree with each other (README:40 vs README:409; `gray_scott.pi` says "GPU-backed worker"; `field.lambda` says "parallel CPU workers"). Either wire the dispatch up or state that `arrayf-pmap` is serial and remove the GPU claims. VERIFIED.

### Standard library (second batch)

| id | problem | status |
| --- | --- | --- |
| L10 | **`Complex::exp` is wrong.** It uses `cos/sin (Complex::arg a)` (the angle of `a`) where `cos/sin a_im` is required. `exp(0+1i)` returns `(6.1e-17, 1.0)` instead of `(0.5403, 0.8415)`, and `exp(0+πi)` returns `(6.1e-17, 1.0)` instead of `(-1, 1.2e-16)`. It is correct only when `a_im = 0`, which is why `exp(1+0i)` looks fine. | VERIFIED |
| L11 | **`Complex::modulus` returns NaN for zero**: `ratio = min/max` is `0/0`. `modulus(0+0i)` and `modulus(-0+0i)` give `nan`, so `toPolar 0` is NaN too. Special-case `max = 0` (and infinities). Other inputs (`3+4i`, `1e200+1e200`, `0+5i`) were correct, and overflow-safe. | VERIFIED |
| L12 | `complex.lambda` defines global `max` and `min` over *absolute values* (`min` returns `abs`). Any program that includes it and defines its own `max` fails with `duplicate definition of 'max' in the same block` (reproduced). Rename to `cabs-max`/`cabs-min` or make them local. | VERIFIED |
| L13 | `matrix.lambda` says shape errors "surface as ordinary bounds failures". They do not: `mat-add`, `mat-sub`, `mat-mul` ignore the other operand's dimensions. A 2x2 plus a 3x3 returns a 2x2 silently; a 2x2 times a 3x2 silently uses the first two rows; `mat-get m 0 3` on a 2x2 returns element (1,1); `mat-set` writes the wrong cell; `mat-zero -2 -3` allocates 6 elements with negative `rows`. Check `ac == br`, equal shapes, and `0 <= i < r`, `0 <= j < c`. | VERIFIED |
| L14 | `field.lambda` calls itself "bounds-safe", but `field-get`/`field-set` only bound the whole array: `i >= n` aliases into the next plane (`field-set f 1 3 3 0 5.0` writes plane 1, element 0 without error). `field-length` divides by `k` (k = 0 gives a division-by-zero runtime error). | VERIFIED |
| L15 | `stream.lambda`: `put-u32` truncates, so a payload over 4 GiB writes a wrong length in the header; payloads are "raw host values" but the header claims little-endian (wrong on big-endian hosts). `emit-f64s` binds an unused `w`. | READING |
| L16 | `vec2.lambda` repeats its header comment twice (the first copy is stale). `vec3.lambda` has no `cross`/`norm`. `kalman.lambda` duplicates every matrix 3-4 times to work around consume-on-use (the symptom of 2.4), uses `Q = q*I` rather than a discretised process noise, and the `(I-KH)P` form, which can lose symmetry or positive definiteness over many steps (Joseph form is the stable choice). | READING |

### Language details

- `str->int` saturates silently on overflow (`"99999999999999999999999"` gives `9223372036854775807`) and accepts trailing junk (`"12abc"` gives 12, `"abc"` gives 0). There is no way to tell failure from `0`. `str-byte-at`/`str-codepoint-at` return `-1` out of range. Document, or add a checked variant. VERIFIED.
- `shl`/`shr` mask the shift count (`shl 1 64` is 1, `shl 1 -1` is `-2^63`) and `shr` is a logical shift (`shr -8 70` is a large positive number). Same status as 2.4a (undocumented, defined behaviour). VERIFIED.
- Diagnostics for `0xFF` and `1_000` are misleading (`unbound variable 'xFF'`, `'_000'`): the lexer reads `0` then an identifier. A hint such as "hex/underscore literals are not supported" would help. VERIFIED.
- `run_tests.py`: nothing exercises `pi aot`, the atomic-refcount build, or any sanitizer, and CI runs only `run_tests.py` (the Pi tests and `Pi/tools/check_cpp_examples.py` are not in the workflow). READING.

### Pi

- **`pi aot` cannot compile its own output** (VERIFIED). `Pi/pilang/aot.py:363` compiles with `-std=c99`, which hides POSIX declarations, so `usleep`, `useconds_t` and `setenv` are undeclared and clang reports 4 errors on `report_demo.pi`. With `-std=c99 -D_POSIX_C_SOURCE=200809L` one error remains; `-std=gnu17` or `-std=c99 -D_GNU_SOURCE` compiles, and the resulting binary runs. No test or CI job covers `aot`, which is how this went unnoticed. Use `-std=gnu99` (or define the feature macros at the top of the emitted C).
- `aot.py:_cstr` escapes only `\`, `"`, `\n`, `\r`, `\t`; NUL and other control characters pass through into C string literals. Tested in the fourth pass: a NUL truncates the string, see A5.
- **Worker pipes are inherited across `fork`** (READING). `spawn_worker` (pi_runtime.cpp ~310) uses `pipe()` without `O_CLOEXEC`, so every later worker inherits the stdin/stdout pipe ends of all earlier workers, plus any other open descriptor. Shutdown uses `SIGKILL` so nothing hangs today, but a worker could write to a sibling's stdin, and closing a pipe will never deliver EOF while a copy lives. Use `pipe2(..., O_CLOEXEC)`.
- **`setenv("LAMBDA_GPU")` in the parent** (READING) is called under `g_spawn_mu`, but `getenv` readers on other threads (timeout branches, `par for`) are not covered; `setenv` is not thread-safe. Pass an `envp` to `execle` instead; this also removes the Windows save/restore code.
- **`timeout` detaches the branch thread instead of cancelling it.** Reproduced in the fourth pass (P2: exit status 1 after a handled `Deadline`, and a hang on `restart`). Still READING only: at exit `shutdown_all_workers` does `delete w` on `Worker` objects the detached thread may still be using, a possible use-after-free that I did not reproduce.
- Probed `pi_runtime.cpp` for `system`, `popen`, `strcpy`, `sprintf`, `gets`, `tmpnam`: none are used; subprocess launch is `execl` with an argv. A quote in a worker path is refused.

## 3C. Fourth-pass findings (backend differential, shipped programs, Pi runtime and AOT)

### The type-error fallback is worse than 1.3 suggested (VERIFIED)

I ran every non-compile-fail case of `run_tests.py` (143) with `--dynamic`, which is the backend the silent fallback
switches to:

| outcome | cases |
| --- | --- |
| identical stdout and `live=0` | 106 |
| cannot compile in the boxed backend | 36 |
| runs, but is far slower | 1 (`nth_prime_parallel`: 0.88 s typed, still running after 280 s dynamic) |
| different output | 0 |

The 36 failures are every program that uses a sum type, a record/`data` type, `arrayf-sum`, `arrayi-sum`,
`arrayf-pmap-into`, `arrayf-get-unchecked`, or the `matrix`/`vec2`/`vec3`/`complex`/`kalman` libraries
("unknown enum variant", "unknown sum type", "primitive ... is not implemented"). Two more (`floats/newton`,
`programs/nth_prime_sieve`) fail with a spurious `consumed by a destructive array update` because the fallback turns
borrows off. So for a quarter of programs the fallback cannot work at all, and when it does work it can be hundreds
of times slower. That strengthens the case for 1.3: make `--strict` the default and keep the fallback behind a flag.

### Language and compiler

| id | problem | status |
| --- | --- | --- |
| C9 | Variants of 2.4 that compile with no diagnostic and silently alias one buffer: (a) the same array passed twice to a consuming function, `(h a a)` with `h x y = (arrayi-get (arrayi-set x 0 5) 0) + (arrayi-get y 0)` returns 10, not 6; (b) an alias binding, `g → a` then `arrayi-set g`, makes the later `(arrayi-get a 0)` return 2, not 1; (c) two `arrayi-set` calls on the same source, `b → (arrayi-set a 0 7)` and `c → (arrayi-set a 1 8)`, leave `c[0] = 7` and `b[1] = 8`. valgrind was clean and `live=0` in all three. | VERIFIED |
| C10 | **Multi-parameter generics cannot be declared.** `data Pair<A, B> = P (a: A, b: B)` and `data Pair<A,B> = ...` both fail with `expected '=' after data name`. `parser.py:267` splits the parameter list on `,`, but the lexer has already split the token at the comma, so that code is dead. Only one type parameter works. `--layout` fails the same way. Either support it or give a clear error. | VERIFIED |
| C11 | `type` declarations cannot mention other types, including themselves, and the error is misleading: `type Q (a: P, b: P)` and `type T (A (x: T))` report `unknown type annotation 'P'` / `'T'`. The fix is to use `data`, which the message never says. | VERIFIED |
| C12 | A `data` type with no base case (`data T = A (x: T)`) is accepted; it is uninhabited, so harmless, but a warning would help. | VERIFIED |
| C13 | Redefining a parameter from its own value (`data → (arrayi-set data ...)` inside a function that has a parameter `data`) is rejected with `definition 'data' is used before it is initialised`. The message is accurate but the idiom is common in imperative-style code, and `learn/quicksort.lambda` uses it. Mention the rule in the README. | VERIFIED |

### Shipped programs that do not compile (VERIFIED)

I compiled every `.lambda` file in `examples/`, `learn/`, `simulations/`, `boostrap/` and `bench/` (48 files). 42 compile.
These six do not:

| file | failure |
| --- | --- |
| `learn/closure.lambda` | 15 bytes, `derivative →` and nothing else (truncated). |
| `learn/quicksort.lambda` | unfinished: `quicksort` has no else branch and no final expression; also uses the rejected rebind idiom (C13). |
| `simulations/gas.lambda` | ends after its definitions with no final expression ("add a final expression"). |
| `boostrap/lambda_ast.lambda` | ends with an unfinished `node-walk \to (\lambda n.` definition (empty body); the parse error is at line 50. `boostrap/lexer.lambda`, which includes it, fails too. (The directory name is also misspelt: `boostrap`.) |
| `examples/complex_safe_div.lambda` | calls `Complex::safe-div`, which does not exist. The test version, `tests/borrows/complex_safe_div.lambda`, uses `Complex::div`; the example was not updated after the rename. |
| `examples/brainf_argv.lambda` | `type error: expected ArrayB, got Int -> 't` at line 40, then the fallback adds `a borrowed inspection cannot be passed to a consuming parameter`. My reading is that the trailing bare `0` on its own line after `(bf ...)` is parsed as an application of the result, but I did not isolate it. |

Nothing runs these files in CI. Adding a "compile every example with `--strict`" step to `run_tests.py` would have caught all six.

### Pi runtime (`pi_runtime.cpp`)

| id | problem | status |
| --- | --- | --- |
| P1 | **A worker that crashes mid-call takes the whole runtime down.** `write_all`/`read_all` call `die()`, which prints `pi-cpp: worker read` and exits with status 1. `WorkerDied` is only raised for a worker that was explicitly killed (`kill_worker`) or restarted, so `on_error`, `restart` and `retry` never see a real crash. Reproduced with a worker op that reads `arrayi 1 0` at index 5: the program prints `a 0`, then `pi-cpp: worker read`, exit 1, and the `on_error(... ) { WorkerError(5001) ⇒ restart }` block does not run. The fault-tolerance features only cover error frames. No `SIGPIPE` handler is installed either, so a write to a dead worker can kill the process with the signal instead (READING). | VERIFIED (crash), READING (SIGPIPE) |
| P2 | **`timeout` leaves a detached thread behind, with two visible effects.** (a) A program that catches `Deadline` and finishes normally (`done 1` printed, report written) still exits with status 1 and prints `pi-cpp: worker read`: the shutdown kills the hung worker and the detached branch thread's `read_all` then calls `die()`. 3 of 3 runs. (b) The documented recovery for `Deadline`, `\| Deadline ⇒ restart(max = 1)` (pispec.md:426), hangs forever: the `Deadline` error carries no worker, so `restart` does nothing, and the next `call(w, ...)` blocks on `w->io_mu`, which the detached thread still holds while it waits on the hung worker. The run used 0.04 s of CPU in 25 s and was killed by `timeout 25`. Cancel the branch (or kill and restart the worker it holds) before detaching, and join before `delete w` at shutdown. | VERIFIED (a, b); mechanism READING |
| P3 | **`par for` starts one OS thread per element with no limit.** `par for i in payload(range(1, 100001))` ran about 86 s, printed up to `i 32620`, then died with `terminate called without an active exception`: `std::thread` construction throws at the process limit (`ulimit -u` is 32018 here) while earlier threads are still joinable, so `~vector<thread>` calls `std::terminate`. Use a bounded pool (hardware threads, or the `Pool` size) and join in an exception-safe way. | VERIFIED |
| P4 | **The plan JSON parser does not decode `\uXXXX`** (`pi_runtime.cpp:109`, the `default:` branch keeps the letter and drops the backslash). A label containing a NUL byte, which Python's `json` writes as `\u0000`, is emitted as `au0000b`; `\b` and `\f` become `b` and `f`. Any control character in a label or string is mangled. | VERIFIED |
| P5 | **Large but flat programs hit two different undocumented limits.** A chain of 500 `+` terms runs; 700 fails in the C++ runtime with `plan JSON: nesting too deep at 17367` (cap 512, and a left-associated chain nests one level per term); 1000 fails earlier in the Python frontend with a bare `pi run: maximum recursion depth exceeded` and no source location. Generated Pi programs can reach this. Raise the recursion limit in the frontend as `lambda.py` does, build the plan iteratively, or report the limit with a location. | VERIFIED |
| P6 | `die()` calls `std::exit(1)` from any thread (worker, timeout, `par for`, controller). `exit` from a non-main thread runs static destructors while other threads keep running; this is the common cause behind P1 and P2. Throw a `PiError` (or set a global failure flag) instead and exit from `main`. | READING |
| P7 | `as_int` (`(long)v.num`) and the AOT `pi_as_int` cast a double to `long` with no range check. NaN, infinities and values above 2^63 are undefined behaviour in C/C++ (`max: 1e30` in `retry`, `chan(1e30)`). `SECURITY_REVIEW.md` says integer conversions are checked; these are not. | READING |
| P8 | **The worker build writes a shared schema file.** `worker.py:78` writes `<builddir>/schema/pi_schema_<Proto>.lambda`, keyed only by the protocol *name*, then compiles the worker with that directory on `LAMBDA_PATH`. Two programs (or two versions of one program) with a protocol of the same name but different ops race: one worker can be compiled against the other's constants and cached under the first one's hash. At run time that fails with `worker schema mismatch`, and because the executable is trusted if it exists (2.5), every later run fails until `/tmp/pi-build` is cleared. The temporary output name `exe + ".tmp"` is also fixed, so concurrent builds of one worker can install a half-written file. The cache also never shrinks: I saw six variants of `drone_matrix`. Put the schema in a per-build directory and use `mkstemp` for the output. | READING |

### Pi AOT (`aot.py`, `aot_runtime.c`)

Beyond the compile failure already listed (3A/3B). These apply when the generated C is built with `-D_GNU_SOURCE`.

| id | problem | status |
| --- | --- | --- |
| A1 | **Without `--out`, CSV artifacts are written to the filesystem root.** `aot.py:342` emits `pi_set_output("")`, so `g_out` is an empty string, not `NULL`; `open_out` then builds `"%s/%s%s"` = `/index.csv`. Running `report_demo.pi` through `aot` as root created `/index.csv`. (I deleted the two test files I created this way.) The fallback branch `if (!g_out)` that prints a summary instead is never reached. Treat an empty directory as unset. | VERIFIED |
| A2 | **Artifact labels are not sanitised.** `open_out` formats the label straight into a path, so `emit_series("../ESCAPED_aot", xs)` writes outside the output directory (`/ESCAPED_aot.csv` in my run). The C++ runtime has `safe_artifact_name` for exactly this; the AOT runtime does not. | VERIFIED |
| A3 | The AOT runtime reads and discards the HELLO frame: no magic-version check, no schema-hash comparison. The C++ runtime refuses a worker built against a different protocol (`worker schema mismatch`); AOT binaries accept any worker. | READING |
| A4 | The reply parser trusts the worker: the stats count `k = (long)body[1]` is never checked against the frame length. A negative `k` makes `off` negative, so the copy loop reads `body[off..]` before the start of the heap buffer (out-of-bounds read); a large `k` gives `pi_payload` a negative length. There is also no `MAX_FRAME` cap on `nbytes` (a 4 GiB `malloc`). The C++ runtime validates all of these (`reply BadFrame stats length`). | READING |
| A5 | Strings containing NUL are truncated by the C `printf("%s")` (`emit_text("note", "a\0b")` prints `a`, the C++ runtime prints the whole string), and control characters pass through `_cstr` unescaped (harmless in a C literal except NUL). | VERIFIED |
| A6 | `pi_call` serialises every request twice: the first pass only counts `nargs` and is thrown away (the comment says "header first was wrong"). For a 10M-element payload that doubles the `%.17g` formatting cost. | READING |

### Also checked, no problem found

- `Pi/pilang` Python frontend: no `eval`/`exec`/`shell=True`/`pickle`; `subprocess` uses argument lists; the plan file is created with `mkstemp`.
- The HTML report escapes labels correctly: an `emit` label of `<img src=x onerror=alert(1)>&amp;</script><script>alert(2)</script>` appears only as `&lt;...`/`&amp;amp;` text and as `\x3c` in the embedded JSON.
- `editors/`: install scripts only copy files into `~/.vscode/extensions`; the completion provider is regex-only. Nit: `install_vscode.sh` does not delete stale files from the versioned directory on upgrade. `bench/`: the runner and sources look fine; benchmark numbers were not re-run.
- Lambda codegen probes, all with `live=0` and a clean valgrind run: closures returning closures (3 levels), partial application of 8-argument functions, closures that capture strings and arrays, mutual tail recursion 10 million deep, a 20,000-iteration string-building loop, a 100,000-deep non-tail recursion, branch-asymmetric ownership (an array used in one `if` arm only), unused definitions and arguments, `match` payloads unused in one arm, `data` types 1,000,000 nodes deep (arena) and a 2^19-node tree.
- Scoping: shadowing of globals by parameters, closures over shadowed names, mutually recursive local functions, duplicate parameters/definitions, and recursive values without a base case are all handled or diagnosed correctly.

## 3D. Additional notes (language limits, driver, test harness)

| id | problem | status |
| --- | --- | --- |
| N1 | **`match` has no wildcard or default arm.** `A → 1 \| _ → 2` and `Some x: x, _: 0` fail with `'_' is not a variant of enum S` / `of sum Opt`. Every variant must be listed, so adding a variant to a large enum breaks every `match` over it (arguably a feature, but undocumented, and the message does not say wildcards are unsupported). | VERIFIED |
| N2 | **Every build recompiles the C runtime at `-O3`.** `_runtime_object` (lambda.py:29) was changed to a per-build directory to remove the `/tmp` trust problem, but that makes each build pay for compiling about 3,460 lines of C: 0.9 s warm and 3.6 s cold in my runs, against 1.2 s for the whole `hello` build. A per-user cache directory (`0700`, keyed by a hash of `RUNTIME_C` and flags, built with `mkstemp` + `os.replace`) would keep the safety and remove the cost; it is the same cache design proposed in 1.2. | VERIFIED (timings) |
| N3 | **Internal compiler errors are reduced to one line.** `lambda.py:291` catches every `Exception` and prints `compilation error: <message>` with no traceback or location unless `--debug` is passed, which makes compiler bugs hard to report. Print the traceback to stderr (or a `LAMBDA_DEBUG` hint) for unexpected exception types. | READING |
| N4 | **`LAMBDA_PATH` and `LAMBDA_LIB` precede the bundled `lib/`** (loader.py:29-38, deliberate per its comment). A hostile or stale environment variable can therefore replace `list`, `string`, etc. for every program compiled in that shell, with no warning. Worth a note in the docs, and perhaps a `--verbose` line listing which file satisfied each include. | READING |
| N5 | `loader.py`: `include` also expands `~`, accepts absolute paths and `.lambda`-suffix-less names, and tries up to `2 + 2 × len(search_paths)` candidates, so the error text for a miss is long. Harmless; mentioned because `include` is the only way a source file reads other files at compile time (see C6). | READING |
| N6 | `run_tests.py` details: (a) `SUSPICIOUS_STDERR` lists only two markers, so other compiler warnings (for example the likely-infix warning, or a future one) pass silently; (b) a manifest entry with neither `stdout` nor `compile_fail` compares against `None` and reports a confusing `stdout mismatch ... expected: None`; (c) in single-file mode the manifest is parsed up to three times; (d) executable names are `<group with / replaced by _>_<name>`, so `a/b` + `c` and `a` + `b_c` would collide (harmless today because cases run sequentially, but it would break any parallel runner); (e) there is no way to run the suite under `--dynamic`, the atomic-refcount build, or valgrind, which is how the fallback-backend differential in 3C had to be done with a throwaway script. | READING |
| N7 | `kalman.lambda` and `vec2.lambda` allocate and copy matrices several times per step to avoid use-after-consume (see L16 and 2.4), so the "no hidden copies" performance story in the README does not apply to library code written against linear arrays. | READING |

## 3E. Fifth pass: security and memory safety

Scope: runtime C code (extracted `RUNTIME_C`), the dynamic backend, input primitives, thread pool, `tools/viewer.py`,
plus three dynamic sweeps: valgrind memcheck over the whole suite, DRD over the repo's parallel tests, and a 600-mutant
fuzz run (about 120 mutants compiled and ran; the rest were rejected by the front end). No sanitizers exist on this
machine, so everything below was found by probes, valgrind or reading.

### Findings

| id | severity | problem | status |
| --- | --- | --- | --- |
| S1 | Medium-High | **`tools/viewer.py` serves the whole parent directory of the file you open, and does not check `Host`/`Origin`.** `Handler.do_GET` serves `/data/<rel>` from `self.root`, and `load_source` sets the root to `path.parent` for a file argument (or `HERE.parent` for stdin). Opening `~/proj/plan.json` therefore exposes everything under `~/proj/`, including hidden directories such as `.ssh/id_rsa` or `.git/config`, to anything that can send an HTTP request to `127.0.0.1:<port>`. Because the server is bound to loopback but accepts any `Host` header, a web page the user visits can reach it through DNS rebinding (the page's origin resolves to 127.0.0.1 after the first lookup). In my test the server accepted requests with an arbitrary `Host` header and with no `Origin` check. Fix direction: serve only an explicit allowlist (the files the viewer itself generated), reject dot-files, and reject requests whose `Host` is not `127.0.0.1:<port>`/`localhost:<port>`. | VERIFIED |
| S2 | Medium | **Dynamic-backend `str-len` has no type check and returns `v->i` as the length.** Because the type-error fallback (1.3) routes ill-typed programs to this backend by default, `(str-len [1,2])` printed `94342112607008` (a heap pointer), a closure printed `94271052565280`, and `2.5` printed the raw bits of the double. That is an ASLR bypass and an information leak primitive reachable from any program that fails type checking without `--strict`. Same cause as S3. | VERIFIED |
| S3 | Medium | **Dynamic-backend `sum` and `product` segfault on non-list arguments.** `lambda_op_sum`/`lambda_op_product` check only for `NULL` and `TAG_NIL`, then read `list->i` as a pointer without checking `TAG_CONS`. Non-list arguments crash with SIGSEGV instead of the usual `expected List, got ...` error. Audit the other boxed primitives for the same missing tag check (my type-confusion battery fed wrong-typed arguments to the primitives; `sum`, `product` and `str-len` were the ones that did not fail cleanly). | VERIFIED |
| S4 | Medium | **Freeing a long closure chain overflows the native stack.** The closure environment destructor frees captured closures recursively. About 150,000 nested closures segfault when they are released, without any of them being called (exit 139 at N=200,000; smaller chains passed). The `data` ADT path frees iteratively and handles 1,000,000 nodes, so this is an inconsistency, not a design limit. Reachable by any program that builds a chain of closures in a loop (for example composing a function with itself) and then drops it. | VERIFIED |
| S5 | Medium | **`read-line` splits long lines.** The runtime reads with `char buf[4096]; fgets(...)`, so a line longer than 4095 bytes is returned as several reads, and a line of exactly 4095 bytes is followed by a phantom empty read. A program that consumes input line by line silently desynchronises on such input (for example a CSV with one very long row). | VERIFIED |
| S6 | Low-Medium | **`read-int` saturates on overflow.** `errno = 0` is set before `strtoll` but never checked afterwards, so `99999999999999999999` reads as `INT64_MAX` with no error. | VERIFIED |
| S7 | Low | **`read-line` and `read-int` truncate at an embedded NUL byte** (same C-string cause as 2.3), so input containing `\0` is cut silently. | VERIFIED |
| S8 | Low | **Viewer: a NUL (`%00`) in the request path raises an uncaught exception** in the handler (traceback on stderr, connection dropped). The server survives. | VERIFIED |
| S9 | Low | **Misleading rejection of `data` inside `pmap`.** The compiler correctly rejects allocating a `data` value inside a `pmap` function (the arena allocator is not thread-safe, so this prevents a real race), but the message is the generic "no printing, terminal input, or calls through unknown function values". Say why. | VERIFIED |
| S10 | Low | **Deep non-tail recursion crashes with a bare SIGSEGV.** Mutants such as `fact` with a wrong decrement or a start of `100000000` reach the 8 MB native stack limit and die with exit 139 and no message (5 of the 8 fuzz failures; with an unlimited stack they run until the 3 GB memory cap). This is normal for compiled languages, but a guard page handler that prints `stack overflow` would turn it into a diagnosable error. (3A already notes native stack overflow; this adds that the failure is silent.) | VERIFIED |
| S11 | Low | **`argv` with an out-of-range index returns an empty string** instead of an error, so a missing command-line argument and an empty one cannot be told apart. Docs note. | VERIFIED |
| S12 | Low | **`rc` is an `int32_t`.** A value with 2^31 references would wrap into the negative "immortal" sentinel and never be freed. Needs more than 16 GB of pointers, so unreachable in practice. | READING |
| S13 | Low | **Immortalised values are invisible to the leak check.** `immortalize` excludes values from `live=` stats, so `live=0` cannot detect a leak of an immortalised allocation; valgrind reports exactly these allocations (2 in the whole suite, both intentional). | VERIFIED |

C8 (thread-create failure) is narrower than first written: the main `pmap` path handles a failed `pthread_create`
correctly and the main thread still completes the work; only the raw/`LAMBDA_RAWLOOP` paths do not.

### Checked and found sound (this pass)

- **Valgrind memcheck over all 144 suite programs that run without input:** zero invalid reads or writes and zero uses
  of uninitialised values. (This run used the stock runtime, whose array-data pool hides misuse of recycled blocks; section 3F repeats
  it with the pool disabled and finds one real size-class defect, F1, by a different route.) The only reports are the two intentional `immortalize` allocations (S13).
- **DRD over the 8 repo parallel tests:** zero conflicts. They never share a tail between workers, which is why they do
  not see 1.1; the repro in 1.1 was re-run in this pass and still fails with 8 threads (crash or hang in most of 60 runs,
  always correct with `LAMBDA_THREADS=1`).
- **`pmap` cannot be reached through an alias:** `p → pmap` then `(p f xs)` is rejected with "pmap must be applied
  directly to a provably pure function and a list", so the purity and atomic-refcount switch
  (`_uses_parallel_primitives`, compiler.py:485-499) cannot be bypassed by renaming.
- **`lambda_arrayf_pmap_into` is a serial loop** (no threads, so no race, and no need to be in
  `_uses_parallel_primitives`).
- UTF-8 and UTF-16 encode/decode, `snprintf` buffer sizes, the pool mutex and the `LAMBDA_MAX_THREADS = 64` cap.
- **Fuzz run:** 600 mutants of the shipped test programs (delete, duplicate, swap, extreme numbers, identifier and
  string replacement, bracket removal). 476 were rejected cleanly by the front end, 95 ran and exited 0, 20 ended in a
  clean runtime error, and none produced an internal compiler error, a clang failure, a Python traceback, or a leak
  (`live != 0`). The 8 bad outcomes were 5 stack-overflow segfaults from deep recursion (S10) and 3 timeouts from
  mutated loop bounds (expected).

### Not covered

No sanitizer (ASan/TSan/MSan unavailable); the Pi C++ runtime's pipe protocol and the AOT runtime are READING-only (see
3C); `compiler.py`'s typed-primitive emitter was not read line by line, so a missing tag or bounds check there would
not have been seen except through the differential and fuzz runs.

## 3F. Sixth pass: memory safety of the compiled programs

Question asked: do the executables Lambda produces have memory errors or leaks? Everything below was run, not just
read, unless marked READING. No ASan or TSan exists on this machine, so the checks were built from what is available.

### Method

1. **Hardened rebuild of every suite program.** Each of the 143 runnable suite programs was compiled twice (typed
   `--strict`, and `--dynamic`), the `--keep-ir` output was kept, and the same IR was linked against three runtimes:
   the stock one (reference output), one with the array-data pool disabled and run under **valgrind** (`--leak-check=full`,
   definite, indirect and possible leaks plus invalid access and uninitialised use all counted as failures), and one
   with **trapping UBSan** (`-fsanitize=undefined,float-cast-overflow -fsanitize-trap=all`, which needs no runtime
   library). Exit code and stdout of the UBSan build had to match the stock build. The atomic-refcount runtime was
   used for the 22 programs that use `pmap`.
2. **Why the pool is disabled in one build.** The runtime recycles array data blocks through a small pool
   (`pool_take`/`pool_put` in `RUNTIME_C`, up to 16 blocks per power-of-two size class). A block in the
   pool is still allocated as far as valgrind can tell, so use-after-free and double free of array data are invisible in
   the stock build, and the `live=` counter does not see them either. Values themselves are plain `malloc`/`free`, so
   those are visible. This also means the "zero invalid accesses" in 3E was measured on the pooled runtime and could
   not see pool misuse; the new run closes that gap (and finds F1 below by a different route).
3. **Ownership stress programs** (20 written, 18 compiled; the other two were rejected by the compiler's own purity or type rules: string building and borrowing, closures capturing arrays and
   strings, closures never called, lists of closures, `data` trees, `match` arms that ignore payloads, arrays used in
   one branch only, clone-and-set loops, float and byte arrays, `pmap` over strings, lists and nested lists, `fix`,
   partial application, nested lists, unused arguments), each run under stock valgrind, no-pool valgrind and UBSan.
4. **Ownership-focused mutation fuzz:** 1,500 mutants generated (1,337 differed from their seed) of the memory-heavy test groups (arrays,
   closures, strings, borrows, data, lists, records, enums, programs, core, floats, integers), with identifier swaps and
   extra-argument mutations that keep programs type-correct more often than random edits. Survivors were run under the
   same no-pool valgrind and UBSan checks.
5. **Size-class sweep** of the array pool (ArrayI, ArrayF and ArrayB, 40 sizes from 0 to about 1,900 elements,
   each size allocated, filled and dropped against every other size) on the stock runtime under valgrind.

### Results in one table

| check | programs | result |
| --- | ---: | --- |
| suite, typed, no-pool valgrind + UBSan trap | 142 compiled | 140 clean; 2 flagged, both the intentional `immortalize` in `nth_prime_sieve` and `nth_prime_sieve_100k` (a leaked-by-design array: 24 bytes definite, 16-32 KiB indirect) |
| suite, `--dynamic`, same checks | 107 compiled | 105 clean; 2 timed out in the 60 s reference run (`nth_prime`, `nth_prime_parallel`: the known slow fallback, 1.3/3C); 36 programs do not compile in this backend (3C) |
| UBSan trap in the runtime C | 247 runs | no trap, identical stdout and exit code to the stock build every time |
| ownership stress programs | 18 | no leak, no invalid access, `live=0`; one finding (F1) from the two programs written to hit the pool |
| mutation fuzz | 1,337 mutants | 1,144 rejected by the front end; 182 ran clean; 8 timed out at 8 s (mutated loop bounds); 1 compiler failure (F5); 2 flagged, neither a memory bug (a mutant that recurses forever and overflows the stack, S10, and a runtime-error exit with a half-built value, which is expected because `fail` calls `exit` without unwinding) |
| pool size-class sweep | 1 program, 1,680 valgrind errors in 9 contexts | all from F1; with that one line corrected in a scratch copy the same program reports 0 errors, so F1 is the only size-class defect for these sizes |

### Findings

| id | severity | problem | status |
| --- | --- | --- | --- |
| F1 | Medium (a real heap overflow; likely masked on glibc) | **Freeing an empty `ArrayI` or `ArrayF` puts its 1-byte block in the 8-byte size class.** `lambda_arrayi_new(0, ...)` and `lambda_arrayf_new(0, ...)` size the block with `checked_array_bytes`, which returns 1 for an empty array, so `pool_take` hands out (or mallocs) a 1-byte block of class 0. On free, `lambda_release_zero` (the `TAG_ARRAYF`/`TAG_ARRAYI` case, `pool_put(v->p, sizeof(double) * ...)`) computes the class from `sizeof(double) * (v->i > 0 ? v->i : 1)`, which is 8, so the block is filed under class 3 (capacity 8). The next `pool_take` for 5 to 8 bytes (an `ArrayI`/`ArrayF` with one element, or an `ArrayB` with 5 to 8) receives the 1-byte block and writes up to 8 bytes into it. valgrind: `Invalid write of size 8 ... Address is 0 bytes inside a block of size 1 alloc'd` in `lambda_arrayi_new`/`lambda_arrayf_new` and in compiled code. Output is correct in every run because glibc's smallest `malloc` chunk has 24 usable bytes (my expectation from how glibc works, not tested on another allocator), so on glibc the write stays inside the chunk; any allocator with exact-size blocks could corrupt the neighbour. The `_uninit` constructors used by `arrayi-pmap`/`arrayf-pmap` with a length of 0 take the same 1-byte path (READING). The suite never frees an empty array before allocating a one-element one, so stdout tests, `live=0` and the earlier valgrind sweep all missed it. Fix direction: make `pool_put` use the same size rule as `pool_take` (store the capacity on the value, or use `1` when the length is 0); I checked in a scratch copy that this single change takes the size-class sweep from 1,680 errors to 0, and the two small repros (`p01`, `p02`) from 2 errors to 0. Add a test that frees an empty array and then allocates a one-element array, and run the suite under valgrind in CI (output-only tests cannot catch it). | VERIFIED |
| F2 | Medium | **`data` values are never reclaimed.** The `data` allocator is an append-only arena (`lambda_arena_new`/`arena_reserve`: `count` only increases; no free list). A loop that builds and drops one small `data` value per iteration grows memory without bound even though nothing is alive: 20,000,000 iterations of `(match (B n) with B v: v)` reached a peak RSS of about 300 MB (16 bytes per node for a one-field type); 200,000,000 iterations under `ulimit -v 2000000` stops with `out of memory growing ADT arena`. A control loop that conses and drops a list 200,000,000 times stays flat. `live=` does not count arena rows and valgrind sees the arena as still reachable, so both checks report this as clean. Programs that run long or in a service loop and use `data` for short-lived values will exhaust memory; this should be stated in the docs, or rows recycled once a node's reference count reaches zero. | VERIFIED |
| F3 | Low-Medium | **Each `data` arena reserves 2,097,152 rows up front** (`a->cap = 2097152`, then `malloc(cap * (width + 1) * 8)`): 16 MiB per word of row width, so a type with 10 fields reserves about 176 MiB and the 64-field maximum about 1 GiB, per arena, before the program does anything. Overcommit hides it on a normal Linux box (a 10-field type ran under `ulimit -v 400000`), but a hard address-space limit, a container with no overcommit, or several large types will fail at startup. I did not count how many arenas a program creates. | READING for the size, VERIFIED for the 10-field case |
| F4 | Medium | **Dropping a deeply left-nested list overflows the stack in the `--dynamic` backend.** `(cons (cons (cons ... nil) nil) nil)` nested 100,000 deep frees fine; nested 400,000 and 2,000,000 deep segfaults when the value is dropped (exit 139). Judging by the crash, the release is iterative along the tail but recursive through the head. The typed backend rejects the program (`cannot construct infinite type`), so this is reached through the default fallback (1.3), and a typed `data Rose = Rose (v: Int, kids: [Rose])` built 1,000,000 deep frees fine. Same cause and fix as S4 (closure chains). | VERIFIED |
| F5 | Low | **A top-level expression of record type fails in clang with a raw IR error.** `type Box (value: Float)` followed by `(Box 3.0)` gives `clang failed to build the program: ... '%res1' defined with type '%rec_Box = type { double }' but expected 'ptr'` instead of a Lambda diagnostic. `(println (Box 3.0))` and a list of records give the clearer `product values cannot be boxed/first-class yet`. Found by fuzzing `tests/records/record_if.lambda` (`(Box-value (choose ...))` changed to `(choose ...)`). Under `--dynamic` the same program says `unknown product type 'Box'`. | VERIFIED |
| F6 | Low | **A top-level `data` value prints its arena index.** `data Box = B (value: Float)` then `x → (B 1.0)`, `y → (B 2.0)`, `y` prints `1`. Nothing says that the printed number is an internal handle, and a different allocation order prints a different number. | VERIFIED |

### Re-checked and not a problem

- **Pool and `read-file-bytes`.** The only place an array's length changes after allocation is
  `lambda_read_file_bytes_raw` (`a->i = got` with `got <= size`). The free path then picks a size class no larger than
  the allocation, which wastes space but cannot overflow.
- **Emitted IR carries no UB flags.** `add`/`sub`/`mul` have no `nsw`/`nuw` (so they wrap, as 2.4a says), there is no
  `inbounds`, `undef`, `poison` or `unreachable` in the IR I inspected, shifts mask the count with `& 63`, division
  by a non-literal or `-1` goes through the checked helpers, and `float->int` is saturating (NaN gives 0).
- **Runtime errors inside `pmap` workers:** `(/ 100 (- i 50))` over 200 elements with 8 threads exited with status 1 and the
  message `division by zero` in 60 of 60 runs; no hang, no crash.
- **Nested `pmap`:** list `pmap` inside list `pmap` compiles and returns the same sum on every run. The suspected
  race in `int_cache_ensure` (a `realloc` of the shared small-integer cache) is not reachable: the cache is only
  grown by `arrayi-pmap`/`arrayf-pmap`, which are called from the main thread, and an `arrayi-pmap` inside another
  `arrayi-pmap` is rejected as not provably pure.
- **File handles:** `read-file`, `read-file-bytes` and `write-file` close the handle on every path (READING).
- **Atomic-refcount build:** the 22 `pmap` suite programs were clean under the no-pool valgrind and UBSan builds; the
  only defect in this area remains the race in 1.1.
- **Leak checks that do not apply:** the intentional `immortalize` allocations (S13) are the only "definitely lost"
  blocks in the whole suite, and a runtime error (`fail`) exits without freeing, which is expected.

### Limits of this pass

- Valgrind with the pool disabled cannot see pool bugs (that is why F1 had to be found with the stock build); the
  stock build cannot see use-after-free of pooled blocks. Each blind spot is covered by the other run only for the
  programs I ran.
- The fuzz and stress programs only reach what the existing tests and my 20 programs use. I did not generate
  programs from a grammar, so unusual combinations of consume-on-write arrays, borrows and closures may remain
  untested.
- `compiler.py` was not read line by line (see section 5); a bug that only shows in an untried combination of
  ownership transfers would not have been found by these runs.
- No ASan/TSan/MSan, no Windows or macOS, no CUDA.

## 4. What looked solid

- Syntax errors carry `file:line:col` and a clear message; integer and float literal range is checked; unterminated
  strings and lists, empty programs and include cycles are rejected cleanly.
- Array bounds, negative sizes and huge sizes are all checked with runtime errors (not UB); `head`/`tail` of empty
  lists fail cleanly. `arrayf-get-unchecked` segfaults as its name warns.
- The borrow checker rejected returning a borrowed parameter, capturing it in a closure, and consuming a borrowed
  inspection.
- `pmap` purity analysis rejected `println`, `write-file`, and an impure function reached through a higher-order
  wrapper; CSE did not merge impure calls.
- Valgrind was clean (no invalid accesses, `live=0`) on every program I ran, including the use-after-consume variants, and on the whole suite in the fifth pass (3E). The 600-mutant fuzz run found no compiler crash, clang failure or leak.
- `lambda.py` builds the C runtime in a private per-build directory (the old shared `/tmp` object was removed).
- The whole suite (143 programs, typed and `--dynamic`) rebuilt against a no-pool runtime ran clean under valgrind and
  under a trapping UBSan build of the runtime C (3F): no invalid access, no uninitialised use, no UB trap, identical output.
- `tools/viewer.py` binds `127.0.0.1` only, `viewer.js` escapes label text, and `plot_svg.py` escapes titles.
- Subprocesses use argv lists, never a shell, as `SECURITY_REVIEW.md` says.
- `lib/bigint.lambda` multiplication and squaring matched Python's integers on 240 randomized and edge-case operands
  (0 to 500 limbs, all-`999999999` limbs, powers of the base, sparse limbs, sizes on both sides of the Karatsuba 50,
  square 52 and Toom-3 220 cutoffs), plus 80 add/subtract cases, with `live=0` every time. An undersized `cap` is
  caught as a safe out-of-bounds error.
- The Pi C++ runtime handled empty, garbage, truncated, trailing-data and 10-million-deep JSON plans with a clear
  message and a non-zero exit (nesting is capped at 512), and the shipped `heat.pi` example ran end to end in 10 s.
- Deeply nested source (5,000 parentheses, 3,000-arm `else if` chains) compiles; the large compiler thread stack works.
- With `--strict`, non-exhaustive and duplicate `match` arms are caught at compile time with the arm's location.
- The `pure` parameter annotation is enforced at call sites: passing a function that prints (directly, as a lambda, or
  via a block-sequenced `println`) to a `pure` parameter is a compile error, and the `pmap` impure-capture case is
  rejected. Every primitive is classified either impure or with an arity in `analysis.py`, none left unclassified.
- Lexer: out-of-range integers, non-finite floats, bad escapes, unterminated strings, CRLF, nested `;` comments,
  non-ASCII identifiers and emoji all behave sensibly; `INT64_MIN` as a literal works; `INT_MIN / -1` does not trap.
- Runtime division by zero, byte values outside 0..255, negative and oversized array sizes, and out-of-bounds
  `arrayi-get`/`set` all produce a clean runtime error.

## 5. Coverage and limits of this review

**Done:** full `run_tests.py` (170/170, 197 s); Pi `pytest` (17/17); about 130 targeted probe programs (literals,
integer edge cases, strings, files, arrays, borrows, CSE, `pmap`, typed vs `--dynamic`, polymorphism, `match`, parser
stress, every standard library file; see the third pass below for `matrix`, `complex`, `field`); a
differential test of `bigint` against Python; all 19 README code blocks compiled; the Pi C++ runtime built with
`-Wall -Wextra` and fed malformed plans; valgrind on about 15 programs;
`-Wall -Wextra -Wshadow` build of the extracted C runtime; reading of `lambda.py`, `loader.py`, `run_tests.py`, the
runtime's allocator/refcount, file I/O, thread-pool and GPU-loader code, Pi `cpp.py`/`worker.py`, the artifact-name
check, and the viewer and SVG tools.

**Third pass added:** `match` exhaustiveness and duplicate arms in the correct syntax (strict and fallback modes);
runtime primitive edge cases (about 20 probes); lexer edge cases (18 probes); `pure` annotation enforcement; the
purity tables in `analysis.py`; `matrix`, `kalman`, `complex`, `vec2`, `vec3`, `field` and `stream` (read, and
`complex`, `matrix`, `field` run); `pi aot` compile flags; spawn, shutdown and `timeout` code in `pi_runtime.cpp`.

**Fourth pass added:** all 48 shipped `.lambda` programs compiled; the whole test suite re-run under `--dynamic`;
about 50 more probes (closures, ownership, aliasing, `data` declarations, scoping); `editors/`, `bench/`,
`learn/`, `boostrap/`, `simulations/` read or compiled; `pi_runtime.cpp` worker call/restart/`timeout`/`par for` code and
`aot_runtime.c` read in full, with Pi runs for worker crash, hung worker, 100,000-way `par for`, source-size limits,
HTML escaping and AOT output paths; `type_algebra` unit tests (14/14 pass) and `--layout`.

**Sixth pass added (3F):** the suite in typed and `--dynamic` mode against a no-pool runtime under valgrind and a trapping UBSan runtime, 20 ownership stress programs, a 1,500-mutant ownership fuzz, an array-pool size-class sweep, and probes of arena growth, deep destruction, `pmap` error exits and nested `pmap`.

**Fifth pass added (3E):** valgrind over 144 suite programs, DRD over the 8 parallel tests, a 600-mutant fuzz run, a dynamic-backend type-confusion battery, input-primitive probes, closure-chain and recursion-depth probes, and the viewer's file serving.

**Not done, so no claim is made about them:**
- `compiler.py` was tested by behaviour (about 60 programs) but not read line by line. The typed-primitive emitter
  (`emit_typed_primitive`, lines 3408-3933), the pattern-match lowering for the three layouts, and the capture and
  tail-call helpers were not audited, so a bug that only shows on an untried combination would not have been seen.
  `typecheck.py` and `analysis.py` were probed (inference errors, `match` checks, purity, `pure` parameters,
  borrows) but not read in full.
- `pi_runtime.cpp`: the interpreter's channel/`select` implementation, instantiation of systems/elements, and the
  scheduler (`push`, `tick`, ports) were skimmed, not audited; the Pi findings marked READING above were not
  reproduced. Pi's `parser.py`, `checker.py` and `plan.py` were fuzzed but not read in full.
- No sanitizer run (none installed), no Windows or macOS build, so every `_WIN32` branch is unreviewed.
- The README benchmark numbers and the GPU path under real hardware were not exercised (there is no GPU here).
- Two earlier subagent attempts to cover the large files in parallel hit the usage limit and returned nothing; the
  work above was done directly.
- No AddressSanitizer or ThreadSanitizer (no compiler-rt on this machine), so there was no sanitizer sweep. The race
  in 1.1 was found by stress-running, not TSan, and other races in the thread pool or Pi runtime may exist.
- Not run: the README benchmark table, anything on Windows or macOS (every `_WIN32` branch is unreviewed), and the GPU
  path on real hardware.

**Settled by a later probe:** `match` has no wildcard arm (N1). Previously listed here as unconfirmed.

**How to read the VERIFIED/READING split:** every READING item is something I would want a maintainer to reproduce
before writing the fix; the table in the index marks them. The only fix that has been run is the one-line patch in 1.1.

---
