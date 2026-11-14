"""Pi error model: the closed Err taxonomy (spec 4.3) and compile-time diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ------------------------------------------------------- compile-time diagnostics

@dataclass
class Loc:
    file: str = "<input>"
    line: int = 0
    col: int = 0

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


class PiCompileError(Exception):
    def __init__(self, message: str, loc: Optional[Loc] = None, kind: str = "error"):
        self.message = message
        self.loc = loc
        self.kind = kind
        super().__init__(self.format())

    def format(self) -> str:
        where = f"{self.loc}: " if self.loc else ""
        return f"{where}{self.kind}: {self.message}"


class PiSyntaxError(PiCompileError):
    def __init__(self, message, loc=None):
        super().__init__(message, loc, "syntax error")


@dataclass
class Diagnostic:
    severity: str            # "error" | "warning"
    message: str
    loc: Optional[Loc] = None

    def __str__(self) -> str:
        where = f"{self.loc}: " if self.loc else ""
        return f"{where}{self.severity}: {self.message}"
