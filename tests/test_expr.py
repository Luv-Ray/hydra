"""Tests for expr module: templates and when evaluation."""

from __future__ import annotations

import pytest

from hydra.expr import eval_expr, expand_template, expr_refs, parse_expr, verdict_literals
from hydra.errors import TemplateError


def eval_when(expr, inputs, verdicts=None):
    return eval_expr(parse_expr(expr), inputs, verdicts or {})


def test_verdict_references():
    expr = "steps.review.verdict == 'ACCEPT' or steps.gate.verdict in ['OK', 'SKIP']"
    assert eval_when(expr, {}, {"review": "ACCEPT"}) is True
    assert eval_when(expr, {}, {"gate": "SKIP"}) is True
    # A step with no verdict (skipped / not run) reads as None and matches nothing.
    assert eval_when(expr, {}, {}) is False
    assert eval_when("steps.gate.verdict == None", {}, {}) is True
    assert expr_refs(parse_expr(expr)) == (set(), {"review", "gate"})
    assert sorted(verdict_literals(parse_expr(expr))) == [
        ("gate", "OK"), ("gate", "SKIP"), ("review", "ACCEPT"),
    ]


def test_unsupported_constructs_rejected():
    for bad in ("steps.a.output == 'x'", "inputs.a[0] == 'x'", "len(inputs.a) == 1",
                "inputs.a < 'b'", "steps.a.verdict == inputs.b + 'x'"):
        with pytest.raises(TemplateError):
            parse_expr(bad)


def test_expand_simple():
    assert expand_template("Bug: {{ inputs.desc }}", {"desc": "crash"}) == "Bug: crash"


def test_expand_multiple():
    result = expand_template(
        "{{ inputs.a }} and {{ inputs.b }}", {"a": "X", "b": "Y"}
    )
    assert result == "X and Y"


def test_expand_missing_raises():
    with pytest.raises(TemplateError, match="Undefined input"):
        expand_template("{{ inputs.missing }}", {})


def test_expand_no_templates():
    assert expand_template("plain text", {}) == "plain text"


def test_when_equality():
    assert eval_when("{{ inputs.sev == 'critical' }}", {"sev": "critical"}) is True
    assert eval_when("{{ inputs.sev == 'critical' }}", {"sev": "low"}) is False


def test_when_not_equal():
    assert eval_when("{{ inputs.mode != 'debug' }}", {"mode": "release"}) is True


def test_when_and():
    assert eval_when(
        "{{ inputs.a == 'x' and inputs.b == 'y' }}", {"a": "x", "b": "y"}
    ) is True
    assert eval_when(
        "{{ inputs.a == 'x' and inputs.b == 'y' }}", {"a": "x", "b": "z"}
    ) is False


def test_when_or():
    assert eval_when(
        "{{ inputs.a == 'x' or inputs.a == 'y' }}", {"a": "y"}
    ) is True


def test_when_not():
    assert eval_when("{{ not inputs.debug == 'true' }}", {"debug": "false"}) is True


def test_when_in():
    assert eval_when("{{ inputs.env in 'prod,staging' }}", {"env": "prod"}) is True


def test_when_missing_input_is_empty():
    assert eval_when("{{ inputs.missing == '' }}", {}) is True


def test_when_invalid_syntax():
    with pytest.raises(TemplateError):
        eval_when("{{ import os }}", {})
