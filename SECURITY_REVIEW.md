# Pi / Lambda security and code review

Date: 2026-11-14

## Scope

- Pi frontend (`pilang/`) and C++ runtime (`pi/csrc/pi_runtime.cpp`).
- Lambda parser/compiler/typechecker/runtime (`lambda.py`, `runtime.py`, etc.).
- No `.venv`, generated objects, or external packages.

## Pi changes

### Removed Python execution backend

The Python interpreter/runtime/report path was deleted:

- `pilang/interp.py`
- `pilang/runtime.py`
- `pilang/stdlib.py`
- `pilang/report.py`
- Python-backend tests and mock-worker harness

`pi run` is now C++-only. The Python process is a parser/checker/plan/worker
build frontend only. `pi test` and all examples execute through the C++ runtime.

### Wire and plan hardening

- Reject non-multiple-of-8 and over-`MAX_FRAME` reply lengths before allocating.
- Validate HELLO tag, stream id, body size, version, and the 128-bit schema hash.
- Bound JSON nesting and reject trailing plan data.
- Check integer conversions; reject non-finite/out-of-range values and zero split
  counts.
- Validate coupling dimensions, positions, ring sizes, and empty instance
  binding ranges.
- Sanitize artifact names so `../`/absolute names cannot escape `--out`.
- Add a top-level runtime error handler that still calls `finish()`.

### Lifecycle/concurrency

- Added per-worker I/O mutex, worker registry, shutdown/reap paths, and
  process/pipe cleanup at exit.
- Serialized worker spawning to avoid inheriting another worker's pipes and to
  make `LAMBDA_GPU` set/restore atomic.
- Fixed timeout thread lifetime by moving wait state to the heap.
- Bounded `push` waits (30 s) and react to stopped/failed controllers.
- `close` terminates instance workers, closes tick/ring channels, and joins
  controllers.
- `select` null-checks disconnected ports and uses per-arm deadlines rather than
  sleeping then firing unconditionally.
- Removed duplicate evals and guarded binding patterns.

## Lambda changes

### Legacy syntax removed

- `letrec` / `letloop`
- `Y` (use `fix`)
- `freeze` (use `immortalize`)
- `^` (use `**` or `pow`)
- `&&` (use `&` / `bit-and`)
- `#` comments (use `;`)
- Pi `rpc` keyword (use `worker`)

The removed examples and editor/Pygments/tmLanguage entries were deleted or
regenerated. `runtime_stripe.c/.o` duplicates were removed.

### Memory-safety fixes

- Checked array byte counts before `pool_take` in `arrayf/i/b_new`.
- Checked `W*R`, `2*N`, and `2*W` in the Gray-Scott/Gray-stripe kernels.
- Fixed first-class partial `and`/`or` use-after-free and environment leak;
  release the unused operand.
- Tag-check `str->list` and `str-len`; made `arrayi_sum` use an unsigned
  accumulator; guard `abs(INT64_MIN)`.
- Made `immortalize` tail traversal iterative.
- Typed `div`/`mod` now call the checked C helpers; typed `float->int` clamps
  through `lambda_ftoi`.
- Dynamic `write-file` now gets the correct `i64` ABI.
- Release temporary write-* arrays.

### Compiler/front-end fixes

- Top-level initialisation now accounts for top-level values read through
  user-function calls (without making enum/constants recursive).
- Replaced the predictable `/tmp` runtime-object cache with a private per-build
  object and remove the build workdir in `finally`.
- Fixed `read-file` purity spelling; treat `fix` and `immortalize` as impure for
  CSE/pmap purposes.
- Reject non-finite float literals and out-of-range integer literals.
- GPU `arrayf-get` indexes are bounds-guarded in generated kernels.

## Verification

```text
Pi static tests:   17/17
Pi C++ examples:   field, drone, heat, heat2d_cells, reduce, fault,
                   gray_scott, gray_scott_multi
Lambda tests:      132/132
Editor tests:      grammar, language data, Pygments
```

## Known residual risks

- Lambda parser/resolver recursion can still raise `RecursionError` on
  pathological nesting; it is reported as a compile failure, not a sandbox.
- Lambda atomic-RC tail release has a latent race in an uncommon shared-tail
  scenario; not reproduced in the suite.
- GPU/NVRTC objects are process-lifetime and failed kernels are not freed.
- Pi timeout cancellation cannot force a worker call to stop; the worker I/O
  mutex serializes the abandoned call, but a deadline may be observed late.
- Restart replay can still block on ring channels if a failed iteration already
  filled them; the shipped examples do not hit this ordering.

No shell-injection path was found: compiler/Pi subprocesses use argv lists, not a
shell.
