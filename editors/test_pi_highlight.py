#!/usr/bin/env python3
"""Validate Pi editor assets: TextMate grammar, completion data, Pygments."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VSCODE = ROOT / "vscode"
PI_ROOT = ROOT.parent / "pi-v2.0-impl"


def check_grammar() -> None:
    path = VSCODE / "syntaxes" / "pi.tmLanguage.json"
    g = json.loads(path.read_text(encoding="utf-8"))
    assert g["scopeName"] == "source.pi"
    for rule in g["repository"].values():
        for pat in rule.get("patterns", []):
            for key in ("match", "begin", "end"):
                if key in pat:
                    try:
                        re.compile(pat[key])
                    except re.error as exc:
                        raise SystemExit(f"bad regex {pat[key]!r}: {exc}")
    print("pi grammar ok")


def check_language_data() -> None:
    data = json.loads((VSCODE / "pi-language-data.json").read_text(encoding="utf-8"))
    for kind in ("keywords", "types", "constants", "builtins", "attributes"):
        if not data.get(kind):
            raise SystemExit(f"pi-language-data.json missing {kind}")
    pkg = json.loads((VSCODE / "package.json").read_text(encoding="utf-8"))
    lang = [x for x in pkg["contributes"]["languages"] if x["id"] == "pi"]
    if not lang or ".pi" not in lang[0]["extensions"]:
        raise SystemExit("package.json does not contribute the pi language")
    grammars = [x for x in pkg["contributes"]["grammars"] if x["language"] == "pi"]
    if not grammars or grammars[0]["scopeName"] != "source.pi":
        raise SystemExit("package.json does not contribute the pi grammar")
    print("pi language data ok")


def check_extension() -> None:
    r = subprocess.run(["node", "--check", str(VSCODE / "extension.js")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("extension.js syntax error:\n" + r.stderr)
    src = (VSCODE / "extension.js").read_text(encoding="utf-8")
    if '"pi"' not in src or "providePiCompletions" not in src:
        raise SystemExit("extension.js has no pi completion provider")
    print("pi extension ok")


def check_pygments() -> None:
    sys.path.insert(0, str(ROOT / "pygments"))
    from pygments import lex
    from pi_lexer import PiLexer

    files = sorted(PI_ROOT.rglob("*.pi")) if PI_ROOT.exists() else []
    bad = []
    for path in files:
        tokens = list(lex(path.read_text(encoding="utf-8"), PiLexer()))
        errs = [v for t, v in tokens if str(t) == "Token.Error"]
        if errs:
            bad.append(f"{path.name}: {errs[:3]}")
    if bad:
        raise SystemExit("Pygments Pi errors:\n" + "\n".join(bad))
    print(f"pi Pygments ok: {len(files)} .pi files")


if __name__ == "__main__":
    check_grammar()
    check_language_data()
    check_extension()
    check_pygments()
