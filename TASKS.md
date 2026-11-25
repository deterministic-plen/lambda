# Task list (from the project brief)

`PR.md` fixes are P0 and must be completed before anything else.  Where a
requirement conflicts with `PR.md`, `PR.md` wins.

## Priority 0 — `PR.md`

1. Fix every issue listed in `PR.md`.  Acceptance: all `PR.md` items are
   resolved and regression tests pass.  Evidence and status are tracked in
   [`docs/PR-COMPLIANCE.md`](docs/PR-COMPLIANCE.md).

## Priority 1 — Core semantics

2. Remove `--strict` completely.  Strict checking is always on, with no
   opt-out.  Update CLI, docs, tests, and examples.
3. Strengthen the type checker: support optional explicit function return
   types.  If omitted, infer the return type; the strict type system still
   enforces correctness for general inputs.
4. Recursive halting analysis: implement a solver for a bounded, strictly
   decreasing rank variable.  If the recursive variable decreases and is
   bounded, the halting check passes.  Otherwise warn that the program *may*
   not halt.  This is a warning unless existing semantics require an error.
5. Borrow inference: if a function takes an input but never returns it, infer
   a borrow instead of consumption.  On by default, with explicit user-defined
   linearity annotations.  Propagate borrows: if a type is borrowed, its use is
   also borrowed.
6. Token type: support `type Token = (kind: Kind, value: [Int])`, where `Kind`
   is an enum.  Simplify related ergonomics.

## Priority 2 — Ergonomic expressiveness

7. TCO ergonomics: tail-call style must stay easy when program state has many
   variables; add sugar if needed.
8. Purity: increase purity by being stricter where practical.  Document
   tradeoffs.
9. Match syntax: `match variable value→result | value2→result | default→default
   result`.  Update `brainf.lambda` to remove redundant `if (== chari 93)`
   chains and use readable char literals like `']'` instead of `93`.
10. Arrays: primitive `Array<T>` instead of `array(type)`/arena exposure;
    calls `(array-set array position value)`; sum-type elements; `#[1,2,3,4]`
    sugar; keep low-level arena performance behind implicit lowering.
11. Strings: fix `PR.md` string issues; add `string[a]` returning the
    character/codepoint at `a` (document semantics; support both if practical).
12. Debugger/profiling: display program state each cycle using the world-state
    model; add a simple state-cycle debugger/profiler.

## Priority 3 — Effects and UI

13. World-object purity: pass world objects written in place; the type checker
    enforces stricter rules on logic.
14. GUI: LLVM screen objects `width * height` and a pixel buffer
    `Array<byte>`.  Program returning a GUI type opens a GUI.  Helpers in
    `GUI.lambda`: `Rectangle`, `circle`, `button`.  No Python plotting; Pi can
    invoke `plotter.lambda`.  Cross-platform, minimal native C ABI.

## Constraints

- Do not expose low-level array/arena details to normal programmers.
- Strict typing is default and non-optional.
- Preserve performance where possible; prefer sugar over complexity.
- Update `brainf.lambda`, examples, CLI docs, and tests for changed syntax.
- Lambda should be written (relatively) purely even if the implementation is
  procedural underneath.
