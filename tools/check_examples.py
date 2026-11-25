"""Compile every shipped .lambda program (CI gate for PR 3C).

The test suite covers tests/, but examples/learn/simulations/bootstrap are not
exercised by run_tests.py.  This script compiles each one with the flags the
source needs (`--data` for write-* primitives, `--input` for read-*), so a
syntax or typing regression in shipped programs fails CI.

Usage: python tools/check_examples.py [-v]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUPS = ("examples", "learn", "simulations", "bootstrap")

DATA_RE = re.compile(r"\b(write-bytes|write-f64s|write-i64s|emit-bytes|emit-f64s|emit-i64s)\b")
INPUT_RE = re.compile(r"\b(read-line|read-int|read-float)\b")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    failures = []
    count = 0
    with tempfile.TemporaryDirectory(prefix="lambda-examples-") as tmp:
        for group in GROUPS:
            base = os.path.join(ROOT, group)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames.sort()
                for name in sorted(filenames):
                    if not name.endswith(".lambda"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        source = open(path, encoding="utf-8").read()
                    except OSError as exc:
                        failures.append((path, str(exc)))
                        continue
                    flags = []
                    if (DATA_RE.search(source)
                            or re.search(r'include\s+"[^"]*stream[^"]*"', source)):
                        flags.append("--data")
                    if INPUT_RE.search(source):
                        flags.append("--input")
                    count += 1
                    out = os.path.join(tmp, "example.exe")
                    proc = subprocess.run(
                        [sys.executable, os.path.join(ROOT, "lambda.py"), path,
                         "-o", out] + flags,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300)
                    if (proc.returncode != 0
                            and "program ends after its definitions" in proc.stderr):
                        # Definitions-only includes are valid modules; compile
                        # them through a tiny wrapper so they are still checked.
                        wrapper = os.path.join(tmp, "wrapper.lambda")
                        inc = os.path.relpath(path, tmp).replace(os.sep, "/")
                        with open(wrapper, "w", encoding="utf-8") as f:
                            f.write(f'include "{inc}"\n0\n')
                        proc = subprocess.run(
                            [sys.executable, os.path.join(ROOT, "lambda.py"),
                             wrapper, "-o", out] + flags,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
                    if proc.returncode != 0:
                        failures.append((path, proc.stderr.strip()))
                    elif args.verbose:
                        print(f"ok {os.path.relpath(path, ROOT)}")
    print(f"{count - len(failures)}/{count} shipped programs compile"
          f" (groups: {', '.join(GROUPS)})")
    for path, message in failures:
        print(f"FAIL {os.path.relpath(path, ROOT)}", file=sys.stderr)
        print(message, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
