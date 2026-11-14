"""Pygments lexer for Pi (spec v2.0).

    from pi_lexer import PiLexer
    highlight(code, PiLexer(), HtmlFormatter())
"""

from pygments.lexer import RegexLexer, bygroups, include
from pygments.token import (
    Comment, Keyword, Name, Number, Operator, Punctuation, String, Whitespace,
)

KEYWORDS = (
    "if|then|else|match|with|scope|par|serve|loop|iterate|for|in|"
    "select|call|await|cancel|send|recv|close|closed|after|fail|"
    "on_error|restart|escalate|stop|resume|slice|dims|drop|"
    "instances|couple|expose|ctrl|state|worker|op|purity|contract|main"
)
DECLARATIONS = "protocol|elem|system|program|module|include|import|type"
HANDLE_TYPES = "Chan|Future|Worker|Pool|Tensor|Shard|Config|Err|Result|HaloRing"
PRIM_TYPES = "Float|Int|Bool|UInt8|UInt16|UInt32|UInt64|String|Unit"
BUILTINS = (
    "emit|emit_animation_frame|scalar|emit_series|emit_grid|emit_text|payload|allreduce|split|zip|range|"
    "len|empty|record|Params|Pool|kill_worker"
)
ERR_CTORS = (
    "Ok|Err|WorkerDied|Deadline|Transport|Rejected|BadFrame|WorkerError|"
    "Cancelled|NoQuorum|Incomplete|Unsupported|Renormalize|Report|Halt"
)


class PiLexer(RegexLexer):
    name = "Pi"
    aliases = ["pi"]
    filenames = ["*.pi"]
    mimetypes = ["text/x-pi"]

    tokens = {
        "root": [
            (r"\s+", Whitespace),
            (r"--.*$", Comment.Single),
            (r"\{-", Comment.Multiline, "block-comment"),
            (r'"(?:[^"\\]|\\.)*"', String),
            (r"@(rate|priority|deadline|periodic|sporadic|aperiodic)\b",
             Keyword.Reserved),
            (r"\b\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)\b", Number),
            (r"\b\d+(?:\.\d+)?\b", Number),
            (r"\b(%s)\s+([A-Z][A-Za-z0-9_']*)" % DECLARATIONS,
             bygroups(Keyword.Declaration, Name.Class)),
            (r"\b(%s)\b" % DECLARATIONS, Keyword.Declaration),
            (r"\b(%s)\b" % KEYWORDS, Keyword.Control),
            (r"\b(%s)\b" % HANDLE_TYPES, Name.Builtin),
            (r"\b(%s)\b" % PRIM_TYPES, Keyword.Type),
            (r"\b(%s)\b" % BUILTINS, Name.Builtin),
            (r"\b(%s)\b" % ERR_CTORS, Name.Exception),
            (r"\b[A-Z][A-Za-z0-9_']*\b", Name.Class),
            (r"\b[a-z][A-Za-z0-9_'-]*(?=\s*(?:→|->|:=))", Name.Function),
            (r"\b[a-z][A-Za-z0-9_'-]*\b", Name),
            (r"_", Name.Variable),
            (r"(?:→|←|⇒|‖|≡|≤|≥|:=|->|=>|\|\||==|!=|<=|>=|\.\.|[+*/%<>=|&!?@])",
             Operator),
            (r"[(){}\[\],:;.⟨⟩]", Punctuation),
            (r"-", Operator),
        ],
        "block-comment": [
            (r"[^{-]+", Comment.Multiline),
            (r"\{-", Comment.Multiline, "#push"),
            (r"-\}", Comment.Multiline, "#pop"),
            (r"[-{}]", Comment.Multiline),
        ],
    }
