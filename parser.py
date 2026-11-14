"""Recursive-descent parser for the Lambda language.

Grammar (after desugaring ``let``)::

    program   := block EOF
    block     := definition* expr
    definition:= IDENT '→' atom
    expr      := 'if' expr 'then' expr 'else' expr
               | 'λ' IDENT+ '.' expr
               | 'let' IDENT '=' expr 'in' expr          (sugar)
               | block                                   (if definitions follow)
               | application
    application := atom+
    atom      := '(' expr ')' | '[' expr ... ']' | INT | STRING
               | 'true' | 'false' | IDENT | 'λ' ... | 'if' ... | 'let' ...

Every function binding uses the same ``name → value`` notation, whether or
not it is recursive.  ``let`` is the only recursive-binding form.
"""

from __future__ import annotations

import sys
import math
from typing import List, Optional, Tuple

from lambda_ast import (
    App, Block, Borrow, DataCtor, DataDecl, DataField, DataVariant, Def,
    EnumLit, If, Lam, Lit, Loc, Match, Node, RecordField, RecordLit, SumLit, Var,
)
from errors import SyntaxErr
from lexer import Token, tokenize

# Primitive names understood by the compiler.  Both the unicode operators and
# their ASCII aliases are accepted.
PRIMITIVES = {
    # numeric operations (Int or Float, homogeneous)
    "+", "-", "*", "/", "%", "==", "<", "<=", ">", ">=",
    "⊕", "⊖", "⊗", "⊘", "⊙", "≡", "≺", "≼", "≻", "≽",
    # lists
    "cons", "head", "tail", "empty?", "nil",
    # flat arrays (consume-on-write)
    "arrayf", "arrayi", "arrayb",
    "arrayf-of", "arrayi-of", "arrayb-of",
    "arrayf-get", "arrayf-set", "arrayf-length", "arrayf-clone",
    "arrayi-get", "arrayi-set", "arrayi-length", "arrayi-clone",
    "arrayb-get", "arrayb-set", "arrayb-length", "arrayb-clone",
    "arrayi-sum", "arrayf-sum",
    "arrayf-get-ro", "arrayi-get-ro", "arrayb-get-ro",
    "arrayi-pmap", "arrayi-pmap-chunk", "arrayf-pmap", "arrayf-gray-scott", "arrayf-gray-stripe",
    # binary data stream (requires --data)
    "write-bytes", "write-f64s", "write-i64s",
    # strings
    "str->int", "int->str", "str->list", "str-len", "str-append",
    "str->arrayb", "arrayb->str",
    "read-file", "write-file",
    # process arguments
    "argc", "argv",
    # io / misc
    "print", "println", "not", "and", "or",
    # fixed-point combinator (call-by-value)
    "fix",
    # float conversions and mathematics
    "int->float", "float->int", "str->float", "float->str",
    "√", "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "exp", "ln", "log10", "log2", "floor", "ceil", "round", "trunc",
    "pow", "**", "abs", "π", "pi", "e",
    "∑", "sum", "∏", "product",
    # bit-level integer operations
    "&", "|", "!", "bit-and", "bit-or", "bit-xor", "bit-not",
    "shl", "shr", "popcount",
    # lists and parallelism
    "map", "pmap", "range", "immortalize",
    # optional terminal input (requires --input)
    "read-line", "read-int", "read-float",
}

# Canonical operation for each spelling.  Code generation and the type checker
# share this table so aliases cannot drift apart.
OP_CANON = {
    "+": "add", "⊕": "add",
    "-": "sub", "⊖": "sub",
    "*": "mul", "⊗": "mul",
    "/": "div", "⊘": "div",
    "%": "mod", "⊙": "mod",
    "==": "eq", "≡": "eq",
    "<": "lt", "≺": "lt",
    "<=": "le", "≼": "le",
    ">": "gt", "≻": "gt",
    ">=": "ge", "≽": "ge",
    "cons": "cons", "head": "head", "tail": "tail", "empty?": "empty",
    "nil": "nil",
    "arrayf": "arrayf", "arrayi": "arrayi", "arrayb": "arrayb",
    "arrayf-of": "arrayf_of", "arrayi-of": "arrayi_of", "arrayb-of": "arrayb_of",
    "arrayf-get": "arrayf_get", "arrayf-set": "arrayf_set",
    "arrayf-length": "arrayf_length", "arrayf-clone": "arrayf_clone",
    "arrayi-get": "arrayi_get", "arrayi-set": "arrayi_set",
    "arrayi-length": "arrayi_length", "arrayi-clone": "arrayi_clone",
    "arrayb-get": "arrayb_get", "arrayb-set": "arrayb_set",
    "arrayb-length": "arrayb_length", "arrayb-clone": "arrayb_clone",
    "arrayi-sum": "arrayi_sum", "arrayf-sum": "arrayf_sum",
    "arrayf-get-ro": "arrayf_get_ro", "arrayi-get-ro": "arrayi_get_ro",
    "arrayb-get-ro": "arrayb_get_ro", "arrayi-pmap": "arrayi_pmap", "arrayf-pmap": "arrayf_pmap",
    "arrayf-gray-scott": "gray_scott", "arrayf-gray-stripe": "gray_stripe",
    "arrayi-pmap-chunk": "arrayi_pmap_chunk",
    "write-bytes": "write_bytes", "write-f64s": "write_f64s",
    "write-i64s": "write_i64s",
    "str->int": "str_to_int", "int->str": "int_to_str",
    "str->list": "str_to_list", "str-len": "str_len",
    "str->arrayb": "str_to_arrayb", "arrayb->str": "arrayb_to_str",
    "argc": "argc", "argv": "argv",
    "str-append": "str_append", "read-file": "read_file", "write-file": "write_file",
    "print": "print", "println": "println", "not": "not",
    "and": "and", "or": "or",
    "fix": "fix",
    "int->float": "int_to_float", "float->int": "float_to_int",
    "str->float": "str_to_float", "float->str": "float_to_str",
    "√": "sqrt", "sqrt": "sqrt",
    "sin": "sin", "cos": "cos", "tan": "tan",
    "asin": "asin", "acos": "acos", "atan": "atan", "atan2": "atan2",
    "exp": "exp", "ln": "ln", "log10": "log10", "log2": "log2",
    "floor": "floor", "ceil": "ceil", "round": "round", "trunc": "trunc",
    "pow": "pow", "**": "pow",
    "abs": "abs", "π": "pi", "pi": "pi", "e": "e",
    "∑": "sum", "sum": "sum", "∏": "product", "product": "product",
    "&": "bit_and", "bit-and": "bit_and",
    "|": "bit_or", "bit-or": "bit_or",
    "bit-xor": "bit_xor",
    "!": "bit_not", "bit-not": "bit_not",
    "shl": "shl", "shr": "shr", "popcount": "popcount",
    "map": "map", "pmap": "pmap", "range": "range",
    "immortalize": "immortalize",
    "read-line": "read_line", "read-int": "read_int",
    "read-float": "read_float",
}

INFIX_NAMES = {
    "+", "-", "*", "/", "%", "==", "<", "<=", ">", ">=",
    "⊕", "⊖", "⊗", "⊘", "⊙", "≡", "≺", "≼", "≻", "≽",
}


class Parser:
    def __init__(self, tokens: List[Token], warn_infix: bool = True) -> None:
        self.tokens = tokens
        self.pos = 0
        self.contexts: List[Tuple[str, Loc]] = []
        self.warn_infix = warn_infix
        self.enum_types: set[str] = set()
        self.record_types: set[str] = set()
        self.data_decls: List[DataDecl] = []
        self.block_depth = 0

    # -- token helpers ----------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def pop(self) -> Token:
        tok = self.peek()
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def check(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        return tok.kind == kind and (value is None or tok.value == value)

    def expect(self, kind: str, value: str | None = None, what: str = "") -> Token:
        tok = self.peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            want = what or (value if value is not None else kind)
            if tok.kind == "eof":
                raise SyntaxErr(
                    f"expected {want}, but the file ended before it"
                    + self.context_suffix(), tok.loc)
            raise SyntaxErr(f"expected {want}, got '{tok.value}'", tok.loc)
        return self.pop()

    def push_context(self, label: str, loc: Loc) -> None:
        self.contexts.append((label, loc))

    def pop_context(self) -> None:
        self.contexts.pop()

    def context_suffix(self) -> str:
        if not self.contexts:
            return ""
        label, loc = self.contexts[-1]
        return (f" (while parsing {label} starting at "
                f"line {loc.line}, column {loc.col})")

    def at_definition_start(self) -> bool:
        tok = self.peek()
        nxt = self.peek(1)
        return tok.kind == "ident" and nxt.kind == "arrow"

    def at_enum_start(self) -> bool:
        tok = self.peek()
        return tok.kind == "keyword" and tok.value == "enum"

    def at_type_start(self) -> bool:
        tok = self.peek()
        return tok.kind == "keyword" and tok.value == "type"

    def at_data_start(self) -> bool:
        return (self.peek().kind == "ident" and self.peek().value == "data"
                and self.peek(1).kind == "ident")

    def at_include(self) -> bool:
        return (self.peek().kind == "keyword"
                and self.peek().value == "include"
                and self.peek(1).kind == "string")

    # -- entry point ------------------------------------------------------
    def parse_includes(self, loader) -> List[Def]:
        defs: List[Def] = []
        while self.at_include():
            inc_tok = self.pop()
            path_tok = self.expect("string", what="a quoted include path")
            if loader is None:
                raise SyntaxErr(
                    "include directives require compiling from a file path",
                    inc_tok.loc)
            defs.extend(loader.include(path_tok.value, inc_tok.loc))
        return defs

    def parse_definitions(self) -> List[Def]:
        defs: List[Def] = []
        while (self.at_enum_start() or self.at_type_start()
               or self.at_data_start() or self.at_definition_start()):
            if self.at_data_start():
                defs.extend(self.parse_data_decl())
            elif self.at_enum_start():
                defs.extend(self.parse_enum())
            elif self.at_type_start():
                defs.extend(self.parse_type_decl())
            else:
                name_tok = self.pop()
                self.expect("arrow", what="'→'")
                value = self.parse_definition_value()
                defs.append(Def(name_tok.value, value, name_tok.loc))
        return defs

    def parse_data_decl(self) -> List[Def]:
        start = self.pop().loc
        if self.block_depth:
            raise SyntaxErr("data declarations are only allowed at the top level", start)
        name_tok = self.expect("ident", what="a data type name")
        name = name_tok.value
        params: List[str] = []
        if "<" in name:
            base, _, rest = name.partition("<")
            name = base
            params = [x.strip() for x in rest.rstrip(">").split(",") if x.strip()]
        if self.peek().kind == "ident" and self.peek().value == "=":
            self.pop()
        else:
            raise SyntaxErr("expected '=' after data name", self.peek().loc)
        variants: List[DataVariant] = []
        while True:
            v = self.expect("ident", what="a data variant name")
            fields: List[DataField] = []
            if self.check("punct", "("):
                self.pop()
                while not self.check("punct", ")"):
                    f = self.expect("ident", what="a field name")
                    self.expect("punct", ":", "':'")
                    fields.append(DataField(f.value, self.parse_type_annotation(), f.loc))
                    if self.check("punct", ","):
                        self.pop()
                        continue
                    break
                self.expect("punct", ")", "')'")
            variants.append(DataVariant(v.value, fields, v.loc))
            if self.check("pipe"):
                self.pop()
                continue
            break
        self.data_decls.append(DataDecl(name, params, variants, start))
        defs: List[Def] = []
        for tag, variant in enumerate(variants):
            args = [Var(f.name, f.loc) for f in variant.fields]
            body = DataCtor(name, variant.name, tag, args, variant.loc)
            if variant.fields:
                ctor = Lam([f.name for f in variant.fields], body, variant.loc)
                ctor.param_annotations = [f.type_text for f in variant.fields]
                defs.append(Def(variant.name, ctor, variant.loc))
            else:
                defs.append(Def(variant.name, body, variant.loc))
        return defs

    def parse_type_decl(self) -> List[Def]:
        start = self.pop().loc  # type
        name_tok = self.expect("ident", what="a product type name")
        if name_tok.value in ("Int", "Float", "Bool", "String",
                              "ArrayF", "ArrayI", "ArrayB"):
            raise SyntaxErr(
                f"'{name_tok.value}' is a built-in type name", name_tok.loc)
        if name_tok.value in self.record_types or name_tok.value in self.enum_types:
            raise SyntaxErr(
                f"duplicate type '{name_tok.value}'", name_tok.loc)
        self.record_types.add(name_tok.value)

        self.expect("punct", "(", "'('")
        # A field list starts with "name :"; otherwise this is a sum type.
        if self.check("ident") and self.peek(1).kind == "punct" and self.peek(1).value == ":":
            return self.parse_product_body(name_tok.value, start)
        return self.parse_sum_body(name_tok.value, start)

    def parse_product_body(self, type_name: str, start: Loc) -> List[Def]:
        fields: List[tuple[str, str, Loc]] = []
        seen = set()
        while not self.check("punct", ")"):
            field_tok = self.expect("ident", what="a field name")
            if field_tok.value in seen:
                raise SyntaxErr(
                    f"duplicate field '{field_tok.value}'", field_tok.loc)
            seen.add(field_tok.value)
            self.expect("punct", ":", "':'")
            ftype = self.parse_type_annotation()
            fields.append((field_tok.value, ftype, field_tok.loc))
            if self.check("punct", ","):
                self.pop()
                continue
            break
        self.expect("punct", ")", "')'")
        if not fields:
            raise SyntaxErr(f"type '{type_name}' needs at least one field",
                            start)

        names = [f[0] for f in fields]
        types = [f[1] for f in fields]
        ctor_body = RecordLit(
            type_name, names, types,
            [Var(n, start) for n in names], start)
        ctor = Lam(names, ctor_body, start)
        ctor.param_annotations = list(types)
        defs = [Def(type_name, ctor, start)]

        for field_name, field_type, _ in fields:
            value_name = "record"
            accessor = Lam(
                [value_name],
                RecordField(Var(value_name, start), type_name,
                            field_name, field_type, start),
                start)
            accessor.param_annotations = [type_name]
            defs.append(Def(f"{type_name}-{field_name}", accessor, start))
        return defs

    def parse_sum_body(self, type_name: str, start: Loc) -> List[Def]:
        defs: List[Def] = []
        seen_variants = set()
        tag = 0
        while not self.check("punct", ")"):
            var_tok = self.expect("ident", what="a sum variant name")
            if var_tok.value in seen_variants:
                raise SyntaxErr(
                    f"duplicate variant '{var_tok.value}'", var_tok.loc)
            seen_variants.add(var_tok.value)
            fields: List[tuple[str, str, Loc]] = []
            if self.check("punct", "("):
                self.pop()
                seen_fields = set()
                while not self.check("punct", ")"):
                    field_tok = self.expect("ident", what="a payload field name")
                    if field_tok.value in seen_fields:
                        raise SyntaxErr(
                            f"duplicate payload field '{field_tok.value}'",
                            field_tok.loc)
                    seen_fields.add(field_tok.value)
                    self.expect("punct", ":", "':'")
                    fields.append((field_tok.value,
                                   self.parse_type_annotation(), field_tok.loc))
                    if self.check("punct", ","):
                        self.pop()
                        continue
                    break
                self.expect("punct", ")", "')'")
                if len(fields) > 4:
                    raise SyntaxErr(
                        f"variant '{var_tok.value}' has more than 4 inline "
                        "payload fields", var_tok.loc)
            field_names = [f[0] for f in fields]
            field_types = [f[1] for f in fields]
            body = SumLit(
                type_name, var_tok.value, tag, field_names, field_types,
                [Var(n, var_tok.loc) for n in field_names], var_tok.loc)
            if fields:
                ctor = Lam(field_names, body, var_tok.loc)
                ctor.param_annotations = list(field_types)
                defs.append(Def(var_tok.value, ctor, var_tok.loc))
            else:
                defs.append(Def(var_tok.value, body, var_tok.loc))
            tag += 1
            if self.check("pipe"):
                self.pop()
                continue
            break
        self.expect("punct", ")", "')'")
        if tag == 0:
            raise SyntaxErr(f"type '{type_name}' needs at least one variant",
                            start)
        return defs

    def parse_enum(self) -> List[Def]:
        start = self.pop().loc  # enum
        name_tok = self.expect("ident", what="an enum type name")
        if name_tok.value in ("Int", "Float", "Bool", "String",
                              "ArrayF", "ArrayI", "ArrayB"):
            raise SyntaxErr(
                f"'{name_tok.value}' is a built-in type name", name_tok.loc)
        if name_tok.value in self.enum_types or name_tok.value in self.record_types:
            raise SyntaxErr(
                f"duplicate enum type '{name_tok.value}'", name_tok.loc)
        self.enum_types.add(name_tok.value)
        self.expect("punct", "(", "'('")
        variants: List[tuple[str, Loc]] = []
        seen = set()
        while not self.check("punct", ")"):
            var_tok = self.expect("ident", what="an enum variant name")
            if var_tok.value in seen:
                raise SyntaxErr(
                    f"duplicate enum variant '{var_tok.value}'", var_tok.loc)
            seen.add(var_tok.value)
            variants.append((var_tok.value, var_tok.loc))
            if self.check("punct", ","):
                self.pop()
                continue
            break
        self.expect("punct", ")", "')'")
        if not variants:
            raise SyntaxErr(f"enum '{name_tok.value}' needs at least one variant",
                            start)
        return [
            Def(variant, EnumLit(name_tok.value, tag, loc), loc)
            for tag, (variant, loc) in enumerate(variants)
        ]

    def parse_unit(self, loader=None, require_body: bool = True) -> Block:
        """Parse one file: leading includes, definitions, then optionally a body."""

        start = self.peek().loc
        if self.peek().kind == "keyword" and self.peek().value == "include":
            if not self.at_include():
                raise SyntaxErr("expected a quoted path after 'include'",
                                self.peek().loc)
        included = self.parse_includes(loader)
        defs = included + self.parse_definitions()
        if not require_body:
            if self.peek().kind != "eof":
                tok = self.peek()
                raise SyntaxErr(
                    "included files must contain definitions only "
                    f"(found '{tok.value}')", tok.loc)
            return Block(defs, None, start, data_decls=self.data_decls)
        if self.peek().kind == "eof":
            if defs:
                raise SyntaxErr(
                    "the program ends after its definitions; add a final "
                    "expression (definitions-only files are only valid as "
                    "includes)", self.peek().loc)
            raise SyntaxErr(
                "the program is empty or ends after its includes; add a final "
                "expression", self.peek().loc)
        body = self.parse_expr()
        if self.peek().kind != "eof":
            tok = self.peek()
            raise SyntaxErr(f"unexpected token '{tok.value}' after program", tok.loc)
        return Block(defs, body, start, data_decls=self.data_decls)

    # -- blocks and definitions ------------------------------------------
    def parse_block(self) -> Block:
        start = self.peek().loc
        self.block_depth += 1
        defs = self.parse_definitions()
        if self.peek().kind == "eof":
            where = self.context_suffix()
            if defs:
                raise SyntaxErr(
                    "this block ends after its definitions; add a final "
                    "expression" + where, self.peek().loc)
            raise SyntaxErr(
                "expected an expression, but the file ended" + where,
                self.peek().loc)
        body = self.parse_expr()
        return Block(defs, body, start, data_decls=self.data_decls)

    def parse_definition_value(self) -> Node:
        """A definition value is one atom, so ``f → (λ...)`` is unambiguous."""
        return self.parse_atom()

    def parse_expr(self) -> Node:
        tok = self.peek()

        if tok.kind in ("lambda", "lambda_par"):
            return self.parse_lambda(tok.kind == "lambda_par")
        if tok.kind == "keyword":
            if tok.value == "if":
                return self.parse_if()
            if tok.value == "match":
                return self.parse_match()
            if tok.value == "let":
                return self.parse_let()
            if tok.value == "enum":
                raise SyntaxErr(
                    "enum declarations must appear before the block's final "
                    "expression", tok.loc)
            if tok.value == "type":
                raise SyntaxErr(
                    "type declarations must appear before the block's final "
                    "expression", tok.loc)
            if tok.value == "include":
                raise SyntaxErr(
                    "include directives must appear at the top of a file", tok.loc)
        if self.at_definition_start():
            return self.parse_block()
        return self.parse_application()

    # -- individual forms -------------------------------------------------
    def parse_lambda(self, parallel: bool = False) -> Lam:
        start = self.pop().loc  # λ, λp or \
        self.push_context("lambda expression", start)
        params: List[str] = []
        annotations: List[Optional[str]] = []
        borrows: List[bool] = []
        while not self.check("punct", "."):
            borrowed = False
            tok = self.peek()
            if tok.kind == "ident" and tok.value == "&":
                self.pop()
                borrowed = True
                tok = self.peek()
            if tok.kind == "eof":
                raise SyntaxErr(
                    "the lambda parameter list ended at the file end; "
                    "expected '.' followed by the body" + self.context_suffix(),
                    tok.loc)
            if tok.kind != "ident":
                raise SyntaxErr(
                    f"expected a parameter name or '.', got '{tok.value}'", tok.loc)
            name = tok.value
            if name.startswith("&") and len(name) > 1:
                name = name[1:]
                borrowed = True
            self.pop()
            params.append(name)
            borrows.append(borrowed)
            if self.check("punct", ":"):
                self.pop()
                annotations.append(self.parse_type_annotation())
            else:
                annotations.append(None)
        if not params:
            raise SyntaxErr("lambda needs at least one parameter", start)
        self.expect("punct", ".")
        body = self.parse_expr()
        self.pop_context()
        lam = Lam(params, body, start)
        lam.param_annotations = annotations
        lam.param_borrows = borrows
        lam.is_parallel = parallel
        return lam

    def parse_type_annotation(self) -> str:
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "[":
            self.pop()
            inner = self.parse_type_annotation()
            self.expect("punct", "]", "']'")
            return f"[{inner}]"
        if tok.kind == "ident":
            self.pop()
            return tok.value
        raise SyntaxErr(
            "expected a type name or [T], "
            f"got '{tok.value}'", tok.loc)

    def parse_if(self) -> If:
        start = self.pop().loc  # if
        self.push_context("if expression", start)
        cond = self.parse_expr()
        self.expect("keyword", "then", "'then'")
        then = self.parse_expr()
        self.expect("keyword", "else", "'else'")
        else_ = self.parse_expr()
        self.pop_context()
        return If(cond, then, else_, start)

    def parse_match(self) -> Match:
        start = self.pop().loc  # match
        self.push_context("match expression", start)
        subject = self.parse_expr()
        self.expect("keyword", "with", "'with'")
        arms = []
        binders = []
        while True:
            pat = self.expect("ident", what="an enum or sum variant name")
            arm_binders = []
            while self.check("ident"):
                arm_binders.append(self.pop().value)
            if self.check("arrow"):
                self.pop()
            else:
                self.expect("punct", ":", "':' or '→'")
            arm = self.parse_expr()
            arms.append((pat.value, arm))
            binders.append(arm_binders)
            if self.check("punct", ",") or self.check("pipe"):
                self.pop()
                continue
            break
        self.pop_context()
        match = Match(subject, arms, start)
        match.binders = binders
        return match

    def parse_let(self) -> Block:
        start = self.pop().loc
        self.push_context("let expression", start)
        name_tok = self.expect("ident", what="a definition name")
        # '=' is tokenised as an identifier atom.
        self.expect("ident", "=", "'='")
        value = self.parse_expr()
        self.expect("keyword", "in", "'in'")
        body = self.parse_expr()
        self.pop_context()
        return Block([Def(name_tok.value, value, name_tok.loc)], body, start)

    def parse_application(self) -> Node:
        first = self.parse_atom()
        args: List[Node] = []
        while True:
            tok = self.peek()
            if tok.kind == "eof":
                break
            if tok.kind in ("keyword", "pipe", "ppipe", "range", "larrow"):
                break
            if tok.kind == "punct" and tok.value in ")],.:":
                break
            if self.at_definition_start():
                break
            args.append(self.parse_atom())
        if not args:
            return first
        self.warn_infix_use(first, args)
        return App(first, args, first.loc)

    def warn_infix_use(self, first: Node, args: List[Node]) -> None:
        """Warn about the ``z == 1`` shape, which is an application.

        Operators are first-class, so this cannot be an error; but when an
        operator appears between two expressions it is almost always a mistake.
        Higher-order primitive heads such as ``map + xs`` are left alone.
        """
        if not self.warn_infix:
            return
        if isinstance(first, Var) and first.name in PRIMITIVES:
            return
        for i, arg in enumerate(args[:-1]):
            if isinstance(arg, Var) and arg.name in INFIX_NAMES:
                head = first.name if isinstance(first, Var) else "function"
                loc = arg.loc
                print(
                    f"{loc.source}:{loc.line}:{loc.col}: warning: "
                    f"'{arg.name}' was passed as an argument. Lambda has no "
                    f"infix operators, so this parses as an application. If "
                    f"you meant infix, write ({arg.name} {head} ...)",
                    file=sys.stderr,
                )
                return

    def parse_list(self) -> Node:
        start = self.expect("punct", "[").loc
        self.push_context("list or comprehension", start)
        if self.check("punct", "]"):
            self.pop()
            self.pop_context()
            return Var("nil", start)

        first = self.parse_expr()

        if self.check("pipe") or self.check("ppipe"):
            marker = self.pop()
            name_tok = self.expect("ident", what="a comprehension variable")
            self.expect("larrow", what="'←'")
            source = self.parse_expr()
            if not self.check("punct", "]"):
                tok = self.peek()
                raise SyntaxErr(
                    f"expected ']' after comprehension source, got '{tok.value}'",
                    tok.loc)
            self.pop()
            self.pop_context()
            fn = "pmap" if marker.kind == "ppipe" else "map"
            body_lam = Lam([name_tok.value], first, first.loc)
            return App(Var(fn, marker.loc), [body_lam, source], start)

        if self.check("range"):
            self.pop()
            hi = self.parse_expr()
            self.expect("punct", "]", "']'")
            self.pop_context()
            return App(Var("range", start), [first, hi], start)

        elements = [first]
        while self.check("punct", ","):
            self.pop()
            elements.append(self.parse_expr())
        self.expect("punct", "]", "']'")
        self.pop_context()
        expr: Node = Var("nil", start)
        for el in reversed(elements):
            expr = App(Var("cons", start), [el, expr], start)
        return expr

    def parse_atom(self) -> Node:
        tok = self.peek()

        if tok.kind == "punct" and tok.value == "(":
            start = self.pop().loc
            self.push_context("parenthesized expression", start)
            expr = self.parse_expr()
            self.expect("punct", ")", "')'")
            self.pop_context()
            return expr
        if tok.kind == "punct" and tok.value == "[":
            return self.parse_list()

        if tok.kind == "int":
            self.pop()
            value = int(tok.value)
            if value < -(1 << 63) or value >= (1 << 63):
                raise SyntaxErr("integer literal is outside the 64-bit range", tok.loc)
            return Lit(value, tok.loc)
        if tok.kind == "float":
            self.pop()
            value = float(tok.value)
            if not math.isfinite(value):
                raise SyntaxErr("float literal must be finite", tok.loc)
            return Lit(value, tok.loc)
        if tok.kind == "string":
            self.pop()
            return Lit(tok.value, tok.loc)
        if tok.kind == "lambda":
            return self.parse_lambda(False)
        if tok.kind == "lambda_par":
            return self.parse_lambda(True)
        if tok.kind == "keyword" and tok.value == "if":
            return self.parse_if()
        if tok.kind == "keyword" and tok.value == "let":
            return self.parse_let()
        if tok.kind == "pipe":
            self.pop()
            return Var("|", tok.loc)
        if tok.kind == "borrow":
            start = self.pop().loc
            name = self.expect("ident", what="a variable after '&'")
            node = Borrow(Var(name.value, name.loc), start)
            node.value.type = None
            return node
        if tok.kind in ("ident", "op"):
            self.pop()
            if tok.value == "true":
                return Lit(True, tok.loc)
            if tok.value == "false":
                return Lit(False, tok.loc)
            return Var(tok.value, tok.loc)

        if tok.kind == "eof":
            raise SyntaxErr(
                "expected an expression, but the file ended"
                + self.context_suffix(), tok.loc)
        raise SyntaxErr(f"expected an expression, got '{tok.value}'", tok.loc)


def parse_program(source: str, filename: str = "<input>") -> Node:
    tokens = tokenize(source, filename)
    parser = Parser(tokens)
    return parser.parse_program()
