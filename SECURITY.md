# Security policy

Lambda and Pi are research software.  They execute **trusted local code** —
generated native executables and worker processes — and are **not a sandbox**
for untrusted programs (see [`docs/safety.md`](docs/safety.md)).  They do,
however, handle source files, command lines, worker protocols, artifacts and a
local web viewer, so memory-safety and input-handling bugs are taken seriously.

## Supported versions

Security fixes target the current `main` branch of
[`deterministic-plen/lambda`](https://github.com/deterministic-plen/lambda).
Older snapshots and forks may not receive backports.

## Reporting a vulnerability

**Please report privately — do not open a public issue for a vulnerability.**

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/deterministic-plen/lambda/security/advisories/new>

In the report, include:

- a short description and the security impact;
- the commit or release you tested;
- the platform (Windows/Linux/macOS) and compiler versions;
- a minimal reproducer (a `.lambda`/`.pi` file, command line or input file);
- any suggested fix or mitigation.

If GitHub private reporting is unavailable to you, open a minimal public issue
that only asks the maintainers for a private contact channel; do not include
exploit details in it.

## Scope

In scope:

- the compiler driver and front end (`lambda.py`, `lexer.py`, `parser.py`,
  `resolve.py`, `analysis.py`, `typecheck.py`, `compiler.py`, `loader.py`);
- the C runtime emitted by `runtime.py`, including file I/O, arrays,
  reference counting, the thread pool and the GPU/NVRTC loader;
- the bundled standard library in `lib/`;
- the Pi frontend, C++ runtime and worker protocol in `Pi/`;
- the local viewer and plotting tools (`tools/`).

Out of scope / by design:

- bugs that only affect the *semantics* of a program you compiled (report them
  as ordinary issues);
- generated executables being able to consume CPU, memory, disk or invoke the
  configured C compiler — Lambda is not a sandbox;
- a malicious Pi worker binary (`Pi` trusts worker artifacts; protocol checks
  catch accidental mismatch, not a hostile binary);
- vulnerabilities in third-party LLVM/clang/CUDA installations.

## What to expect

- Acknowledgement of a private report: target within 7 days.
- An initial assessment (valid / not valid, severity): target within 14 days.
- A fix or documented mitigation, then coordinated disclosure.  The audit
  history and fix status are tracked in
  [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) and
  [`docs/PR-COMPLIANCE.md`](docs/PR-COMPLIANCE.md).

## Safe-harbour

Good-faith security research that follows this policy (no privacy violations,
no service disruption, no data destruction, and disclosure coordinated with the
maintainers) will not be pursued legally by the project.
