#!/usr/bin/env python3
"""Lightweight validation for the Lambda editor grammars.

This does not emulate VS Code's TextMate engine.  It checks that:

* the extension JSON is valid and its grammar path exists;
* every TextMate regex compiles under Python's regex engine;
* the Pygments lexer consumes every .lambda file without Error tokens.

TextMate uses Oniguruma, so a regex accepted here can still differ in edge
cases, but malformed JSON and obvious pattern errors are caught.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VSCODE = ROOT / "editors" / "vscode"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "editors" / "pygments"))

from lexer import KEYWORDS  # noqa: E402
from parser import PRIMITIVES  # noqa: E402

WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_?!<>-]*$")
NAMED_SYMBOLS = {"π", "∑", "∏", "√"}


def check_grammar() -> None:
    package = json.loads((VSCODE / "package.json").read_text(encoding="utf-8"))
    grammar_path = VSCODE / package["contributes"]["grammars"][0]["path"]
    grammar = json.loads(grammar_path.read_text(encoding="utf-8"))

    main = package.get("main")
    if not main or not (VSCODE / main).is_file():
        raise SystemExit(f"package.json main is missing: {main!r}")
    extension = (VSCODE / main).read_text(encoding="utf-8")
    if "registerCompletionItemProvider" not in extension:
        raise SystemExit("extension.js does not register a completion provider")

    errors = []

    def visit(node, path=""):
        if isinstance(node, dict):
            for key in ("match", "begin"):
                if key in node:
                    try:
                        re.compile(node[key])
                    except re.error as exc:
                        errors.append(f"{path}/{key}: {exc}: {node[key]!r}")
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                visit(value, f"{path}/{i}")

    visit(grammar)
    if errors:
        raise SystemExit("grammar regex errors:\n" + "\n".join(errors))
    print(f"grammar ok: {grammar_path.relative_to(ROOT)}")


def check_language_data() -> None:
    path = VSCODE / "language-data.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_primitives = {
        p for p in PRIMITIVES if WORD.match(p) or p in NAMED_SYMBOLS
    }
    expected_operators = {
        p for p in PRIMITIVES if not (WORD.match(p) or p in NAMED_SYMBOLS)
    }
    checks = [
        ("primitives", set(data["primitives"]), expected_primitives),
        ("operators", set(data["operators"]), expected_operators),
        ("keywords", set(data["keywords"]), set(KEYWORDS)),
    ]
    for name, actual, expected in checks:
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise SystemExit(
                f"language-data.json {name} drift: missing={missing} extra={extra}")
    print(f"language data ok: {len(data['primitives'])} primitives, "
          f"{len(data['operators'])} operators")


def check_pygments() -> None:
    from pygments import lex
    from lambda_lexer import BUILTINS, LambdaLexer

    expected = {
        p for p in PRIMITIVES
        if WORD.match(p) or p in NAMED_SYMBOLS
    } - {"nil"}
    mismatch = expected.symmetric_difference(set(BUILTINS.split()))
    if mismatch:
        raise SystemExit(
            "Pygments BUILTINS drift: " + ", ".join(sorted(mismatch)))

    files = sorted(ROOT.glob("**/*.lambda"))
    bad = []
    for path in files:
        try:
            tokens = list(lex(path.read_text(encoding="utf-8"), LambdaLexer()))
        except Exception as exc:  # pragma: no cover - diagnostic path
            bad.append(f"{path}: {exc!r}")
            continue
        if any(str(token) == "Token.Error" for token, value in tokens if value):
            bad.append(f"{path}: Error token")
    if bad:
        raise SystemExit("Pygments lexer failures:\n" + "\n".join(bad))
    print(f"Pygments ok: {len(files)} .lambda files")


if __name__ == "__main__":
    check_grammar()
    check_language_data()
    check_pygments()
