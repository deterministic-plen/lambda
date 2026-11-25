# Self-hosting boundary and plan

## Chosen boundary

The Lambda compiler is responsible for:

```text
.lambda source  ->  LLVM IR text (.ll)
```

`clang`/`llc` and the linker remain external.  The compiler does **not** need
the LLVM C++ API, an LLVM builder library, or an in-process linker.

This matches the existing Python backend (`compiler.py` already emits IR text),
so the port target is well defined.

## Input/output

Already available in the runtime:

- `read_file(path)` returns `String`;
- `write_bytes(ArrayB)` writes bytes to stdout;
- `write_file(path, ArrayB)` writes a file;
- `str_to_list`, `arrayb_of`, `arrayb_get/set`.

Still needed for a proper compiler CLI:

- command-line arguments (`argv`) so the compiler can accept an input and output
  path without hard-coding them;
- an efficient `String` -> `ArrayB` / byte-cursor primitive; `str_to_list`
  boxes every character and is too expensive for a lexer;
- a growable byte buffer for IR emission (or an equivalent chunked builder),
  since Lambda arrays are fixed-size.

The compiler can also be driven from stdin/stdout for bootstrap experiments:
read source bytes from stdin and emit IR bytes to stdout, letting the shell
redirect to a `.ll` file.

## AST first, then scanner/parser

The AST schema is the first artifact.  A fused "lexer computes the AST"
design is valid, but that phase is really a **scannerless parser**, not a pure
lexer: constructing nodes requires grammar context (unary vs binary `-`, `|`
in data vs match arms, whether an identifier is a definition or a pattern).

Recommended bootstrap order:

1. Define the canonical node sum and its side tables:
   - node variants;
   - child lists (CSR);
   - symbol IDs and literal IDs;
   - source spans and diagnostics in side tables keyed by node ID.
2. Define parser state and result/error types.
3. Implement a scannerless recursive-descent parser with an embedded
   byte-DFA scanner.
4. Only add a separate token ADT if a later stage needs reusable tokens.

The final AST should not contain raw `String` fields.  Intern identifiers and
string literals into integer IDs so the AST can stay in the arena.

A minimal starting schema is:

```lambda
data Node =
  NBlock (defs: [Node], body: Node)
| NDef (name: Int, value: Node)
| NLam (params: [Node], body: Node)
| NApp (func: Node, args: [Node])
| NIf (cond: Node, then_: Node, else_: Node)
| NMatch (subject: Node, arms: [Node])
| NVar (sym: Int)
| NInt (value: Int)
| NLiteral (lit: Int)
| NData (name: Int, params: [Int], variants: [Node])
```

Parser results can be an ADT such as `Ok(node, pos)` / `Error(pos, code)`;
the cursor is a byte array plus a position.  The parser, not a separate lexer,
owns grammar context.

## Lexing: FSMs

The existing Python lexer is hand-written and does not use regex.  The language
tokens are regular, so a DFA/table-driven byte scanner is sufficient.

Rules:

- scan UTF-8 bytes with explicit character classes;
- use a DFA or a small state enum, not a regex engine;
- emit tokens with source spans;
- intern identifier and literal text in side tables;
- handle string escapes as a DFA/transducer;
- error on unterminated strings/comments via an explicit error state.

No backtracking lexer is required.

## Parsing

Parsing is **not** an FSM problem; the grammar is not regular.  Use a
deterministic recursive-descent/Pratt parser with bounded lookahead:

- one token of lookahead (plus a small peek buffer);
- precedence climbing for infix operators;
- explicit error productions and recovery;
- no backtracking search.

This matches the current parser’s deterministic style.

## IR emission

The compiler emits `.ll` text, not LLVM objects.  String escaping and IR
formatting are regular/transducer operations; no regex is needed.

A growable byte buffer is the main missing runtime facility.  Without it,
either:

- pre-size an `ArrayB` from an estimated IR size, or
- build a list of string chunks and concatenate them once.

The former is simpler for a first bootstrap; the latter avoids a hard size
bound.

## Port order

1. Byte cursor / argv / growable output primitives.
2. Lexer FSM for a Lambda subset.
3. Deterministic parser producing an interned, arena-backed AST.
4. Resolver and scope tables.
5. Typechecker subset, then full HM inference with side tables.
6. Ownership/capture analysis.
7. IR lowering and `.ll` emission.
8. Full bootstrap and differential tests against the Python compiler.

## Current estimate

The ADT/layout substrate is in place.  The actual compiler pipeline is still
early: arithmetic AST interpreter/bytecode prototypes exist, but lexer, parser,
resolver, typechecker, analysis, and IR backend are not ported.

Removing the in-process LLVM/link layer makes the boundary cleaner but does not
remove the backend’s lowering work; `compiler.py` already emitted IR text.  The
FSM lexer removes the need for a regex engine and makes the front end more
tractable.

Still the dominant work is:

- HM inference and error diagnostics;
- closure/capture/ownership lowering;
- IR emission for the full expression/array/primitive surface;
- bootstrapping and differential validation.
