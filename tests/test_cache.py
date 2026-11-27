"""Root-level entry point for the Pi cache ownership regression.

The implementation and its full test suite live in ``Pi/tests``; some CI
invocations refer to ``tests/test_cache.py`` from the repository root, so this
module re-exports the security test without duplicating its body.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Pi"))
sys.path.insert(0, str(ROOT / "Pi" / "tests"))

_spec = importlib.util.spec_from_file_location(
    "pi_test_cache", ROOT / "Pi" / "tests" / "test_cache.py")
_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_module)

test_cache_root_refuses_foreign_owner = (
    _module.test_cache_root_refuses_foreign_owner)
