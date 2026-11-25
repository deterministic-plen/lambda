"""AOT compiler/runtime: build flags and PR.md A1-A6.

The AOT path had no test at all (3A/3B), so these tests compile and run real
native binaries.  ``-std=gnu99`` is exercised implicitly: with the old
``-std=c99`` the runtime does not compile on POSIX (usleep/setenv undeclared),
and on Windows the flag is accepted by clang as well.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from conftest import PI_ROOT, REPO_ROOT, fake_worker, require_toolchain, run_pi

PROTO = """protocol F {
  op ping : () → Shard
  errors : { 10001: "unknown_op" }
  purity : Pure
}
"""

FAKE_PROGRAM = PROTO + """
program Main() = scope {
  w → Pool("__WORKER__", 1).recruit(1)
  v → await call(w, "ping")
  emit("ping", v)
}
"""


def _exe(tmp_path: Path, name: str) -> Path:
    return tmp_path / (name + (".exe" if os.name == "nt" else ""))


def _compile(tmp_path: Path, name: str, text: str, *extra: str) -> Path:
    path = tmp_path / (name + ".pi")
    path.write_text(text, encoding="utf-8", newline="\n")
    exe = _exe(tmp_path, name)
    proc = run_pi("aot", str(path), "-o", str(exe), *extra, timeout=240)
    assert proc.returncode == 0, proc.stderr
    return exe


def _run(exe: Path, *, cwd: Path | None = None, env: dict | None = None,
         timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe)], capture_output=True, timeout=timeout,
                          cwd=str(cwd) if cwd else None, env=env)


def test_aot_builds_its_own_runtime_and_runs_without_out_dir(tmp_path):
    """A1: no ``--out`` must print a summary instead of writing '/'+name.csv."""
    require_toolchain()
    src = PI_ROOT / "examples" / "report_demo.pi"
    exe = _exe(tmp_path, "report_demo")
    proc = run_pi("aot", str(src), "-o", str(exe), timeout=240)
    assert proc.returncode == 0, proc.stderr
    run = _run(exe, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert b"series index: 128 points" in run.stdout
    assert list(tmp_path.glob("*.csv")) == []
    assert not (tmp_path / "index.csv").exists()


def test_aot_out_dir_writes_artifacts(tmp_path):
    require_toolchain()
    src = PI_ROOT / "examples" / "report_demo.pi"
    exe = _exe(tmp_path, "report_out")
    data = tmp_path / "data"
    proc = run_pi("aot", str(src), "-o", str(exe), "--out", str(data), timeout=240)
    assert proc.returncode == 0, proc.stderr
    run = _run(exe, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert (data / "index.csv").is_file()
    assert (data / "index.csv").read_text(encoding="utf-8").startswith("x,y")


def test_aot_rejects_unsafe_artifact_labels(tmp_path):
    """A2: ``../`` labels must not escape the output directory."""
    require_toolchain()
    data = tmp_path / "data"
    exe = _compile(tmp_path, "escape", """program Main() = scope {
  xs → payload(range(0, 10))
  emit_series("../ESCAPED_aot", xs)
}
""", "--out", str(data))
    run = _run(exe, cwd=tmp_path)
    assert run.returncode != 0
    assert b"unsafe artifact name" in run.stderr
    assert not (tmp_path / "ESCAPED_aot.csv").exists()
    assert not (data / "ESCAPED_aot.csv").exists()
    assert not Path(os.path.abspath(os.sep) + "ESCAPED_aot.csv").exists()


def test_aot_preserves_nul_and_control_characters_in_strings(tmp_path):
    """A5: strings carry a length; printf("%s")-style truncation is gone."""
    require_toolchain()
    path = tmp_path / "nultext.pi"
    path.write_text(
        'program Main() = scope {\n  emit_text("note", "a\x00b")\n}\n',
        encoding="utf-8", newline="\n")
    exe = _exe(tmp_path, "nultext")
    proc = run_pi("aot", str(path), "-o", str(exe), timeout=240)
    assert proc.returncode == 0, proc.stderr
    run = _run(exe)
    assert run.returncode == 0, run.stderr
    assert b"a\x00b" in run.stdout


def test_aot_rejects_worker_with_wrong_schema(tmp_path):
    """A3: the AOT runtime verifies the worker's HELLO schema hash."""
    require_toolchain()
    worker = tmp_path / "fake_worker.exe"
    shutil.copy2(fake_worker(), worker)
    exe = _compile(tmp_path, "schema", FAKE_PROGRAM.replace("__WORKER__", worker.name))
    env = {**os.environ, "PI_FAKE_MODE": "bad_schema"}
    env.pop("PI_FAKE_SCHEMA", None)
    run = _run(exe, env=env)
    assert run.returncode != 0
    assert b"schema mismatch" in run.stderr


def test_aot_validates_reply_frame_lengths(tmp_path):
    """A4: a negative stats count must not under-read the frame buffer."""
    require_toolchain()
    from pilang.parser import parse
    from pilang.worker import schema_hex

    proto = parse(PROTO).protocols["F"]
    hash_hex = schema_hex(proto)
    worker = tmp_path / "fake_worker.exe"
    shutil.copy2(fake_worker(), worker)
    exe = _compile(tmp_path, "badframe", FAKE_PROGRAM.replace("__WORKER__", worker.name))
    env = {**os.environ, "PI_FAKE_MODE": "stats_negative", "PI_FAKE_SCHEMA": hash_hex}
    run = _run(exe, env=env)
    # A clean pi-aot error (exit 1), not a crash from the huge/negative length.
    assert run.returncode == 1
    assert b"stats length" in run.stderr


def test_aot_checked_integer_conversion(tmp_path):
    """P7: the AOT ``pi_as_int`` is range-checked too."""
    require_toolchain()
    exe = _compile(tmp_path, "largeint", """program Main() = scope {
  xs → payload(range(0, 1e30))
}
""")
    run = _run(exe)
    assert run.returncode == 1
    assert b"integer out of range" in run.stderr
