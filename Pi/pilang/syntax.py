"""Pi abstract syntax."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import Loc


@dataclass
class Node:
    loc: Loc = field(default_factory=Loc, repr=False, compare=False, kw_only=True)


# ------------------------------------------------------------------ types
@dataclass
class TypeExpr(Node):
    name: str                       # "Chan", "Ring", "Worker", "tuple", "unit", "list", "int", "ident" ...
    args: List[Any] = field(default_factory=list)   # TypeExpr | int | str | list

    def show(self) -> str:
        if self.name == "unit":
            return "()"
        if self.name == "tuple":
            return "(" + ", ".join(_show(a) for a in self.args) + ")"
        if not self.args:
            return self.name
        return f"{self.name}[{', '.join(_show(a) for a in self.args)}]"


def _show(a) -> str:
    return a.show() if isinstance(a, TypeExpr) else str(a)


# ------------------------------------------------------------ expressions
@dataclass
class Lit(Node):
    value: Any
    kind: str                        # int float bool unit string duration


@dataclass
class Name(Node):
    id: str


@dataclass
class TupleE(Node):
    items: List[Node]


@dataclass
class ListE(Node):
    items: List[Node]


@dataclass
class Call(Node):
    fn: Node
    args: List[Node]
    kwargs: Dict[str, Node] = field(default_factory=dict)


@dataclass
class Member(Node):
    obj: Node
    name: str


@dataclass
class Index(Node):
    obj: Node
    index: Node


@dataclass
class BinOp(Node):
    op: str
    left: Node
    right: Node


@dataclass
class UnOp(Node):
    op: str
    operand: Node


@dataclass
class RangeE(Node):
    lo: Node
    hi: Node


@dataclass
class Await(Node):
    expr: Node


@dataclass
class Annot(Node):
    expr: Node
    name: str


# ------------------------------------------------------------ patterns
@dataclass
class PName(Node):
    id: str


@dataclass
class PWild(Node):
    pass


@dataclass
class PTuple(Node):
    items: List[Node]


@dataclass
class PCtor(Node):                   # WorkerError(4001), Rejected(_), WorkerDied
    name: str
    args: List[Node]                 # PWild | PName | Lit


# ------------------------------------------------------------ statements
@dataclass
class Block(Node):
    stmts: List[Node]


@dataclass
class Bind(Node):                    # pat → expr   (sequential scoping)
    pat: Node
    expr: Node


@dataclass
class Assign(Node):                  # name := expr
    name: str
    expr: Node


@dataclass
class If(Node):
    cond: Node
    then: Node
    orelse: Optional[Node]


@dataclass
class For(Node):
    var: Node                        # pattern
    iter: Node
    body: Node


@dataclass
class ParFor(Node):
    pat: Node
    iter: Node
    body: Node


@dataclass
class Par(Node):
    branches: List[Node]


@dataclass
class Race(Node):
    left: Node
    right: Node


@dataclass
class ScopeE(Node):
    body: Node


@dataclass
class Attr(Node):
    name: str                        # rate | priority | deadline | periodic | sporadic | aperiodic
    value: Any = None                # seconds (rate/deadline) or int (priority)


@dataclass
class Serve(Node):
    chan: Node
    attrs: List[Attr]
    pat: Node
    body: Node


@dataclass
class Loop(Node):
    body: Node


@dataclass
class Iterate(Node):
    n: Node
    body: Node


@dataclass
class SelectArm(Node):
    guard: Node                      # recv(c) | closed(c) | after(d)
    body: Node


@dataclass
class Select(Node):
    arms: List[SelectArm]


@dataclass
class HandlerArm(Node):
    pat: Node
    action: Node                     # Block | Name(resume|escalate|stop) | Call(restart, ...) | expr


@dataclass
class OnError(Node):
    body: Node
    arms: List[HandlerArm]


@dataclass
class Timeout(Node):
    duration: Node
    body: Node


@dataclass
class Retry(Node):
    opts: Dict[str, Node]
    body: Node


@dataclass
class AfterBlock(Node):
    duration: Node
    body: Node


# ------------------------------------------------------------ declarations
@dataclass
class OpDecl(Node):
    name: str
    args: List[TypeExpr]
    ret: TypeExpr


@dataclass
class ErrorDecl(Node):
    code: int
    name: str
    klass: Optional[str] = None      # recoverable | transient | terminal


@dataclass
class Protocol(Node):
    name: str
    tparams: List[str]
    ops: List[OpDecl]
    errors: Dict[int, ErrorDecl]
    has_errors: bool
    purity: str = "Pure"
    canonical: str = ""


@dataclass
class Port(Node):
    direction: str                   # in | out
    name: str
    type: TypeExpr


@dataclass
class WorkerDecl(Node):
    name: str
    protocol: str
    proto_args: List[Any]


@dataclass
class StateDecl(Node):
    name: str
    type: TypeExpr


@dataclass
class Ctrl(Node):
    name: str
    body: Node


@dataclass
class Elem(Node):
    name: str
    tparams: List[str]
    vparams: List[Tuple[str, Optional[TypeExpr]]]
    ports: List[Port]
    workers: List[WorkerDecl]
    states: List[StateDecl]
    ctrls: List[Ctrl]
    contract: Dict[str, str]
    on_failure: Optional[List[HandlerArm]]


@dataclass
class InstanceDecl(Node):
    name: str
    idx: List[str]                   # index variables, e.g. ["r", "c"]
    elem: str
    targs: List[Any]
    args: List[Node]
    bindings: List[Tuple[str, Node]]  # for r in 0..rows, c in 0..cols
    using: Dict[str, str]


@dataclass
class Equation(Node):                # a.port[k] ≡ b.port[k'] [if guard]
    left: Node
    right: Node
    guard: Optional[Node]


@dataclass
class System(Node):
    name: str
    vparams: List[Tuple[str, Optional[TypeExpr]]]
    ports: List[Port]
    instances: List[InstanceDecl]
    couple: List[Node]               # Call (combinator) | Equation
    expose: Dict[str, Node]


@dataclass
class Fun(Node):
    name: str
    params: List[str]
    body: Node


@dataclass
class ProgramDecl(Node):
    name: str
    params: List[Tuple[str, Optional[TypeExpr]]]
    body: Node


@dataclass
class Import(Node):
    path: str


@dataclass
class Module(Node):
    imports: List[Import]
    protocols: Dict[str, Protocol] = field(default_factory=dict)
    elems: Dict[str, Elem] = field(default_factory=dict)
    systems: Dict[str, System] = field(default_factory=dict)
    funs: Dict[str, Fun] = field(default_factory=dict)
    programs: Dict[str, ProgramDecl] = field(default_factory=dict)
    file: str = "<input>"
    warnings: List[str] = field(default_factory=list)
