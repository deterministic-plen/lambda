"""Error types shared by the front end and the compiler."""

from __future__ import annotations

from typing import Optional

from ast import Loc


class LambdaError(Exception):
    """Base class for user-facing compile errors."""

    kind = "error"

    def __init__(self, message: str, loc: Optional[Loc] = None) -> None:
        self.message = message
        self.loc = loc
        super().__init__(message)

    def format(self, source_name: str = "<input>") -> str:  # pragma: no cover
        if self.loc and self.loc.line:
            return f"{source_name}:{self.loc.line}:{self.loc.col}: {self.kind}: {self.message}"
        return f"{source_name}: {self.kind}: {self.message}"


class SyntaxErr(LambdaError):
    kind = "syntax error"


class ResolveError(LambdaError):
    kind = "name error"


class TypeError_(LambdaError):
    kind = "type error"


class CompileError(LambdaError):
    kind = "compile error"
