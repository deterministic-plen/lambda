"""Build cache and atomic-artifact tests (PR.md 1.2, 2.5, 2.6, P8).

These are fast unit tests over the key/complexity logic; the actual Lambda
worker builds are exercised by ``test_runtime.py`` and ``test_aot.py``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from conftest import REPO_ROOT, require_toolchain
from pilang import cache as cache_mod
from pilang import worker as worker_mod
from pilang.cache import (CacheError, atomic_write, cache_root, is_regular_file,
                          make_temp_output)
from pilang.cpp import _runtime_binary


def _fake_lambda_home(tmp_path: Path) -> Path:
    home = tmp_path / "lambda-home"
    home.mkdir()
    for name in ("lambda.py", "compiler.py", "runtime.py", "typecheck.py"):
        (home / name).write_text(f"# {name}\n", encoding="utf-8")
    (home / "lib").mkdir()
    (home / "lib" / "list.lambda").write_text("; bundled lib\n", encoding="utf-8")
    return home


def _fake_worker(tmp_path: Path) -> Path:
    src = tmp_path / "worker.lambda"
    src.write_text('include "inc.lambda"\n(0)\n', encoding="utf-8")
    (tmp_path / "inc.lambda").write_text('include "inc2.lambda"\n; one\n', encoding="utf-8")
    (tmp_path / "inc2.lambda").write_text("; two\n", encoding="utf-8")
    return src


# ------------------------------------------------------------------ 1.2 cache

def test_cache_root_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CACHE_DIR", str(tmp_path / "cache"))
    root = Path(cache_root())
    assert root.is_dir()
    if os.name != "nt":
        st = os.lstat(root)
        assert stat.S_IMODE(st.st_mode) & 0o077 == 0, oct(stat.S_IMODE(st.st_mode))
        assert st.st_uid == os.getuid()


def test_cache_root_tightens_permissions(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    root = tmp_path / "cache"
    root.mkdir()
    os.chmod(root, 0o755)
    monkeypatch.setenv("PI_CACHE_DIR", str(root))
    cache_root()
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700


def test_cache_root_refuses_symlink(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("creating symlinks needs privileges on Windows")
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("PI_CACHE_DIR", str(link))
    with pytest.raises(CacheError):
        cache_root()


def test_cache_root_refuses_foreign_owner(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("PI_CACHE_DIR", str(root))
    if os.name == "nt":
        # Windows has no POSIX uid; exercise the same ownership branch with a
        # synthetic POSIX stat so the security check is covered on every host.
        import stat as stat_mod
        import types
        fake = types.SimpleNamespace(
            st_mode=stat_mod.S_IFDIR | 0o700, st_uid=1001,
            st_file_attributes=0)

        class PosixOS:
            name = "posix"

            def __getattr__(self, attr):
                return getattr(os, attr)

            def lstat(self, path):
                return fake

            def getuid(self):
                return 1000

        monkeypatch.setattr(cache_mod, "os", PosixOS())
    else:
        # Save the real function before patching: a lambda that calls
        # os.getuid() would resolve to the patched attribute and recurse.
        real_getuid = os.getuid
        monkeypatch.setattr(cache_mod.os, "getuid",
                            lambda real=real_getuid: real() + 1)
    with pytest.raises(CacheError):
        cache_root()


def test_atomic_install_and_temp_names_are_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CACHE_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "x.bin"
    atomic_write(str(dest), b"hello")
    assert dest.read_bytes() == b"hello"
    a = Path(make_temp_output(str(tmp_path / "cache"), stem="t"))
    b = Path(make_temp_output(str(tmp_path / "cache"), stem="t"))
    assert a != b
    assert is_regular_file(str(a))
    a.unlink()
    b.unlink()


# ------------------------------------------------------- 2.5 complete cache key

def test_worker_cache_key_covers_all_compiler_files_and_includes(tmp_path):
    home = _fake_lambda_home(tmp_path)
    src = _fake_worker(tmp_path)

    def key():
        worker_mod._compiler_fingerprint.cache_clear()
        return worker_mod.worker_cache_key(str(src), None, str(home))

    k0 = key()
    # PR.md 2.5: analysis.py is not one of the four original files.
    (home / "analysis.py").write_text("# changed\n", encoding="utf-8")
    k1 = key()
    assert k1 != k0, "a change to another compiler source must invalidate the key"
    k0 = k1
    # Transitive includes: inc2 is only reached through inc.lambda.
    (tmp_path / "inc2.lambda").write_text("; changed\n", encoding="utf-8")
    k1 = key()
    assert k1 != k0, "a transitive include change must invalidate the key"
    k0 = k1
    # Bundled Lambda standard library.
    (home / "lib" / "list.lambda").write_text("; changed\n", encoding="utf-8")
    k1 = key()
    assert k1 != k0, "a bundled lib/*.lambda change must invalidate the key"
    k0 = k1
    # A new include edge.
    src.write_text('include "inc.lambda"\ninclude "extra.lambda"\n(0)\n', encoding="utf-8")
    (tmp_path / "extra.lambda").write_text("; extra\n", encoding="utf-8")
    k1 = key()
    assert k1 != k0, "a new include must invalidate the key"


def test_worker_cache_key_covers_protocol_schema(tmp_path):
    from pilang.parser import parse

    home = _fake_lambda_home(tmp_path)
    src = _fake_worker(tmp_path)
    text = 'protocol P { op ping : () → Shard\n errors : { 10001: "x" }\n purity : Pure }'
    p1 = parse(text).protocols["P"]
    p2 = parse(text.replace("op ping", "op pong")).protocols["P"]
    assert p1.name == p2.name
    assert (worker_mod.worker_cache_key(str(src), p1, str(home))
            != worker_mod.worker_cache_key(str(src), p2, str(home)))


# ---------------------------------------------------- P8 per-build schema dir

def test_worker_build_uses_a_per_build_directory_and_schema_file(tmp_path, monkeypatch):
    require_toolchain()
    from pilang.parser import parse

    monkeypatch.setenv("PI_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "worker.lambda"
    src.write_text(
        'include "pi_worker.lambda"\ninclude "pi_schema_P.lambda"\n'
        'hello → (pi-hello schema-hi schema-lo 2)\n0\n', encoding="utf-8")
    text = 'protocol P { op ping : () → Shard\n errors : { 10001: "x" }\n purity : Pure }'
    proto = parse(text).protocols["P"]
    exe1 = worker_mod.build_worker(str(src), proto, lambda_home=str(REPO_ROOT),
                                   builddir=None)
    build_dir = Path(exe1).parent
    cache = (tmp_path / "cache").resolve()
    assert cache in Path(exe1).resolve().parents
    schema = build_dir / "schema" / "pi_schema_P.lambda"
    assert schema.is_file()
    assert "op-ping → 0" in schema.read_text(encoding="utf-8")
    # Cached: same key, same path, no rebuild.
    exe2 = worker_mod.build_worker(str(src), proto, lambda_home=str(REPO_ROOT),
                                   builddir=None)
    assert exe2 == exe1
    # Same protocol *name*, different ops: distinct build dirs (no shared schema race).
    proto2 = parse(text.replace("op ping", "op pong")).protocols["P"]
    exe3 = worker_mod.build_worker(str(src), proto2, lambda_home=str(REPO_ROOT),
                                   builddir=None)
    assert exe3 != exe1
    assert Path(exe3).parent != build_dir
    assert "op-pong → 0" in (Path(exe3).parent / "schema" / "pi_schema_P.lambda").read_text(
        encoding="utf-8")


# ------------------------------------------------------- 2.6 atomic C++ build

def test_cpp_runtime_is_built_in_the_cache_and_reused(tmp_path, monkeypatch):
    require_toolchain()
    monkeypatch.setenv("PI_CACHE_DIR", str(tmp_path / "cache"))
    first = _runtime_binary()
    second = _runtime_binary()
    assert first == second
    cache = (tmp_path / "cache").resolve()
    assert cache in Path(first).resolve().parents
    assert is_regular_file(first)
