# Lambda standard library

These modules are plain Lambda source, compiled textually with `include`.
There is no runtime package dependency and no installation step: the compiler
always adds its checkout's `lib/` directory to the include search path.

```lambda
include "lib/bigint.lambda"   ; canonical checkout-relative spelling
include "numerics"            ; bare name, .lambda optional
```

## Include search order

1. Directory of the including file.
2. Each `-L/--lib DIR` passed to `lambda.py` (repeatable).
3. `LAMBDA_PATH`, then `LAMBDA_LIB`.
4. The Lambda compiler checkout root.
5. The bundled `lib/` directory.

A file next to the including program shadows a bundled module of the same name.
`include` is textual, cycle-checked, and include-once.

## Rules for library modules

- A library file must contain **definitions only**. Runnable demos belong in
  `examples/` or `tests/`.
- Avoid top-level expressions; they make the file unusable as an include.
- Include dependencies explicitly at the top of the module.
- Keep modules bootstrap-friendly: pure Lambda, no compiler flags, no external
  packages, no generated data files.
- Names beginning with a capital letter are constructors/types; lower-case
  bindings are ordinary top-level definitions and are spliced into the importer.
- Helper definitions are prefixed with their module (`mat-`, `prime-`,
  `cabs-`, ...).  Modules must not define generic globals such as `max`/`min`:
  one include would then collide with an importer's own definition of the same
  name in the same block.

## Invalid arguments

The language has no dedicated panic/exception primitive, so the checked
library functions report invalid arguments by evaluating the generic
polymorphic failure `(head nil)`.  The program stops with
`lambda: runtime error: head of empty list` and a non-zero exit status instead
of continuing with a silently wrong result.  Guards are used for:

- `matrix.lambda` — shape mismatch in `mat-add`/`mat-sub`, inner-dimension
  mismatch in `mat-mul`, out-of-range `mat-get`/`mat-set`, negative
  `mat-zero`/`mat-id`, non-scalar `mat-scalar`, wrong element counts in the
  `*-values` extractors.
- `field.lambda` — `k < 1`, inconsistent `k*n` length, out-of-range element
  or plane index.
- `vector.lambda` — `vec-get` outside `[0, length)`, `vec-set` outside
  `[0, length]`.
- `arena.lambda` — negative capacity/cursor, and `arena-alloc` with a
  negative size (the overflow case is *not* fatal: it returns offset `-1` and
  the unchanged cursor).
- `bigint.lambda` — negative `bi-from-int`, a value needing more than `cap`
  limbs, and subtraction underflow.
- `numerics.lambda` — negative `iexp` exponent.
- `stream.lambda` — a stream id or payload length outside the u32 range.

## Modules

| module | purpose |
| --- | --- |
| `arena.lambda` | linear bump allocator over one `ArrayF`; `arena-alloc` returns `[offset, used']`, offset `-1` (cursor unchanged) on overflow; `arena-reserve` grows a full arena to `max(2*cap, used+1)` and keeps the live prefix |
| `bigint.lambda` | arbitrary-precision unsigned integers over `arrayi`; values are normalised (no high zero limbs, base-1e9 limbs); `bi-sub` rejects underflow |
| `complex.lambda` | rectangular/polar complex values, total safe division; `Complex::exp` is `e^re * (cos im, sin im)`; `Complex::modulus` is 0.0 for zero and infinite for infinite input; absolute-value extrema are `cabs-max`/`cabs-min` |
| `field.lambda` | checked planar component layout for `Array<T>` of float products (`k` planes of `n` elements) |
| `kalman.lambda` | constant-velocity 2D Kalman filter over `matrix.lambda`; predict uses the discretised white-noise-acceleration `Q = q*[[dt^4/4, dt^3/2],[dt^3/2, dt^2]]`, update uses the Joseph form `(I-KH)P(I-KH)^T + KRK^T` |
| `list.lambda` | list utilities; `append`/`filter` are tail-recursive accumulator loops (no native stack growth) |
| `matrix.lambda` | dense row-major matrices over `ArrayF` with shape/index checks |
| `numerics.lambda` | integer powers (non-negative exponents) and numerical differentiation |
| `prime.lambda` | primality and sieving helpers; `is-prime` uses trial division below 1e12 and deterministic Miller-Rabin (bases 2..37) above, exact for all Int64 and fast for the top of the range |
| `statistics.lambda` | population variance, `minf`/`maxf`; NaN is ignored and an empty (or all-NaN) input gives `0.0` |
| `stream.lambda` | `LMB1` binary stream framing for `--data`; 16-byte little-endian header + native-endian payload (portable between same-endian hosts); u32 length/id fields are range-checked |
| `string.lambda` | byte/code-point scanner helpers |
| `vec2.lambda` | structure-of-arrays 2D vector fields and first-class `Vec2` |
| `vec3.lambda` | first-class `Vec3` with `v3-add`/`v3-dot`/`v3-scale`/`v3-cross`/`v3-norm` |
| `vector.lambda` | growable flat Float vector: `[length, capacity, v0, v1, ...]` with checked `vec-get`/`vec-set` |

## Bootstrap use

```lambda
include "bigint"
include "numerics"

; use bi-* and derivative/iexp here
```

To keep a separate algorithm tree:

```powershell
python lambda.py app.lambda -o app -L .\stdlib
```

with `stdlib/` laid out as `stdlib/<module>.lambda`.
