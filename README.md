# Lambda

A small, curried lambda-calculus language that compiles to native code through
LLVM.  It aims to stay functional and minimal while producing fast machine
code: type-directed unboxing, flat closures, reference counting, common
sub-expression sharing, f64 mathematics, textual dependencies, optional
shared-memory parallelism, and opt-in terminal input.

```
  source .lambda + includes
      │  loader / lexer / parser
      ▼
  AST (blocks, arrow definitions, λp, comprehensions)
      │  resolver
      │  purity + common-subexpression sharing
      │  resolver (again)
      │  Hindley–Milner inference ──failure──► dynamic (boxed) fallback
      │  capture analysis / λp purity
      ▼
  typed AST
      │  code generator
      ▼
  LLVM IR ──clang──► native executable
      └── linked with a reference-counted C runtime
```

## Quick start

```
python lambda.py examples/nth_prime.lambda -o nth_prime
./nth_prime

python lambda.py examples/nth_prime_parallel.lambda -o np
LAMBDA_THREADS=4 ./np

python lambda.py examples/nth_prime_sieve.lambda -o sieve
./sieve

python lambda.py tests/input_int.lambda -o ask --input
echo 41 | ./ask

python run_tests.py          # 129 regression tests
```

Requirements: Python 3.10+, LLVM `clang` on `PATH`.

Useful flags:

| flag | effect |
| --- | --- |
| `--strict` | reject programs that cannot be type-inferred |
| `--dynamic` | force the fully boxed backend |
| `--input` | enable `read-line`/`read-int`/`read-float` (off by default) |
| `--data` | enable binary `write-*` primitives; binary stdout; skip final print |
| `--keep-ir` | keep the generated `.ll` next to the executable |
| `--dump-ir` | print LLVM IR to stdout |
| `--no-musttail` | disable `musttail`/`tail` emission (debugging) |
| `--no-warn-infix` | suppress the likely-infix warning for `z == 1`-style applications |
| `--debug` | print the inferred program type |

Environment:

| variable | effect |
| --- | --- |
| `LAMBDA_THREADS` | worker count for `pmap` (default: hardware concurrency) |
| `LAMBDA_PATH` | extra include search directories (`;` on Windows, `:` elsewhere) |
| `LAMBDA_STATS` | print allocation statistics on exit; used by the test suite |

## Syntax

A program is a **block**: leading `include` directives, zero or more
definitions, then one expression.  A block may appear at the top level, after
`(`, in a lambda body, or in an `if` branch.

```lambda
; definitions are recursive and visible to each other
fib → (λn.
  go → (λa b i. if (== i n) then a else (go b (+ a b) (+ i 1)))
  (go 0 1 0))

(fib 25)
```

Every function binding uses the same arrow notation, recursive or not.  There
is no `letloop`/`letrec` distinction: inference chooses unboxed or boxed code.
(`letrec`/`letloop` are accepted as deprecated aliases of `let`.)

Other forms:

```lambda
(λx y. (+ x y))                 ; multiple parameters
(λp x. (+ x 1))                 ; parallel-safe marker (must be pure)
(λx: Float. (* x x))            ; parameter type annotation
(let x = (fib 5) in (+ x 1))     ; local definition sugar
[1, 2, 3]                        ; list literal
[ (* x x) | x ← [1..10] ]        ; sequential comprehension
[ (* x x) ∥ x ← [1..10] ]        ; parallel comprehension (pmap)
[0..n]                           ; half-open integer range 0 .. n-1
"a\nb"                           ; strings
(if cond then a else b)
enum Turnstile (Locked, Unlocked) ; nullary enum declaration
```

A definition value is one atom, so compound values go in parentheses:
`f → (+ 1 2)` is valid, `f → + 1 2` is not.

`λp` (no space after the lambda) is the parallel-lambda marker.  To write an
ordinary lambda whose first parameter is literally named `p`, put a space
after the lambda: `λ p. ...` or `λ p xs. ...` (see `lib/list.lambda`).

### Includes

```lambda
include "lib/prime.lambda"
include "lib/list.lambda"          ; .lambda is optional
```

Include directives must appear before definitions.  Included files contain
definitions only.  Definitions are spliced into the program as ordinary
mutually recursive top-level definitions.  Paths resolve relative to the file
that writes the directive, then in `LAMBDA_PATH`, the compiler root, and
`lib/`.  A file is included at most once per compilation; cycles are errors;
duplicate definition names are errors.  Compiled definitions carry their own
source file in diagnostics.

### Qualified names

A definition name may contain `::` segments.  The segments are ordinary parts of
one global name; this is namespacing sugar, not a module system yet:

```lambda
Complex::add → (λa: Complex b: Complex. ...)
(Complex::add z w)
```

Use it for numeric libraries (`Complex::add`, `Matrix::mul`, `Vector::dot`)
instead of flooding the global namespace with `cadd`/`cmul`.  A single `:` is
still the type/field/match separator; only the doubled `::` is part of a name.

### Types and annotations

`Int`, `Bool`, `Float`, `String`, `[T]`, and curried function types are
inferred.  Numeric operators are overloaded over `Int`/`Float` but require both
operands to have the **same** type; there is no implicit runtime conversion.
Unconstrained numeric variables default to `Int`.  Use a parameter annotation
when a generic-looking function should be specialized to `Float`:

```lambda
square → (λx: Float. (* x x))     ; without the annotation this defaults to Int
squarei → (λx. (* x x))           ; Int -> Int
```

`int->float` and `float->int` compile to one `sitofp`/`fptosi` instruction;
`float->int` truncates toward zero.

### Primitives

| group | names |
| --- | --- |
| numeric | `+`, `-`, `*`, `/`, `%` (Int or Float, homogeneous) |
| comparison | `<`, `<=`, `>`, `>=`; `==` structural equality |
| float math | `sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `exp`, `ln`, `log10`, `log2`, `floor`, `ceil`, `round`, `trunc`, `pow`/`**`/`^`, `abs`, `pi`, `e` |
| conversions | `int->float`, `float->int`, `str->float`, `float->str` |
| reductions | `sum`, `product` (same type as the list) |
| lists | `cons`, `head`, `tail`, `empty?`, `nil`, `map`, `pmap`, `range`, `immortalize`/`freeze` |
| flat arrays | `arrayf`, `arrayi`, `arrayb`, `*-get`, `*-get-ro`, `*-set`, `*-length`, `*-clone`, `arrayi-sum`, `arrayf-sum`, `arrayi-pmap`, `arrayi-pmap-chunk` |
| logical | `&&`, `\|\|`, `not` (short-circuit, `Bool`) |
| bit-level | `&`/`bit-and`, `\|`/`bit-or`, `bit-xor`, `!`/`bit-not`, `shl`, `shr`, `popcount` |
| strings | `str->int`, `int->str`, `str->list`, `str-len`, `str-append`, `read-file` |
| IO | `print`, `println` (return their argument), `not` |
| binary data (`--data`) | `write-bytes : ArrayB -> Int`, `write-f64s : ArrayF -> Int`, `write-i64s : ArrayI -> Int` |
| input (`--input`) | `read-line`, `read-int`, `read-float` |
| fixed point | `fix`, `Y` (call-by-value, unary functions) |

`print`/`println`, terminal input, and destructive array updates are the
effectful primitives.  Division/modulo are inlined `sdiv`/`srem` for Int and
`fdiv`/`frem` for Float; integer division by zero traps.

### Logical operators

`&&` and `||` short-circuit and return `Bool`:

```lambda
(a > 0) && (b > 0)
(is-valid x) || (== x 0)
not (empty? xs)
```

They expand in the parser to `if` expressions, so `a && b` is exactly
`(if a then b else false)` and `a || b` is exactly `(if a then true else b)`.
The generated IR and machine code are identical to the explicit `if` form;
no intermediate boolean is materialised and the right operand is not
evaluated when the left one decides the result. `&&` is *logical* and takes
`Bool`; use `&`/`bit-and` for bitwise-and on `Int`.

| operator | meaning | operands |
| --- | --- | --- |
| `&&` | logical and, short-circuit | `Bool` |
| `||` | logical or, short-circuit | `Bool` |
| `not` | logical negation (`xor i64 x, 1`) | `Bool` |
| `&` / `bit-and` | bitwise and | `Int` |
| `|` / `bit-or`, `bit-xor`, `!` / `bit-not` | bitwise | `Int` |

### Operator spellings

The ASCII operators are the canonical spelling.  The old Unicode arithmetic
spellings are still accepted as legacy aliases for backward compatibility; they
do not select different implementations and should not be used in new code.
They are reserved for future distinct operations (for example matrix-aware
kernels); if that happens they will get their own names or explicit
type-directed semantics rather than silently changing the meaning of existing
programs.

## Numbers and machine code

* `Int` and `Bool` are unboxed `i64`; `Float` is an unboxed LLVM `double`.
* `String`, lists, and functions are reference-counted `%Value*`.
* Arithmetic/comparisons emit `add`, `mul`, `fadd`, `fmul`, `fcmp`, etc.;
  no tags, dictionaries, or allocations on homogeneous numeric paths.
* `sqrt`, `floor`, `ceil`, `round`, `trunc`, `abs`, and `pow` use LLVM intrinsics
  (`llvm.sqrt.f64`, `llvm.pow.f64`, ...); trig/exp/log use the platform libm.
  IEEE-754 semantics are preserved (no `-ffast-math`), so NaN/Inf behave
  normally.
* A statically known function called with the right arity gets a direct native
  call with an unboxed ABI.  Partial/higher-order calls use small boxed closure
  wrappers and box/unbox only at the boundary.
* Polymorphic functions (`id : ∀a. a → a`) use a boxed ABI; monomorphic uses
  are box/unboxed at the call site.
* If inference fails (for example self-application in a fixed-point
  combinator), the driver warns and compiles the whole program with the boxed
  dynamic backend.  `--strict` makes that a hard error.

## Borrowed array parameters and `&x`

A `&` parameter is a read-only borrow:

```lambda
sum → (λ&a: ArrayI.
  go → (λ i acc &a2: ArrayI.
    if (>= i (arrayi-length a2))
    then acc
    else (go (+ i 1) (+ acc (arrayi-get a2 i)) a2))
  (go 0 0 a))
```

An expression `&x` is an **idempotent inspection view** of a variable.  It never
moves, mutates, retains or releases `x`, so `x` remains usable afterwards:

```lambda
first → (λ&a. (arrayi-get a 0))
a → (arrayi-set (arrayi 3 0) 1 9)
[(first &a), (first &a), (arrayi-get &a 1)]
```

`&x` is only valid on a named variable.  It may be read, passed to a `&`
parameter, or used in `arrayi-get`; it cannot be consumed, returned, captured or
stored.  `&&` is the preferred spelling for bitwise and, leaving `&` for
inspection; the legacy binary `&` primitive still works.

The checker rejects mutating the borrowed buffer, returning it, capturing it in
a closure, passing it to a consuming parameter, or using a borrowed-parameter
function as a first-class value.  Borrowed parameters are true non-owning
references: no retain at the call boundary, no release in the callee, and LLVM
`nocapture readonly` metadata.  `noalias` is still withheld because callers may
alias two borrowed arguments.  This is explicit borrow checking, not general
inferred linear ownership yet; normal arrays retain their existing RC ownership.
The dynamic (boxed) backend ignores parameter borrow markers, and `&x`
inspection requires typed mode.

## Flat arrays (consume-on-write)

Linked lists are the right structure for symbolic, variable-length data, but
they allocate one cell per element.  For numerical state there are three flat
buffer types:

| constructor | element | accessor |
| --- | --- | --- |
| `(arrayf n init)` | `double` | `arrayf-get` / `arrayf-set` |
| `(arrayi n init)` | `i64` | `arrayi-get` / `arrayi-set` |
| `(arrayb n init)` | byte `0..255` | `arrayb-get` / `arrayb-set` |

Each array is one reference-counted header plus a single contiguous allocation;
elements are unboxed and there is no per-element wrapper.

```lambda
sum-squares → (λa i n acc.
  if (>= i n)
  then acc
  else (sum-squares (arrayi-set a i (* i i))
                    (+ i 1) n
                    (+ acc (arrayi-get a i))))

a → (arrayi 1000 0)
(sum-squares a 0 1000 0)        ; 332833500
```

The one-million-element version fills and sums in roughly 8 ms warm on the
16-thread machine with only three heap `Value`s; the array parameter is moved
through the update loop rather than retained and released per element.

`*-set` is **consume-on-write**, not a general assignment statement: it mutates
the buffer and returns it, and the previous reference must not be reused.  The
functional model stays `f(last_state) = next_state`:

```lambda
next → (arrayi-set state i value)
(go next ...)
```

If persistence is genuinely required, `*-clone` is the explicit copy.  There is
no `set!`, `var`, or general mutation syntax.  Array operations are effects:
they are excluded from CSE, and `λp`/`pmap` bodies cannot use them.  Equality is
identity, and arrays print as `[arrayf N]` / `[arrayi N]` / `[arrayb N]`.
Bounds are checked; byte values are checked to be in `0..255`.

`arrayi-sum` and `arrayf-sum` are flat reduction kernels implemented as
`restrict` C loops. They are auto-vectorized by clang; a 10M-element i64 sum is
about 2.6× C in the current build. The kernels are runtime SIMD, not
compiler-generated SIMD over Lambda loops.

`(arrayi-pmap f n)` is the controlled parallel array builder: it spawns workers,
applies a provably pure `Int -> Int` function to `0..n-1`, and writes a flat
`arrayi` result.  It never builds a per-item list.  The `*-get-ro` accessors are
a read-only promise: they have no effects and may be used inside the mapped
function (for example, to read a shared base-prime array).  Ordinary
`λp`/`pmap` bodies still cannot mutate arrays.

**Current restriction:** array primitives must be fully applied.  First-class
arrays, arrays of arrays, and a typed `Array a` are not implemented yet; the
three concrete types keep the element representation unboxed.

## Enums

Nullary enums are supported as nominal unboxed tags:

```lambda
enum Turnstile (Locked, Unlocked)

next → (λs: Turnstile.
  match s with
    Locked: Unlocked,
    Unlocked: Locked)
```

Each constructor is a value of type `Turnstile`; the parser expands the
declaration into ordinary definitions.  At runtime a constructor is an `i64`
tag: `Locked = 0`, `Unlocked = 1`, in declaration order.  There is no per-state
closure or allocation.  `==` works on enum values; arithmetic and ordering
reject them.

The type is nominal: `Locked` cannot be used as an `Int`, and two different
enums are distinct types even if they have the same number of variants.
Annotations use the enum name (`λs: Turnstile. ...`).

`match` is exhaustive over the enum: every variant must appear exactly once,
and an unknown or duplicate variant is a type error.  Arms are
comma-separated `Variant: expression` pairs; the older
`| Variant → expression` form is still accepted.  The compiler lowers the tag
to an LLVM `switch`; the default edge calls a runtime failure rather than
executing an arbitrary branch.  An `if`/`==` chain is also allowed.

To print a name, write a display function:

```lambda
name → (λs: Turnstile.
  if (== s Locked) then "Locked" else "Unlocked")

shown → (print (name (next Locked)))
""
```

The last `""` is the block's final expression; the `shown` definition sequences
the print.  See `examples/turnstile.lambda` and
`examples/coffee_machine.lambda`.

This is substantially cheaper than a Church encoding for nullary states: a
Church state is a closure and each transition is an indirect call, while the
enum is a compare/branch over an integer tag.  A 20-million-transition local
microbenchmark was orders of magnitude faster with the native enum.  Tail calls
inside `match` arms are emitted as real tail calls; a million-transition test
(`tests/enum_match_deep.lambda`) exercises that path without stack growth.

Payload-carrying variants are supported in fixed-mode form; tuples, generics,
and arena-backed variable-size payloads remain future work.  The planned syntax
for borrowing, arenas, and defunctionalization is in
[docs/roadmap.md](docs/roadmap.md).

## Product types

Monomorphic product types with scalar/enum fields are unboxed LLVM structs:

```lambda
type Complex (re: Float, im: Float)
type Vec3 (x: Float, y: Float, z: Float)

add → (λa: Complex b: Complex.
  (Complex (+ a_re b_re) (+ a_im b_im)))
```

`Complex` is the constructor; `a_re`/`a_im` are field sugar for
`(Complex-re a)`/`(Complex-im a)`.  The generated accessor functions remain
available.  Field sugar splits at the **last** underscore only: an exact
variable name always wins, and `vector-2-dimensional_x` means the field `x` of
`vector-2-dimensional`, never a search through earlier prefixes.  If that prefix
is not a record with field `x`, it is an error.
Values are passed by value in registers/stack: there is no heap allocation or
runtime tag for the product itself.

Current restrictions:

- fields must be `Int`, `Float`, `Bool`, or an enum;
- product types are monomorphic;
- functions taking/returning product values are direct-call only — using one as
  a first-class closure is rejected rather than boxed;
- printing/equality of whole product values is not implemented; compare fields.

These restrictions keep the representation flat.  The first-class/closure,
generic, and arena-memory cases are later phases of the roadmap.

## Sum types

Monomorphic fixed-mode sums with up to four inline payload fields per variant:

```lambda
type Shape (
  Circle (radius: Float)
| Rectangle (width: Float, height: Int)
| Dot
)

area → (λs: Shape.
  match s with
    Circle r: (* 3.0 (* r r)),
    Rectangle w h: (* w (int->float h)),
    Dot: 0.0)
```

Representation is a tagged aggregate:

```text
{ i64 tag, i64 word0, i64 word1, ... }
```

- the word count is the maximum payload field count over the variants;
- `Float` fields are bitcast into i64 words, `Int`/`Bool`/enum fields are stored
  directly;
- nullary variants leave the words zero;
- no heap allocation per value;
- `match` is exhaustive and lowers to an LLVM `switch`;
- payload binders are checked for arity and scoped to their arm.

A 20-million-transition sum-state loop ran in ~0.009 s with zero allocations.

Current restrictions: payload fields must be scalar or enum, at most four per
variant, no nested product/sum payloads, monomorphic only, and sum values are
direct-call-only rather than first-class closures.  Larger payloads will use
the rooted segmented-arena descriptor.

## Big integers

`lib/bigint.lambda` is an arbitrary-precision unsigned integer library written
entirely in Lambda over `arrayi`.  It adds no compiler primitives.  A number is
one `arrayi`:

```
a[0]        = number of limbs
a[1..n]     = little-endian base-1e9 limbs
unused      = zero
```

The library uses direct `arrayi-get`/`arrayi-set` in its kernels, a dedicated
Karatsuba squaring path, in-place subtraction inside the Karatsuba combine, and
tuned multiplication/squaring cutoffs.  Fast doubling and streamed decimal
printing are also ordinary Lambda.

Most of the speedup came from structural compiler/runtime work rather than from
the library: reference-count fast paths are emitted inline, plain variables
passed to `&` parameters are borrowed without a retain/release round trip, tail
calls transfer ownership of parameters and captures, immutable array
length/data fields are loaded once per frame with `!invariant.load`, literal
top-level scalars become LLVM constants, and the single-threaded array-data pool
no longer takes a mutex.  The public API is unchanged: `bi-get`, `bi-set`,
`bi-add`, `bi-mul`, `bi-fib`, `bi-print` and the `arrayi` layout all still work.

`F(100)` and `F(1000)` are exact regression cases; `F(100000)` is checked
through a limb checksum, and `F(1,000,000)` (208,988 digits) is the benchmark:

```
examples/bigint_fib1m.lambda   ; ~95 ms compute + output, exactly 208,988 digits
```

Compute-only, Lambda is about **88 ms** versus Python's C-backed bigint at
~36 ms and gmpy2/GMP at ~2.8 ms.  The total including decimal output is
~95 ms versus ~512 ms for Python, because base-1e9 printing is essentially
free.  This is the "same order as Python" target; GMP is a different class of
implementation (FFT plus assembly).  Full protocol and medians:
[bench/results.md](bench/results.md).

## Sharing, memory, and ownership

A purity fixpoint marks pure functions and primitives.  A CSE pass finds pure
applications repeated in one straight-line region and evaluates them once:

```lambda
bad → (λn.
  if (== n 0) then 0
  else (+ (bad (- n 1)) (+ (bad (- n 1)) (bad (- n 1)))))

(bad 30)   ; 31 recursive calls instead of 3^30
```

The pass does not cross `if` branches, lambda boundaries, or blocks, and never
shares an impure call.  `(println "x")` called twice still prints twice.  There
is no unbounded memo table.

Boxed values are reference counted with a simple ownership discipline:
every boxed expression result is owned; functions own their boxed
parameters/captures and release them at every exit; closure environments own
captured values; cons cells own head and tail; static literals, `nil`, and
primitive closures are immortal.  Cons chains are released iteratively, so a
million-element list does not overflow the C stack.  All tests require
`live=0` with `LAMBDA_STATS=1`.

## Parallelism

`pmap` maps a **provably pure** function over a list using a native thread per
worker (the main thread also participates).  `λp` is an optional marker that
asserts purity at compile time; `pmap` accepts any function the purity analysis
can prove pure:

```lambda
count-block → (λp b.
  ... pure block computation ...)

counts → (pmap count-block (range 0 blocks))
total  → (sum counts)
```

Parallel comprehensions are sugar:

```lambda
[ (* x x) ∥ x ← [1..10] ]         ; pmap
[ (* x x) | x ← [1..10] ]         ; map
```

Independence rule: `pmap`/`λp` bodies may not print, read input, call unknown
function values, or otherwise have effects.  If the analysis cannot prove a
body pure, compilation fails with an explanatory error.  Recursive accumulator
loops are **not** auto-parallelized; express independent work as `pmap` over a
list/range.  The runtime uses an atomic-reference-count build only for programs
that use `pmap`, so ordinary boxed code keeps the cheaper non-atomic path.

`immortalize` (alias `freeze`) marks a read-only value graph as permanent so
workers can share it without any refcount atomics.  It is used for base primes
in the sieve example and intentionally leaks that graph until process exit.

Three nth-prime implementations are included:

| program | algorithm | time (16 logical CPUs) |
| --- | --- | --- |
| `examples/nth_prime.lambda` | sequential trial division, early exit | ~1.00 s |
| `examples/nth_prime_parallel.lambda` | 65,536-candidate blocks, `pmap` trial division | ~0.17 s |
| `examples/nth_prime_sieve.lambda` | 64-bit segmented sieve, one mask per block | ~0.19 s |

Single-thread times for the two parallel programs are ~1.20 s and ~0.85 s.
The sieve has less arithmetic but more communication/allocation (masks and
counts), which is why it is not dramatically faster than parallel trial
division at this size.  Both return **10086767** for the 670,000th prime.

`LAMBDA_THREADS` controls the worker count; a pmap spawns workers per call and
joins before returning.

## Terminal input

Input is compiled out by default to keep pure builds and benchmarks free of
interaction.  Pass `--input` to enable:

```lambda
n → (read-int "n: ")
(+ n 1)
```

* `read-line : String -> String` prints the prompt, flushes output, and returns
  the line without its newline; EOF returns `""`.
* `read-int` / `read-float` parse the line and fail with a runtime error on
  invalid input.
* Prompt strings may be empty (`""`) for scripted use.

## Binary data stream

`--data` turns stdout into a binary channel and removes the automatic print of
the program result.  This is for piping numeric results to a plotter or another
process without formatting them as text:

```lambda
include "lib/stream.lambda"

xs → (arrayf 1000 0.0)
ys → (arrayf 1000 0.0)
; ... fill xs and ys ...
a → (emit-f64s 1 xs)
(emit-f64s 2 ys)
```

```powershell
python lambda.py examples/stream_xy.lambda -o stream_xy --data
cmd /c "stream_xy.exe > data.bin"       # PowerShell's > is not byte-safe
python tools/stream_plot.py data.bin --x 1 --plot
```

The low-level primitives are raw writers:

| primitive | writes |
| --- | --- |
| `write-bytes : ArrayB -> Int` | the array bytes |
| `write-f64s : ArrayF -> Int` | `8*n` raw little-endian doubles |
| `write-i64s : ArrayI -> Int` | `8*n` raw little-endian int64s |

They return the number of bytes written.  `lib/stream.lambda` adds the framing:

```
0..3   "LMB1"
4      tag: 1 = f64 array, 2 = i64 array, 3 = bytes
5      flags (0)
6..7   reserved (0)
8..11  stream id (u32, little-endian)
12..15 payload byte length (u32, little-endian)
16..   payload
```

Each frame is self-identifying, so a reader can resynchronise on `LMB1`.  The
format is little-endian and assumes the host stores IEEE doubles in little-endian
order (x86-64).  `tools/stream_plot.py` reads the frames and can list them,
write CSV, or plot with matplotlib.

Two practical constraints:

* In `--data` mode `print`/`println` are redirected to stderr, so diagnostics
  do not corrupt the binary stdout stream.  Keep the stream to `write-*` frames.
* PowerShell may decode/re-encode a native pipeline.  Prefer redirecting to a
  file, running through `cmd /c`, or using Python's `subprocess` with
  `stdout=PIPE`; those preserve the bytes.

## Fixed points

Call-by-value cannot use the textbook `Y = λf. (λx. f (x x)) (λx. f (x x))`:
the inner `x x` is evaluated eagerly and diverges.  The correct CBV combinator
is `Z`:

```lambda
Z → (λf. (λx. f (λv. ((x x) v))) (λx. f (λv. ((x x) v))))
fact → (Z (λf. λn. if (== n 0) then 1 else (* n (f (- n 1)))))
(fact 10)     ; works via the dynamic fallback (self-application is untypeable)
```

A typed built-in `fix` (alias `Y`) is provided for unary functions:

```lambda
((fix (λself. λn. if (== n 0) then 1 else (* n (self (- n 1))))) 10)
```

`fix` creates a fresh recursive closure per application without a reference
cycle.

## Performance / memory

Windows x64, clang `-O3`, 16 logical CPUs:

* `nth-prime 670000`: sequential ~1.00 s; parallel trial division ~0.17 s;
  segmented sieve ~0.23 s (0.034 s at 16 threads).
* `F(1,000,000)`: 208,988 digits in ~95 ms compute+output (`live=0`);
  compute-only ~88 ms versus Python ~36 ms and gmpy2 ~2.8 ms.
* 100,000-iteration string churn: 100,004 values / 100,002 envs, `live=0`.
* 100,000-element list construction/teardown: 200,002 values, `live=0`.
* 100,000 closures created/dropped: 100,004 values / 100,002 envs, `live=0`.
* Newton's method, fixed-point iteration, and transcendental checks are in the
  regression suite and all report `live=0`.

### Cross-language check

A separate apples-to-apples comparison lives in `bench/`: Lambda, C, C++ and
Rust implement the same algorithms, constants, thread counts and `f64` kernels.
Each process performs several calls; one warm-up is discarded and the median of
three measured wall times is reported (process startup included). Threads are
spawned per call in every language, `-O3`/`opt-level=3` is used everywhere, and
there is no I/O inside a timed region. CPU: AMD Ryzen 7 9700X, 8 cores /
16 logical processors; clang 23.1.4; rustc 1.85.0.

| kernel | threads | Lambda | C | C++ | Rust | Lambda / C |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| sequential nth-prime | 1 | 1022.1 ms | 1016.4 ms | 1014.7 ms | 1034.3 ms | 1.01x |
| parallel trial division | 1 | 1204.1 ms | 1193.5 ms | 1249.1 ms | 1219.8 ms | 1.01x |
| parallel trial division | 16 | 171.0 ms | 165.1 ms | 173.9 ms | 176.1 ms | 1.04x |
| segmented sieve | 1 | 230.3 ms | 167.7 ms | 165.0 ms | 165.1 ms | 1.37x |
| segmented sieve | 16 | 33.9 ms | 21.1 ms | 21.6 ms | 20.6 ms | 1.61x |
| Newton, 100 steps | 1 | 0.375 µs | 0.366 µs | 0.366 µs | 0.368 µs | 1.02x |
| 10M i64 fill+sum | 1 | 16.9 ms | 15.0 ms | 13.3 ms | 16.1 ms | 1.13x |

The sequential and parallel trial-division paths and the f64 Newton kernel are
at parity. The sieve now uses a flat `arrayi` of base primes, chunked
`arrayi-pmap-chunk` counts into a flat result array, and materialises only the
selected block’s mask. A `p >= 64` fast path marks at most one bit directly,
avoiding the recursive mark loop for almost all base primes; this reduced the
gap from 5.1x/8.8x to **1.09x/1.45x**. The remaining single-thread overhead is
the Lambda-level mask loop and bounds checks; the parallel gap is mostly chunk
dispatch. The 10M-element array test is **1.21x C**, with one contiguous
allocation per pass and no per-element wrappers.

The bigint library (`lib/bigint.lambda`) computes **F(1,000,000) = 208,988
digits in ~95 ms**, including decimal output, versus ~512 ms for Python’s
C-backed bigint.  Compute-only it is ~88 ms versus Python ~36 ms (2.5x) and
gmpy2/GMP ~2.8 ms (32x).  The representation is unchanged (base-1e9 limbs in
one `arrayi`, length in limb 0); all arithmetic is Lambda over `arrayi`, with
Toom-3 above the cutoff, Karatsuba/schoolbook at the leaves, and fast doubling.  The compiler now inlines reference-count
fast paths, borrows plain variables passed to `&` parameters, moves tail-call
ownership, caches immutable array metadata per frame, folds literal top-level
scalars into LLVM constants, and skips the array-pool mutex in single-threaded
builds.  Full protocol and raw medians: [bench/results.md](bench/results.md).

Known numerical-methods caveat: `matrix.rs` is not used as a benchmark because
Lambda has no 2D array/matrix primitive; a list-of-lists matrix would measure
boxing and list rebuilding rather than Gaussian-elimination arithmetic. (The
Rust file also allocates `Vec<Vec<f64>>` and clones rows per elimination, and its
`back_sub` indexing looks off by one.)

## Files

| file | role |
| --- | --- |
| `ast.py` | AST nodes, bindings, source locations |
| `lexer.py` | tokens, comments, f64 literals, `λp`, `∥`, `←`, `..` |
| `parser.py` | blocks, arrow definitions, annotations, comprehensions, includes |
| `loader.py` | include resolution, include-once, cycle detection, source paths |
| `resolve.py` | lexical scoping, unique binding identities, primitive resolution |
| `typecheck.py` | HM inference, numeric constraints, primitive schemes, reps |
| `analysis.py` | purity, CSE/sharing, capture analysis, λp validation, topo order |
| `compiler.py` | type-directed LLVM IR, closures, ownership, tail calls, pmap gate |
| `runtime.py` | C runtime: RC values, lists, f64 math, threading, input, stats |
| `lambda.py` | CLI driver, include loading, clang invocation, runtime variants |
| `lib/prime.lambda` | shared primality/bounds/block helpers |
| `lib/list.lambda` | `length`, `reverse`, `filter`, `foldl`, `append` |
| `lib/bigint.lambda` | pure-Lambda base-1e9 bigint, Karatsuba squaring, fast doubling |
| `lib/stream.lambda` | LMB1 binary frame headers, f64/i64/byte emitters |
| `tools/stream_plot.py` | binary frame reader, CSV export, optional matplotlib |
| `editors/` | VS Code TextMate grammar + completion data, Pygments lexer |
| `docs/roadmap.md` | product/sum types, linearity, defunctionalization plan |
| `docs/roadmap-checklist.md` | completed/pending checklist with evidence |
| `run_tests.py` | 129 compile/run/leak regression cases |
| `tests/`, `examples/` | test corpus and runnable examples |
| `bench/` | fair Lambda/C/C++/Rust comparison and results |

## Minimalism review

The bigint work adds **no language primitives or syntax**.  It is still a
library over `arrayi`; the speedup came from making idiomatic Lambda run
better:

* reference-count fast paths are emitted inline, with a runtime slow path only
  when the last reference goes away;
* a plain variable passed to an explicitly borrowed (`&`) parameter is passed
  without a retain/release round trip;
* tail calls transfer ownership of parameters and captures instead of
  retaining them;
* immutable array length/data fields are cached per frame and marked
  `!invariant.load`, so the optimizer can hoist them;
* literal top-level scalars are emitted as LLVM constants;
* the single-threaded array-data pool is lock-free (the atomic/pmap variant
  keeps the lock);
* bigint kernels use direct `arrayi` operations, dedicated Karatsuba squaring,
  in-place subtraction inside the combine step, and tuned cutoffs.

These are compiler/runtime improvements that also benefit other array-heavy
programs; `run_tests.py` still reports 129/129 with `live=0`.  GMP-class
performance would require an FFT/NTT multiplier, but that is a separate
algorithmic step, not a language feature.

The array additions are deliberately narrow: three concrete element types, one
generic read-only accessor promise, and one chunked parallel map.  They avoid a
generic `Array a`/type-class layer that would complicate inference and boxing.
`arrayi-pmap-chunk` is the most specialised primitive; it is justified by the
sieve benchmark, but future parallel work should try to generalise it rather
than add more sieve-shaped operations.  Nullary enums are now included because
they make FSM state explicit at the cost of one integer tag; fixed-mode
payload sums add inline payload words without heap allocation.  Generic ADTs,
tuples, and arena-backed variable-size payloads remain out until their syntax
pays for itself.  Multiple parameters still cover multi-variable iteration.

For self-hosting, the practical remaining dependencies are: strings (lexer and
diagnostics), lists (AST), arrays (token/byte buffers), `read-file`, and the
primitive `print`.  The compiler proper can then be ordinary Lambda; the only
required external interface is the clang/process boundary.

## Known limitations

* Inference failure falls back for the whole program, not per expression.
* Numeric variables left unconstrained default to `Int`; use `λx: Float` for
  float-specialized generic functions.  There is no implicit runtime
  `Int → Float` conversion.
* Polymorphic functions use a boxed ABI; there is no monomorphization pass.
* Parallelism is explicit (`pmap`/`∥` comprehensions).  Automatic
  parallelization of arbitrary recursive calls is deliberately not attempted.
* `pmap`/`λp` reject bodies whose purity cannot be proven; calls through
  function parameters are treated as unknown.
* A pmap creates and joins workers per call; there is no persistent thread
  pool.  Very small pmaps are dominated by thread creation.
* Reference counting does not collect cycles.  The language avoids creating
  them (recursive values are rejected); `fix` is cycle-free by construction.
* `immortalize` is an explicit permanent leak for shared read-only constants.
* Only unary functions are supported by the typed `fix`; multi-argument
  fixpoints can use `Z` or ordinary recursive definitions.
* Integer division by zero traps; there are no exceptions.  `read-int`/
  `read-float` abort on malformed input.
* Array `set` is consume-on-write; the linear convention is documented, not
  statically enforced.  Reusing the previous reference observes the mutation.
  `*-clone` is the explicit persistent copy.
* Arrays are effects: they are not usable in `pmap`/`λp`, and first-class
  arrays (`map arrayf-get ...`), arrays of arrays, and generic `Array a` are not
  implemented.
* Typed array get/set are emitted inline with bounds checks.  Array length and
  data pointers are immutable and are cached per frame; plain variables passed
  to `&` parameters are borrowed without retain/release.  Bounds checks remain
  unless a future unchecked mode is added.
* Tuples, generic ADTs, arena-backed variable-size payloads, and FFT/NTT
  multiplication are not implemented yet.  Enums/products/fixed-mode sums have
  exhaustive matching; sums currently support up to four scalar payload fields
  per variant and are direct-call-only.
* `lib/bigint.lambda` is unsigned add/sub/mul/fast-doubling only; `bi-add`,
  `bi-sub` and `bi-mul` accept a caller-supplied `cap` argument.  Division,
  FFT/NTT multiplication and arbitrary-radix parsing are not implemented.
