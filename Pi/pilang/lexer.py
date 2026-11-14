"""Pi lexer (spec section 3.1).

Both the Unicode and ASCII spellings are accepted:
    →  ->      ⇒  =>      ‖  ||      ≤  <=      ≥  >=      ⟨ ⟩  < >  (in type position)
Every token records whether it is the first on its line (`nl`) and its column, which
the parser uses for layout ("no braces, no semicolons -- blocks delimited by context").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .errors import Loc, PiSyntaxError

KEYWORDS = {
    "if", "then", "else", "match", "with", "type", "include", "import",
    "scope", "par", "serve", "loop", "iterate", "for", "in",
    "select", "call", "await", "cancel", "send", "recv", "close", "closed",
    "after", "fail", "on_error", "restart", "escalate", "stop", "resume",
    "slice", "dims", "drop",
    "elem", "system", "instances", "couple", "expose", "ctrl", "state",
    "worker", "protocol", "op", "purity", "contract",
    "program", "main", "module",
    # structural keywords of the concrete syntax
    "true", "false",
}

DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}

# multi-character operators, longest first
_OPS = [
    ":=", "->", "=>", "||", "<=", ">=", "==", "!=", "..",
    "→", "⇒", "‖", "≤", "≥", "≡", "⟨", "⟩",
]
_SINGLE = set("+-*/%@?(){}[]<>,:;.|=_")

_NORMALISE = {"->": "→", "=>": "⇒", "||": "‖", "<=": "≤", ">=": "≥"}


@dataclass
class Token:
    kind: str            # IDENT CONID INT FLOAT DURATION STRING OP KW EOF
    text: str
    value: object
    line: int
    col: int
    nl: bool             # first token on its line
    file: str = "<input>"

    def loc(self) -> Loc:
        return Loc(self.file, self.line, self.col)

    def is_op(self, *ops: str) -> bool:
        return self.kind == "OP" and self.text in ops

    def is_kw(self, *kws: str) -> bool:
        return self.kind == "KW" and self.text in kws

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind},{self.text!r}@{self.line}:{self.col})"


def lex(src: str, file: str = "<input>") -> List[Token]:
    toks: List[Token] = []
    i, n = 0, len(src)
    line, col = 1, 1
    nl = True

    def err(msg):
        raise PiSyntaxError(msg, Loc(file, line, col))

    def advance(k=1):
        nonlocal i, line, col
        for _ in range(k):
            if i < n and src[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        c = src[i]
        if c == "\n":
            advance()
            nl = True
            continue
        if c in " \t\r":
            advance()
            continue
        # comments: -- to end of line, {- ... -} nested
        if src.startswith("--", i):
            while i < n and src[i] != "\n":
                advance()
            continue
        if src.startswith("{-", i):
            depth = 0
            while i < n:
                if src.startswith("{-", i):
                    depth += 1
                    advance(2)
                elif src.startswith("-}", i):
                    depth -= 1
                    advance(2)
                    if depth == 0:
                        break
                else:
                    advance()
            else:
                err("unterminated block comment")
            continue
        sl, sc = line, col
        # strings
        if c == '"':
            j = i + 1
            buf = []
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(src[j + 1], src[j + 1])
                    buf.append(esc)
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            if j >= n:
                err("unterminated string")
            text = src[i:j + 1]
            toks.append(Token("STRING", text, "".join(buf), sl, sc, nl, file))
            advance(j + 1 - i)
            nl = False
            continue
        # numbers / durations
        if c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            is_float = False
            if j + 1 < n and src[j] == "." and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            if j < n and src[j] in "eE" and j + 1 < n and (src[j + 1].isdigit() or (
                    src[j + 1] in "+-" and j + 2 < n and src[j + 2].isdigit())):
                is_float = True
                j += 2
                while j < n and src[j].isdigit():
                    j += 1
            num = src[i:j]
            # duration suffix directly attached
            for unit in ("ms", "us", "ns", "s", "m", "h"):
                if src.startswith(unit, j) and not (
                        j + len(unit) < n and (src[j + len(unit)].isalnum() or src[j + len(unit)] == "_")):
                    toks.append(Token("DURATION", num + unit, float(num) * DURATION_UNITS[unit], sl, sc, nl, file))
                    advance(j + len(unit) - i)
                    nl = False
                    break
            else:
                if is_float:
                    toks.append(Token("FLOAT", num, float(num), sl, sc, nl, file))
                else:
                    toks.append(Token("INT", num, int(num), sl, sc, nl, file))
                advance(j - i)
                nl = False
            continue
        # identifiers / keywords
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_'"):
                j += 1
            word = src[i:j]
            if word == "_":
                toks.append(Token("IDENT", "_", "_", sl, sc, nl, file))
            elif word in KEYWORDS:
                toks.append(Token("KW", word, word, sl, sc, nl, file))
            elif word[0].isupper():
                toks.append(Token("CONID", word, word, sl, sc, nl, file))
            else:
                toks.append(Token("IDENT", word, word, sl, sc, nl, file))
            advance(j - i)
            nl = False
            continue
        # operators
        matched: Optional[str] = None
        for op in _OPS:
            if src.startswith(op, i):
                matched = op
                break
        if matched is None and c in _SINGLE:
            matched = c
        if matched is None:
            err(f"unexpected character {c!r}")
        text = _NORMALISE.get(matched, matched)
        toks.append(Token("OP", text, text, sl, sc, nl, file))
        advance(len(matched))
        nl = False

    toks.append(Token("EOF", "", None, line, col, True, file))
    return toks
