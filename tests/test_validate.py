"""Cross-reference and graph validation."""

from __future__ import annotations

import pytest

from helpers import load
from hydra.errors import ValidationError
from hydra.validate import validate_pipeline


def _bad(tmp_path, yaml_text, match):
    with pytest.raises(ValidationError, match=match):
        load(tmp_path, yaml_text)


def test_ids_must_be_identifiers(tmp_path):
    # `steps.mini-review.verdict` would parse as subtraction.
    _bad(tmp_path, "version: 2\nsteps: [{id: mini-review, prompt: x}]\n", "Invalid step id")


def test_step_and_loop_ids_share_a_namespace(tmp_path):
    _bad(tmp_path, """
        version: 2
        steps: [{id: a, prompt: x, verdict: [OK, BAD]}]
        loops: [{id: a, body: [a], until: "steps.a.verdict == 'OK'", max_rounds: 2}]
    """, "Duplicate id")


def test_unknown_references(tmp_path):
    _bad(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, after: [b]}]\n", "unknown step 'b'")
    _bad(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, resume: b}]\n", "unknown step 'b'")


def test_fork_and_compact_require_resume(tmp_path):
    _bad(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, fork: true}]\n", "'fork' requires 'resume'")
    _bad(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, compact_before_resume: 50}]\n",
         "requires 'resume'")


def test_two_steps_one_output(tmp_path):
    _bad(tmp_path, """
        version: 2
        steps:
          - {id: a, prompt: x, output: r.md}
          - {id: b, prompt: x, output: r.md}
    """, "both write output 'r.md'")


def test_when_reference_checks(tmp_path):
    head = "version: 2\ninputs: {sev: {default: low}}\nsteps:\n  - {id: a, prompt: x, verdict: [ACCEPT, REJECT]}\n"
    _bad(tmp_path, head + "  - {id: b, prompt: x, when: \"inputs.sevrity == 'x'\"}\n", "not a declared input")
    _bad(tmp_path, head + "  - {id: b, prompt: x, when: \"steps.zz.verdict == 'x'\"}\n", "not a step")
    _bad(tmp_path, head + "  - {id: b, prompt: x}\n  - {id: c, prompt: x, when: \"steps.b.verdict == 'x'\"}\n",
         "declares no verdict")
    # A typo'd verdict literal would otherwise just never match.
    _bad(tmp_path, head + "  - {id: b, prompt: x, when: \"steps.a.verdict == 'ACCPET'\"}\n",
         "not one of its values")
    _bad(tmp_path, head + "  - {id: b, prompt: x, when: \"steps.a.verdict in ['ACCEPT', 'NOPE']\"}\n",
         "not one of its values")
    _bad(tmp_path, head + "  - {id: b, prompt: x, when: \"len(inputs.sev) > 1\"}\n", "Unsupported")


def test_until_must_read_body_verdicts(tmp_path):
    head = (
        "version: 2\nsteps:\n"
        "  - {id: gate, prompt: x, verdict: [OK, BAD]}\n"
        "  - {id: other, prompt: x, verdict: [OK, BAD]}\n"
        "  - {id: fix, prompt: x}\n"
    )
    _bad(tmp_path, head + "loops: [{id: l, body: [gate], fix: fix, until: \"steps.other.verdict == 'OK'\", max_rounds: 2}]\n",
         "not in body")
    _bad(tmp_path, head + "loops: [{id: l, body: [gate], until: 'True', max_rounds: 2}]\n",
         "must read the verdict")


def test_loop_membership_rules(tmp_path):
    head = (
        "version: 2\nsteps:\n"
        "  - {id: gate, prompt: x, verdict: [OK, BAD]}\n"
        "  - {id: fix, prompt: x}\n"
    )
    until = "\"steps.gate.verdict == 'OK'\""
    _bad(tmp_path, head + f"loops: [{{id: l, body: [gate], fix: gate, until: {until}, max_rounds: 2}}]\n",
         "must not also be in body")
    _bad(tmp_path, head + (
        f"loops:\n"
        f"  - {{id: l1, body: [gate], until: {until}, max_rounds: 2}}\n"
        f"  - {{id: l2, body: [gate], until: {until}, max_rounds: 2}}\n"
    ), "belongs to both")


def test_body_may_not_resume_its_fix(tmp_path):
    _bad(tmp_path, """
        version: 2
        steps:
          - {id: fix, prompt: x}
          - {id: gate, prompt: x, resume: fix, verdict: [OK, BAD]}
        loops: [{id: l, body: [gate], fix: fix, until: "steps.gate.verdict == 'OK'", max_rounds: 2}]
    """, "has no session in round 1")


def test_cycle_detected_regardless_of_declaration_order(tmp_path):
    _bad(tmp_path, """
        version: 2
        steps:
          - {id: a, prompt: x, after: [b]}
          - {id: b, prompt: x, after: [a]}
    """, "Circular dependency")


def test_loop_body_that_is_not_closed_is_a_cycle(tmp_path):
    # mid sits between two body steps but is outside the loop: the loop node
    # would both feed and wait for it.
    _bad(tmp_path, """
        version: 2
        steps:
          - {id: first, prompt: x, output: f.md}
          - {id: mid, prompt: x, inject: [f.md], output: m.md}
          - {id: last, prompt: x, inject: [m.md], verdict: [OK, BAD]}
        loops: [{id: l, body: [first, last], until: "steps.last.verdict == 'OK'", max_rounds: 2}]
    """, "Circular dependency")


RESUMERS = """
    version: 2
    steps:
      - {{id: analyze, prompt: x}}
      - {{id: r1, prompt: x, resume: analyze{f1}}}
      - {{id: r2, prompt: x, resume: analyze{f2}{order}}}
"""


def test_concurrent_append_resumers_rejected(tmp_path):
    _bad(tmp_path, RESUMERS.format(f1="", f2="", order=""), "may run concurrently")
    _bad(tmp_path, RESUMERS.format(f1=", fork: true", f2="", order=""), "may run concurrently")


def test_concurrent_resumers_ok_when_forked_or_ordered(tmp_path):
    load(tmp_path, RESUMERS.format(f1=", fork: true", f2=", fork: true", order=""))
    load(tmp_path, RESUMERS.format(f1="", f2="", order=", after: [r1]"))


def test_loop_fix_and_outside_resumer_are_serialized(tmp_path):
    # fix (in the loop) and final (after the loop) both append to analyze:
    # final depends on the loop, so they can never overlap.
    load(tmp_path, """
        version: 2
        steps:
          - {id: analyze, prompt: x, output: rc.md}
          - {id: review, prompt: x, inject: [rc.md], verdict: [OK, BAD]}
          - {id: revise, prompt: x, resume: analyze}
          - {id: final, prompt: x, resume: analyze, after: [review]}
        loops: [{id: l, body: [review], fix: revise, until: "steps.review.verdict == 'OK'", max_rounds: 3}]
    """)


def test_warnings(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: a, prompt: x, verdict: [OK, BAD]}
          - {id: b, prompt: x, inject: ["reports/*.md"]}
    """, validate=False)
    warnings = validate_pipeline(p)
    assert any("no when/until reads" in w for w in warnings)
    assert any("injects glob" in w for w in warnings)
