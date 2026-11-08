#!/usr/bin/env python3
"""Regenerate the editor completion database.

The language data is derived from the compiler itself so primitive names,
keywords and built-in types cannot drift between the language and the editor.
Run from the repository root:

    python editors/generate_language_data.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer import KEYWORDS  # noqa: E402
from parser import PRIMITIVES  # noqa: E402

# These are the nominal built-in types accepted by the type checker.
BUILTIN_TYPES = [
    "Int", "Float", "Bool", "String",
    "ArrayF", "ArrayI", "ArrayB",
]
CONSTANTS = ["true", "false", "nil"]
WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_?!<>-]*$")
NAMED_SYMBOLS = {"π", "∑", "∏", "√"}


def is_word(name: str) -> bool:
    return bool(WORD.match(name)) or name in NAMED_SYMBOLS


def main() -> int:
    primitives = sorted(p for p in PRIMITIVES if is_word(p))
    operators = sorted(p for p in PRIMITIVES if not is_word(p))
    data = {
        "version": 1,
        "source": "lexer.KEYWORDS + parser.PRIMITIVES",
        "keywords": sorted(KEYWORDS),
        "types": BUILTIN_TYPES,
        "constants": CONSTANTS,
        "primitives": primitives,
        "operators": operators,
    }
    out = Path(__file__).resolve().parent / "vscode" / "language-data.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} "
          f"({len(primitives)} primitives, {len(operators)} operators)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
