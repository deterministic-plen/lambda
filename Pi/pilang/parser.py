"""Pi parser: layout-aware recursive descent.

Statements are separated by newlines (or `;`).  Inside (...) and [...] newlines are
insignificant.  `‖` at the start of a line continues the previous expression.  A `ctrl`
(or `fun`/`program`) body without braces extends over the lines indented deeper than the
introducing keyword.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .errors import Loc, PiSyntaxError
from .lexer import Token, lex
from . import syntax as S

PROCESS_FORMS = (S.Serve, S.ScopeE, S.Select, S.Par, S.ParFor, S.Loop, S.Iterate,
                 S.AfterBlock, S.Timeout, S.Retry, S.Await, S.Block, S.OnError)
ATTR_NAMES = {"rate", "priority", "deadline", "periodic", "sporadic", "aperiodic"}
# words that are lexed as keywords but behave as ordinary call targets
CALL_KW = {"call", "send", "recv", "close", "closed", "cancel", "drop", "slice", "dims",
           "fail", "restart", "escalate", "stop", "resume", "after"}

BIN_PREC = {
    "‖": 1,
    "==": 3, "!=": 3, "<": 3, ">": 3, "≤": 3, "≥": 3,
    "..": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}


class Parser:
    def __init__(self, toks: List[Token], file: str):
        self.toks = toks
        self.i = 0
        self.file = file
        self.depth = 0
        self.warnings: List[str] = []

    # -------------------------------------------------------------- token utils
    def peek(self, k: int = 0) -> Token:
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def next(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def err(self, msg: str, tok: Optional[Token] = None):
        tok = tok or self.peek()
        raise PiSyntaxError(msg, tok.loc())

    def at_op(self, *ops: str, k: int = 0) -> bool:
        return self.peek(k).is_op(*ops)

    def at_kw(self, *kws: str, k: int = 0) -> bool:
        return self.peek(k).is_kw(*kws)

    def at_word(self, *words: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind in ("IDENT", "KW", "CONID") and t.text in words

    def accept_op(self, *ops: str) -> Optional[Token]:
        if self.at_op(*ops):
            return self.next()
        return None

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            self.err(f"expected '{op}', found '{self.peek().text}'")
        return self.next()

    def expect_kw(self, kw: str) -> Token:
        if not self.at_kw(kw):
            self.err(f"expected '{kw}', found '{self.peek().text}'")
        return self.next()

    def name(self) -> str:
        t = self.peek()
        if t.kind in ("IDENT", "KW", "CONID"):
            self.next()
            return t.text
        self.err(f"expected a name, found '{t.text or t.kind}'")

    def stops_at_nl(self, t: Token) -> bool:
        return t.nl and self.depth == 0 and not t.is_op("‖")

    # ------------------------------------------------------------------ types
    def parse_type(self) -> S.TypeExpr:
        t = self.peek()
        loc = t.loc()
        if t.is_op("("):
            self.next()
            self.depth += 1
            if self.accept_op(")"):
                self.depth -= 1
                return S.TypeExpr("unit", [], loc=loc)
            items = [self.parse_type()]
            while self.accept_op(","):
                items.append(self.parse_type())
            self.expect_op(")")
            self.depth -= 1
            if len(items) == 1:
                return items[0]
            return S.TypeExpr("tuple", items, loc=loc)
        if t.is_op("["):
            self.next()
            self.depth += 1
            items: List[Any] = []
            if not self.at_op("]"):
                items.append(self.parse_type_arg())
                while self.accept_op(","):
                    items.append(self.parse_type_arg())
            self.expect_op("]")
            self.depth -= 1
            if all(isinstance(x, S.TypeExpr) and x.name == "int" for x in items):
                return S.TypeExpr("dims", [x.args[0] for x in items], loc=loc)
            return S.TypeExpr("list", items, loc=loc)
        if t.kind == "INT":
            self.next()
            return S.TypeExpr("int", [t.value], loc=loc)
        if t.kind in ("IDENT", "CONID", "KW"):
            self.next()
            args: List[Any] = []
            if self.at_op("⟨", "<", "[") and not self.peek().nl:
                closer = {"⟨": "⟩", "<": ">", "[": "]"}[self.next().text]
                self.depth += 1
                if not self.at_op(closer):
                    args.append(self.parse_type_arg())
                    while self.accept_op(","):
                        args.append(self.parse_type_arg())
                self.expect_op(closer)
                self.depth -= 1
            return S.TypeExpr(t.text, args, loc=loc)
        self.err(f"expected a type, found '{t.text or t.kind}'")

    def parse_type_arg(self) -> Any:
        return self.parse_type()

    @staticmethod
    def type_arg_values(args: List[Any]) -> List[Any]:
        out: List[Any] = []
        for a in args:
            if isinstance(a, S.TypeExpr) and a.name == "int":
                out.append(a.args[0])
            elif isinstance(a, S.TypeExpr) and not a.args:
                out.append(a.name)
            else:
                out.append(a)
        return out

    # ------------------------------------------------------------ expressions
    def parse_expr(self, min_prec: int = 0) -> S.Node:
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t.kind != "OP":
                break
            op = t.text
            if op not in BIN_PREC or self.stops_at_nl(t):
                break
            prec = BIN_PREC[op]
            if prec < min_prec:
                break
            self.next()
            right = self.parse_expr(prec + 1)
            loc = t.loc()
            if op == "‖":
                branches: List[S.Node] = []
                for side in (left, right):
                    if isinstance(side, S.Par) and getattr(side, "_infix", False):
                        branches.extend(side.branches)
                    else:
                        branches.append(side)
                left = S.Par(branches, loc=loc)
                left._infix = True  # type: ignore[attr-defined]
            elif op == "+" and self._is_process(left) and self._is_process(right):
                left = S.Race(left, right, loc=loc)
            elif op == "..":
                left = S.RangeE(left, right, loc=loc)
            else:
                left = S.BinOp(op, left, right, loc=loc)
        return left

    @staticmethod
    def _is_process(n: S.Node) -> bool:
        if isinstance(n, PROCESS_FORMS):
            return True
        if isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id in ("call",):
            return True
        return False

    def parse_unary(self) -> S.Node:
        t = self.peek()
        if t.is_op("-") and not self.stops_at_nl(t):
            self.next()
            return S.UnOp("-", self.parse_unary(), loc=t.loc())
        if t.is_kw("await"):
            self.next()
            return S.Await(self.parse_unary(), loc=t.loc())
        return self.parse_postfix(self.parse_primary())

    def parse_postfix(self, e: S.Node) -> S.Node:
        while True:
            t = self.peek()
            if t.kind == "EOF" or self.stops_at_nl(t):
                break
            if t.is_op("("):
                args, kwargs = self.parse_call_args()
                e = S.Call(e, args, kwargs, loc=t.loc())
            elif t.is_op("."):
                self.next()
                e = S.Member(e, self.name(), loc=t.loc())
            elif t.is_op("["):
                self.next()
                self.depth += 1
                items = [self.parse_expr()]
                while self.accept_op(","):
                    items.append(self.parse_expr())
                self.expect_op("]")
                self.depth -= 1
                idx = items[0] if len(items) == 1 else S.TupleE(items, loc=t.loc())
                e = S.Index(e, idx, loc=t.loc())
            elif t.is_op("@") and self.peek(1).kind == "IDENT" and self.peek(1).text not in ATTR_NAMES:
                self.next()
                e = S.Annot(e, self.next().text, loc=t.loc())
            else:
                break
        return e

    def parse_call_args(self) -> Tuple[List[S.Node], Dict[str, S.Node]]:
        self.expect_op("(")
        self.depth += 1
        args: List[S.Node] = []
        kwargs: Dict[str, S.Node] = {}
        while not self.at_op(")"):
            t, t1 = self.peek(), self.peek(1)
            if t.kind in ("IDENT", "KW", "CONID") and t1.is_op("=") and not self.peek(2).is_op("="):
                self.next()
                self.next()
                kwargs[t.text] = self.parse_expr()
            else:
                args.append(self.parse_expr())
            if not self.accept_op(","):
                break
        self.expect_op(")")
        self.depth -= 1
        return args, kwargs

    def parse_primary(self) -> S.Node:
        t = self.peek()
        loc = t.loc()
        if t.kind == "INT":
            self.next()
            return S.Lit(t.value, "int", loc=loc)
        if t.kind == "FLOAT":
            self.next()
            return S.Lit(t.value, "float", loc=loc)
        if t.kind == "DURATION":
            self.next()
            return S.Lit(t.value, "duration", loc=loc)
        if t.kind == "STRING":
            self.next()
            return S.Lit(t.value, "string", loc=loc)
        if t.is_kw("true", "false"):
            self.next()
            return S.Lit(t.text == "true", "bool", loc=loc)
        if t.is_op("("):
            self.next()
            self.depth += 1
            if self.accept_op(")"):
                self.depth -= 1
                return S.Lit(None, "unit", loc=loc)
            items = [self.parse_expr()]
            tup = False
            while self.accept_op(","):
                tup = True
                if self.at_op(")"):
                    break
                items.append(self.parse_expr())
            self.expect_op(")")
            self.depth -= 1
            return S.TupleE(items, loc=loc) if tup else items[0]
        if t.is_op("{") and self.peek(1).kind in ("IDENT", "KW", "CONID") and self.peek(2).is_op(":"):
            # record literal  {cpus: 1, memory: "4GiB"}  -- lowered to record(cpus = 1, ...)
            self.next()
            self.depth += 1
            kw: Dict[str, S.Node] = {}
            while not self.at_op("}"):
                k = self.name()
                self.expect_op(":")
                kw[k] = self.parse_expr()
                if not self.accept_op(","):
                    break
            self.expect_op("}")
            self.depth -= 1
            return S.Call(S.Name("record", loc=loc), [], kw, loc=loc)
        if t.is_op("{"):
            return self.parse_brace_block()          # a bare block groups statements: { a; b }
        if t.is_op("["):
            self.next()
            self.depth += 1
            items = []
            while not self.at_op("]"):
                items.append(self.parse_expr())
                if not self.accept_op(","):
                    break
            self.expect_op("]")
            self.depth -= 1
            return S.ListE(items, loc=loc)
        if t.kind == "KW":
            w = t.text
            if w == "scope":
                self.next()
                return S.ScopeE(self.parse_brace_block(), loc=loc)
            if w == "par":
                return self.parse_par()
            if w == "serve":
                return self.parse_serve()
            if w == "loop":
                self.next()
                return S.Loop(self.parse_brace_block(), loc=loc)
            if w == "iterate":
                self.next()
                args, _ = self.parse_call_args()
                if len(args) != 1:
                    self.err("iterate takes one argument", t)
                return S.Iterate(args[0], self.parse_brace_block(), loc=loc)
            if w == "select":
                return self.parse_select()
            if w == "on_error":
                return self.parse_on_error()
            if w == "if":
                return self.parse_if()
            if w == "for":
                return self.parse_for()
            if w == "after":
                self.next()
                args, _ = self.parse_call_args()
                if self.at_op("{") and not self.peek().nl:
                    return S.AfterBlock(args[0], self.parse_brace_block(), loc=loc)
                return S.Call(S.Name("after", loc=loc), args, {}, loc=loc)
            if w in CALL_KW:
                self.next()
                return S.Name(w, loc=loc)
            self.err(f"unexpected keyword '{w}'")
        if t.kind in ("IDENT", "CONID"):
            self.next()
            if t.text in ("timeout", "retry") and self.at_op("(") and t.kind == "IDENT":
                args, kwargs = self.parse_call_args()
                body = self.parse_brace_block()
                if t.text == "timeout":
                    return S.Timeout(args[0], body, loc=loc)
                return S.Retry(kwargs, body, loc=loc)
            return S.Name(t.text, loc=loc)
        self.err(f"unexpected '{t.text or t.kind}'")

    # ------------------------------------------------------------- compound forms
    def parse_brace_block(self) -> S.Block:
        loc = self.expect_op("{").loc()
        saved, self.depth = self.depth, 0
        stmts = self.parse_stmts(lambda t: t.is_op("}"))
        self.expect_op("}")
        self.depth = saved
        return S.Block(stmts, loc=loc)

    def parse_par(self) -> S.Node:
        t = self.next()
        if self.at_kw("for"):
            self.next()
            pat = self.parse_pattern()
            self.expect_kw("in")
            it = self.parse_expr()
            return S.ParFor(pat, it, self.parse_brace_block(), loc=t.loc())
        block = self.parse_brace_block()
        branches: List[S.Node] = []
        for st in block.stmts:
            if isinstance(st, S.Par) and getattr(st, "_infix", False):
                branches.extend(st.branches)
            else:
                branches.append(st)
        return S.Par(branches, loc=t.loc())

    def parse_serve(self) -> S.Node:
        t = self.next()
        self.expect_op("(")
        self.depth += 1
        chan = self.parse_expr()
        self.expect_op(")")
        self.depth -= 1
        attrs: List[S.Attr] = []
        while self.at_op("@") and self.peek(1).text in ATTR_NAMES:
            at = self.next()
            an = self.next().text
            val = None
            if self.at_op("("):
                self.next()
                self.depth += 1
                lit = self.parse_expr()
                self.expect_op(")")
                self.depth -= 1
                if not isinstance(lit, S.Lit):
                    self.err(f"@{an} takes a literal", at)
                val = lit.value
            attrs.append(S.Attr(an, val, loc=at.loc()))
        loc = self.expect_op("{").loc()
        saved, self.depth = self.depth, 0
        pat = self.parse_pattern()
        self.expect_op("⇒")
        stmts = self.parse_stmts(lambda tk: tk.is_op("}"))
        self.expect_op("}")
        self.depth = saved
        return S.Serve(chan, attrs, pat, S.Block(stmts, loc=loc), loc=t.loc())

    def parse_arm_body(self) -> S.Node:
        if self.at_op("{"):
            return self.parse_brace_block()
        loc = self.peek().loc()
        stmts = self.parse_stmts(lambda tk: tk.is_op("|", "}"))
        if len(stmts) == 1:
            return stmts[0]
        return S.Block(stmts, loc=loc)

    def parse_select(self) -> S.Node:
        t = self.next()
        self.expect_op("{")
        saved, self.depth = self.depth, 0
        arms: List[S.SelectArm] = []
        while not self.at_op("}"):
            self.accept_op("|")
            at = self.peek()
            guard = self.parse_expr()
            self.expect_op("⇒")
            arms.append(S.SelectArm(guard, self.parse_arm_body(), loc=at.loc()))
        self.expect_op("}")
        self.depth = saved
        return S.Select(arms, loc=t.loc())

    def parse_handler_arms(self) -> List[S.HandlerArm]:
        self.expect_op("{")
        saved, self.depth = self.depth, 0
        arms: List[S.HandlerArm] = []
        while not self.at_op("}"):
            self.accept_op("|")
            at = self.peek()
            pat = self.expr_to_pattern(self.parse_expr())
            self.expect_op("⇒")
            arms.append(S.HandlerArm(pat, self.parse_arm_body(), loc=at.loc()))
        self.expect_op("}")
        self.depth = saved
        return arms

    def parse_on_error(self) -> S.Node:
        t = self.next()
        self.expect_op("(")
        self.depth += 1
        body = self.parse_expr()
        self.expect_op(")")
        self.depth -= 1
        return S.OnError(body, self.parse_handler_arms(), loc=t.loc())

    def parse_if(self) -> S.Node:
        t = self.next()
        cond = self.parse_expr()
        if self.at_kw("then"):
            self.next()
            then = self.parse_expr()
            orelse = None
            if self.at_kw("else"):
                self.next()
                orelse = self.parse_expr()
            return S.If(cond, then, orelse, loc=t.loc())
        then = self.parse_brace_block()
        orelse = None
        if self.at_kw("else"):
            self.next()
            orelse = self.parse_if() if self.at_kw("if") else self.parse_brace_block()
        return S.If(cond, then, orelse, loc=t.loc())

    def parse_for(self) -> S.Node:
        t = self.next()
        pat = self.parse_pattern()
        self.expect_kw("in")
        it = self.parse_expr()
        return S.For(pat, it, self.parse_brace_block(), loc=t.loc())

    # ---------------------------------------------------------------- patterns
    def parse_pattern(self) -> S.Node:
        t = self.peek()
        loc = t.loc()
        if t.is_op("("):
            self.next()
            items = []
            while not self.at_op(")"):
                items.append(self.parse_pattern())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            return S.PTuple(items, loc=loc)
        if t.kind == "IDENT":
            self.next()
            return S.PWild(loc=loc) if t.text == "_" else S.PName(t.text, loc=loc)
        if t.kind in ("INT", "STRING", "FLOAT"):
            self.next()
            return S.Lit(t.value, t.kind.lower(), loc=loc)
        self.err(f"expected a pattern, found '{t.text or t.kind}'")

    def expr_to_pattern(self, e: S.Node) -> S.Node:
        if isinstance(e, S.Name):
            if e.id == "_":
                return S.PWild(loc=e.loc)
            if e.id[0].isupper():
                return S.PCtor(e.id, [], loc=e.loc)
            return S.PName(e.id, loc=e.loc)
        if isinstance(e, S.Call) and isinstance(e.fn, S.Name):
            return S.PCtor(e.fn.id, [self.expr_to_pattern(a) for a in e.args], loc=e.loc)
        if isinstance(e, S.Lit):
            return e
        if isinstance(e, S.TupleE):
            return S.PTuple([self.expr_to_pattern(a) for a in e.items], loc=e.loc)
        self.err("invalid handler pattern", self.peek())

    # -------------------------------------------------------------- statements
    def _bind_ahead(self) -> bool:
        t = self.peek()
        if t.kind == "IDENT" and (self.peek(1).is_op("→") or self.peek(1).is_op(":=")):
            return True
        if t.is_op("("):
            j, d = self.i, 0
            while j < len(self.toks):
                tk = self.toks[j]
                if tk.is_op("("):
                    d += 1
                elif tk.is_op(")"):
                    d -= 1
                    if d == 0:
                        return self.toks[j + 1].is_op("→")
                elif tk.kind == "EOF":
                    return False
                j += 1
        return False

    def parse_stmt(self) -> S.Node:
        t = self.peek()
        loc = t.loc()
        if self._bind_ahead():
            if t.kind == "IDENT" and self.peek(1).is_op(":="):
                self.next()
                self.next()
                return S.Assign(t.text, self.parse_expr(), loc=loc)
            pat = self.parse_pattern()
            self.expect_op("→")
            return S.Bind(pat, self.parse_expr(), loc=loc)
        return self.parse_expr()

    def parse_stmts(self, stop) -> List[S.Node]:
        stmts: List[S.Node] = []
        while True:
            while self.accept_op(";"):
                pass
            t = self.peek()
            if t.kind == "EOF" or stop(t):
                break
            stmts.append(self.parse_stmt())
            t = self.peek()
            if t.kind == "EOF" or stop(t) or t.is_op(";") or t.nl:
                continue
            self.err(f"unexpected '{t.text}' after statement")
        return stmts

    def parse_indented_body(self, anchor: Token) -> S.Node:
        """Body of `ctrl x =`, `fun f() =`, `program P() =`: the rest of the line plus every
        following line indented deeper than the introducing keyword."""
        loc = self.peek().loc()
        saved, self.depth = self.depth, 0
        stmts = self.parse_stmts(lambda t: (t.nl and t.col <= anchor.col) or t.is_op("}"))
        self.depth = saved
        if len(stmts) == 1:
            return stmts[0]
        return S.Block(stmts, loc=loc)

    # ------------------------------------------------------------ declarations
    def parse_module(self) -> S.Module:
        mod = S.Module([], file=self.file)
        while self.peek().kind != "EOF":
            t = self.peek()
            if t.is_kw("import"):
                self.next()
                s = self.next()
                if s.kind != "STRING":
                    self.err("import expects a string", s)
                mod.imports.append(S.Import(s.value, loc=t.loc()))
            elif t.is_kw("protocol"):
                p = self.parse_protocol()
                self._add(mod.protocols, p.name, p)
            elif t.is_kw("elem"):
                e = self.parse_elem()
                self._add(mod.elems, e.name, e)
            elif t.is_kw("system"):
                s = self.parse_system()
                self._add(mod.systems, s.name, s)
            elif t.kind == "IDENT" and t.text == "fun":
                f = self.parse_fun()
                self._add(mod.funs, f.name, f)
            elif t.is_kw("program", "main"):
                pr = self.parse_program()
                self._add(mod.programs, pr.name, pr)
            else:
                self.err(f"unexpected '{t.text}' at top level")
        mod.warnings = self.warnings
        return mod

    def _add(self, table: Dict[str, Any], name: str, node: Any):
        if name in table:
            raise PiSyntaxError(f"duplicate declaration '{name}'", node.loc)
        table[name] = node

    def parse_tparams(self) -> List[str]:
        out: List[str] = []
        if self.at_op("⟨", "<") and not self.peek().nl:
            closer = "⟩" if self.next().text == "⟨" else ">"
            while not self.at_op(closer):
                out.append(self.name())
                if not self.accept_op(","):
                    break
            self.expect_op(closer)
        return out

    def parse_vparams(self) -> List[Tuple[str, Optional[S.TypeExpr]]]:
        self.expect_op("(")
        self.depth += 1
        out: List[Tuple[str, Optional[S.TypeExpr]]] = []
        while not self.at_op(")"):
            n = self.name()
            ty = None
            if self.accept_op(":"):
                ty = self.parse_type()
            out.append((n, ty))
            if not self.accept_op(","):
                break
        self.expect_op(")")
        self.depth -= 1
        return out

    def parse_protocol(self) -> S.Protocol:
        t = self.next()
        name = self.name()
        tparams = self.parse_tparams()
        self.expect_op("{")
        saved, self.depth = self.depth, 0
        ops: List[S.OpDecl] = []
        errors: Dict[int, S.ErrorDecl] = {}
        has_errors = False
        purity = "Pure"
        while not self.at_op("}"):
            k = self.peek()
            if k.is_kw("op"):
                self.next()
                oname = self.name()
                self.expect_op(":")
                self.expect_op("(")
                self.depth += 1
                args: List[S.TypeExpr] = []
                while not self.at_op(")"):
                    args.append(self.parse_type())
                    if not self.accept_op(","):
                        break
                self.expect_op(")")
                self.depth -= 1
                self.expect_op("→")
                ret = self.parse_type()
                ops.append(S.OpDecl(oname, args, ret, loc=k.loc()))
            elif k.text == "errors":
                self.next()
                has_errors = True
                self.expect_op(":")
                self.expect_op("{")
                self.depth += 1
                while not self.at_op("}"):
                    c = self.next()
                    if c.kind != "INT":
                        self.err("error code must be an integer", c)
                    self.expect_op(":")
                    s = self.next()
                    if s.kind != "STRING":
                        self.err("error name must be a string", s)
                    klass = None
                    if self.peek().kind in ("IDENT",) and self.peek().text in ("recoverable", "transient", "terminal"):
                        klass = self.next().text
                    if c.value in errors:
                        raise PiSyntaxError(f"duplicate error code {c.value}", c.loc())
                    errors[c.value] = S.ErrorDecl(c.value, s.value, klass, loc=c.loc())
                    if not self.accept_op(","):
                        break
                self.expect_op("}")
                self.depth -= 1
            elif k.is_kw("purity"):
                self.next()
                self.expect_op(":")
                purity = self.name()
            else:
                self.err(f"unexpected '{k.text}' in protocol", k)
        self.expect_op("}")
        self.depth = saved
        canon = f"protocol {name}<{','.join(tparams)}>;" + ";".join(
            f"op {o.name}:({','.join(a.show() for a in o.args)})->{o.ret.show()}" for o in ops
        ) + ";errors:" + ",".join(f"{c}={e.name}" for c, e in sorted(errors.items())) + f";purity:{purity}"
        return S.Protocol(name, tparams, ops, errors, has_errors, purity, canon, loc=t.loc())

    def parse_elem(self) -> S.Elem:
        t = self.next()
        name = self.name()
        tparams = self.parse_tparams()
        vparams = self.parse_vparams() if self.at_op("(") else []
        self.expect_op("{")
        saved, self.depth = self.depth, 0
        e = S.Elem(name, tparams, vparams, [], [], [], [], {}, None, loc=t.loc())
        while not self.at_op("}"):
            k = self.peek()
            if k.is_kw("in") or (k.kind == "IDENT" and k.text == "out"):
                self.next()
                pn = self.name()
                self.expect_op(":")
                e.ports.append(S.Port(k.text, pn, self.parse_type(), loc=k.loc()))
            elif k.is_kw("worker"):
                self.next()
                wn = self.name()
                self.expect_op(":")
                ty = self.parse_type()
                if ty.name != "Worker" or len(ty.args) < 1 or not isinstance(ty.args[0], S.TypeExpr):
                    self.err("a worker field must have type Worker⟨Protocol⟩", k)
                proto = ty.args[0]
                e.workers.append(S.WorkerDecl(wn, proto.name, self.type_arg_values(proto.args),
                                              loc=k.loc()))
            elif k.is_kw("state"):
                self.next()
                sn = self.name()
                self.expect_op(":")
                e.states.append(S.StateDecl(sn, self.parse_type(), loc=k.loc()))
            elif k.is_kw("ctrl"):
                self.next()
                cn = self.name()
                self.expect_op("=")
                e.ctrls.append(S.Ctrl(cn, self.parse_indented_body(k), loc=k.loc()))
            elif k.is_kw("contract"):
                self.next()
                e.contract = self.parse_contract()
            elif k.kind in ("IDENT", "KW") and k.text == "on_failure":
                self.next()
                if self.at_op("{"):
                    e.on_failure = self.parse_handler_arms()
                else:
                    act = self.parse_expr()
                    e.on_failure = [S.HandlerArm(S.PWild(loc=k.loc()), act, loc=k.loc())]
            else:
                self.err(f"unexpected '{k.text}' in elem", k)
        self.expect_op("}")
        self.depth = saved
        return e

    def parse_contract(self) -> Dict[str, str]:
        self.expect_op("{")
        out: Dict[str, str] = {}
        lines: List[List[Token]] = []
        depth = 1
        while True:
            t = self.next()
            if t.kind == "EOF":
                self.err("unterminated contract")
            if t.is_op("{"):
                depth += 1
            elif t.is_op("}"):
                depth -= 1
                if depth == 0:
                    break
            if t.nl or not lines:
                lines.append([])
            lines[-1].append(t)
        for ln in lines:
            key = ln[0].text
            out[key] = " ".join(x.text for x in ln[1:])
        return out

    def parse_system(self) -> S.System:
        t = self.next()
        name = self.name()
        vparams = self.parse_vparams() if self.at_op("(") else []
        self.expect_op("{")
        saved, self.depth = self.depth, 0
        sysd = S.System(name, vparams, [], [], [], {}, loc=t.loc())
        while not self.at_op("}"):
            k = self.peek()
            sec = k.text
            self.next()
            if sec == "expose" and not self.at_op("{"):
                # inline form used by the spec's examples:  expose step = <...>
                en = self.name()
                self.expect_op("=")
                sysd.expose[en] = self.parse_expose_body()
                continue
            self.expect_op("{")
            if sec == "ports":
                while not self.at_op("}"):
                    pn = self.name()
                    self.expect_op(":")
                    sysd.ports.append(S.Port("in", pn, self.parse_type(), loc=k.loc()))
            elif sec == "instances":
                while not self.at_op("}"):
                    sysd.instances.append(self.parse_instance())
            elif sec == "couple":
                while not self.at_op("}"):
                    sysd.couple.append(self.parse_couple_entry())
            elif sec == "expose":
                while not self.at_op("}"):
                    en = self.name()
                    self.expect_op("=")
                    sysd.expose[en] = self.parse_expose_body()
            else:
                self.err(f"unexpected section '{sec}' in system", k)
            self.expect_op("}")
        self.expect_op("}")
        self.depth = saved
        return sysd

    def parse_instance(self) -> S.InstanceDecl:
        t = self.peek()
        name = self.name()
        idx: List[str] = []
        if self.at_op("["):
            self.next()
            while not self.at_op("]"):
                idx.append(self.name())
                if not self.accept_op(","):
                    break
            self.expect_op("]")
        self.expect_op(":")
        elem = self.name()
        targs: List[Any] = []
        if self.at_op("⟨", "<"):
            closer = "⟩" if self.next().text == "⟨" else ">"
            self.depth += 1
            while not self.at_op(closer):
                targs.append(self.parse_type_arg())
                if not self.accept_op(","):
                    break
            self.expect_op(closer)
            self.depth -= 1
            targs = self.type_arg_values(targs)
        args: List[S.Node] = []
        if self.at_op("(") and not self.peek().nl:
            args, _ = self.parse_call_args()
        bindings: List[Tuple[str, S.Node]] = []
        if self.at_kw("for") and not self.peek().nl:
            self.next()
            while True:
                v = self.name()
                self.expect_kw("in")
                bindings.append((v, self.parse_expr()))
                if self.at_op(",") and self.peek(1).kind == "IDENT" and self.peek(2).is_kw("in"):
                    self.next()
                    continue
                break
        using: Dict[str, str] = {}
        if self.peek().kind == "IDENT" and self.peek().text == "using" and not self.peek().nl:
            self.next()
            self.expect_op("{")
            while not self.at_op("}"):
                wn = self.name()
                self.expect_op(":")
                s = self.next()
                using[wn] = s.value
                if not self.accept_op(","):
                    break
            self.expect_op("}")
        return S.InstanceDecl(name, idx, elem, targs, args, bindings, using, loc=t.loc())

    def parse_couple_entry(self) -> S.Node:
        t = self.peek()
        left = self.parse_expr()
        if self.at_op("≡"):
            self.next()
            right = self.parse_expr()
            guard = None
            if self.at_kw("if"):
                self.next()
                guard = self.parse_expr()
            return S.Equation(left, right, guard, loc=t.loc())
        return left

    def parse_expose_body(self) -> S.Node:
        t = self.peek()
        if t.is_op("<"):
            self.next()
            words: List[str] = []
            while not self.at_op(">"):
                if self.peek().kind == "EOF":
                    self.err("unterminated expose description")
                words.append(self.next().text)
            self.next()
            if "every" not in words:
                self.err("expected '<push () into every <instances>'s <port> channel>'", t)
            k = words.index("every")
            fam_word = words[k + 1]
            fam = fam_word.split("'")[0]
            # "w's tick channel" lexes as one word "w's"; "w 's tick" would put "s" next
            port = words[k + 2] if fam_word.endswith("'s") or words[k + 2] != "s" else words[k + 3]
            return S.Call(S.Name("push", loc=t.loc()),
                          [S.Member(S.Name(fam, loc=t.loc()), port, loc=t.loc())], {}, loc=t.loc())
        return self.parse_expr()

    def parse_fun(self) -> S.Fun:
        t = self.next()
        name = self.name()
        params = [p for p, _ in self.parse_vparams()]
        self.expect_op("=")
        return S.Fun(name, params, self.parse_indented_body(t), loc=t.loc())

    def parse_program(self) -> S.ProgramDecl:
        t = self.next()
        name = self.name() if not self.at_op("(") else "Main"
        params = self.parse_vparams() if self.at_op("(") else []
        self.expect_op("=")
        return S.ProgramDecl(name, params, self.parse_indented_body(t), loc=t.loc())


def parse(src: str, file: str = "<input>") -> S.Module:
    return Parser(lex(src, file), file).parse_module()


def load_module(path: str, _seen: Optional[set] = None) -> S.Module:
    """Parse a .pi file and merge its imports (cycle-safe, import-once)."""
    _seen = _seen if _seen is not None else set()
    full = os.path.abspath(path)
    if full in _seen:
        return S.Module([], file=full)
    _seen.add(full)
    with open(full, encoding="utf-8") as f:
        mod = parse(f.read(), full)
    for imp in mod.imports:
        sub = load_module(os.path.join(os.path.dirname(full), imp.path), _seen)
        for tbl in ("protocols", "elems", "systems", "funs", "programs"):
            for k, v in getattr(sub, tbl).items():
                if k in getattr(mod, tbl):
                    raise PiSyntaxError(f"'{k}' imported from {imp.path} is already defined", imp.loc)
                getattr(mod, tbl)[k] = v
        mod.warnings.extend(sub.warnings)
    return mod
