"""Compile-and-run regression tests for the Lambda compiler.

Test cases live next to their sources: every directory under ``tests/`` may
contain a ``cases.yml`` describing the cases whose ``.lambda`` files sit in that
same directory.  Discovery is recursive, so new groups can be added by creating
a directory and a manifest -- no Python changes required.

Manifest format
---------------

```yaml
description: Short human summary of the group      # optional
cases:
  - name: hello            # -> hello.lambda in the same directory
    stdout: |              # expected stdout, exactly
      3
  - name: arraydyn
    stdout: |
      [5.0, 3.5, 1.25]
    flags: [--dynamic]     # extra `lambda.py` arguments
  - name: stream_f64
    stdout: 4c4d42...      # hex-encoded bytes when `binary: true`
    binary: true
    flags: [--data]
  - name: input_int
    stdout: |
      41
    flags: [--input]
    stdin: |
      41
  - name: typeerror
    compile_fail: type error   # substring of compiler stderr
    flags: [--strict]
```

Every successful run also enables the runtime allocation statistics and
requires ``live=0``, so reference-counting leaks fail the suite.

Usage
-----

    python run_tests.py                  # run everything
    python run_tests.py data floats      # only these groups
    python run_tests.py tests/data/adt_tree.lambda   # a single case
    python run_tests.py --list           # show discovered cases
    python run_tests.py -k adt           # filter by case-name substring
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a build dependency
    yaml = None

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "tests")
MANIFEST_NAMES = ("cases.yml", "cases.yaml")


@dataclass
class Case:
    name: str
    source: str
    group: str
    expected: object = None          # str or bytes
    binary: bool = False
    flags: List[str] = field(default_factory=list)
    stdin: Optional[str] = None
    fail_substr: Optional[str] = None
    strict: bool = True

    @property
    def label(self) -> str:
        return f"{self.group}/{self.name}"


# Compiler diagnostics that must never appear in a passing run.  These are the
# silent-fallback paths: a type error followed by boxed code is a different
# (usually wrong and much slower) program, and it used to hide real bugs.
SUSPICIOUS_STDERR = (
    "compiling in dynamic (boxed) mode",
    "blocking stderr",
)


def suspicious_markers(text: str) -> List[str]:
    return [m for m in SUSPICIOUS_STDERR if m in text]


def _read_manifest(path: str) -> dict:
    if yaml is None:
        raise SystemExit(
            "PyYAML is required to read test manifests; pip install pyyaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: manifest must be a mapping")
    return data


def discover(paths: Optional[Iterable[str]] = None) -> List[Case]:
    """Collect cases from manifests, recursively, in a stable order."""

    roots: List[str] = []
    explicit: List[Case] = []
    if paths:
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(ROOT, p)
            if not os.path.exists(full) and not os.path.isabs(p):
                # Allow bare group names such as `python run_tests.py data`.
                alt = os.path.join(TESTS, p)
                if os.path.exists(alt):
                    full = alt
            if os.path.isfile(full) and full.endswith(".lambda"):
                source = os.path.abspath(full)
                dirpath = os.path.dirname(source)
                group = os.path.relpath(dirpath, TESTS).replace(os.sep, "/")
                name = os.path.splitext(os.path.basename(source))[0]
                manifest = next(
                    (os.path.join(dirpath, n) for n in MANIFEST_NAMES
                     if os.path.exists(os.path.join(dirpath, n))), None)
                spec = None
                if manifest is not None:
                    for entry in _read_manifest(manifest).get("cases") or []:
                        if str(entry.get("name")) == name:
                            spec = entry
                            break
                if spec is None:
                    raise SystemExit(
                        f"{manifest or dirpath}: no manifest entry for '{name}'")
                binary = bool(spec.get("binary", False))
                stdout = spec.get("stdout")
                fail = spec.get("compile_fail")
                expected = None
                if stdout is not None and fail is None:
                    text = str(stdout)
                    expected = bytes.fromhex(text.strip()) if binary else text
                manifest_strict = True
                if manifest is not None:
                    manifest_strict = bool(
                        (_read_manifest(manifest) or {}).get("strict", True))
                explicit.append(Case(
                    name=name, source=source, group=group, expected=expected,
                    binary=binary, flags=[str(f) for f in (spec.get("flags") or [])],
                    stdin=spec.get("stdin"), fail_substr=fail,
                    strict=bool(spec.get("strict", manifest_strict))))
            elif os.path.isdir(full):
                roots.append(full)
            else:
                raise SystemExit(f"no such test path: {p}")
    if not roots and not explicit:
        roots = [TESTS]

    cases: List[Case] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            manifest = next(
                (os.path.join(dirpath, n) for n in MANIFEST_NAMES if n in filenames),
                None)
            group = os.path.relpath(dirpath, TESTS).replace(os.sep, "/")
            if manifest is None:
                continue
            data = _read_manifest(manifest)
            specs = data.get("cases") or []
            if not isinstance(specs, list):
                raise SystemExit(f"{manifest}: 'cases' must be a list")
            for entry in specs:
                if not isinstance(entry, dict) or "name" not in entry:
                    raise SystemExit(f"{manifest}: every case needs a 'name'")
                name = str(entry["name"])
                source = os.path.join(dirpath, name + ".lambda")
                if not os.path.exists(source):
                    raise SystemExit(f"{manifest}: missing source for '{name}'")
                binary = bool(entry.get("binary", False))
                stdout = entry.get("stdout")
                fail = entry.get("compile_fail")
                expected: object = None
                if stdout is not None and fail is None:
                    text = "" if stdout is None else str(stdout)
                    expected = bytes.fromhex(text.strip()) if binary else text
                cases.append(Case(
                    name=name,
                    source=source,
                    group=group,
                    expected=expected,
                    binary=binary,
                    flags=[str(f) for f in (entry.get("flags") or [])],
                    stdin=entry.get("stdin"),
                    fail_substr=fail,
                    strict=bool(entry.get("strict", data.get("strict", True))),
                ))
    return cases + explicit


def run_case(case: Case, workdir: str) -> tuple[bool, str]:
    exe = os.path.join(workdir, case.group.replace("/", "_") + "_" + case.name)
    cmd = [sys.executable, os.path.join(ROOT, "lambda.py"), case.source,
           "-o", exe] + case.flags
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "compilation timed out"
    compile_time = time.time() - start

    if case.fail_substr is not None:
        if proc.returncode == 0:
            return False, f"expected compilation to fail ({case.fail_substr})"
        if case.fail_substr not in proc.stderr:
            return False, (
                f"expected '{case.fail_substr}' in compiler stderr, got:\n"
                f"{proc.stderr}")
        return True, f"compile-fail ok ({compile_time:.2f}s)"

    if proc.returncode != 0:
        return False, f"compilation failed:\n{proc.stderr}"

    if case.strict:
        bad = suspicious_markers(proc.stderr)
        if bad:
            return False, (
                "compiler emitted a fallback diagnostic "
                f"({', '.join(bad)}); fix the type error or mark the case "
                f"strict: false:\n{proc.stderr.strip()}")

    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"
    if not os.path.exists(exe):
        return False, "compiler did not produce an executable"

    env = dict(os.environ)
    env["LAMBDA_STATS"] = "1"
    run_start = time.time()
    try:
        if case.binary:
            run = subprocess.run([exe], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, timeout=300)
            stdout: object = run.stdout
            stderr = run.stderr.decode("utf-8", errors="replace")
        else:
            run = subprocess.run([exe], input=case.stdin, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env,
                                 timeout=300, encoding="utf-8", errors="replace")
            stdout = run.stdout
            stderr = run.stderr
    except subprocess.TimeoutExpired:
        return False, "program timed out"
    run_time = time.time() - run_start
    stderr = stderr.replace("\r\n", "\n")
    if not case.binary:
        stdout = stdout.replace("\r\n", "\n")

    if run.returncode != 0:
        return False, f"runtime exited {run.returncode}:\n{stderr}"
    if stdout != case.expected:
        return False, (f"stdout mismatch\n  expected: {case.expected!r}\n"
                       f"  actual:   {stdout!r}")
    match = re.search(r"live=(\d+)", stderr)
    if match is None:
        return False, f"missing allocation statistics in stderr:\n{stderr}"
    live = int(match.group(1))
    if live != 0:
        return False, f"reference-count leak: live={live}\n{stderr}"
    return True, f"ok (compile {compile_time:.2f}s, run {run_time:.2f}s, live=0)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*",
                    help="test directories, manifest paths or single .lambda files")
    ap.add_argument("-k", "--filter", default=None,
                    help="only run cases whose label contains this substring")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="list discovered cases and exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show per-case timings for passes too")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temporary build directory")
    args = ap.parse_args(argv)

    cases = discover(args.paths)
    if args.filter:
        needle = args.filter.lower()
        cases = [c for c in cases if needle in c.label.lower()]
    if not cases:
        print("no test cases found", file=sys.stderr)
        return 2
    if args.list_only:
        for c in cases:
            kind = "compile-fail" if c.fail_substr else "run"
            print(f"{c.label:40s} {kind}")
        print(f"\n{len(cases)} cases")
        return 0

    workdir = tempfile.mkdtemp(prefix="lambda-tests-")
    failures = []
    total_start = time.time()
    try:
        for case in cases:
            ok, message = run_case(case, workdir)
            status = "PASS" if ok else "FAIL"
            if ok and not args.verbose and "compile-fail" in message:
                print(f"[{status}] {case.label}: {message}")
            elif ok and not args.verbose:
                summary = message.split("(")[1].rstrip(")") if "(" in message else message
                print(f"[{status}] {case.label}: {summary}")
            else:
                print(f"[{status}] {case.label}: {message}")
            if not ok:
                failures.append((case.label, message))
    finally:
        if args.keep:
            print(f"\nbuild directory kept at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    elapsed = time.time() - total_start
    print(f"\n{len(cases) - len(failures)}/{len(cases)} passed in {elapsed:.2f}s")
    if failures:
        print("\nFailures:", file=sys.stderr)
        for label, message in failures:
            print(f"  {label}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
