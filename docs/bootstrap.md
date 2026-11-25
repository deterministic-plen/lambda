# Bootstrapping Lambda: diagnostics and halting

This note describes how a self-hosted Lambda compiler should handle syntax
errors and process termination without relying on Python exceptions or an
implicit "print the final value" convention.

## AST first

Define the guest AST and span/token schemas before the parser.  The parser is
not the first thing to write: it is the first thing that *produces* the AST, so
the AST contract must be frozen first.

A monomorphic AST is sufficient **if it is recursive**:

```lambda
type Expr (
  EInt  (value: Int)
| EAdd  (left: Expr, right: Expr)
| EVar  (sym: Int)
)
```

This is recursive, not polymorphic.  Polymorphism is a separate feature useful
for `Result<a, e>` or generic containers, but it is not required for a single
compiler AST.

Current Lambda cannot express that declaration with `type`: `type` cannot
mention other types, including itself (use `data` for recursive declarations —
PR C11; the misleading error does not mention `data`).  With `data` the
declaration is accepted for scalar and recursive fields, but payload fields are
still scalar/enum-only, product values are not first-class, and
boxed/record/string/list payloads are not supported yet (see
[`type-algebra-status.md`](type-algebra-status.md)).  There are two bootstrap
choices:

1. Add the missing boxed/record payload support to `data` ADTs first.
2. Use a flat, monomorphic arena now:

```text
nodes[4*i + 0] = tag
nodes[4*i + 1] = child/symbol a
nodes[4*i + 2] = child/symbol b
nodes[4*i + 3] = span/source position
```

The arena is just `ArrayI` plus a symbol/string table.  Ast node handles are
`Int` indices, so no recursive type is needed.

A pragmatic bootstrap uses the arena first, then adds recursive ADTs as a
language improvement rather than blocking the compiler on it.

## Current constraints

- Lambda has **no exceptions**. `SyntaxErr`/`CompileError` are Python-host
  exceptions; the generated program's `fail()` calls `exit(1)`.
- Generated `main` prints the final expression and returns 0.  `--exit-code`
  changes this to require an `Int` result and return it as the process status.
- Sum payloads are scalar/enum-only; product values cannot currently be stored
  in lists/closures; `data` supports monomorphic recursion through the arena
  layout, but records/strings/lists as payloads and generic ADTs are missing.
- Include resolution and the standard library are available, but there is no
  dedicated diagnostics or stderr primitive.
- Type errors are compile errors by default; there is no silent boxed fallback.
  `--allow-dynamic` is the explicit opt-out (see README).

## Recommended architecture

### 1. Errors are values

Every compiler phase returns an explicit outcome, never throws:

```
parse  : source -> (nodes, diags, count)
check  : nodes  -> (typed, diags, count)
codegen: typed  -> (ir,    diags, count)
```

A diagnostic contains at least:

```
severity (warning/error), code, start line/col, end line/col, message
```

Because current sum payloads cannot carry records, the bootstrap compiler
should use a **flat diagnostic arena**:

- one `ArrayI` for severity/code/start/end;
- one `ArrayI` or `ArrayB` symbol table for message text;
- a scalar count.

The same convention works for the AST: flat node arrays with integer tags and
child indices. This avoids recursive types until Lambda grows them.

### Arena layout without append

Lambda arrays are fixed-length and mutable: `arrayi-set` takes the array
(consuming the old handle) and returns a handle to the *same* buffer, so it is
not copy-on-write.  There is no `push`/`append`, so an arena chooses a capacity
up front and tracks a logical `used` count.  See the README's "Flat arrays"
section for the single-owner/aliasing rules.

The simplest bootstrap layout is **one flat `ArrayI` with a fixed slot**:

```text
SLOT = 8
node id i occupies:
  i*SLOT + 0  tag
  i*SLOT + 1  child / symbol a
  i*SLOT + 2  child / symbol b
  i*SLOT + 3  span start
  i*SLOT + 4  span end
  i*SLOT + 5  auxiliary index
  i*SLOT + 6  next-sibling index
  i*SLOT + 7  reserved
```

`arrayi cap 0` allocates the whole arena.  A node is created by writing its
slot with `arrayi-set` and incrementing a scalar `used`.  If capacity is
exceeded, either report a compiler limit or grow by allocating a larger
`arrayi`, copying the live prefix in a loop, and swapping it in.

A second option is **parallel arrays**:

```text
tags:  ArrayI
a:     ArrayI
b:     ArrayI
span:  ArrayI
used:  Int
```

Node id `i` indexes every array at `i`.  This is often nicer for pattern
matching and avoids stride multiplication, but the arrays must be threaded
through parser functions separately (current product fields cannot hold arrays).

An **array of arrays** is not available directly: `ArrayI` stores integers,
not `ArrayI` handles.  It can be emulated with an offset table:

```text
offsets: ArrayI   -- offsets[i] = start of node i in data
data:    ArrayI   -- flattened fields
```

That is useful for variable-sized nodes, but usually unnecessary for ASTs.
For variable arity (blocks), store `first_child` and `next_sibling` indices in
the fixed slot; that gives a linked child list without nested arrays or append.

Pre-size the arena from the token count (or source length) so normal compiles
never need growth.  Always bounds-check node ids and tags when reading; an arena
is just integers, so malformed data is otherwise silently unsafe.

### Recommendation: recursive ADTs before the bootstrap parser

For a compiler that is meant to mirror the existing Python AST, recursive ADTs
are the better first investment.  They let the source describe the tree
directly:

```lambda
type Expr (
  Block (defs: [Expr], body: Expr)
| Def   (name: String, value: Expr)
| Lam   (params: [String], body: Expr)
| App   (fn: Expr, args: [Expr])
| If    (cond: Expr, then: Expr, else: Expr)
| Lit   (value: Int)
)
```

This is monomorphic recursion; no generic parameters are needed.  Some of the
missing pieces now exist for `data`: the arena layout supports monomorphic
recursion, release is iterative, and boxed fallbacks cover string/function
fields (see [`type-algebra-status.md`](type-algebra-status.md)).  Still missing:

- record/string/list/array payload fields (payloads are scalar/enum-only);
- first-class ADT values (lists or closures containing `data` values);
- generic ADTs and monomorphization; `type` cannot be recursive, so use `data`;
- arena reclamation: `data` arenas are process-lifetime (PR F2).

Do not mirror the Python classes' mutable semantic fields (`binding`, `type`,
`pattern_tags`, `arm_bindings`) directly in the syntax ADT.  Those belong in a
separate annotation pass keyed by a stable node identity, or in a separate
`Annotated` tree.  Immutable syntax ADTs plus side tables are much easier to
reason about than mutable OOP nodes.

A pragmatic implementation order is:

1. monomorphic recursive ADTs with boxed nodes and pattern matching;
2. syntax ADT for the compiler, no semantic fields;
3. annotation tables / typed tree;
4. optional arena lowering of recursive ADTs for performance.

An arena is a lowering choice.  It is compatible with recursive ADTs because
recursive fields become integer node ids, but the ADT semantics should not be
defined in terms of a particular arena layout.

### 2. Lexer and parser state

The lexer should never abort on a bad character. It emits an error token and a
diagnostic, then continues.

The parser should be written as a state-threading recursive-descent parser:

- token stream, cursor, node arena, diagnostic arena and counts are passed
  explicitly;
- each production returns `(new_cursor, node_id)` where `node_id == -1` means
  "error node";
- `expect()` records a diagnostic and performs **panic-mode recovery**: skip to
  a synchronization point (`;`, newline, `)`, `]`, `end` of definition) and
  return an error node;
- cap recovery at a fixed number of diagnostics to avoid cascading errors.

Support two modes:

1. **Fail-fast** for the first bootstrap: stop at the first syntax error and
   report it. This is much easier to verify.
2. **Recovering** later: collect multiple diagnostics, then optionally suppress
   cascades caused by an error node.

A diagnostic should be printable as:

```
file:line:col: error[E0102]: unexpected ')'
  <source line>
       ^
```

The source line can be kept in a line table or reread from the input file at
reporting time.

### 3. Phase boundaries

- If parsing produced errors, do not typecheck or codegen.
- If typechecking produced errors, do not codegen.
- Emit all diagnostics before returning status 1.
- Internal invariant failures (unreachable parser state, corrupt arena) are a
  separate `panic` status 2, not a user diagnostic.

### 4. Entry point and halting

A compiler build should use an explicit integer entry:

```lambda
; --exit-code: this becomes the process exit status
0
```

or, in a richer compiler:

```lambda
if (== errors 0) then 0 else 1
```

`lambda.py --exit-code` now enforces an `Int` result and emits it from
`main` without printing the final value.

Exit-code convention:

| code | meaning |
| ---: | --- |
| 0 | success |
| 1 | source diagnostics |
| 2 | internal compiler panic |
| other | reserved for phase-specific status |

Do not call `exit()` inside the parser/checker. Return through `main` so the
runtime can flush output and release program-lifetime globals. Keep `fail()`
only for genuine runtime invariants (out of memory, corrupt runtime state).

The final-expression rule remains expression-oriented: an `if` or `match`
used as the compiler result must have the same type in every branch. Return
`1` from every error branch rather than trying to abort early.

### 5. Diagnostics output

For a useful compiler, diagnostics must go to stderr, not stdout. Lambda needs
one of:

- an `eprint`/`eprintln` primitive for strings; or
- an explicit diagnostic file descriptor / `World` capability.

An `eprintln` string primitive is the smallest useful addition. The compiler
then has a clean protocol:

```text
stdout: generated artifacts / machine-readable output
stderr: diagnostics
status: 0/1/2
```

### 6. Bootstrap stages

1. **Front-end stage 0**: implement lexer + parser in a restricted Lambda
   subset. Host compiler builds stage0; compare its diagnostics with the
   Python parser on the test corpus.
2. **Checker stage 0**: add the type checker, using the same flat symbols and
   diagnostics.
3. **Codegen stage 0**: emit the existing normalized AST/IR and call the host
   linker. The compiler itself is still built by the host.
4. **Self-host**: stage0 compiles stage1; compare output and diagnostics;
   stage1 compiles stage2; require stage1 == stage2.
5. Remove the Python compiler only after the fixed point is stable.

### 7. Language changes worth making first

- Recursive ADTs and nested/boxed payloads (otherwise the AST is flat arrays).
- A first-class `Result`/`Option` with record payloads.
- A stderr primitive.
- A documented entry/status convention (now `--exit-code`).
- Optional: multiple-result/product support so parser state does not need
  separately threaded arrays.

Until those exist, flat integer arenas are the pragmatic bootstrap path. They
are also a useful target for the first self-hosted compiler because they avoid
depending on the language features being bootstrapped.
