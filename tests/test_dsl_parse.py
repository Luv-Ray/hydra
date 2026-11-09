"""Strict parsing: unknown keys, wrong types and the wrong version fail at load."""

from __future__ import annotations

import pytest

from helpers import load
from hydra.errors import ValidationError


def test_unknown_step_key_rejected(tmp_path):
    # v1 silently dropped this typo; the step then wrote no file.
    with pytest.raises(ValidationError, match=r"unknown key\(s\) \['outptu'\]"):
        load(tmp_path, """
            version: 2
            steps:
              - id: a
                prompt: x
                outptu: a.md
        """)


def test_unknown_top_level_and_loop_keys_rejected(tmp_path):
    with pytest.raises(ValidationError, match="unknown key"):
        load(tmp_path, "version: 2\nstepz: []\nsteps: [{id: a, prompt: x}]\n")
    with pytest.raises(ValidationError, match=r"unknown key\(s\) \['max_iterations'\]"):
        load(tmp_path, """
            version: 2
            steps:
              - {id: a, prompt: x, verdict: [OK, BAD]}
            loops:
              - {id: l, body: [a], until: "steps.a.verdict == 'OK'", max_rounds: 2, max_iterations: 3}
        """)


@pytest.mark.parametrize("old_key", ["routes", "check", "trigger", "loop_body", "retry_inject", "max_iterations"])
def test_v1_step_keys_are_gone(tmp_path, old_key):
    with pytest.raises(ValidationError, match="unknown key"):
        load(tmp_path, f"""
            version: 2
            steps:
              - id: a
                prompt: x
                {old_key}: 1
        """)


@pytest.mark.parametrize("version", ["", "version: 1", "version: '2'"])
def test_version_must_be_2(tmp_path, version):
    with pytest.raises(ValidationError, match="Unsupported DSL version"):
        load(tmp_path, f"{version}\nsteps: [{{id: a, prompt: x}}]\n")


def test_wrong_types_rejected(tmp_path):
    with pytest.raises(ValidationError, match="'timeout' has wrong type"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, timeout: soon}]\n")
    with pytest.raises(ValidationError, match="must not be a boolean"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, retry: true}]\n")
    with pytest.raises(ValidationError, match="'fork' must be a boolean"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x}, {id: b, prompt: x, resume: a, fork: yes please}]\n")


def test_verdict_short_and_long_form(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: a, prompt: x, verdict: [ACCEPT, REJECT]}
          - id: b
            prompt: x
            verdict: {values: ['yes', 'no'], prompt: "Did the tests pass?"}
          - {id: c, prompt: x, when: "steps.a.verdict == 'ACCEPT' and steps.b.verdict == 'yes'"}
    """)
    a, b, _ = p.steps
    assert a.verdict.values == ["ACCEPT", "REJECT"] and a.verdict.prompt is None
    assert b.verdict.values == ["yes", "no"] and b.verdict.prompt == "Did the tests pass?"


def test_unquoted_yaml_boolean_verdict_explained(tmp_path):
    # PyYAML reads unquoted NO / yes as booleans: [OK, NO] → ["OK", False].
    with pytest.raises(ValidationError, match="YAML boolean"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, verdict: [OK, NO]}]\n")


def test_verdict_values_must_be_distinct(tmp_path):
    with pytest.raises(ValidationError, match="duplicate values"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, verdict: [OK, ok]}]\n")


def test_loop_field_checks(tmp_path):
    base = "version: 2\nsteps: [{id: a, prompt: x, verdict: [OK, BAD]}]\nloops:\n"
    with pytest.raises(ValidationError, match="max_rounds"):
        load(tmp_path, base + "  - {id: l, body: [a], until: \"steps.a.verdict == 'OK'\", max_rounds: 0}\n")
    with pytest.raises(ValidationError, match="on_exhausted"):
        load(tmp_path, base + "  - {id: l, body: [a], until: \"steps.a.verdict == 'OK'\", max_rounds: 2, on_exhausted: goto}\n")


def test_inputs_resolution_and_undeclared(tmp_path):
    yaml_text = """
        version: 2
        inputs:
          issue: {required: true}
          severity: {default: medium}
        steps:
          - {id: a, prompt: "fix {{ inputs.issue }} at {{ inputs.severity }}"}
    """
    p = load(tmp_path, yaml_text, vars={"issue": "ISS-1"})
    assert p.input_values == {"issue": "ISS-1", "severity": "medium"}
    assert p.steps[0].prompt == "fix ISS-1 at medium"
    with pytest.raises(ValidationError, match="Missing required inputs"):
        load(tmp_path, yaml_text)
    with pytest.raises(ValidationError, match="Undeclared input"):
        load(tmp_path, yaml_text, vars={"issue": "x", "sevrity": "high"})


def test_undefined_template_variable_is_validation_error(tmp_path):
    with pytest.raises(ValidationError, match="Undefined input variable"):
        load(tmp_path, "version: 2\nsteps: [{id: a, prompt: '{{ inputs.nope }}'}]\n")
