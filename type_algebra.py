"""Type algebra for Lambda: sum/product/μ types and layout planning.

This is the compiler-side model described in docs/type-algebra.md.  It is not
yet wired into the parser; it provides the normalizer and the layout decision
that later compiler stages will consume.

Run the self-test with:
    python type_algebra.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------- type terms

class Ty:
    pass


@dataclass(frozen=True)
class Empty(Ty):
    pass


@dataclass(frozen=True)
class Unit(Ty):
    pass


@dataclass(frozen=True)
class Base(Ty):
    name: str


@dataclass(frozen=True)
class Var(Ty):
    name: str


@dataclass(frozen=True)
class App(Ty):
    """A nominal type constructor application, e.g. Tree<Int>."""
    name: str
    args: Tuple[Ty, ...]


@dataclass(frozen=True)
class Sum(Ty):
    """Labelled alternatives.  Labels keep constructor tags stable."""
    alternatives: Tuple[Tuple[str, Ty], ...]


@dataclass(frozen=True)
class Prod(Ty):
    """Labelled fields.  An empty product is Unit."""
    fields: Tuple[Tuple[str, Ty], ...]


@dataclass(frozen=True)
class Mu(Ty):
    var: str
    body: Ty


@dataclass(frozen=True)
class ListTy(Ty):
    """Canonical finite list: μL. 1 + head*L."""
    element: Ty


@dataclass(frozen=True)
class Variant:
    name: str
    fields: Tuple[Tuple[str, Ty], ...] = ()


@dataclass(frozen=True)
class DataDecl:
    """A nominal recursive declaration before μ elaboration."""
    name: str
    params: Tuple[str, ...]
    variants: Tuple[Variant, ...]

    def body(self) -> Ty:
        return data_body(self)

    def instantiate(self, *args: Ty) -> Ty:
        return instantiate_decl(self, args)


BASE_TYPES = {
    "Int", "Float", "Bool", "String", "ArrayF", "ArrayI", "ArrayB",
    "UInt8", "UInt16", "UInt32", "UInt64",
}


def sum_(*alts: Tuple[str, Ty]) -> Ty:
    flat: List[Tuple[str, Ty]] = []
    for label, ty in alts:
        if isinstance(ty, Empty):
            continue
        if isinstance(ty, Sum):
            flat.extend(ty.alternatives)
        else:
            flat.append((label, ty))
    if not flat:
        return Empty()
    if len(flat) == 1:
        return flat[0][1]
    return Sum(tuple(flat))


def prod(*fields: Tuple[str, Ty]) -> Ty:
    flat: List[Tuple[str, Ty]] = []
    for label, ty in fields:
        if isinstance(ty, Unit):
            continue
        if isinstance(ty, Prod):
            flat.extend(ty.fields)
        else:
            flat.append((label, ty))
    if not flat:
        return Unit()
    if len(flat) == 1:
        return flat[0][1]
    return Prod(tuple(flat))


def mu(var: str, body: Ty) -> Ty:
    if not _occurs(var, body):
        return body
    return Mu(var, body)


def list_(element: Ty) -> Ty:
    return ListTy(element)


def _self_var(decl: DataDecl) -> str:
    return "_" + decl.name


def _resolve_decl_type(t: Ty, decl: DataDecl, self_var: str,
                       args: Optional[Dict[str, Ty]] = None) -> Ty:
    if isinstance(t, Var):
        if args is not None and t.name in args:
            return args[t.name]
        return t
    if isinstance(t, App):
        if t.name == decl.name:
            return Var(self_var)
        return App(t.name, tuple(_resolve_decl_type(a, decl, self_var, args) for a in t.args))
    if isinstance(t, Sum):
        return Sum(tuple((l, _resolve_decl_type(x, decl, self_var, args)) for l, x in t.alternatives))
    if isinstance(t, Prod):
        return Prod(tuple((l, _resolve_decl_type(x, decl, self_var, args)) for l, x in t.fields))
    if isinstance(t, Mu):
        return mu(t.var, _resolve_decl_type(t.body, decl, self_var, args))
    if isinstance(t, ListTy):
        return ListTy(_resolve_decl_type(t.element, decl, self_var, args))
    return t


def data_body(decl: DataDecl) -> Ty:
    self_var = _self_var(decl)
    variants = tuple((v.name, Prod(tuple((f, _resolve_decl_type(ty, decl, self_var))
                                          for f, ty in v.fields)))
                     for v in decl.variants)
    return mu(self_var, sum_(*variants))


def instantiate_decl(decl: DataDecl, args: Sequence[Ty]) -> Ty:
    if len(args) != len(decl.params):
        raise ValueError(f"{decl.name} expects {len(decl.params)} type arguments, got {len(args)}")
    self_var = _self_var(decl)
    subst = dict(zip(decl.params, args))
    variants = tuple((v.name, Prod(tuple((f, _resolve_decl_type(ty, decl, self_var, subst))
                                          for f, ty in v.fields)))
                     for v in decl.variants)
    return mu(self_var, sum_(*variants))


def _occurs(var: str, t: Ty) -> bool:
    if isinstance(t, Var):
        return t.name == var
    if isinstance(t, Sum):
        return any(_occurs(var, x) for _, x in t.alternatives)
    if isinstance(t, Prod):
        return any(_occurs(var, x) for _, x in t.fields)
    if isinstance(t, Mu):
        return t.var != var and _occurs(var, t.body)
    if isinstance(t, App):
        return any(_occurs(var, a) for a in t.args)
    if isinstance(t, ListTy):
        return _occurs(var, t.element)
    return False


def substitute(t: Ty, var: str, replacement: Ty) -> Ty:
    if isinstance(t, Var):
        return replacement if t.name == var else t
    if isinstance(t, Sum):
        return sum_(*((l, substitute(x, var, replacement)) for l, x in t.alternatives))
    if isinstance(t, Prod):
        return prod(*((l, substitute(x, var, replacement)) for l, x in t.fields))
    if isinstance(t, Mu):
        if t.var == var:
            return t
        return mu(t.var, substitute(t.body, var, replacement))
    if isinstance(t, App):
        return App(t.name, tuple(substitute(a, var, replacement) for a in t.args))
    if isinstance(t, ListTy):
        return list_(substitute(t.element, var, replacement))
    return t


# ---------------------------------------------------------------- parsing

@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int


def _lex(src: str) -> List[Token]:
    out: List[Token] = []
    i = 0
    while i < len(src):
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c in "+*()<>.,/|-=:[]":
            out.append(Token(c, c, i))
            i += 1
            continue
        if c.isalnum() or c == "_":
            j = i + 1
            while j < len(src) and (src[j].isalnum() or src[j] == "_"):
                j += 1
            out.append(Token("ident", src[i:j], i))
            i = j
            continue
        raise ValueError(f"type-algebra: unexpected character {c!r} at {i}")
    out.append(Token("eof", "", len(src)))
    return out


class TypeParser:
    """Small parser for the mathematical surface:

        expr    := sum
        sum     := product ('+' product)*
        product := atom ('*' atom)*
        atom    := '0' | '1' | ident | '(' expr ')'
                 | 'List' '<' expr '>' | 'mu' ident '.' expr
                 | '1/(1-' expr ')'
    """

    def __init__(self, src: str):
        self.src = src
        self.tokens = _lex(src)
        self.i = 0
        self._alt = 0
        self._field = 0
        self.decl_name: Optional[str] = None

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self, kind: str, text: Optional[str] = None) -> Token:
        tok = self.peek()
        if tok.kind != kind or (text is not None and tok.text != text):
            raise ValueError(f"type-algebra: expected {text or kind} at {tok.pos}, got {tok.text!r}")
        self.i += 1
        return tok

    def take_value(self, text: str) -> None:
        tok = self.peek()
        if tok.text != text:
            raise ValueError(f"type-algebra: expected {text!r} at {tok.pos}, got {tok.text!r}")
        self.i += 1

    def parse_data(self) -> DataDecl:
        tok = self.peek()
        if tok.kind == "ident" and tok.text == "data":
            self.take("ident")
        name = self.take("ident").text
        self.decl_name = name
        params: List[str] = []
        if self.peek().kind == "<":
            self.take("<")
            params.append(self.take("ident").text)
            while self.peek().kind == ",":
                self.take(",")
                params.append(self.take("ident").text)
            self.take(">")
        self.take("=")
        variants: List[Variant] = []
        while True:
            vname = self.take("ident").text
            fields: List[Tuple[str, Ty]] = []
            if self.peek().kind == "(":
                self.take("(")
                if self.peek().kind != ")":
                    while True:
                        fname = self.take("ident").text
                        self.take(":")
                        fields.append((fname, self.expr()))
                        if self.peek().kind == ",":
                            self.take(",")
                            continue
                        break
                self.take(")")
            variants.append(Variant(vname, tuple(fields)))
            if self.peek().kind == "|":
                self.take("|")
                continue
            break
        if self.peek().kind != "eof":
            raise ValueError(f"type-algebra: trailing token {self.peek().text!r}")
        return DataDecl(name, tuple(params), tuple(variants))

    def parse(self) -> Ty:
        result = self.expr()
        if self.peek().kind != "eof":
            raise ValueError(f"type-algebra: trailing token {self.peek().text!r}")
        return result

    def expr(self) -> Ty:
        parts = [self.product()]
        while self.peek().kind == "+":
            self.take("+")
            parts.append(self.product())
        if len(parts) == 1:
            return parts[0]
        return Sum(tuple((f"alt{self._alt + i}", p) for i, p in enumerate(parts)))

    def product(self) -> Ty:
        parts = [self.atom()]
        while self.peek().kind == "*":
            self.take("*")
            parts.append(self.atom())
        if len(parts) == 1:
            return parts[0]
        return Prod(tuple((f"field{self._field + i}", p) for i, p in enumerate(parts)))

    def atom(self) -> Ty:
        tok = self.peek()
        if tok.kind == "ident":
            self.take("ident")
            name = tok.text
            if name == "0":
                return Empty()
            if name == "1":
                if self.peek().kind == "/":
                    self.take("/")
                    self.take("(")
                    self.take_value("1")
                    self.take("-")
                    inner = self.expr()
                    self.take(")")
                    return ListTy(inner)
                return Unit()
            if name in ("mu", "μ"):
                var = self.take("ident").text
                self.take(".")
                return mu(var, self.expr())
            if (self.decl_name is not None and name == self.decl_name
                    and self.peek().kind == "<"):
                self.take("<")
                args = [self.expr()]
                while self.peek().kind == ",":
                    self.take(",")
                    args.append(self.expr())
                self.take(">")
                return App(name, tuple(args))
            if name == "List":
                if self.peek().kind in ("<", "("):
                    close = ">" if self.peek().kind == "<" else ")"
                    self.take(self.peek().kind)
                    el = self.expr()
                    self.take(close)
                    return ListTy(el)
                raise ValueError("type-algebra: List needs <T> or (T)")
            if self.peek().kind == "<":
                self.take("<")
                args = [self.expr()]
                while self.peek().kind == ",":
                    self.take(",")
                    args.append(self.expr())
                self.take(">")
                return App(name, tuple(args))
            if name in BASE_TYPES:
                return Base(name)
            return Var(name)
        if tok.kind == "0":
            self.take("0")
            return Empty()
        if tok.kind == "1":
            self.take("1")
            # Rational sugar 1/(1-T) => List(T).
            if self.peek().kind == "/":
                self.take("/")
                self.take("(")
                self.take_value("1")
                self.take("-")
                inner = self.expr()
                self.take(")")
                return ListTy(inner)
            return Unit()
        if tok.kind == "[":
            self.take("[")
            el = self.expr()
            self.take("]")
            return ListTy(el)
        if tok.kind == "(":
            self.take("(")
            v = self.expr()
            self.take(")")
            return v
        raise ValueError(f"type-algebra: unexpected token {tok.text!r} at {tok.pos}")


def parse_type(src: str) -> Ty:
    return TypeParser(src).parse()


def parse_data(src: str) -> DataDecl:
    return TypeParser(src).parse_data()


# ---------------------------------------------------------------- normalization

def normalize(t: Ty) -> Ty:
    if isinstance(t, Empty) or isinstance(t, Unit) or isinstance(t, Base) or isinstance(t, Var):
        return t
    if isinstance(t, ListTy):
        return ListTy(normalize(t.element))
    if isinstance(t, Sum):
        alts = tuple((l, normalize(x)) for l, x in t.alternatives)
        return sum_(*alts)
    if isinstance(t, Prod):
        fields = tuple((l, normalize(x)) for l, x in t.fields)
        return prod(*fields)
    if isinstance(t, Mu):
        body = normalize(t.body)
        # μL. 1 + X*L  -> List(X)
        el = _as_list_mu(t.var, body)
        if el is not None:
            return ListTy(normalize(el))
        return mu(t.var, body)
    raise TypeError(f"unknown type {t!r}")


def _as_list_mu(var: str, body: Ty) -> Optional[Ty]:
    if not isinstance(body, Sum) or len(body.alternatives) != 2:
        return None
    nil = None
    cons = None
    for _, alt in body.alternatives:
        if isinstance(alt, Unit):
            nil = alt
        elif isinstance(alt, Prod):
            cons = alt
    if nil is None or cons is None or len(cons.fields) != 2:
        return None
    fields = cons.fields
    # Find the recursive field; the other is the element.
    for i, (_, ty) in enumerate(fields):
        if isinstance(ty, Var) and ty.name == var:
            other = fields[1 - i][1]
            return other
    return None


def validate_decl(decl: DataDecl) -> None:
    """Structural validation for a parsed data declaration."""
    if not decl.name:
        raise ValueError("data declaration needs a name")
    if len(set(decl.params)) != len(decl.params):
        raise ValueError(f"{decl.name}: duplicate type parameter")
    seen_variants = set()
    for v in decl.variants:
        if v.name in seen_variants:
            raise ValueError(f"{decl.name}: duplicate variant {v.name}")
        seen_variants.add(v.name)
        seen_fields = set()
        for fname, _ in v.fields:
            if fname in seen_fields:
                raise ValueError(f"{decl.name}.{v.name}: duplicate field {fname}")
            seen_fields.add(fname)
    if not decl.variants:
        raise ValueError(f"{decl.name}: data declaration needs at least one variant")


def layout_plan(decl: DataDecl) -> Layout:
    validate_decl(decl)
    return layout(decl.body())


def pretty(t: Ty) -> str:
    if isinstance(t, Empty):
        return "0"
    if isinstance(t, Unit):
        return "1"
    if isinstance(t, Base):
        return t.name
    if isinstance(t, Var):
        return t.name
    if isinstance(t, ListTy):
        return f"List<{pretty(t.element)}>"
    if isinstance(t, Sum):
        return " + ".join(f"{l}:{pretty(x)}" for l, x in t.alternatives)
    if isinstance(t, Prod):
        return " * ".join(f"{l}:{pretty(x)}" for l, x in t.fields)
    if isinstance(t, Mu):
        return f"mu {t.var}. {pretty(t.body)}"
    return repr(t)


# ---------------------------------------------------------------- layouts

@dataclass(frozen=True)
class Layout:
    kind: str                  # inline | list | arena | boxed
    size: Optional[int] = None
    reason: str = ""


def _inline_size(t: Ty) -> Optional[int]:
    if isinstance(t, Empty):
        return 0
    if isinstance(t, Unit):
        return 0
    if isinstance(t, Base):
        return 8
    if isinstance(t, Var):
        return None
    if isinstance(t, ListTy):
        return None
    if isinstance(t, Sum):
        sizes = [_inline_size(x) for _, x in t.alternatives]
        if any(s is None for s in sizes):
            return None
        return 8 + max(sizes)                 # tag + payload
    if isinstance(t, Prod):
        sizes = [_inline_size(x) for _, x in t.fields]
        if any(s is None for s in sizes):
            return None
        return sum(sizes)
    if isinstance(t, Mu):
        return None
    return None


def layout(t: Ty, *, inline_limit: int = 1 << 20) -> Layout:
    """Choose a representation.  This is a planning result, not codegen."""
    t = normalize(t)
    size = _inline_size(t)
    if size is not None and size <= inline_limit:
        return Layout("inline", size, "fixed-size non-recursive")
    if isinstance(t, ListTy):
        el = layout(t.element, inline_limit=inline_limit)
        if el.kind == "inline":
            return Layout("list", None, "List of inline elements -> dense array")
        return Layout("boxed", None, "List element is not inline")
    if isinstance(t, Mu):
        body = t.body
        if _as_list_mu(t.var, body) is not None:
            # normalize should have folded this; defensive fallback.
            return Layout("list", None, "list-like recursive type")
        return Layout("arena", None, "recursive type -> node arena")
    if isinstance(t, (Sum, Prod)):
        # A non-recursive composite can still contain a recursive field.
        for sub in _children(t):
            if layout(sub, inline_limit=inline_limit).kind != "inline":
                return Layout("arena", None, "composite contains a non-inline field")
        return Layout("inline", size, "fixed-size composite")
    return Layout("boxed", None, "opaque/parameterized type")


def _children(t: Ty) -> Iterator[Ty]:
    if isinstance(t, Sum):
        for _, x in t.alternatives:
            yield x
    elif isinstance(t, Prod):
        for _, x in t.fields:
            yield x
    elif isinstance(t, ListTy):
        yield t.element
    elif isinstance(t, Mu):
        yield t.body


# ---------------------------------------------------------------- examples/tests

EXAMPLES = {
    "list": "mu L. 1 + Int * L",
    "list-sugar": "List<Int>",
    "binary-tree": "mu T. 1 + X * T * T",
    "rose": "X * 1/(1 - R)",
    "inline": "Float * Float * Float",
    "ast": "mu E. 1 + Int * E * E + List<E>",
}

EXPECTED = {
    "list": ("list", None),
    "list-sugar": ("list", None),
    "binary-tree": ("arena", None),
    "rose": ("arena", None),
    "inline": ("inline", 24),
    "ast": ("arena", None),
}


def _self_test() -> None:
    for name, src in EXAMPLES.items():
        t = normalize(parse_type(src))
        lay = layout(t)
        want = EXPECTED[name]
        assert (lay.kind, lay.size) == want, (name, pretty(t), lay, want)
        print(f"{name:10} {pretty(t):55} {lay.kind:7} {lay.reason}")
    # identities
    assert normalize(parse_type("0 + Int")) == Base("Int")
    assert normalize(parse_type("1 * Int")) == Base("Int")
    assert isinstance(normalize(parse_type("mu L. 1 + Int * L")), ListTy)
    print("type-algebra self-test: ok")


if __name__ == "__main__":
    _self_test()
