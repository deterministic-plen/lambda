"""AST for the Lambda language.

The language is a small lambda calculus with integer/boolean/string literals,
lists and uniform recursive definitions.

Design notes
------------
* Every expression lives in a ``Block``: zero or more mutually recursive
  definitions followed by a tail expression.  A function body and the program
  itself are therefore blocks, so *all* recursion is expressed with the same
  ``name -> value`` syntax.
* ``Lam`` stores all of its parameters at once (``lambda x y z. body``).
  Currying is still the runtime calling convention, but knowing the full arity
  is what lets the compiler turn a curried call chain into one native call and
  avoid allocating intermediate closures.
* Nodes carry a coarse source location for diagnostics.
"""

from __future__ import annotations

from typing import Any, List, Optional


class Loc:
    """A source position (1-based line/column) plus the originating file."""

    __slots__ = ("line", "col", "source")

    def __init__(self, line: int = 0, col: int = 0, source: str = "<input>") -> None:
        self.line = line
        self.col = col
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.source}:{self.line}:{self.col}"

    def __str__(self) -> str:
        return f"{self.source}:{self.line}:{self.col}"


class Node:
    """Base class for all AST nodes."""

    __slots__ = ("loc", "type", "parenthesized")

    def __init__(self, loc: Optional[Loc] = None) -> None:
        self.loc = loc or Loc()
        self.type = None
        # Set by the parser when this node was written as a parenthesised
        # expression; the application loop uses it to separate statements.
        self.parenthesized = False


class Block(Node):
    """Definitions followed by a tail expression.

    All definitions in a block are visible in every definition value and in the
    tail expression; they are therefore mutually recursive.
    """

    __slots__ = ("defs", "body", "data_decls")

    def __init__(self, defs: List["Def"], body: Node, loc: Optional[Loc] = None,
                 data_decls: Optional[List["DataDecl"]] = None) -> None:
        super().__init__(loc)
        self.defs = defs
        self.body = body
        self.data_decls = data_decls or []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Block({self.defs!r}, {self.body!r})"


class Def(Node):
    """A single ``name -> value`` binding."""

    __slots__ = ("name", "value", "binding", "is_function")

    def __init__(self, name: str, value: Node, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.name = name
        self.value = value
        self.binding: Optional["Binding"] = None
        self.is_function = False

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Def({self.name!r}, {self.value!r})"


class Lam(Node):
    """A (possibly multi-parameter) lambda abstraction.

    ``param_annotations`` holds optional type names (``"Float"``, ``"[Float]"``)
    in the same order as ``params``.  ``is_parallel`` marks a ``λp`` lambda: a
    compile-time promise that its body is pure and safe to run in parallel.
    """

    __slots__ = (
        "params", "body", "param_bindings", "param_annotations", "param_borrows",
        "is_parallel", "info", "captures", "capture_set", "param_types",
    )

    def __init__(self, params: List[str], body: Node, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.params = params
        self.body = body
        self.param_bindings: List["Binding"] = []
        self.param_annotations: List[Optional[str]] = [None] * len(params)
        self.param_borrows: List[bool] = [False] * len(params)
        self.is_parallel = False
        self.captures: List["Binding"] = []
        # Filled in by the type checker: the resolved parameter types.
        self.param_types: List[object] = []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Lam({self.params!r}, {self.body!r})"


class App(Node):
    """Application of a function to one or more arguments (left-associative)."""

    __slots__ = ("func", "args")

    def __init__(self, func: Node, args: List[Node], loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.func = func
        self.args = args

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"App({self.func!r}, {self.args!r})"


class If(Node):
    __slots__ = ("cond", "then", "else_")

    def __init__(self, cond: Node, then: Node, else_: Node, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.cond = cond
        self.then = then
        self.else_ = else_

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"If({self.cond!r}, {self.then!r}, {self.else_!r})"


class Match(Node):
    """Exhaustive match over a nullary enum.

    ``arms`` holds ``(variant_name, expression)`` pairs.  The type checker
    stores the corresponding tags in ``pattern_tags`` and verifies that every
    variant is covered; code generation lowers the tag to an LLVM ``switch``.
    """

    __slots__ = ("subject", "arms", "binders", "pattern_tags", "arm_bindings")

    def __init__(self, subject: Node, arms, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.subject = subject
        self.arms = arms
        self.binders = [[] for _ in arms]
        self.pattern_tags = []
        self.arm_bindings = [[] for _ in arms]

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Match({self.subject!r}, {self.arms!r})"


class Lit(Node):
    """Integer, boolean or string literal."""

    __slots__ = ("value",)

    def __init__(self, value: Any, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Lit({self.value!r})"


class EnumLit(Node):
    """A nullary enum constructor value.

    Enum declarations expand to one definition per constructor, so ``Locked``
    is an ordinary value of its enum type whose runtime representation is its
    tag.  The tag is unboxed and is compared directly with ``==``.
    """

    __slots__ = ("enum_name", "tag")

    def __init__(self, enum_name: str, tag: int, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.enum_name = enum_name
        self.tag = tag

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"EnumLit({self.enum_name}.{self.tag})"


class RecordLit(Node):
    """A monomorphic product value.

    The parser turns ``type Complex (re: Float, im: Float)`` into a constructor
    lambda whose body is a ``RecordLit``.  Field types are stored as annotation
    strings and resolved by the type checker.
    """

    __slots__ = ("record_name", "field_names", "field_types", "args")

    def __init__(self, record_name: str, field_names, field_types, args,
                 loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.record_name = record_name
        self.field_names = list(field_names)
        self.field_types = list(field_types)
        self.args = list(args)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RecordLit({self.record_name}, {self.args!r})"


class SumLit(Node):
    """A constructor value of a monomorphic inline sum type."""

    __slots__ = ("sum_name", "variant", "tag", "field_names", "field_types", "args")

    def __init__(self, sum_name: str, variant: str, tag: int, field_names,
                 field_types, args, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.sum_name = sum_name
        self.variant = variant
        self.tag = tag
        self.field_names = list(field_names)
        self.field_types = list(field_types)
        self.args = list(args)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"SumLit({self.sum_name}.{self.variant}, {self.args!r})"


class RecordField(Node):
    """Extract one field from a product value."""

    __slots__ = ("value", "record_name", "field_name", "field_type")

    def __init__(self, value: Node, record_name: str, field_name: str,
                 field_type: str, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.value = value
        self.record_name = record_name
        self.field_name = field_name
        self.field_type = field_type

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RecordField({self.record_name}.{self.field_name})"


class Var(Node):
    """A variable reference, optionally resolved to a :class:`Binding`."""

    __slots__ = ("name", "binding", "field_name")

    def __init__(self, name: str, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.name = name
        self.binding: Optional["Binding"] = None
        self.field_name: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Var({self.name!r})"


class Borrow(Node):
    """A non-owning, read-only inspection of a variable (``&x``)."""

    __slots__ = ("value",)

    def __init__(self, value: Node, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Borrow({self.value!r})"


class DataField(Node):
    """A named field in a recursive ``data`` declaration."""
    __slots__ = ("name", "type_text")

    def __init__(self, name: str, type_text: str, loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.name = name
        self.type_text = type_text

    def __repr__(self) -> str:
        return f"DataField({self.name!r}, {self.type_text!r})"


class DataVariant(Node):
    __slots__ = ("name", "fields")

    def __init__(self, name: str, fields: List[DataField],
                 loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.name = name
        self.fields = fields

    def __repr__(self) -> str:
        return f"DataVariant({self.name!r}, {self.fields!r})"


class DataDecl(Node):
    """A monomorphic/generic recursive data declaration.

    The syntax layer keeps the declaration intact; type algebra/layout and
    code generation consume it in later compiler stages.
    """
    __slots__ = ("name", "params", "variants")

    def __init__(self, name: str, params: List[str], variants: List[DataVariant],
                 loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.name = name
        self.params = params
        self.variants = variants

    def __repr__(self) -> str:
        return f"DataDecl({self.name!r}, {self.params!r}, {self.variants!r})"


class DataCtor(Node):
    """A constructor value for a recursive ``data`` declaration."""
    __slots__ = ("data_name", "variant", "tag", "args")

    def __init__(self, data_name: str, variant: str, tag: int,
                 args: List[Node], loc: Optional[Loc] = None) -> None:
        super().__init__(loc)
        self.data_name = data_name
        self.variant = variant
        self.tag = tag
        self.args = args

    def __repr__(self) -> str:
        return f"DataCtor({self.data_name}.{self.variant}, {self.args!r})"


# Binding kinds -------------------------------------------------------------
PARAM = "param"
LOCAL_DEF = "local_def"
TOP_DEF = "top_def"
PRIM = "prim"


class Binding:
    """A resolved variable binding.

    ``uid`` is unique across the whole program, which is what analysis passes
    and code generation key their maps on (names may be shadowed).
    """

    __slots__ = (
        "uid", "name", "kind", "def_node", "lam", "scheme", "type", "loc",
        "borrowed",
    )

    _next_uid = 0

    def __init__(self, name: str, kind: str, def_node: Optional[Def] = None,
                 lam: Optional[Lam] = None, loc: Optional[Loc] = None) -> None:
        Binding._next_uid += 1
        self.uid = Binding._next_uid
        self.name = name
        self.kind = kind
        self.def_node = def_node
        self.lam = lam
        self.scheme = None   # TypeScheme after inference
        self.type = None     # Type for parameters
        self.loc = loc
        self.borrowed = False

    @property
    def is_function(self) -> bool:
        return self.kind in (LOCAL_DEF, TOP_DEF) and self.lam is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Binding({self.name}#{self.uid},{self.kind})"


def walk(node: Node):
    """Yield every AST node in *node* (pre-order)."""

    yield node
    if isinstance(node, Block):
        for d in node.defs:
            yield from walk(d.value)
        for d in getattr(node, "data_decls", ()):
            yield from walk(d)
        yield from walk(node.body)
    elif isinstance(node, Def):
        yield from walk(node.value)
    elif isinstance(node, DataCtor):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, Lam):
        yield from walk(node.body)
    elif isinstance(node, App):
        yield from walk(node.func)
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, If):
        yield from walk(node.cond)
        yield from walk(node.then)
        yield from walk(node.else_)
    elif isinstance(node, Match):
        yield from walk(node.subject)
        for _, arm in node.arms:
            yield from walk(arm)
    elif isinstance(node, RecordLit):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, SumLit):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, RecordField):
        yield from walk(node.value)
    elif isinstance(node, Borrow):
        yield from walk(node.value)
