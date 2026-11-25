# Type and ownership roadmap

Status: nullary enums, exhaustive `match`, LLVM `switch`, tail calls inside
match arms, monomorphic unboxed product types, and fixed-mode monomorphic sum
types with up to four inline scalar payload fields are implemented.  Variable
payloads/arenas, borrowing, defunctionalization, generics, and SIMD remain
proposals.

## 1. Nominal product types

Implemented for monomorphic scalar/enum fields:

```lambda
type Complex (re: Float, im: Float)
type Vec3 (x: Float, y: Float, z: Float)
```

Construction and access:

```lambda
c → (Complex 1.0 2.0)
re → c_re
im → c_im
```

Field sugar `c_re` resolves to `(Complex-re c)` when `c` is a bound record
value and `re` is a known field.  The split is at the **last** underscore only:
`vector-2-dimensional_x` means `(Vec-x vector-2-dimensional)`, with no prefix
searching.  Exact variable names take precedence.  The generated accessor
functions remain available for explicit use.  `c.re` dot syntax is not used.

The value is an LLVM struct passed by value/register, not a heap object.
Current restrictions: fields must be scalar or enum, types are monomorphic, and
product functions are direct-call only; first-class/boxed product closures are
rejected.  Records containing lists, strings, closures, or arrays need the
ownership model settled first.

## 2. Sum types and payloads

Implemented for fixed-mode, monomorphic sums with up to four scalar/enum payload
fields per variant.  Syntax:

```lambda
type Option (a) (
  None
| Some (value: a)
)

type Shape (
  Circle (radius: Float)
| Rect (width: Float, height: Float)
)
```

Multiple payload fields per variant are part of the design.  Payloads may mix
scalar types.  The initial implementation is monomorphic; generic parameters
come after the monomorphic representation is stable.

Construction:

```lambda
(Some 3)
(Rect 2.0 3.0)
```

Pattern matching:

```lambda
match shape with
  Circle r: (* 3.141592653589793 (* r r)),
  Rect w h: (* w h)
```

### Representation: inline value embedding

For fixed-size fields, use a tagged inline union:

```text
{ i32 tag, i64 word0, i64 word1, ... }
```

- The word count is the maximum number of scalar fields in any variant.
- `Float` fields are bitcast into a word; `Int`/`Bool`/enum fields use the word
  directly.
- A `Circle(radius)` occupies `{tag=0, word0=radius}`; a
  `Rect(width,height)` occupies `{tag=1, word0=width, word1=height}`.
- No heap allocation.
- Fixed-size product fields can be flattened into consecutive words.
- A payload larger than the inline threshold, or one containing a
  runtime-sized stack/array/string, uses a tagged **arena reference** instead:
  `{ i32 tag, i32 block, i64 offset, i64 length }`.

A suggested initial threshold is **4 words inline** (32 bytes) per variant.
Beyond that, use a 16-byte segmented-arena descriptor:

```text
{ region:u16, block:u16, offset:u32, len:u32, layout:u16, flags:u16 }
```

`block` indexes the arena's block table (not a raw pointer), `layout` is a
checked type/layout id, and `region` ties the reference to a lifetime root.
This is deliberately richer than `(pointer, length)`: it supports segmented
growth, validation, and type-safe access without making every value pointer
sized.  Fixed-size payloads stay inline; runtime-sized stacks/strings/arrays
use this descriptor.  The descriptor is non-owning and cannot escape its root.

### Nested user-defined field types

Monomorphic named products and sums should be legal as fields.  Fixed-size
nesting is flattened into words; runtime-sized nesting uses an arena
descriptor.  Recursive types require the arena descriptor and are deferred.

### Pattern binders

Each payload field binds a name in its arm:

```lambda
match shape with
  Circle r: ...,
  Rect w h: ...
```

Nullary variants have no binder.  Exhaustiveness, duplicate patterns, and
binder arity are checked by the type checker.


## 3. Linearity and borrowing

Proposed surface syntax:

```lambda
norm   → (λ&v: Vec3. ...)   ; read-only borrow
scale  → (λv: Vec3. c: Float. ...) ; consumes v
```

- Every value is owned by exactly one binding unless explicitly borrowed with
  `&`.
- Passing to `&` is a borrow: the callee may read fields but not store the value
  or return it.
- Passing without `&` may consume the value; using it afterward is an error.
- The compiler should infer the weaker form automatically where possible:
  - if a parameter is only inspected, borrow it;
  - if it is stored, returned, or mutated, consume it.
  Explicit `&` is the escape hatch when inference is ambiguous.
- `noalias` and `nocapture` can be attached to unique/borrowed pointer
  parameters.  `noalias` is only valid if the function cannot retain the
  pointer or alias it through another argument.

This is not a small change: current arrays already use a mutable single-owner
buffer convention (with `&` borrows and no full aliasing analysis yet; see
PR 2.4/C9), but the rest of the language is reference-counted and closures can
capture values.  A strict linear mode would reject programs that currently rely
on sharing.

### Migration policy

- New product/sum/state code is checked strictly linear.
- Legacy RC-dependent code compiles in a compatibility mode but emits a
  deprecation warning naming the RC reason (shared list/string/closure/escape).
- Tests and examples that use legacy sharing should be rewritten toward linear
  ownership where practical; the warning list becomes the migration checklist.
- The goal is not to remove RC from the runtime overnight, but to keep RC off
  the fixed-size numeric/state hot path and make it an explicit fallback.

### Heap-free boundary

Fixed-size products/sums, scalars, and non-escaping closures can be stack/SSA
values.  These cannot be globally heap-free:

- lists of unknown length;
- strings;
- big integers;
- arrays whose size is runtime-defined;
- escaping closures.

Those need either an arena/stack region or the existing RC heap.  A segmented
arena fits the phase model: each block has a bump cursor and an optional
"next block" pointer; allocation bumps the cursor, and exhaustion allocates a
new segment rather than copying.  A block header is:

```text
{ next: Block*, end: u8*, data: bytes... }
```

Old references remain valid only while the region is live; resetting the
region invalidates every view into it.  Fixed-size sum payloads stay inline;
runtime-sized payloads store a `(block, offset, length)` arena descriptor.  The
realistic goal is **no heap allocation in fixed-size numeric/state loops**, not
a globally heap-free language.

Big integers are a measured special case: the representation remains an RC
`arrayi`, and the library stays ordinary Lambda.  Structural compiler/runtime
work (inline RC fast paths, tail ownership moves, borrowed call arguments,
immutable array metadata, constant folding, lock-free pool) plus a dedicated
Karatsuba squaring path brought `F(1,000,000)` compute from ~706 ms to ~100 ms,
the same order as Python's C bigint.  GMP still wins through FFT multiplication;
an FFT/NTT kernel is the next bigint step if GMP-class throughput is required.

## 4. Defunctionalization

Surface closures should stay:

```lambda
(map (λx. (* x x)) xs)
```

The Goose model is a useful target: function values are compile-time entities
that cannot escape, so every call can be direct and inlined, and higher-order
code compiles to the loop it describes.  Lambda cannot adopt that wholesale
without giving up first-class closures, so the plan is staged:

1. Inline/known-argument specialization: a local lambda passed directly to a
   known higher-order function becomes a direct call.
2. Closure conversion to `(tag, environment)` for a finite set of escaping
   closures.
3. Direct `switch` dispatch instead of an indirect code pointer.
4. Heap allocation only for closures whose environment escapes or whose target
   is unknown.

This must be whole-program and conservative: dynamic calls through unknown
values keep the current boxed closure path.

## 5. Tail calls

Current `musttail` works when caller/callee signatures match.  Remaining risks:

- closure wrappers with different arities/reps;
- calls through unknown function values;
- match/if merge paths.

The direction is:

- defunctionalize direct tail calls so more become `musttail`;
- use a trampoline for unavoidable unknown closure tail calls;
- keep regression tests with millions of recursive transitions, including
  matches, mutual recursion, and multi-argument accumulator loops.

## 6. SIMD and LLVM attributes

After ownership/aliasing is explicit:

- mark unique array/record pointers `noalias` where valid;
- add `nocapture` to borrowed parameters;
- emit unchecked array accesses in loops proven in-bounds;
- add `llvm.loop` vectorization metadata;
- keep checked accesses in safe/general code.

Vectorization also needs loop shapes the compiler can recognize; an indirect
closure call or a bounds-check branch in the hot loop will usually block it.

## Goose-inspired subset
Goose (<https://aardappel.github.io/goose/index.html>) is a useful reference for
the memory model, not a template for the whole language:

Adopt the ideas that preserve Lambda's semantics:

- bump/data-stack allocation with segmented growth and no per-object free;
- memory that does not move, so references into a growing buffer stay valid;
- inline fixed-size aggregates and inline nested data where practical;
- compiler-inferred lifetime roots and non-escaping references;
- compile-time function values for local higher-order code;
- bounds-check elimination after length proofs;
- fixed-vs-variable-size enum modes as a later optimization.

Adapt or defer the parts that conflict with Lambda's functional core:

- Lambda keeps first-class closures as a fallback; Goose forbids them.  Local
  known closures are defunctionalized first.
- Lambda will use a hybrid inline/arena ADT representation rather than requiring
  every nested structure to be one contiguous byte blob.
- Whole-program data-stack assignment is a large compiler project; start with
  segmented arenas and region roots.
- Threads, relative links, serialization, and error-return constructs are out of
  scope for this phase.

The governing design rule remains the phase-space model: a program is a value
moving through states linked by transforms.  Heap-free fixed-size values are the
default; dynamic state uses an explicitly rooted arena; RC is a deprecated
fallback for sharing/escape.



## Parallelism limits

The current `λp`/`pmap` model requires provable purity.  That makes the
scheduler safe and deterministic, but it also excludes useful parallel
algorithms: in-place updates, reductions with private accumulators, work
stealing over mutable state, and tasks that need controlled shared memory.
Pure `arrayi-pmap` also forces index-to-value mapping rather than arbitrary
task bodies.

Future parallel work should decouple **purity for automatic scheduling** from
**linear tasks with disjoint ownership**:

- a task may consume a linear slice and return a new slice;
- disjoint chunks may write into disjoint regions of one linear array;
- reductions use explicit private accumulators merged by the parent;
- effectful tasks remain opt-in and are not CSE'd or reordered.

Until then, the parallel sieve remains limited to disjoint result slots plus a
read-only immortal base-prime array, which is why its remaining gap is task
dispatch rather than data races.

## Suggested order

1. Monomorphic product types with unboxed structs.
2. Sum types with payloads and exhaustive `switch` match.
3. Generic monomorphization.
4. Borrow syntax plus inference for records/arrays.
5. Non-escaping closure defunctionalization.
6. TCO/trampoline hardening.
7. `noalias`/unchecked/SIMD work, measured against C benchmarks.

## Decisions needed

1. Accessors: generated `(Type-field value)` names, or `value.field` sugar?
2. Keep `enum` as sugar, or make `type` the only declaration syntax?
3. Product fields: require explicit types, or allow inference?
4. Generic ADTs now, or monomorphic first?
5. Strict linearity/borrow checking, or inference with RC fallback?
6. Are arenas acceptable for lists/strings/bigints, or must those remain RC?
7. Defunctionalization scope: local known closures first, or whole-program?
