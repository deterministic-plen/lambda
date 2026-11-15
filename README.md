# Lambda

**Lambda is an impure, eagerly evaluated, statically typed functional language
in the ML/Lisp tradition: a strict lambda calculus with Hindley–Milner
inference, algebraic data types, flat arrays, reference-counted containers, and
a native-code compiler.**  It is designed as a *compute* language: small,
predictable, and fast enough to sit underneath a coordinator such as
[Pi](pi/).

```
source .lambda (+ includes)
  │ lexer / layout-sensitive parser
  │ resolver
  │ purity + common-subexpression sharing
  │ Hindley–Milner inference ── failure ──▶ boxed dynamic fallback
  │ capture analysis / λp purity
  ▼
typed AST
  │ LLVM IR + C runtime
  ▼
native executable
```

## Design goals

1. **Stay a lambda calculus.**  Definitions are `name → value`; application is
   prefix; currying is real.  There are no statements, loops, or mutable
   variables.  Syntax is added only when it removes a bug class or unlocks a
   common pattern.
2. **Native performance without leaving the language.**  Scalars and small
   records are unboxed; known calls are direct calls; self-recursion uses
   `musttail`; immutable array metadata is cached; RC fast paths are inlined.
   The bigint library reaches within ~2.5× of Python's C-backed integers
   without a single bigint primitive.
3. **Predictable memory.**  Fixed-size values live in registers/stack; heap
   values are reference-counted; arrays have an explicit consume-on-write
   discipline; `&` gives a checked read-only borrow.  There is no tracing GC.
4. **Opt-in parallelism.**  `pmap`, `arrayi-pmap`, and `arrayf-pmap` run pure
   functions over disjoint outputs with OS threads.  `arrayf-pmap` also has an
   optional CUDA backend.  There are no user threads, channels, or races.
5. **Mathematics first.**  `Int`, `Float`, `Bool`, arrays, product/sum types,
   a complex library, a bigint library, and a streaming plotting path are part
   of the distribution rather than afterthoughts.

## Quick start

```sh
python lambda.py hello.lambda -o hello
./hello
```

```lambda
-- hello.lambda
println "hello"
```

A definition takes exactly one atom, so parenthesize compound values:

```lambda
add → (λa b. (+ a b))
square → (λx. (* x x))
answer → (square (add 2 3))
(println answer)
```

Useful flags:

| flag | meaning |
| --- | --- |
| `--data` | binary `write-*` output; suppresses the final print |
| `--input` | enable `read-line`, `read-int`, `read-float` |
| `--exit-code` | require an `Int` result and use it as the process exit status (no final print) |
| `--layout` | parse `data` declarations and print their type-algebra layouts; do not compile |
| `-L, --lib DIR` | add a library/include directory (repeatable) |
| `--strict` | make inference failure an error |
| `--dynamic` | force the boxed backend |
| `--keep-ir` / `--dump-ir` | keep or print LLVM IR |
| `--debug` | print the inferred program type |

Environment: `LAMBDA_PATH` for include search, `LAMBDA_THREADS` for worker
count, `LAMBDA_GPU=1` to enable the CUDA path for `arrayf-pmap`,
`LAMBDA_STATS=1` for allocation statistics.

## Language

### Functions and application

```lambda
id → (λx. x)
const → (λx y. x)
compose → (λf g x. (f (g x)))
```

Application is left-associative and prefix: `(f a b)` means `((f a) b)`.
There are no infix operators, so `(+ a b)` and `(== x 0)` are calls.  A
multi-parameter `λ` is compiled as one native function, but remains curried
when partially applied.

### Types

Inference covers `Int`, `Bool`, `Float`, `String`, `[T]`, products, sums,
arrays, and function types.  Numeric operators are overloaded over `Int` and
`Float` but require homogeneous operands; unconstrained numeric variables
default to `Int`.  Annotate when you need a specific specialization:

```lambda
squaref → (λx: Float. (* x x))
squarei → (λx. (* x x))       ; Int -> Int
```

If inference fails, the driver warns and compiles the whole program with the
boxed dynamic backend.  `--strict` turns that into an error.

### Products, sums, and enums

```lambda
type Complex (re: Float, im: Float)
type Vec3 (x: Float, y: Float, z: Float)

conj → (λz: Complex. (Complex (Complex-re z) (- 0.0 (Complex-im z))))
dot  → (λa: Vec3 b: Vec3.
        (+ (* (Vec3-x a) (Vec3-x b))
           (+ (* (Vec3-y a) (Vec3-y b)) (* (Vec3-z a) (Vec3-z b)))))
```

```lambda
type Shape (Circle (r: Float) | Rect (w: Float, h: Float) | Point)
area → (λs: Shape.
  match s with
  Circle r    ⇒ (* 3.141592653589793 (* r r)),
  Rect w h    ⇒ (* w h),
  Point       ⇒ 0.0)
```

Product values are unboxed LLVM structs passed by value: constructing one is
two `insertvalue`s, not an allocation.  Sums are inline tagged words.  Current
restrictions: product fields are scalar/enum only (not arrays), types are
monomorphic, and product values cannot be stored in lists or closures.  Field
sugar `z_re` splits at the last underscore and is resolved before inference, so
annotate parameters when using it.

### Typed field helpers

`lib/field.lambda` makes the planar convention explicit:

```lambda
include "lib/complex.lambda"
include "lib/field.lambda"

n → 3
z0 → (field-new 2 n)
z1 → (field-set z0 2 n 0 0 1.0)   ; real part of element 0
z2 → (field-set z1 2 n 0 1 2.0)   ; imaginary part of element 0
real0 → (field-get z2 2 n 0 0)
```

`lib/complex.lambda` and `lib/vec3.lambda` provide the scalar product helpers.
`Array<Complex>` and `Array<Vec3>` are the planar `ArrayF` conventions described
above.

### Qualified names

```lambda
Complex::add → (λa: Complex b: Complex.
  (Complex (+ a_re b_re) (+ a_im b_im)))
(Complex::add z w)
```

`::` is part of a global name, not a module system.  It is a naming convention
for libraries (`Complex::add`, `mat-mul`); single `:` remains the
type/field/match separator.

### Logic and bit operations

```lambda
valid → (and (> x 0) (< x 100))
either → (or flag (== x 0))
negated → (not flag)
```

`and`, `or`, `not` are `Bool` primitives.  `and`/`or` evaluate both operands; if
the right side can fault, write the `if` form.  `&`/`bit-and` are Int
bitwise operations, as are `|`, `bit-xor`, `!`/`bit-not`, `shl`, `shr`,
`popcount`.

### Lists

Lists are persistent, homogeneous linked lists:

```lambda
xs → [1, 2, 3]
ys → (cons 0 xs)
zs → (map (λx. (* x x)) xs)
total → (sum xs)
```

`map`, `pmap`, `range`, `head`, `tail`, `empty?`, `nil`, and `sum`/`product`
are available.  A list is boxed and reference-counted.

### Flat arrays

For numeric state, use one of three mutable flat buffers:

| constructor | element |
| --- | --- |
| `(arrayf n init)` | `Float` (double) |
| `(arrayi n init)` | `Int` (int64) |
| `(arrayb n init)` | byte (0–255) |

To instantiate an array from values, use the list constructors:

```lambda
xs → (arrayf-of [1.0, 2.5, -3.0])
is → (arrayi-of [1, 2, 3])
bs → (arrayb-of [0, 255, 7])
```

They consume the list and copy its elements into one flat buffer; an empty list
gives a zero-length array.  The constructor forms are `arrayf-of`, `arrayi-of`,
and `arrayb-of`.

Annotations may also use the generic scalar form: `Array<Float>` = `ArrayF`,
`Array<Int>` = `ArrayI`, `Array<Bool>` = `ArrayB`.  `Array<T>` is also accepted
for **any product/data type whose fields are all Float**: Vec2, Vec3, Complex,
and user-defined float products.  Such an array is the planar SoA `ArrayF`
convention with one component plane per field:
`[component0 of all elements, component1 of all elements, ...]`.
It is still a convention, not a compiler-known element stride; use
`lib/field.lambda` to carry width and element index explicitly.

Operations are `arrayf-get/set/length/clone`, and the same for `arrayi`/`arrayb`.
`arrayf-get-unchecked` is an explicit low-level escape hatch: the caller asserts
the index is in bounds.  The checked default remains `arrayf-get`.
`arrayf-sum`/`arrayi-sum` reduce a whole buffer.  Arrays follow
**consume-on-write**: `arrayf-set` consumes its array and returns a new handle
to the same buffer.  Use `arrayf-clone` when you need two independent values.
Passing an array to a function consumes it unless the parameter is `&`.

```lambda
fill → (λi a.
  if (>= i 100) then a
  else (fill (+ i 1) (arrayf-set a i (sin (int->float i)))))
xs → (fill 0 (arrayf 100 0.0))
```

### Matrices

`lib/matrix.lambda` provides a row-major dense matrix type over `ArrayF`:

```lambda
include "lib/matrix.lambda"

a → (M 2 2 (arrayf-of [1.0, 2.0, 3.0, 4.0]))
b → (M 2 2 (arrayf-of [5.0, 6.0, 7.0, 8.0]))
c → (mat-mul a b)
p → (mat-mul c (M 2 1 (arrayf-of [1.0, 1.0])))
[(mat-get c 0 0), (mat-get c 1 1), (mat-get p 0 0)]
```

Available operations include `mat-zero`, `mat-get`, `mat-set`, `mat-add`,
`mat-sub`, `mat-scale`, `mat-transpose`, `mat-mul`, `mat-id`, `mat-scalar`,
and `mat-clone`; `mat2-values`/`matvec2-values` extract unboxed product values
without leaking the matrix shell.  Dimensions travel with the data; every
element access is bounds-checked.

`lib/kalman.lambda` builds the 2D constant-velocity Kalman filter on top of
`Matrix`, exposing `Kalman2`, `k2-predict`, and `k2-update`.  This is what the
matrix drone worker uses.  The worker is
`Pi/examples/workers/drone_matrix.lambda`.

### Borrows and inspection

A `&` parameter is a read-only borrow: no retain/release at the call boundary,
no release in the callee, and LLVM `nocapture readonly` metadata.

```lambda
sum-range → (λ&a.
  go → (λ i acc &b.
    if (>= i (arrayi-length b)) then acc
    else (go (+ i 1) (+ acc (arrayi-get b i)) b))
  (go 0 0 a))
```

`&x` is an **inspection expression**: a non-owning, idempotent view of a
variable.  It can be read, passed to a `&` parameter, or used in `array*-get`;
it cannot be consumed, returned, captured, or stored.  The variable remains
usable:

```lambda
first → (λ&a. (arrayi-get a 0))
a → (arrayi-set (arrayi 3 0) 1 9)
[(first &a), (first &a), (arrayi-get &a 1)]
```

`&` is the borrow marker; `bit-and` is bitwise-and.  `&x` requires adjacency (`& x`
is the binary operator).  Borrow inspection is typed-mode only; the dynamic
backend clears borrow markers.

### Recursion and blocks

Every block is a set of mutually recursive definitions followed by one
expression.  This gives recursion without a special `letrec`:

```lambda
fact → (λn. if (== n 0) then 1 else (* n (fact (- n 1))))
```

Self-recursive calls in tail position use `musttail`; non-tail recursion uses
the native stack.  `fix` is available for first-class recursive values.

### Includes

```lambda
include "lib/bigint.lambda"       ; canonical, checkout-relative form
include "stream"                  ; bare name: resolves through the compiler lib/
include "numerics"                ; .lambda is optional
```

Includes are textual, cycle-checked, include-once, and resolve in this order:

1. relative to the including file;
2. each `-L/--lib DIR` supplied to `lambda.py`;
3. `LAMBDA_PATH`, then `LAMBDA_LIB`;
4. the compiler checkout root;
5. the compiler's bundled `lib/`.

The bundled `lib/` is therefore available to every program without configuration.
Use `-L DIR` (repeatable) or `LAMBDA_LIB` for an external package tree.  A local
file always wins over the bundled library of the same name.  Included files must
contain definitions only; runnable demos belong in `tests/` or `examples/`.

The implementation audit (implemented vs deferred, known tradeoffs) is in
[`docs/type-algebra-status.md`](docs/type-algebra-status.md).  The memory,
type, process and escape-hatch safety review is in
[`docs/safety.md`](docs/safety.md).

The self-hosting boundary and port plan (`.lambda` -> `.ll`, FSM lexer,
deterministic parser, external clang) is in
[`docs/self-hosting-plan.md`](docs/self-hosting-plan.md).

### Recursive AST prototype

[`examples/ast_interpreter.lambda`](examples/ast_interpreter.lambda) defines a
recursive `Expr` ADT (`Lit`, `Add`, `Mul`, `Neg`, `If`) and evaluates it. All
expression fields are scalar or recursive, so the AST uses the flat arena
layout.

[`examples/ast_compiler.lambda`](examples/ast_compiler.lambda) compiles the same
kind of AST to a postfix integer instruction list and executes it with a small
stack machine. It supports literals, arithmetic, negation, conditional
selection, and lexical `Let`/`Var` via compile-time substitution, and uses the
recursive ADT plus the generic list library. It is the
first compiler-shaped prototype written in Lambda itself.

## Runtime and compiler

* `Int`/`Bool` are unboxed `i64`; `Float` is an LLVM `double`.
* Small records and enums are unboxed values; lists, strings, arrays, and
  escaping closures are `%Value*` with reference counts.
* Known functions with the right arity get direct native calls and an unboxed
  ABI.  Partial/higher-order uses go through boxed closures.
* The compiler emits inline RC fast paths, moves ownership through tail calls,
  borrows plain variables passed to `&` parameters, caches immutable array
  length/data fields, folds literal top-level scalars, and skips the array pool
  mutex in single-threaded builds.
* Purity analysis drives `pmap` and CSE.  Top-level values are initialized in
  dependency order.

## Parallelism and GPU

`pmap` maps a pure function over a list.  `arrayi-pmap`/`arrayf-pmap` map a
pure `Int -> Int`/`Int -> Float` over an index range into a flat output array.
Outputs are disjoint; workers read only immutable input.  `LAMBDA_THREADS`
controls the runtime pool.  A pure one-parameter `arrayf-pmap` or `arrayi-pmap` lambda is
currently inlined into a raw serial `i64` loop (faster per element, but no
intra-array threading); Pi obtains parallelism across workers/tiles.  A
parallel raw-loop lowering is the remaining performance project.  An unknown function value in `arrayf-pmap` falls back to a
serial loop; higher-order helpers can declare pure parameters, e.g.
`fx: pure`, and a call-site check keeps the parallel path sound.  The Vec2 SoA
helper in `lib/vec2.lambda` uses this.

`arrayf-pmap` is also the GPU seam.  When the lambda is a simple expression
that the compiler can translate to CUDA C (arithmetic, comparisons, `sin`,
`cos`, `tan`, `exp`, `log`, `sqrt`, `pow`, `floor`, `ceil`, `round`, `fabs`,
and inlined pure helpers), the compiler embeds a kernel source and the runtime
uses **NVRTC + the CUDA Driver API** to JIT and launch it.  A lambda may also
read **one captured `ArrayF`** through `arrayf-get`; the compiler passes it as a
`const double*` kernel argument, which is what makes 5-point stencils possible.
`LAMBDA_GPU=1` enables this path; unsupported lambdas and all failures fall
back to the CPU thread pool.  Only one GPU is used; multi-GPU is not implemented.  This is a
one-GPU backend, not a GPU language.

All numerical kernels are ordinary Lambda: `arrayf-pmap` is the generic
data-parallel primitive, and there are no domain-specific stencil primitives.

Requirements for the GPU path: an NVIDIA GPU, the CUDA driver (`nvcuda.dll`
or `libcuda.so.1`), and NVRTC (`nvrtc64_*.dll` or `libnvrtc.so`). No CUDA link
step is needed; the runtime loads both dynamically. If anything is missing or
the kernel source does not compile, it falls back to the CPU pool.

## Files and streams

`read-file : String -> String` returns file bytes as a string (empty on error).
`write-file : String × ArrayB -> Int` writes bytes and returns the count, or
`-1` on failure; it is the bootstrap-oriented file writer.

`--data` enables `write-bytes`, `write-f64s`, `write-i64s` on stdout.  The
`lib/stream.lambda` framing (`LMB1`) is used by
[`tools/plot_svg.py`](tools/plot_svg.py) and
[`tools/stream_plot.py`](tools/stream_plot.py) to plot or export arrays; no
third-party package is needed for SVG.

## Libraries

| file | purpose |
| --- | --- |
| `lib/bigint.lambda` | arbitrary-precision unsigned integers over `arrayi`; Karatsuba/Toom-3 |
| `lib/complex.lambda` | complex arithmetic |
| `lib/numerics.lambda` | integer powers and numerical differentiation |
| `lib/statistics.lambda` | mean, variance, min/max |
| `lib/list.lambda` | list utilities |
| `lib/prime.lambda` | primality and sieving helpers |
| `lib/stream.lambda` | binary stream framing |
| `examples/gpu_pmap.lambda` | GPU-shaped `arrayf-pmap` smoke test |

The bigint library is pure Lambda: base-10⁹ limbs in one `arrayi`, direct limb
loops, Karatsuba/schoolbook leaves, Toom-3 above the cutoff, and fast doubling.
On the reference machine `F(1,000,000)` is about **90 ms compute**, **97 ms
including 208,988 decimal digits**, ~2.5× Python's C-backed integers and ~32×
gmpy2/GMP.  No bigint primitives are involved.

## Performance snapshot

Reference: AMD Ryzen 7 9700X, clang `-O3`, Windows x64.

| workload | Lambda | comparison |
| --- | ---: | --- |
| sequential nth-prime | ~1.02 s | C ~1.00 s |
| 16-thread trial division | ~0.17 s | C ~0.17 s |
| segmented sieve | ~0.14 s | C ~0.17 s |
| 10M i64 fill+sum | ~16 ms | C ~14 ms |
| F(1,000,000) compute | ~90 ms | Python ~36 ms, gmpy2 ~2.8 ms |

The compiler is not magic: the remaining bigint gap is limb throughput and
algorithm constants, not allocation or bounds checks.  See
[`bench/results.md`](bench/results.md) for raw numbers and methodology.

## Editor support

[`editors/`](editors/) contains a dependency-free VS Code extension and a
Pygments lexer for Lambda and Pi.  Install by copying `editors/vscode` to
`%USERPROFILE%\.vscode\extensions\lambda-language-0.1.0` and restarting VS
Code.  Completion covers primitives, keywords, types, local definitions,
fields, variants, and qualified names.  Validation:
`python editors/test_highlight.py` and `python editors/test_pi_highlight.py`.

## Tests

```sh
python run_tests.py
```

144 compile/run/leak regression cases; every program must finish with
`live=0` allocation statistics.  The suite covers scalars, floats, strings,
lists, arrays, products, sums, enums, closures, CSE, tail calls, includes,
parallel maps, bigint, and compile-time errors.

## Limitations and roadmap

* Product fields cannot hold arrays or other boxed values yet.  A `Matrix`
  record needs boxed-record ownership or linear/arena fields; the current
  workaround is a flat array with a dimensions header.
* Product values are not first-class (no list of records, no record closure).
* No user threads, channels, actors, or general async; parallelism is data
  parallel only.
* No type classes/generics over constructors; monomorphic products/sums.
* Bigint is a library, not a compiler type.
* GPU support is a single-device `arrayf-pmap` backend for simple expressions;
  no multi-GPU and no domain-specific numerical primitives.
* The dynamic backend is a fallback, not an optimization target.

## Repository layout

```
lambda.py            driver: parse, infer, compile, link
lexer.py parser.py   layout-sensitive front end
lambda_ast.py        AST and bindings
resolve.py           name resolution
analysis.py          purity, captures, CSE
typecheck.py         HM inference and runtime reps
compiler.py          LLVM IR codegen
runtime.py           C runtime, RC, arrays, threads, CUDA bridge
lib/                 standard libraries
examples/            runnable programs
bench/               cross-language benchmarks
tools/               SVG plotting and stream readers
editors/             VS Code + Pygments support
docs/                roadmap, parallelism, checklist
```

## License

See the repository root.  The language and runtime are intentionally small
enough to read end to end.
