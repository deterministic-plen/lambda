# Type-algebra / ADT status

This is the implementation audit for the type-algebra/recursive-ADT work.
It separates what is implemented and verified from what is still speculative or
intentionally deferred.

## Implemented

- **Type algebra prototype** (`type_algebra.py`)
  - algebraic terms: empty, unit, base types, sums, products, parameters,
    recursion (`mu`), `List`, rational `1/(1-T)` sugar;
  - normalization, substitution, fixed-size inline sizing, and layout planning
    (`inline`, `list`, `arena`, `boxed`);
  - parsed `DataDecl`/`Variant` declarations, including generic parameters;
  - validation and a compile-time benchmark.

- **Source `data` declarations**
  - `data Name<Params> = Variant (field: Type, ...) | ...`;
  - constructor definitions and resolution;
  - monomorphic recursive type checking, constructor typing, exhaustive match,
    and non-exhaustive-match diagnostics;
  - generic declarations currently use an **erased/boxed** path.

- **Recursive ADT code generation**
  - arena layout for scalar, float, recursive, nullary, and wide nodes;
  - CSR child lists for arena-ADT list fields;
  - boxed fallback for string/function/other boxed fields;
  - constructor generation, exhaustive matching, and RC/leak cleanup;
  - arena rows are freed at program exit.

- **Tests**
  - the full Lambda compile/run/leak suite (`python run_tests.py`); run
    `python run_tests.py --list` for the current case count, which changes as
    regressions are added.  Do not hard-code it here;
  - type-algebra checks: 10/10;
  - source data parser checks: 2/2;
  - data typecheck checks: 2/2;
  - edge cases: nullary, float, five-field, boxed string/list, CSR rose tree,
    arena AST interpreter, bytecode compiler.

- **Benchmarks**
  - fixed product/sum ADT: ~1.11x C;
  - generated recursive arena: ~1.9–2.4x C and ~1.2–2.0x C++ across runs.
    The <=2x-C gate is met in the best runs but is **not stable**; it remains
    an open performance item rather than a consistently met gate.
  - type algebra: compile-time only (~3.4 us/op), no runtime cost.

- **Bootstrapped AST prototypes**
  - `examples/ast_interpreter.lambda`: recursive arena `Expr` evaluator;
  - `examples/ast_compiler.lambda`: postfix bytecode compiler + stack machine,
    including negation, conditionals, and compile-time `Let`/`Var`
    substitution.

## Compiler robustness notes

- Long `else if` dispatch chains are flattened by `_flatten_chain`; each chain
  costs one Python frame regardless of arm count, and consecutive
  `if a then b else (if c then d else e)` chains are joined when every branch
  is a self tail call, producing real loops (no trampolining, because the
  backend already emits `musttail` for tail calls).
- The driver runs the compiler on a 64 MB worker thread with a raised Python
  recursion limit, since parsing/typechecking/codegen itself is recursive.
- Known gap: chains of ``x -> (if ...)`` **definitions** (blocks inside a call
  argument) still nest parser frames, so extremely long definition-based
  dispatches are bounded by the worker stack rather than fully flattened.

## Deliberately not implemented

- **Full monomorphization** of generic data. `data List<X>` is erased and
  boxed; constructors are not specialized per instantiation.
- **Generic layout specialization** (e.g. `List<Int>` as dense array vs
  `List<String>` as boxed).
- **A full self-hosted Lambda front end** (tokenizer, parser, name resolution,
  type checker, code generator). The current compiler prototype starts from an
  already-built AST.
- **Source-level `mu`/rational type syntax**; these exist only in the Python
  type-algebra prototype.
- **List/CSR edge layout for non-ADT element types** and general variable
  arity beyond child lists.
- **Garbage collection / arena reuse.** Arenas are process-lifetime and freed
  at exit; there is no compaction or reclamation during execution.
- **Thread-safe ADT arenas.** The words-pointer cache and arenas assume a
  single constructing thread; parallel ADT construction is not supported.
- **Lazy `if` in the bytecode prototype.** Both branch instruction sequences are
  emitted and evaluated, then selected.

## Known tradeoffs / risks

- The initial arena capacity is deliberately large to keep the words cache hot
  and meet the performance gate; this raises virtual memory per ADT arena.
  Pages are demand-committed, but it is not a general memory policy.
- Arena words cache refresh occurs after constructor allocation. It is not
  atomic; do not use ADT construction from `pmap`/parallel regions yet.
- The boxed fallback uses ordinary cons/RC data and materializes a boxed list
  when matching a CSR child field.
- Generic erasure means `List<Int>` and `List<String>` share a representation
  and are not type-specialized; this is sound only within the erased subset.
- The bytecode compiler's `if` is eager; the interpreter's `if` is the actual
  lazy semantics.

## Verification commands

Run these from the repository root (the old `C:\Users\mumuj\Documents\lambda`
path here was stale):

```powershell
python run_tests.py
python tests/test_type_algebra.py
python tests/test_data_parser.py
python tests/test_data_typecheck.py
python bench/run_adt_bench.py
python bench/run_adt_recursive_bench.py
python bench/type_algebra_bench.py
python lambda.py examples/ast_compiler.lambda -o $env:TEMP/ast_compiler.exe
```
