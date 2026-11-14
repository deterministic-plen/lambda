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

## Modules

| module | purpose |
| --- | --- |
| `bigint.lambda` | arbitrary-precision unsigned integers over `arrayi` |
| `complex.lambda` | rectangular/polar complex values, total safe division |
| `list.lambda` | list utilities |
| `numerics.lambda` | integer powers and numerical differentiation |
| `prime.lambda` | primality and sieving helpers |
| `statistics.lambda` | mean, variance, min/max |
| `stream.lambda` | `LMB1` binary stream framing for `--data` |

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
