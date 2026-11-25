# Safety and security audit

Status: last full review 2026-11-14, before the `PR.md` fix cycle.  The current
fix status for every item mentioned here is tracked in
[`PR-COMPLIANCE.md`](PR-COMPLIANCE.md).  Re-verify the "review claim" bullets
against the code after the PR lands; several were found to be inaccurate by that
review (marked below).

This audit covers the Lambda compiler/runtime and the Pi C++/worker frontend as
they stood after the generic-array, matrix, and worker-shim work.

## Verified safety invariants

- **Default array access is checked.** `arrayf-get`, `arrayi-get`, `arrayb-get`,
  and all library matrix access go through index bounds checks.
- **The unchecked escape hatch is isolated.** `arrayf-get-unchecked` is used
  only by `tests/arrays/arrayf_get_unchecked.lambda` and
  `bench/stencil_bench_fast.lambda`; no shipped library, Pi worker, or example
  uses it.  It remains explicitly documented as caller-asserted safety.
- **`arrayf-pmap` output is fully written.** The uninitialized output allocation
  is only used by the pmap paths, which write every element before returning.
- **`arrayf-pmap-into` checks destination length** before writing and every
  source read remains bounds-checked.  Passing aliasing source/destination is a
  semantic error, not a memory-safety hole.
- **Matrix element access is bounds-checked** through `arrayf-get`.  Matrix
  dimensions are carried with the data.  Shape mismatches can give incorrect
  mathematical results but do not read/write outside the backing array.  (The
  missing shape checks are PR L13; fix owned by the stdlib owner.)
- **Pi `snapshot_grid` bounds its allocation.** Tile counts are validated and
  `nx*ny*rows*cols` is checked against a fixed cell cap before allocation; tile
  payload lengths are checked before copying.
- **`tile_shim` validates init arity.** An init request with fewer than six
  arguments replies with a worker error instead of indexing the argument array.
- **Worker process spawning does not use a shell.** Windows rejects worker paths
  containing a quote and starts `CreateProcessA` with only the quoted executable;
  POSIX uses `execl`.  There are no shell metacharacter paths.
- **Viewer server is local-only.** `tools/viewer.py` binds to `127.0.0.1` and
  supports byte ranges without evaluating data as code.  *Review correction
  (PR S1):* it serves the whole parent directory of the opened file (including
  dot-files) and accepts any `Host` header.  The allowlist/Host-check fix is
  owned by the Pi/tools owner (S1); until it lands, do not open untrusted data
  files with the viewer.
- **No Python dynamic execution.** The compiler and Pi tooling do not use
  `eval`, `exec`, `os.system`, or `shell=True`.
- **Duplicate data type definitions are rejected** across the main file and
  includes rather than silently overwriting a nominal type.  Repeated copies of
  the same parser declaration on nested blocks are deduplicated by identity
  before that check.
- **Matrix extraction does not leak.**  Matrix-to-product extractors
  (`mat2-values`, `matvec2-values`) return unboxed product values; the leaky
  raw-data extractor used during prototyping was removed.
- **Matrix Kalman step has a numerical regression test**
  (`arrays/matrix_kalman_step`) against a closed-form 2x2/1x1 result.

## Residual risks and limits

- `arrayf-get-unchecked` is unsafe by design and must not be used on
  unvalidated indices.
- **Type checking is strict and non-optional.** A type inference failure is a
  compile error by default.  The only opt-out is `--allow-dynamic`, which
  recompiles the program with the boxed backend after printing a warning; that
  backend is best-effort (a quarter of the suite cannot compile in it, see
  PR 3C) and can be hundreds of times slower.  The old `--strict` flag has been
  removed because strict checking is now the default.  Dynamic mode is
  memory-safe but can still hide type errors.
- **Array aliasing is not fully enforced** (PR 2.4/C9).  `arrayf-set` mutates in
  place and returns a handle to the same buffer; the checker rejects a direct
  use-after-consume, but two handles derived from one array alias each other and
  are not diagnosed.  This is memory-safe (the compiler retains correctly) but
  can produce surprising values; use `arrayf-clone` for independence.
- **`data` values live in process-lifetime arenas** (PR F2/F3).  Rows are not
  reclaimed during execution and the allocation statistics (`live=`) do not
  count arena rows, so a long loop that builds and drops `data` values grows
  memory until the arena limit.  `data` values also cannot be constructed inside
  `pmap` (the arenas are not thread-safe).
- `Array<T>` for float products is a **representation convention**: the
  underlying type remains `ArrayF`, so `Array<Complex>` and `Array<Vec3>` are
  not nominally distinct at runtime.  Use the width-aware `lib/field.lambda`
  helpers to keep component indexing explicit.
- Matrix shape mismatches are not all diagnosed; bounds checks prevent memory
  corruption, but callers must respect dimensions.
- Lambda/Pi execute trusted local code; this is not a sandbox for untrusted
  native programs.  A hostile program can consume CPU/memory or invoke the
  configured C compiler.
- Pi worker executables are trusted artifacts; protocol/schema checks prevent
  accidental mismatch, not a malicious worker binary.
- `arrayf-pmap-into` does not detect source/destination aliasing.

## Verification performed

```text
python run_tests.py
-> full suite passes; `python run_tests.py --list` prints the current case count

python -m pytest Pi/tests        # 17/17 at review time
python Pi/tools/check_cpp_examples.py
-> all C++ example checks passed at review time
```

Static audit also confirmed that `arrayf-get-unchecked` is absent from
`lib/`, `Pi/pilang/lib/`, `Pi/examples/workers/`, and example programs.

## Reporting a vulnerability

See [`../SECURITY.md`](../SECURITY.md) for the private reporting channel and
scope.
