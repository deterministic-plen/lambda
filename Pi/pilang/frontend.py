"""Run the recursive Pi frontend on a large stack (PR.md P5).

Pi programs that are large but flat (for example a 1,000-term ``+`` chain) used
to fail with a bare ``maximum recursion depth exceeded`` because the parser,
checker and plan encoder are all recursive.  ``lambda.py`` solves the same
problem for the Lambda compiler with a raised recursion limit and a 64 MB
compiler thread; this module provides the equivalent for the Pi frontend.

The C++ runtime raises its own JSON nesting cap in the same change, so a program
accepted here can also be executed.
"""

from __future__ import annotations

import sys
import threading
import traceback
from typing import Any, Callable, TypeVar

T = TypeVar("T")

#: Same budget as ``lambda.py``'s COMPILER_RECURSION_LIMIT.
FRONTEND_RECURSION_LIMIT = 20000
#: Same stack size as ``lambda.py``'s compiler thread.
FRONTEND_STACK_BYTES = 64 * 1024 * 1024


def _run_in_thread(fn: Callable[[], T]) -> T:
    result: dict = {}

    def runner() -> None:
        try:
            result["value"] = fn()
        except BaseException:  # noqa: BLE001 - re-raised in the caller's thread
            result["exc"] = sys.exc_info()

    old_stack = threading.stack_size()
    try:
        threading.stack_size(FRONTEND_STACK_BYTES)
    except (ValueError, RuntimeError):
        # Platform without configurable thread stacks: run inline instead.
        return fn()
    try:
        thread = threading.Thread(target=runner, name="pi-frontend", daemon=False)
        thread.start()
        thread.join()
    finally:
        try:
            threading.stack_size(old_stack)
        except (ValueError, RuntimeError):
            pass
    if "exc" in result:
        exc_type, exc, tb = result["exc"]
        raise exc.with_traceback(tb)
    return result["value"]


def run_frontend(fn: Callable[[], T]) -> T:
    """Call *fn* with a raised recursion limit and a 64 MB thread stack.

    ``RecursionError`` is translated into a clear ``RuntimeError`` (the CLI
    prints it as ``pi run: ...``) instead of a bare traceback.
    """
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, FRONTEND_RECURSION_LIMIT))
    try:
        return _run_in_thread(fn)
    except RecursionError as e:
        raise RuntimeError(
            "program nesting is too deep for the Pi frontend "
            f"(limit {FRONTEND_RECURSION_LIMIT} nested terms); split the expression "
            "or reduce its nesting") from e
    finally:
        sys.setrecursionlimit(old_limit)


def format_recursion_hint(path: str) -> str:
    """Human-readable hint used when a program is rejected for nesting."""
    return (f"{path}: program is nested too deeply to be serialised as a plan; "
            "split the expression into sequential bindings")
