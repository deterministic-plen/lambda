"""File loader with textual, include-once ``include "path"`` dependencies.

Included files contain definitions only.  Their definitions are spliced into
the including program as ordinary mutually recursive top-level definitions;
paths are resolved relative to the file that writes the directive, then in
``LAMBDA_PATH``/``LAMBDA_LIB`` and the compiler's ``lib/`` directory.  Cycles are errors and a
file is included at most once per compilation.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Set

from lambda_ast import Block, Def, Loc, Node
from errors import SyntaxErr
from lexer import tokenize
from parser import Parser


class ProgramLoader:
    def __init__(self, search_paths: Optional[List[str]] = None,
                 warn_infix: bool = True) -> None:
        self.warn_infix = warn_infix
        root = os.path.dirname(os.path.abspath(__file__))
        default_lib = os.path.join(root, "lib")
        paths = list(search_paths or [])
        env = os.environ.get("LAMBDA_PATH", "")
        if env:
            paths.extend(p for p in env.split(os.pathsep) if p)
        lib_env = os.environ.get("LAMBDA_LIB", "")
        if lib_env:
            paths.extend(p for p in lib_env.split(os.pathsep) if p)
        # The compiler checkout itself and its bundled standard library are
        # always available, even when the source lives outside the checkout.
        paths.append(root)
        paths.append(default_lib)
        # Stable order, duplicates removed.  A caller-supplied or LAMBDA_PATH
        # directory intentionally precedes the bundled standard library.
        unique = []
        for p in paths:
            ap = os.path.abspath(p)
            if ap not in unique:
                unique.append(ap)
        self.search_paths = unique
        self.loaded: Set[str] = set()
        self.active: List[str] = []

    def load_main(self, path: str) -> Node:
        full = os.path.abspath(path)
        if not os.path.exists(full):
            raise SyntaxErr(f"file '{path}' not found")
        return self._parse_file(full, is_main=True)

    def include(self, path: str, loc: Loc) -> List[Def]:
        full = self._resolve(path, loc)
        key = self._key(full)
        if key in self.loaded:
            return []
        if key in self.active:
            chain = " -> ".join(os.path.basename(p) for p in self.active + [key])
            raise SyntaxErr(f"include cycle: {chain}", loc)
        block = self._parse_file(full, is_main=False)
        return list(block.defs)

    def _parse_file(self, path: str, is_main: bool) -> Block:
        key = self._key(path)
        self.active.append(key)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                source = f.read()
            parser = Parser(tokenize(source, path), warn_infix=self.warn_infix)
            block = parser.parse_unit(self, require_body=is_main)
        finally:
            self.active.pop()
        self.loaded.add(key)
        return block

    def _resolve(self, path: str, loc: Loc) -> str:
        candidates: List[str] = []
        raw = os.path.expanduser(path)
        if os.path.isabs(raw):
            candidates.append(raw)
        else:
            base = os.path.dirname(loc.source) if loc.source else os.getcwd()
            if not os.path.isabs(base):
                base = os.path.abspath(base)
            candidates.append(os.path.join(base, raw))
        if not raw.lower().endswith(".lambda"):
            candidates.append(candidates[0] + ".lambda")
        for directory in self.search_paths:
            if os.path.isabs(raw):
                continue
            candidates.append(os.path.join(directory, raw))
            if not raw.lower().endswith(".lambda"):
                candidates.append(os.path.join(directory, raw + ".lambda"))
        seen = set()
        for cand in candidates:
            cand = os.path.abspath(cand)
            if cand in seen:
                continue
            seen.add(cand)
            if os.path.isfile(cand):
                return cand
        tried = "\n  ".join(sorted(seen))
        raise SyntaxErr(
            f"cannot find include '{path}'; tried:\n  {tried}", loc)

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))


def load_program(path: str, search_paths: Optional[List[str]] = None,
                 warn_infix: bool = True) -> Node:
    return ProgramLoader(search_paths, warn_infix=warn_infix).load_main(path)
