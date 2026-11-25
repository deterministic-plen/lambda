# Type algebra and layout for Lambda

Status: design + compiler-side prototype. `type_algebra.py` implements the
term language, normalization, layout planning, and a parsed `DataDecl` model
for declarations such as `data Tree<X> = Leaf | Node (...)`. The Lambda source
parser now accepts `data` declarations into the AST, generates constructor
definitions (`DataCtor`), and `lambda.py --layout` elaborates and prints their
layouts. Monomorphic recursive ADTs now typecheck, including constructor typing
and exhaustive pattern matching. A first **boxed** code generation path uses
the existing cons/RC runtime (tag + fields as a list); it is correct and passes
the leak gate, but it is not the arena/list lowering yet. Generic declarations
(e.g. `data List<X>`) currently use an **erased, boxed** path: type parameters
are accepted and constructors/matches compile, but instantiations are not yet
monomorphized or layout-specialized.

Arena coverage includes nullary variants, scalar/float fields, recursive ADT
fields, arbitrary field counts (batched helpers cover 1–4 fields; wider nodes
use the direct row path), and **CSR child lists** whose elements are arena ADTs.
A list field stores an edge-array start index; construction appends child IDs
to the per-arena edge array, and matching materializes a boxed child list.
String/function/unsupported list fields still use the fully boxed fallback.

[`examples/ast_interpreter.lambda`](../examples/ast_interpreter.lambda) is the
first AST prototype built on recursive data: `Expr` has scalar/recursive fields
and therefore uses the arena layout, and `eval` is a recursive exhaustive
match. [`examples/ast_compiler.lambda`](../examples/ast_compiler.lambda) emits a
postfix integer instruction list and runs a stack machine. It handles
literals, arithmetic, negation, `if` selection, and lexical `Let`/`Var` through
compile-time substitution, exercising the ADT plus generic list library in a
compiler-shaped Lambda program. The first integration will add
`data` declarations without changing the existing `type` semantics.

## 1. Type expressions

```text
κ ::= * | κ → κ                         kinds
τ ::= α | 0 | 1 | B
    | Σ { ℓᵢ : τᵢ }                    labelled sum
    | Π { ℓᵢ : τᵢ }                    labelled product
    | τ τ                               type application
    | μ α. τ                            least fixed point
```

- `B` is a base type: `Int`, `Float`, `Bool`, `String`, arrays.
- `0` is the empty type; `1` is unit (the empty product).
- Sums/products are labelled so constructors and fields are stable under
  normalization.
- `μ` is a **least fixed point**: values are finite trees. Greatest fixed
  points (infinite/cyclic values) are a separate feature and require a cycle
  collector or a region discipline.

A named declaration is sugar for a (possibly parameterized) fixed point:

```lambda
data List<X> =
  Nil
| Cons (head: X, tail: List<X>)
```

elaborates to:

```text
List X = μ L. 1 + (head : X) × (tail : L)
```

A single-constructor product type is:

```lambda
data Vec3 =
  Vec3 (x: Float, y: Float, z: Float)
```

```text
Vec3 = 1 × (x : Float) × (y : Float) × (z : Float)
```

The existing `type` declaration remains for fixed-size inline products/sums.
`data` is the recursive/boxed/arena form; the compiler may choose the same
inline layout when the declaration is not recursive.

## 2. The examples

### List

```text
L = 1 + X × L
```

Finite lists of `X`: `Nil` or `Cons (head: X, tail: L)`. This is the classic
list type. If `X` is inline, a closed list can be represented as a dense array
of `X` values plus a length.

### Binary tree

```text
T = 1 + X × T²
```

`Leaf` or `Node (value: X, left: T, right: T)`.

### Rose tree / AST with variable arity

The rational generating function

```text
1/(1 - T) = 1 + T + T² + T³ + ...
```

is the type of finite sequences of `T`. As a recursive type:

```text
List T = μ L. 1 + T × L
```

Therefore

```text
X × 1/(1 - T)
```

means a node carrying `X` and a **finite sequence** of `T` children:

```text
T = X × List T
```

This is a well-founded rose tree/AST with finite branching. It is not an
infinite tree: the least fixed point only contains finite values.

The rational notation is accepted as **sugar** for `List`, but it is not a
primitive type constructor. `1/(1-T)` alone is a generating function; a real
type needs the recursive `μ` or the `List` abbreviation.

## 3. Layout classes

The compiler computes one of three layouts for every type.

### Inline

A type is inline when it is non-recursive and all fields are inline/fixed-size:

```text
size(1) = 0
size(B) = fixed
size(Σ {ℓᵢ:τᵢ}) = tag + max(size(τᵢ)) + padding
size(Π {ℓᵢ:τᵢ}) = sum(size(τᵢ)) with alignment
```

Inline values are passed by value/register, as today.

### Arena / flat

A recursive or variable-arity type cannot contain itself inline. Use a flat
node table and integer node ids:

```text
Tree<X>:
  tags:   ArrayB/ArrayI        -- Leaf = 0, Node = 1
  values: ArrayX               -- X payloads
  left:   ArrayI               -- node ids, or -1
  right:  ArrayI
  root:   Int
```

For a **list of inline X**, the tag is unnecessary in the common case:

```text
List<X>:
  values: ArrayX
  length: Int
```

For variable arity / rose trees, use a CSR-style child table:

```text
Rose<X>:
  tags:        ArrayI
  values:      ArrayX
  child_start: ArrayI
  child_count: ArrayI
  child_ids:   ArrayI          -- concatenated child ranges
  root:        Int
```

or first-child/next-sibling links. Both are flat arrays; no per-node heap
object or reference counted pointer is required.

### Boxed

Use the existing `%Value*`/RC representation when:

- the type is not regular/finite;
- a value must escape the arena/region;
- sharing/cycles are required;
- a polymorphic field cannot be monomorphized;
- the compiler cannot prove a bounded lifetime.

Boxed is the correctness fallback; the type algebra layout is an optimization.

## 4. What "unboxed" means

For a recursive type, “unboxed” cannot mean fully inline: a value of unbounded
depth cannot have a fixed inline size. It means:

- no individual heap allocation per node;
- no per-node reference count/header;
- node references are integer ids into a flat arena;
- one allocation (or a few arrays) per closed value/region;
- pattern matching indexes fields directly.

The representation is unboxed at the node level, not structurally finite.

## 5. Layout selection

For each strongly connected component of recursive type declarations:

1. Elaborate labels and parameters.
2. Normalize sums/products:
   - flatten nested sums/products;
   - `0` is the identity of `+`;
   - `1` is the identity of `×`;
   - keep labels and constructor tags stable.
3. Detect recursion; compute SCCs.
4. If an SCC is a single list-like type `μL. 1 + X×L`, specialize to a dense
   array when `X` is inline.
5. Otherwise emit an arena layout:
   - union of variant payloads;
   - recursive fields become node ids;
   - sequences of recursive fields become child ranges;
   - inline non-recursive fields stay inline.
6. Run escape/lifetime analysis. If an arena value can escape its region, copy
   it to a boxed value or reject with a diagnostic.
7. Emit constructors, field accessors, and pattern matches against the chosen
   layout.

Mutually recursive SCCs share one arena/node-id space, or one table per type
with a common id domain. Pattern matching checks the tag before reading fields.

## 6. Type parameters

`data List<X>` and `data Tree<X>` need parameters. The initial implementation
can monomorphize **closed instantiations**:

```text
List<Int>, List<Float>, Tree<Expr>
```

Each instantiation gets its own layout. A uniform boxed fallback handles
polymorphic code where monomorphization is not possible. Generic layouts are a
later optimization.

The bootstrap AST itself can be monomorphic:

```lambda
data Expr =
  Block (items: [Expr])
| Def   (name: String, value: Expr)
| Lam   (params: [String], body: Expr)
| App   (fn: Expr, args: [Expr])
| Lit   (value: Int)
```

## 7. Constructors and matching

`data` declarations generate:

- one constructor per labelled variant;
- field accessors when the constructor is a product;
- an exhaustive `match` checker over variants;
- layout-specific construction/matching code.

Constructor values are nominal; two structurally identical declarations do not
unify. Recursive fields are unfolded only through constructors and pattern
matching, never by infinite expansion of the type expression.

## 8. Syntax summary

```lambda
data List<X> =
  Nil
| Cons (head: X, tail: List<X>)

data Tree<X> =
  Leaf
| Node (value: X, left: Tree<X>, right: Tree<X>)

data Rose<X> =
  Rose (value: X, children: [Rose<X>])

data Expr =
  Lit (value: Int)
| Add (lhs: Expr, rhs: Expr)
| Block (items: [Expr])
```

Rational sugar:

```text
T = X × 1/(1-T)    ≡    T = X × List T
```

is a documentation/elaboration shorthand; the canonical form is `List` or an
explicit `μ`.

## 9. Performance gates

Benchmarks are part of the acceptance criteria, not an afterthought:

- **Inline ADTs**: generated code must stay within roughly 1.2x of the same
  C/C++ data structure.  The current fixed product/sum loop is ~1.11x C.
- **Recursive arenas**: after codegen, flat arena traversal must stay within a
  small constant factor of C/C++ (target: <= 2x C).  The generated arena path
  currently measures ~1.9–2.4x C and ~1.2–2.0x C++ on the 1M-node tree
  benchmark. The <=2x-C gate is met in the best runs but is not stable, so it
  remains an open optimization item. The old explicit-stack prototype (~6x C)
  is historical only. Arena memory footprint and row width are the main
  remaining costs.
- **Type algebra**: it is compile-time-only; its runtime cost is zero.  Track
  parse/normalize/layout time separately from generated-code benchmarks.
- Benchmark runners and checksums are regression gates.  Never compare
  implementations with different algorithms or allocation counts.

The expected optimization levers for recursive arenas are:

- direct field loads instead of generic `arrayi-get`/`set` with bounds checks;
- layout-proven node-id ranges;
- specialized list/SoA layouts (dense arrays, CSR children);
- avoiding an explicit stack when the traversal is naturally recursive;
- eliminating RC traffic for compile-time arena values;
- loop/TCO lowering for natural recursive traversals.

## 9. Open questions

- Least vs greatest fixed points for user-declared recursive types.
- Region syntax/lifetime for arenas that escape a compilation unit.
- Coercion between inline and arena layouts.
- Generic monomorphization and code-size policy.
- Whether `1/(1-T)` should be accepted literally or only as `List T`.
