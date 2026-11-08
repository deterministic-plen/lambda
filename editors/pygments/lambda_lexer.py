"""Pygments lexer for the Lambda language.

Usage:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from lambda_lexer import LambdaLexer
    print(highlight(source, LambdaLexer(), HtmlFormatter()))

If this file is on PYTHONPATH, the lexer can also be selected with
``-l lambda`` after registering it in a Pygments plugin.
"""

from __future__ import annotations

import re

from pygments.lexer import RegexLexer, bygroups
from pygments.token import (
    Comment, Keyword, Name, Number, Operator, Punctuation, String, Text,
    Whitespace,
)

KEYWORDS = (
    "if", "then", "else", "let", "letrec", "letloop", "in", "include",
    "enum", "type", "match", "with",
)

BUILTINS = (
    "abs acos arrayb arrayb-clone arrayb-get arrayb-get-ro arrayb-length "
    "arrayb-set arrayf arrayf-clone arrayf-get arrayf-get-ro arrayf-length "
    "arrayf-set arrayf-sum arrayi arrayi-clone arrayi-get arrayi-get-ro "
    "arrayi-length arrayi-sum arrayi-pmap arrayi-pmap-chunk arrayi-set "
    "asin atan atan2 bit-and bit-not bit-or bit-xor ceil cons cos e empty? "
    "exp fix float->int "
    "float->str floor freeze head immortalize int->float int->str ln log10 "
    "log2 map not pi pmap popcount pow print println product range read-file "
    "read-float read-int read-line round shl shr sin sqrt str->float str->int "
    "str->list str-append str-len sum tail tan trunc write-bytes write-f64s "
    "write-i64s Y π ∑ ∏ √"
)

TYPES = ("Int", "Float", "Bool", "String", "ArrayF", "ArrayI", "ArrayB")

OPERATORS = (
    r"\|\||&&|==|<=|>=|\*\*|[-+*/%<>&|!^]|⊕|⊖|⊗|⊘|⊙|≡|≺|≼|≻|≽|∥|→|->|←|<-"
)

SEGMENT = r"[A-Za-z_][A-Za-z0-9_?!<>-]*"
QUALIFIED = SEGMENT + r"(?:::" + SEGMENT + r")+"
WORD = r"(?:" + SEGMENT + r"::)*" + SEGMENT
BOUNDARY_BEFORE = r"(?<![A-Za-z0-9_?!<>-])"
BOUNDARY_AFTER = r"(?![A-Za-z0-9_?!<>-])"


def _alternation(words) -> str:
    if isinstance(words, str):
        words = words.split()
    return "|".join(re.escape(word) for word in words)


class LambdaLexer(RegexLexer):
    name = "Lambda"
    aliases = ["lambda"]
    filenames = ["*.lambda"]
    mimetypes = ["text/x-lambda"]

    tokens = {
        "root": [
            (r";.*$", Comment.Single),
            (r"#.*$", Comment.Single),
            (r'"', String, "string"),
            (r"λp(?=\s)|\\p(?=\s)", Keyword.Reserved),
            (r"λ|\\", Keyword.Reserved),
            (r"→|->", Operator.Word),
            (r"←|<-", Operator.Word),
            (r"[.][.]", Operator),
            (r"(enum)(\s+)(" + WORD + r")",
             bygroups(Keyword, Whitespace, Name.Class)),
            (r"(type)(\s+)(" + WORD + r")",
             bygroups(Keyword, Whitespace, Name.Class)),
            (r"(?<![A-Za-z0-9_?!<>-])-?(?:(?:[0-9]+[.][0-9]+"
             r"(?:[eE][+-]?[0-9]+)?)|(?:[0-9]+[eE][+-]?[0-9]+)|(?:[0-9]+))",
             Number),
            (BOUNDARY_BEFORE + r"(?:" + _alternation(KEYWORDS) + r")"
             + BOUNDARY_AFTER, Keyword),
            (BOUNDARY_BEFORE + r"(?:true|false)" + BOUNDARY_AFTER,
             Keyword.Constant),
            (r"\bnil\b", Keyword.Constant),
            (BOUNDARY_BEFORE + r"(?:" + _alternation(TYPES) + r")"
             + BOUNDARY_AFTER, Keyword.Type),
            (WORD + r"(?=\s*(?:→|->))", Name.Function),
            (BOUNDARY_BEFORE + QUALIFIED + BOUNDARY_AFTER, Name.Namespace),
            (BOUNDARY_BEFORE + r"(?:" + _alternation(BUILTINS) + r")"
             + BOUNDARY_AFTER, Name.Builtin),
            (BOUNDARY_BEFORE + r"[A-Z][A-Za-z0-9_?!<>-]*"
             + BOUNDARY_AFTER, Name.Class),
            (r"&(?=[A-Za-z_])", Keyword.Reserved),
            (OPERATORS, Operator),
            (r"[()]", Punctuation),
            (r"\[|\]", Punctuation),
            (r"[{},.:]", Punctuation),
            (WORD, Name),
            (r"\s+", Whitespace),
            (r".", Text),
        ],
        "string": [
            (r"\\.", String.Escape),
            (r'"', String, "#pop"),
            (r'[^"\\]+', String),
        ],
    }
