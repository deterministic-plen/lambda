# Lambda editor support

Two highlighting implementations are provided:

- `vscode/` — a VS Code extension with a TextMate grammar plus a small
  completion provider (also usable by other TextMate-compatible editors for
  highlighting)
- `pygments/` — a Pygments lexer for generated HTML, Sphinx, MkDocs, etc.

This is not a language server: completion is a lightweight, dependency-free
provider over the generated language data and the current document.  Hover,
diagnostics, and go-to-definition are not implemented.

## VS Code

The extension contributes:

- language id `lambda`
- file extension `.lambda`
- line comments `;` and `#`
- bracket matching for `()` and `[]`
- scope `source.lambda`
- completion for primitives, keywords, built-in types, constants, operators,
  and definitions/types/fields/variants found in the current document
- qualified names with `::` (`Complex::add`) and the `&x` borrow inspection

### Install locally

Copy the `vscode` directory into your extensions folder:

```text
%USERPROFILE%\.vscode\extensions\lambda-language-0.1.0
```

On Linux/macOS:

```bash
cp -r editors/vscode ~/.vscode/extensions/lambda-language-0.1.0
```

Restart VS Code and open a `.lambda` file.  To inspect token scopes, run
**Developer: Inspect Editor Tokens and Scopes**.

For packaging, install `vsce` and run `vsce package` inside `editors/vscode`.
No `node_modules` or build step is required.

## Completion data

`vscode/language-data.json` is generated from the compiler's own tables, so it
cannot drift from the language:

```bash
python editors/generate_language_data.py
```

It contains the primitive names, operators, keywords, built-in types and
constants.  The completion provider adds document-local definitions
(`name → ...`), `type`/`enum` names, sum variants, and field names on top.

## Pygments

```bash
PYTHONPATH=editors/pygments python - <<'PY'
from pygments import highlight
from pygments.formatters import HtmlFormatter
from lambda_lexer import LambdaLexer

source = open("examples/nth_prime.lambda", encoding="utf-8").read()
print(highlight(source, LambdaLexer(), HtmlFormatter(full=True)))
PY
```

The lexer recognizes the same comments, strings, numbers, keywords, type
annotations, primitives, Unicode/ASCII operators, arrows, comprehension pipes,
and range syntax as the VS Code grammar.

Validation:

```bash
python editors/test_highlight.py
```

This checks the extension manifest and completion provider, verifies that
`language-data.json` matches `parser.PRIMITIVES`/`lexer.KEYWORDS`, compiles each
TextMate regex under Python's engine, and lexes every `.lambda` file with no
`Error` tokens.  It does not replace opening a file in VS Code, since TextMate
uses Oniguruma.

## Scope names

| construct | scope |
| --- | --- |
| `;` / `#` comment | `comment.line.*.lambda` |
| string | `string.quoted.double.lambda` |
| integer/float | `constant.numeric.lambda` |
| `if then else let in` | `keyword.control.lambda` |
| `match` / `with` | `keyword.control.lambda` |
| `enum` declaration | `keyword.declaration.enum.lambda` |
| `type` declaration | `keyword.declaration.type.lambda` |
| `include` | `keyword.other.import.lambda` |
| `true false` | `constant.language.boolean.lambda` |
| `nil` | `constant.language.nil.lambda` |
| `Int Float Bool String ArrayF/I/B` | `support.type.lambda` |
| definition name before `→`/`->` | `entity.name.function.lambda` |
| `Name::part` qualified name | `entity.name.namespace.lambda` |
| primitive name | `support.function.lambda` |
| constructor / type / sum variant | `entity.name.type.lambda` |
| `&x` inspection / `&` after `λ` | `storage.modifier.borrow.lambda` |
| `&&` and `&` bitwise and | `keyword.operator.lambda` |
| `→` / `->` / `←` / `<-` | `keyword.operator.arrow.lambda` |
| `λ`, `λp`, `\`, `\p` | `storage.type.function*.lambda` |

The grammars intentionally do not highlight user variables differently from
function parameters; the language has no declaration syntax beyond `name →`.
