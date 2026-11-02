"""Name resolution.

Every variable occurrence is bound to exactly one :class:`ast.Binding`.  Later
passes (inference, capture analysis, code generation) key their maps on the
binding's globally unique ``uid``, so shadowing is handled correctly.
"""

from __future__ import annotations

from typing import Dict, Optional

from ast import (
    App, Binding, Block, Def, If, Lam, Lit, LOCAL_DEF, Node, PARAM, PRIM,
    TOP_DEF, Var,
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
        if not isinstance(program, Block):
            program = Block([], program, program.loc)
            self.program = program
        self.resolve_block(program, self.top_scope, top_level=True)
        return program

    def resolve(self, node: Node, scope: Scope) -> None:
        if isinstance(node, Var):
            b = scope.lookup(node.name)
            if b is None:
                if node.name in PRIMITIVES:
                    b = self.primitive(node.name)
                else:
                    raise ResolveError(f"unbound variable '{node.name}'", node.loc)
            node.binding = b
            return

        if isinstance(node, Lit):
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
