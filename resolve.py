"""Name resolution.

Every variable occurrence is bound to exactly one :class:`ast.Binding`.  Later
passes (inference, capture analysis, code generation) key their maps on the
binding's globally unique ``uid``, so shadowing is handled correctly.
"""

from __future__ import annotations

from typing import Dict, Optional

from lambda_ast import (
    App, Binding, Block, Borrow, Def, EnumLit, If, Lam, Lit, LOCAL_DEF, Match,
    Node, PARAM, PRIM, RecordField, RecordLit, SumLit, TOP_DEF, Var, walk,
)
from errors import ResolveError
from parser import PRIMITIVES


class Scope:
    __slots__ = ("names", "parent")

    def __init__(self, parent: Optional["Scope"] = None) -> None:
        self.names: Dict[str, Binding] = {}
        self.parent = parent

    def lookup(self, name: str) -> Optional[Binding]:
        scope: Optional[Scope] = self
        while scope is not None:
            b = scope.names.get(name)
            if b is not None:
                return b
            scope = scope.parent
        return None

    def define(self, binding: Binding, loc) -> None:
        if binding.name in self.names:
            raise ResolveError(
                f"duplicate definition of '{binding.name}' in the same block", loc)
        self.names[binding.name] = binding

    def extend(self) -> "Scope":
        return Scope(self)


class Resolver:
    def __init__(self) -> None:
        self.primitives: Dict[str, Binding] = {}
        self.top_scope = Scope()
        self.program: Optional[Node] = None

    def primitive(self, name: str) -> Binding:
        b = self.primitives.get(name)
        if b is None:
            b = Binding(name, PRIM)
            self.primitives[name] = b
        return b

    def resolve_program(self, program: Node) -> Node:
        self.program = program
        self.record_fields = set()
        for n in walk(program):
            if isinstance(n, RecordLit):
                self.record_fields.update(n.field_names)
        if not isinstance(program, Block):
            program = Block([], program, program.loc)
            self.program = program
        self.resolve_block(program, self.top_scope, top_level=True)
        return program

    def resolve(self, node: Node, scope: Scope) -> None:
        if isinstance(node, Var):
            b = scope.lookup(node.name)
            if b is not None:
                node.binding = b
                return
            # Physics-style field sugar: split at the LAST underscore only.
            # vector-2-dimensional_x means (Vec-x vector-2-dimensional).
            # Exact names win; no prefix searching.
            if "_" in node.name:
                prefix, field = node.name.rsplit("_", 1)
                if prefix and field in self.record_fields:
                    pb = scope.lookup(prefix)
                    if pb is not None:
                        node.binding = pb
                        node.field_name = field
                        return
            if node.name in PRIMITIVES:
                b = self.primitive(node.name)
            else:
                raise ResolveError(f"unbound variable '{node.name}'", node.loc)
            node.binding = b
            return

        if isinstance(node, Lit):
            return
        if isinstance(node, EnumLit):
            return

        if isinstance(node, RecordLit):
            for a in node.args:
                self.resolve(a, scope)
            return

        if isinstance(node, SumLit):
            for a in node.args:
                self.resolve(a, scope)
            return

        if isinstance(node, Borrow):
            self.resolve(node.value, scope)
            return

        if isinstance(node, RecordField):
            self.resolve(node.value, scope)
            return

        if isinstance(node, Lam):
            inner = scope.extend()
            seen = set()
            bindings = []
            for p in node.params:
                if p in seen:
                    raise ResolveError(f"duplicate parameter '{p}'", node.loc)
                seen.add(p)
                # A parameter shadows anything in the enclosing scope.
                b = Binding(p, PARAM, loc=node.loc)
                idx = len(bindings)
                if idx < len(node.param_borrows):
                    b.borrowed = node.param_borrows[idx]
                inner.names[p] = b
                bindings.append(b)
            node.param_bindings = bindings
            self.resolve(node.body, inner)
            return

        if isinstance(node, App):
            self.resolve(node.func, scope)
            for a in node.args:
                self.resolve(a, scope)
            return

        if isinstance(node, If):
            self.resolve(node.cond, scope)
            self.resolve(node.then, scope)
            self.resolve(node.else_, scope)
            return

        if isinstance(node, Match):
            self.resolve(node.subject, scope)
            for idx, (_, arm) in enumerate(node.arms):
                inner = scope.extend()
                bindings = []
                if idx < len(node.binders):
                    for name in node.binders[idx]:
                        b = Binding(name, PARAM, loc=arm.loc)
                        inner.define(b, arm.loc)
                        bindings.append(b)
                node.arm_bindings[idx] = bindings
                self.resolve(arm, inner)
            return

        if isinstance(node, Block):
            self.resolve_block(node, scope, top_level=False)
            return

        raise ResolveError(f"cannot resolve node {type(node).__name__}", node.loc)

    def resolve_block(self, block: Block, scope: Scope, top_level: bool) -> None:
        block_scope = scope.extend()
        # Definitions are visible in all values and in the body (mutual
        # recursion), so bind them all first.
        for d in block.defs:
            kind = TOP_DEF if top_level else LOCAL_DEF
            lam = d.value if isinstance(d.value, Lam) else None
            b = Binding(d.name, kind, def_node=d, lam=lam, loc=d.loc)
            d.binding = b
            d.is_function = lam is not None
            block_scope.define(b, d.loc)
        # Resolve values with the complete block scope.
        for d in block.defs:
            self.resolve(d.value, block_scope)
        self.resolve(block.body, block_scope)
