"""Static semantics: spec 7.3 (resume), 7.4 (errors), 7.5 (scope), 7.6 (exhaustiveness), 5.1 (linearity)."""

import textwrap

from pilang.checker import check
from pilang.parser import parse


def static(text: str):
    mod = parse(textwrap.dedent(text), "<test>")
    return [d.message for d in check(mod) if d.severity == "error"]


def static_warnings(text: str):
    mod = parse(textwrap.dedent(text), "<test>")
    return [d.message for d in check(mod) if d.severity == "warning"]

PROTO = """
protocol M {
  op echo : (Float) → Shard
  op bump : () → Shard
  errors : { 10001: "oops" recoverable, 10002: "boom" terminal }
  purity : Pure
}
"""


def test_protocol_requires_errors():
    errs = static("protocol P { op a : () → () \n purity : Pure }")
    assert any("no `errors` declaration" in e for e in errs)


def test_reserved_code_range_warns_not_rejects():
    w = static_warnings("protocol P { op a : () → ()\n errors : { 4001: \"x\" }\n purity : Pure }")
    assert any("reserved range" in x for x in w)


def test_resume_rejected_in_stateless_element():                    # law 8
    errs = static(PROTO + """
    elem E() {
      in tick : Chan[()]
      worker w : Worker⟨M⟩
      state s : Shard
      ctrl c =
        serve(tick) { _ ⇒ s := await call(w, "echo", s) }
      on_failure {
        | WorkerError(10001) ⇒ { await call(w, "bump"); resume }
        | _ ⇒ escalate
      }
    }""")
    assert any("resume requires stateful worker" in e for e in errs)


def test_resume_accepted_in_stateful_element():
    errs = static(PROTO + """
    elem E() {
      in tick : Chan[()]
      worker w : Worker⟨M⟩
      ctrl c =
        serve(tick) { _ ⇒ await call(w, "bump") }
      on_failure {
        | WorkerError(10001) ⇒ resume
        | _ ⇒ escalate
      }
    }""")
    assert errs == []


def test_resume_outside_handler_rejected():
    errs = static("program P() = scope { resume }")
    assert any("only valid inside" in e for e in errs)


def test_contract_resume_capable_checked_against_state_field():
    errs = static(PROTO + """
    elem E() {
      in tick : Chan[()]
      worker w : Worker⟨M⟩
      state s : Shard
      ctrl c = serve(tick) { _ ⇒ s := await call(w, "echo", s) }
      contract { recovery = resume-capable }
    }""")
    assert any("contract claims recovery = resume-capable" in e for e in errs)


def test_nonexhaustive_on_error_rejected():                          # law 1
    errs = static(PROTO + """
    program P() = scope {
      w → Pool("M", 1).recruit(1)
      on_error(await call(w, "echo", 1.0)) {
        | WorkerDied ⇒ escalate
      }
    }""")
    assert any("non-exhaustive on_error" in e for e in errs)


def test_wildcard_makes_on_error_exhaustive():
    errs = static(PROTO + """
    program P() = scope {
      w → Pool("M", 1).recruit(1)
      on_error(await call(w, "echo", 1.0)) {
        | WorkerDied ⇒ escalate
        | _ ⇒ stop
      }
    }""")
    assert errs == []


def test_all_constructors_listed_is_exhaustive():
    arms = "\n".join(f"| {c} ⇒ escalate" for c in
                     ["WorkerDied", "Deadline", "Transport", "Rejected(_)", "BadFrame", "WorkerError(_)",
                      "Cancelled", "NoQuorum", "Incomplete", "Unsupported"])
    errs = static(PROTO + f"program P() = scope {{ w → Pool(\"M\", 1).recruit(1)\n on_error(await call(w, \"echo\", 1.0)) {{\n{arms}\n}} }}")
    assert errs == []


def test_workererror_with_specific_code_does_not_cover_constructor():
    errs = static(PROTO + """
    program P() = scope {
      w → Pool("M", 1).recruit(1)
      on_error(await call(w, "echo", 1.0)) {
        | WorkerDied ⇒ escalate | Deadline ⇒ escalate | Transport ⇒ escalate | Rejected(_) ⇒ escalate
        | BadFrame ⇒ escalate | Cancelled ⇒ escalate | NoQuorum ⇒ escalate | Incomplete ⇒ escalate
        | Unsupported ⇒ escalate
        | WorkerError(10001) ⇒ escalate
      }
    }""")
    assert any("WorkerError" in e for e in errs)


def test_handler_code_must_be_declared_by_the_elements_protocols():   # 7.4
    errs = static(PROTO + """
    elem E() {
      in tick : Chan[()]
      worker w : Worker⟨M⟩
      ctrl c = serve(tick) { _ ⇒ await call(w, "bump") }
      on_failure { | WorkerError(77) ⇒ resume | _ ⇒ escalate }
    }""")
    assert any("declares code 77" in e for e in errs)


def test_unknown_op_rejected():
    errs = static(PROTO + """
    elem E() {
      in tick : Chan[()]
      worker w : Worker⟨M⟩
      ctrl c = serve(tick) { _ ⇒ await call(w, "nope") }
    }""")
    assert any("no op 'nope'" in e for e in errs)


def test_serve_outside_scope_rejected():                             # 7.5
    errs = static("program P() = serve(c) { x ⇒ x }")
    assert any("outside any scope" in e for e in errs)


def test_ctrl_counts_as_scope():
    assert static(PROTO + "elem E() { in tick : Chan[()]\n ctrl c = serve(tick) { _ ⇒ () } }") == []


def test_future_used_after_await_rejected():                         # P1
    errs = static(PROTO + """
    program P() = scope {
      w → Pool("M", 1).recruit(1)
      f → call(w, "echo", 1.0)
      a → await f
      b → await f
    }""")
    assert any("used after it was consumed" in e for e in errs)


def test_cancel_then_await_is_fine_but_await_then_cancel_is_not():   # law 3
    ok = static(PROTO + """
    program P() = scope { w → Pool("M", 1).recruit(1)
      f → call(w, "echo", 1.0)
      cancel(f)
      cancel(f)
      await f }""")
    assert ok == []
    bad = static(PROTO + """
    program P() = scope { w → Pool("M", 1).recruit(1)
      f → call(w, "echo", 1.0)
      await f
      cancel(f) }""")
    assert any("used after it was consumed" in e for e in bad)


def test_channel_end_used_after_close_rejected():
    errs = static("program P() = scope { (tx, rx) → chan(cap = 1)\n close(tx)\n send(tx, ()) }")
    assert any("used after it was consumed" in e for e in errs)

