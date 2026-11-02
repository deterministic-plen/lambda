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

python run_tests.py          # 87 regression tests
```

Requirements: Python 3.10+, LLVM `clang` on `PATH`.

Useful flags:

| flag | effect |
| --- | --- |
| `--strict` | reject programs that cannot be type-inferred |
| `--dynamic` | force the fully boxed backend |
| `--input` | enable `read-line`/`read-int`/`read-float` (off by default) |
| `--keep-ir` | keep the generated `.ll` next to the executable |
| `--dump-ir` | print LLVM IR to stdout |
| `--no-musttail` | disable `musttail`/`tail` emission (debugging) |
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
  go → (λa b i. if (≡ i n) then a else (go b (⊕ a b) (⊕ i 1)))
  (go 0 1 0))

(fib 25)
```

Every function binding uses the same arrow notation, recursive or not.  There
is no `letloop`/`letrec` distinction: inference chooses unboxed or boxed code.
(`letrec`/`letloop` are accepted as deprecated aliases of `let`.)

Other forms:

```lambda
(λx y. (⊕ x y))                 ; multiple parameters
(λp x. (⊕ x 1))                 ; parallel-safe marker (must be pure)(λx: Float. (⊗ x x))            ; parameter type annotation
(let x = (fib 5) in (⊕ x 1))     ; local definition sugar
[1, 2, 3]                        ; list literal
[ (⊗ x x) | x ← [1..10] ]        ; sequential comprehension
[ (⊗ x x) ∥ x ← [1..10] ]        ; parallel comprehension (pmap)
[0..n]                           ; half-open integer range 0 .. n-1
"a\nb"                           ; strings
(if cond then a else b)
```

A definition value is one atom, so compound values go in parentheses:
`f → (⊕ 1 2)` is valid, `f → ⊕ 1 2` is not.

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

### Types and annotations

`Int`, `Bool`, `Float`, `String`, `[T]`, and curried function types are
inferred.  Numeric operators are overloaded over `Int`/`Float` but require both
operands to have the **same** type; there is no implicit runtime conversion.
Unconstrained numeric variables default to `Int`.  Use a parameter annotation
when a generic-looking function should be specialized to `Float`:

```lambda
square → (λx: Float. (⊗ x x))     ; without the annotation this defaults to Int
squarei → (λx. (⊗ x x))           ; Int -> Int
```

`int->float` and `float->int` compile to one `sitofp`/`fptosi` instruction;
`float->int` truncates toward zero.

### Primitives

| group | names |
| --- | --- |
| numeric | `⊕`/`+`, `⊖`/`-`, `⊗`/`*`, `⊘`/`/`, `⊙`/`%` (Int or Float, homogeneous) |
| comparison | `≺`/`<`, `≼`/`<=`, `≻`/`>`, `≽`/`>=`; `≡`/`==` structural equality |
| float math | `√`/`sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `exp`, `ln`, `log10`, `log2`, `floor`, `ceil`, `round`, `trunc`, `pow`/`**`/`^`, `abs`, `π`/`pi`, `e` |
| conversions | `int->float`, `float->int`, `str->float`, `float->str` |
| reductions | `∑`/`sum`, `∏`/`product` (same type as the list) |
| lists | `cons`, `head`, `tail`, `empty?`, `nil`, `map`, `pmap`, `range`, `immortalize`/`freeze` |
| flat arrays | `arrayf`, `arrayi`, `arrayb`, `*-get`, `*-set`, `*-length`, `*-clone` |
| bit-level | `&`/`bit-and`, `\|`/`bit-or`, `bit-xor`, `!`/`bit-not`, `shl`, `shr`, `popcount` |
| strings | `str->int`, `int->str`, `str->list`, `str-len`, `str-append`, `read-file` |
| IO | `print`, `println` (return their argument), `not` |
| input (`--input`) | `read-line`, `read-int`, `read-float` |
| fixed point | `fix`, `Y` (call-by-value, unary functions) |

`print`/`println` and terminal input are the only effectful primitives.
Division/modulo are inlined `sdiv`/`srem` for Int and `fdiv`/`frem` for Float;
integer division by zero traps.

## Numbers and machine code

* `Int` and `Bool` are unboxed `i64`; `Float` is an unboxed LLVM `double`.
* `String`, lists, and functions are reference-counted `%Value*`.
* Arithmetic/comparisons emit `add`, `mul`, `fadd`, `fmul`, `fcmp`, etc.;
  no tags, dictionaries, or allocations on homogeneous numeric paths.
* `√`, `floor`, `ceil`, `round`, `trunc`, `abs`, and `pow` use LLVM intrinsics
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
  if (≽ i n)
  then acc
  else (sum-squares (arrayi-set a i (⊗ i i))
                    (⊕ i 1) n
                    (⊕ acc (arrayi-get a i))))

a → (arrayi 1000 0)
(sum-squares a 0 1000 0)        ; 332833500
```

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

**Current restriction:** array primitives must be fully applied.  First-class
arrays, arrays of arrays, and a typed `Array a` are not implemented yet; the
three concrete types keep the element representation unboxed.

## Sharing, memory, and ownership

A purity fixpoint marks pure functions and primitives.  A CSE pass finds pure
applications repeated in one straight-line region and evaluates them once:

```lambda
bad → (λn.
  if (≡ n 0) then 0
  else (⊕ (bad (⊖ n 1)) (⊕ (bad (⊖ n 1)) (bad (⊖ n 1)))))

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
total  → (∑ counts)
```

Parallel comprehensions are sugar:

```lambda
[ (⊗ x x) ∥ x ← [1..10] ]         ; pmap
[ (⊗ x x) | x ← [1..10] ]         ; map
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
(⊕ n 1)
```

* `read-line : String -> String` prints the prompt, flushes output, and returns
  the line without its newline; EOF returns `""`.
* `read-int` / `read-float` parse the line and fail with a runtime error on
  invalid input.
* Prompt strings may be empty (`""`) for scripted use.

## Fixed points

Call-by-value cannot use the textbook `Y = λf. (λx. f (x x)) (λx. f (x x))`:
the inner `x x` is evaluated eagerly and diverges.  The correct CBV combinator
is `Z`:

```lambda
Z → (λf. (λx. f (λv. ((x x) v))) (λx. f (λv. ((x x) v))))
fact → (Z (λf. λn. if (≡ n 0) then 1 else (⊗ n (f (⊖ n 1)))))
(fact 10)     ; works via the dynamic fallback (self-application is untypeable)
```

A typed built-in `fix` (alias `Y`) is provided for unary functions:

```lambda
((fix (λself. λn. if (≡ n 0) then 1 else (⊗ n (self (⊖ n 1))))) 10)
```

`fix` creates a fresh recursive closure per application without a reference
cycle.

## Performance / memory

Windows x64, clang `-O3`, 16 logical CPUs:

* `nth-prime 670000`: sequential ~1.00 s; parallel trial division ~0.17 s;
  segmented sieve ~0.19 s (0.85 s on one thread).
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
| sequential nth-prime | 1 | 1026.9 ms | 1013.4 ms | 1020.6 ms | 1028.9 ms | 1.01x |
| parallel trial division | 1 | 1198.7 ms | 1196.2 ms | 1242.9 ms | 1202.2 ms | 1.00x |
| parallel trial division | 16 | 172.1 ms | 169.2 ms | 174.3 ms | 170.2 ms | 1.02x |
| segmented sieve | 1 | 845.5 ms | 165.0 ms | 166.9 ms | 174.8 ms | 5.12x |
| segmented sieve | 16 | 175.3 ms | 20.0 ms | 20.7 ms | 20.3 ms | 8.76x |
| Newton, 100 steps | 1 | 0.365 µs | 0.364 µs | 0.363 µs | 0.365 µs | 1.00x |

The sequential and parallel trial-division paths and the f64 Newton kernel are
at parity. The sieve is the outlier: it creates ~1 million boxed values per call
and walks the shared base-prime list ~78 million times through `head`/`tail` and
recursive `mark` calls, while the native versions use contiguous arrays. Its
16-thread result also exposes `pmap`'s per-task list/closure overhead: the
C/Rust/C++ sieve scales almost 8x, Lambda ~4.8x. This is representation and
parallel-runtime overhead, not arithmetic — the Newton result shows the f64
paths are equivalent. Full protocol and raw medians: [bench/results.md](bench/results.md).

Known numerical-methods caveat: `matrix.rs` is not used as a benchmark because
Lambda has no mutable array type; a list-of-lists matrix would measure boxing
and list rebuilding rather than Gaussian-elimination arithmetic. (The Rust file
also allocates `Vec<Vec<f64>>` and clones rows per elimination, and its
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
| `run_tests.py` | 87 compile/run/leak regression cases |
| `tests/`, `examples/` | test corpus and runnable examples |
| `bench/` | fair Lambda/C/C++/Rust comparison and results |

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
* Typed array get/set are emitted inline with bounds checks, but the compiler
  still retains/releases the array reference around each access; a move/borrow
  optimization is needed before element loops match raw C speed.
* Tuples, named enums, big integers and FFT are not implemented yet.  The flat
  arrays are the storage prerequisite for the bigint/fast-doubling work.
