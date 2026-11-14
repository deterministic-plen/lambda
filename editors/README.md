# Lambda and Pi editor support

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

**Lambda** (`source.lambda`)
- language id `lambda`, extension `.lambda`
- comments `;` and `#`; brackets `()[]`
- language icon fallback for `.lambda` files (VS Code 1.64+); the extension
  does not contribute a global file-icon theme
- completion for primitives, keywords, types, constants, operators, local
  definitions/types/fields/variants
- qualified names (`Complex::add`) and `&x` inspection

**Pi** (`source.pi`)
- language id `pi`, extension `.pi`
- comments `--` and `{- ... -}`; brackets `()[]{}`
- keywords, handle types (`Chan`, `Worker`, `Pool`, `Shard`, `Err`, …),
  declarations (`protocol`, `elem`, `system`, `program`), builtins
  (`emit_series`, `Pool`, `allreduce`, …), error constructors
- scheduler attributes (`@rate`, `@deadline`, `@priority`, …)
- local definitions (`name →`, `name :=`) and protocol/element names

### One-command install

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File editors\install_vscode.ps1
```

```bash
sh editors/install_vscode.sh
```

The script copies both grammars and both completion providers into
`~/.vscode/extensions/lambda-language-0.1.0`; restart VS Code afterwards.
No `npm install`, build step, or language server is required.

### Install locally (no build step)

**Option A - copy into the extensions folder (simplest):**

```powershell
# Windows PowerShell, from the Lambda project root
$dst = "$env:USERPROFILE\.vscode\extensions\lambda-language-0.1.0"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item editors\vscode\* $dst -Recurse -Force
```

```bash
# Linux / macOS
mkdir -p ~/.vscode/extensions/lambda-language-0.1.0
cp -r editors/vscode/* ~/.vscode/extensions/lambda-language-0.1.0/
```

Then **fully restart VS Code** (not just "Reload Window" the first time - the
extension host only scans the extensions directory at startup). Open a
`.lambda` file: the status bar shows "Lambda".

**Option B - package as a `.vsix`:**

```powershell
cd editors\vscode
npx @vscode/vsce package          # produces lambda-language-0.1.0.vsix
code --install-extension lambda-language-0.1.0.vsix
```

### Troubleshooting: nothing is highlighted

1. Bottom-right status bar must read **Lambda**. If it says *Plain Text*,
   click it and choose Lambda, or add to `settings.json`:
   ```json
   "files.associations": { "*.lambda": "lambda" }
   ```
2. Check the extension actually loaded: **Extensions** view -> search
   "Lambda" -> it should be listed as installed (if not, re-check the folder
   name is `lambda-language-0.1.0` and that `package.json` sits directly inside
   it, not one level deeper).
3. Inspect a token: **Ctrl+Shift+P** -> **Developer: Inspect Editor Tokens and
   Scopes**. Hover `Complex::add`; you should see
   `entity.name.namespace.lambda` (the `Complex::` part) and
   `entity.name.function.member.lambda` (the `add` part), and hover `and` ->
   `support.function.lambda`.
4. If tokens are right but colors are dull, the theme does not color that scope.
   Map it in `settings.json`:
   ```json
   "editor.tokenColorCustomizations": {
     "textMateRules": [
       { "scope": "entity.name.namespace.lambda", "settings": { "foreground": "#4EC9B0" } },
       { "scope": "entity.name.function.member.lambda", "settings": { "foreground": "#DCDCAA" } },
       { "scope": "support.function.lambda", "settings": { "foreground": "#C586C0" } },
       { "scope": "storage.modifier.borrow.lambda", "settings": { "foreground": "#569CD6", "fontStyle": "italic" } }
     ]
   }
   ```
5. Cached grammar after an update: **Developer: Reload Window**, or restart.
6. If every file icon became blank/generic after installing an older Lambda
   extension build, the old `Lambda Theme` file-icon theme was selected
   globally. The current extension no longer contributes an icon theme. Run
   **Preferences: File Icon Theme** and select **Seti** (or your preferred
   theme) once; the extension supplies only a `.lambda` language-icon fallback.

### Verify the grammar without VS Code

```powershell
python editors/test_highlight.py    # grammar regexes, data drift, Pygments
```

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
python editors/test_pi_highlight.py
```

`test_pi_highlight.py` checks the Pi grammar, `pi-language-data.json`, the
extension's Pi completion provider, and lexes every `.pi` file with
`PiLexer` (no `Error` tokens).  The Pi lexer handles `--` and nested
`{- ... -}` comments, durations (`5ms`, `1s`), attributes, handle/type names,
protocol/element declarations, error constructors, and Unicode operators.

`test_highlight.py` checks the Lambda manifest and completion provider, verifies that
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
| `&` and `bit-and` bitwise and | `keyword.operator.lambda` |
| `→` / `->` / `←` / `<-` | `keyword.operator.arrow.lambda` |
| `λ`, `λp`, `\`, `\p` | `storage.type.function*.lambda` |

The grammars intentionally do not highlight user variables differently from
function parameters; the language has no declaration syntax beyond `name →`.
