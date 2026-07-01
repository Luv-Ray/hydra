"""Tests for expr module: templates and when evaluation."""

from __future__ import annotations

import pytest

from hydra.expr import eval_when, expand_template
from hydra.errors import TemplateError


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
