"""C++ runtime behaviour: crash recovery, deadlines, bounded par-for, limits.

Covers PR.md P1, P2, P3, P4, P5, P6, P7 and the artifact-name NUL/':' fix.
Each test builds real Lambda worker executables and runs ``pi cpp``; the
build cache is shared between tests (``conftest.cache_dir``).
"""

from __future__ import annotations

from conftest import require_toolchain, run_pi

SLOW_WORKER = """include "pi_worker.lambda"
include "pi_schema_Slow.lambda"

slow-loop → (λz.
  hd → (pi-next-call 0)
  corr → (arrayi-get hd 0)
  if (< corr 0) then 0
  else
    op → (arrayi-get hd 1)
    nargs → (arrayi-get hd 2)
    a → (pi-read-floats (arrayf nargs 0.0) 0 nargs)
    if (== op op-ping)
    then
      w → (pi-reply-ok corr (pi-vals1 7.0))
      (slow-loop 0)
    else
      ; block forever waiting for input that Pi will never send
      (str->int (read-line "")))

hello → (pi-hello schema-hi schema-lo 2)
(slow-loop 0)
"""

CRASH_WORKER = """include "pi_worker.lambda"
include "pi_schema_Crash.lambda"

crash-loop → (λz.
  hd → (pi-next-call 0)
  corr → (arrayi-get hd 0)
  if (< corr 0) then 0
  else
    op → (arrayi-get hd 1)
    nargs → (arrayi-get hd 2)
    a → (pi-read-floats (arrayf nargs 0.0) 0 nargs)
    if (== op op-ping)
    then
      w → (pi-reply-ok corr (pi-vals1 42.0))
      (crash-loop 0)
    else
      ; fatal Lambda runtime error: the worker process dies mid-call
      (arrayi-get (arrayi 1 0) 5))

hello → (pi-hello schema-hi schema-lo 2)
(crash-loop 0)
"""

SLOW_PROTOCOL = """protocol Slow {
  op hang : () → Shard
  op ping : () → Shard
  errors : { 10001: "unknown_op" }
  purity : Pure
}
"""

CRASH_PROTOCOL = """protocol Crash {
  op ping : () → Shard
  op crash : () → Shard
  errors : { 10001: "unknown_op" }
  purity : Pure
}
"""


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _prepare_slow(tmp_path):
    _write(tmp_path / "slow.lambda", SLOW_WORKER)
    _write(tmp_path / "slow.pi", SLOW_PROTOCOL + """
program Main() = scope {
  w → Pool("slow.lambda", 1).recruit(1)
}
""")
    return tmp_path / "slow.pi"


def test_worker_crash_raises_worker_died_and_restart_recovers(tmp_path):
    """P1: a real crash must reach on_error as WorkerDied, not kill the runtime."""
    require_toolchain()
    _write(tmp_path / "crash.lambda", CRASH_WORKER)
    program = tmp_path / "crash.pi"
    _write(program, CRASH_PROTOCOL + """
program Main() = scope {
  w → Pool("crash.lambda", 1).recruit(1)
  a → await call(w, "ping")
  emit("a", a[0])
  on_error(await call(w, "crash")) {
    | WorkerDied ⇒ restart(max = 2)
    | _ ⇒ escalate
  }
  b → await call(w, "ping")
  emit("b", b[0])
}
""")
    proc = run_pi("cpp", str(program), timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "a 42" in proc.stdout and "b 42" in proc.stdout
    assert "pi-cpp: done" in proc.stdout


def test_timeout_deadline_caught_exits_zero(tmp_path):
    """P2(a): a handled Deadline no longer leaves a detached thread that exits 1."""
    require_toolchain()
    _prepare_slow(tmp_path)
    program = tmp_path / "timeout_stop.pi"
    _write(program, SLOW_PROTOCOL + """
program Main() = scope {
  w → Pool("slow.lambda", 1).recruit(1)
  on_error(timeout(0.3s) { await call(w, "hang") }) {
    | Deadline ⇒ resume
    | _ ⇒ escalate
  }
  emit("done")
}
""")
    proc = run_pi("cpp", str(program), timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "done" in proc.stdout and "pi-cpp: done" in proc.stdout
    # The abandoned branch used to die on the worker pipe during shutdown.
    assert "worker read" not in proc.stderr


def test_timeout_deadline_restart_recovers_worker(tmp_path):
    """P2(b): ``| Deadline ⇒ restart`` releases the hung worker instead of hanging."""
    require_toolchain()
    _prepare_slow(tmp_path)
    program = tmp_path / "timeout_restart.pi"
    _write(program, SLOW_PROTOCOL + """
program Main() = scope {
  w → Pool("slow.lambda", 1).recruit(1)
  on_error(timeout(0.3s) { await call(w, "hang") }) {
    | Deadline ⇒ restart(max = 1)
    | _ ⇒ escalate
  }
  b → await call(w, "ping")
  emit("b", b[0])
}
""")
    proc = run_pi("cpp", str(program), timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "b 7" in proc.stdout
    assert "pi-cpp: done" in proc.stdout


def test_par_for_uses_a_bounded_pool(tmp_path):
    """P3: 100,000 ``par for`` elements used to abort with std::terminate."""
    require_toolchain()
    program = tmp_path / "parfor.pi"
    _write(program, """program Main() = scope {
  xs → par for i in payload(range(1, 100001)) {
  }
  emit("parfor done", 1)
}
""")
    proc = run_pi("cpp", str(program), timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "parfor done 1" in proc.stdout
    assert "terminate" not in proc.stderr


def test_large_flat_program_runs(tmp_path):
    """P5: a 1,000-term flat expression used to fail on the frontend/JSON caps."""
    require_toolchain()
    chain = " + ".join(["1"] * 1000)
    program = tmp_path / "chain.pi"
    _write(program, f"program Main() = scope {{\n  x → {chain}\n  emit(\"x\", x)\n}}\n")
    proc = run_pi("cpp", str(program), timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "x 1000" in proc.stdout


def test_checked_integer_conversion_reports_a_runtime_error(tmp_path):
    """P7: ``chan(1e30)``-style conversions must fail cleanly, not be UB."""
    require_toolchain()
    program = tmp_path / "largeint.pi"
    _write(program, """program Main() = scope {
  xs → payload(range(0, 1e30))
}
""")
    proc = run_pi("cpp", str(program), timeout=120)
    assert proc.returncode == 1
    assert "integer out of range" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_plan_json_decodes_unicode_escapes(tmp_path):
    """P4: Python's json writes control characters as \\uXXXX; decode them."""
    require_toolchain()
    program = tmp_path / "ctrl.pi"
    program.write_text(
        'program Main() = scope {\n  emit_text("note", "a\x01b")\n}\n',
        encoding="utf-8", newline="\n")
    proc = run_pi("cpp", str(program), timeout=120, bytes_stdout=True)
    assert proc.returncode == 0, proc.stderr
    assert b"a\x01b" in proc.stdout
    assert b"au0001b" not in proc.stdout


def test_plan_json_decodes_non_ascii_text(tmp_path):
    require_toolchain()
    program = tmp_path / "unicode.pi"
    _write(program, 'program Main() = scope {\n  emit_text("note", "café λ")\n}\n')
    proc = run_pi("cpp", str(program), timeout=120, bytes_stdout=True)
    assert proc.returncode == 0, proc.stderr
    assert "café λ".encode("utf-8") in proc.stdout


def test_safe_artifact_name_rejects_colon_and_nul(tmp_path):
    require_toolchain()
    colon = tmp_path / "colon.pi"
    _write(colon, """program Main() = scope {
  xs → payload(range(0, 10))
  emit_series("a:b", xs)
}
""")
    proc = run_pi("cpp", str(colon), outdir=str(tmp_path / "out"), timeout=120)
    assert proc.returncode == 1
    assert "unsafe artifact name" in proc.stderr
    assert not (tmp_path / "out" / "a:b.csv").exists()

    nul = tmp_path / "nul.pi"
    nul.write_text(
        'program Main() = scope {\n  xs → payload(range(0, 10))\n'
        '  emit_series("a\x00b", xs)\n}\n',
        encoding="utf-8", newline="\n")
    proc = run_pi("cpp", str(nul), outdir=str(tmp_path / "out"), timeout=120)
    assert proc.returncode == 1
    assert "unsafe artifact name" in proc.stderr


def test_runtime_failure_inside_background_branch_is_reported_by_main(tmp_path):
    """P6: errors raised in timeout/par-for threads must not exit() the process.

    ``main`` still runs the report/finish path and prints the failure marker,
    which the old ``die()``-from-any-thread did not.
    """
    require_toolchain()
    timeout_prog = tmp_path / "fatal_timeout.pi"
    _write(timeout_prog, """program Main() = scope {
  timeout(2s) { xs → payload(range(0, 1e30)) }
}
""")
    proc = run_pi("cpp", str(timeout_prog), timeout=120)
    assert proc.returncode == 1
    assert "integer out of range" in proc.stderr
    assert "pi-cpp: failed" in proc.stdout

    parfor_prog = tmp_path / "fatal_parfor.pi"
    _write(parfor_prog, """program Main() = scope {
  xs → par for i in payload(range(0, 64)) {
    y → payload(range(0, 1e30))
  }
}
""")
    proc = run_pi("cpp", str(parfor_prog), timeout=120)
    assert proc.returncode == 1
    assert "integer out of range" in proc.stderr
    assert "pi-cpp: failed" in proc.stdout
