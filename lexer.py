"""Tokenizer for the Lambda language.

The token set is deliberately tiny.  Anything that is not punctuation, a
unicode operator, a lambda/arrow or a string literal is an *atom*; the parser
decides whether an atom is an identifier, a number, a keyword or a primitive.
This keeps names such as ``nth-prime`` and ``str->int`` in one piece.

New in this revision: f64 literals (``1.0``, ``1e-3``), type-annotation
punctuation, list-comprehension/pipeline tokens (``|``, ``∥``, ``←``), range
sugar (``..``) and the ``λp`` parallel-lambda marker.

Comments run from ``;`` (or ``#``) to the end of the line.
"""

from __future__ import annotations

from typing import List

from ast import Loc
from errors import SyntaxErr

UNICODE_OPS = set("⊕⊖⊗⊘⊙≺≡")

PUNCT = set("()[],.:")
LAMBDA_CHARS = set("λ\\")
ARROW = "→"
LARROW = "←"
RANGE = ".."

KEYWORDS = {
    "if", "then", "else", "let", "in", "letrec", "letloop", "include",
}

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "0": "\0",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}


class Token:
    __slots__ = ("kind", "value", "loc")

    def __init__(self, kind: str, value: str, loc: Loc) -> None:
        self.kind = kind
        self.value = value
        self.loc = loc

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Token({self.kind},{self.value!r},{self.loc})"


def _decode_string(raw: str, loc: Loc) -> str:
    """Decode the body of a double-quoted string (without the quotes)."""

    out: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise SyntaxErr("unterminated escape sequence in string", loc)
        esc = raw[i]
        i += 1
        if esc in _ESCAPES:
            out.append(_ESCAPES[esc])
        elif esc == "u":
            if i + 4 > n:
                raise SyntaxErr("truncated \\u escape in string", loc)
            try:
                out.append(chr(int(raw[i:i + 4], 16)))
            except ValueError:
                raise SyntaxErr("invalid \\u escape in string", loc) from None
            i += 4
        elif esc == "x":
            if i + 2 > n:
                raise SyntaxErr("truncated \\x escape in string", loc)
            try:
                out.append(chr(int(raw[i:i + 2], 16)))
            except ValueError:
                raise SyntaxErr("invalid \\x escape in string", loc) from None
            i += 2
        else:
            raise SyntaxErr(f"unknown escape sequence '\\{esc}' in string", loc)
    return "".join(out)


def _scan_number(source: str, start: int):
    """Return (end, is_float) for a numeric literal at *start*, or None."""

    n = len(source)
    j = start
    if j < n and source[j] == "-":
        j += 1
    digits_start = j
    while j < n and source[j].isdigit():
        j += 1
    if j == digits_start:
        return None
    is_float = False
    # Decimal part only if there is a digit after the dot, so 1..5 stays a range.
    if j + 1 < n and source[j] == "." and source[j + 1].isdigit():
        is_float = True
        j += 2
        while j < n and source[j].isdigit():
            j += 1
    # Exponent.
    if j < n and source[j] in "eE":
        k = j + 1
        if k < n and source[k] in "+-":
            k += 1
        if k < n and source[k].isdigit():
            is_float = True
            j = k + 1
            while j < n and source[j].isdigit():
                j += 1
    return j, is_float


def tokenize(source: str, filename: str = "<input>") -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(source)
    line = 1
    col = 1

    def loc() -> Loc:
        return Loc(line, col, filename)

    def advance(text: str) -> None:
        nonlocal line, col
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)

    while i < n:
        ch = source[i]

        if ch.isspace():
            j = i + 1
            while j < n and source[j].isspace():
                j += 1
            advance(source[i:j])
            i = j
            continue

        if ch in ";#":
            j = source.find("\n", i)
            if j < 0:
                j = n
            advance(source[i:j])
            i = j
            continue

        if ch == '"':
            start = loc()
            j = i + 1
            buf: List[str] = []
            closed = False
            while j < n:
                c = source[j]
                if c == '"':
                    closed = True
                    j += 1
                    break
                if c == "\\":
                    if j + 1 >= n:
                        break
                    buf.append(c)
                    buf.append(source[j + 1])
                    j += 2
                    continue
                if c == "\n":
                    raise SyntaxErr("newline in string literal", start)
                buf.append(c)
                j += 1
            if not closed:
                raise SyntaxErr("unterminated string literal", start)
            tokens.append(Token("string", _decode_string("".join(buf), start), start))
            advance(source[i:j])
            i = j
            continue

        # Parallel lambda marker ``λp`` (requires whitespace after the p, so
        # ``λp. p`` is still an ordinary lambda whose parameter is named p).
        if source.startswith("λp", i) and (i + 2 >= n or source[i + 2].isspace()):
            tokens.append(Token("lambda_par", "λp", loc()))
            advance("λp")
            i += 2
            continue

        if ch in LAMBDA_CHARS:
            tokens.append(Token("lambda", ch, loc()))
            advance(ch)
            i += 1
            continue

        if ch == ARROW:
            tokens.append(Token("arrow", ch, loc()))
            advance(ch)
            i += 1
            continue

        # ASCII arrow, but only as a standalone token.  Names such as
        # ``str->int`` are scanned as one atom because the '-' is not at the
        # start of a token there.
        if ch == "-" and i + 1 < n and source[i + 1] == ">":
            tokens.append(Token("arrow", "->", loc()))
            advance("->")
            i += 2
            continue

        if ch == LARROW:
            tokens.append(Token("larrow", ch, loc()))
            advance(ch)
            i += 1
            continue

        if ch == "∥":
            tokens.append(Token("ppipe", ch, loc()))
            advance(ch)
            i += 1
            continue

        if ch == "|":
            tokens.append(Token("pipe", ch, loc()))
            advance(ch)
            i += 1
            continue

        if ch == ".":
            if i + 1 < n and source[i + 1] == ".":
                tokens.append(Token("range", "..", loc()))
                advance("..")
                i += 2
            else:
                tokens.append(Token("punct", ".", loc()))
                advance(".")
                i += 1
            continue

        if ch in PUNCT:
            tokens.append(Token("punct", ch, loc()))
            advance(ch)
            i += 1
            continue

        if ch in UNICODE_OPS:
            tokens.append(Token("op", ch, loc()))
            advance(ch)
            i += 1
            continue

        # Numbers (integers and floats).
        if ch.isdigit() or (ch == "-" and i + 1 < n and source[i + 1].isdigit()):
            scanned = _scan_number(source, i)
            if scanned is not None:
                end, is_float = scanned
                text = source[i:end]
                tokens.append(Token("float" if is_float else "int", text, loc()))
                advance(text)
                i = end
                continue

        # Atoms: identifiers, keywords, ASCII operators.
        start = loc()
        j = i + 1
        while j < n:
            c = source[j]
            if (c.isspace() or c in '()[],.:;"#' or c in LAMBDA_CHARS
                    or c in (ARROW, LARROW, "|", "∥")
                    or c in UNICODE_OPS):
                break
            j += 1
        text = source[i:j]
        if not text:
            raise SyntaxErr(f"unexpected character {ch!r}", start)
        if text in KEYWORDS:
            tokens.append(Token("keyword", text, start))
        elif text == "..":
            tokens.append(Token("range", "..", start))
        else:
            tokens.append(Token("ident", text, start))
        advance(source[i:j])
        i = j

    tokens.append(Token("eof", "<eof>", Loc(line, col, filename)))
    return tokens
