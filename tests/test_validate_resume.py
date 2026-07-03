"""Resume-uniqueness validation: many route resumers OK, one DAG resumer max.

resume appends to the target's on-disk session rather than forking it, so
concurrent resumers corrupt each other. DAG steps in a layer run in parallel
(hence 1-max), while trigger:route steps fire one at a time via goto (unlimited).
"""

from pathlib import Path

import pytest

from hydra.errors import ValidationError
from hydra.loader import load_pipeline
from hydra.validate import validate_pipeline


def _validate(tmp_path: Path, yaml: str):
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml)
    return validate_pipeline(load_pipeline(p))


TWO_ROUTE_RESUMERS = """\
name: two-route
version: 1
steps:
  - id: analyze
    prompt: "analyze"
  - id: review
    after: [analyze]
    prompt: "review"
    routes:
      - if: success
        goto: $end
      - if: failure
        goto: revise_a
  - id: revise_a
    trigger: route
    resume: analyze
    prompt: "revise a"
    routes:
      - if: success
        goto: revise_b
  - id: revise_b
    trigger: route
    resume: analyze
    prompt: "revise b"
    routes:
      - if: success
        goto: review
"""

TWO_DAG_RESUMERS = """\
name: two-dag
version: 1
steps:
  - id: analyze
    prompt: "analyze"
  - id: review_a
    after: [analyze]
    resume: analyze
    prompt: "review a"
  - id: review_b
    after: [analyze]
    resume: analyze
    prompt: "review b"
"""

ONE_DAG_ONE_ROUTE = """\
name: mixed
version: 1
steps:
  - id: analyze
    prompt: "analyze"
  - id: review
    after: [analyze]
    resume: analyze
    prompt: "review"
    routes:
      - if: success
        goto: $end
      - if: failure
        goto: revise
  - id: revise
    trigger: route
    resume: analyze
    prompt: "revise"
    routes:
      - if: success
        goto: review
"""


def test_multiple_route_resumers_allowed(tmp_path: Path):
    # mini-revise + revise both resuming analyze is the real-world case.
    _validate(tmp_path, TWO_ROUTE_RESUMERS)


def test_multiple_dag_resumers_rejected(tmp_path: Path):
    # Two parallel DAG steps resuming the same session would corrupt it.
    with pytest.raises(ValidationError, match="Multiple non-route steps resume"):
        _validate(tmp_path, TWO_DAG_RESUMERS)


def test_one_dag_one_route_resumer_allowed(tmp_path: Path):
    # A single DAG resumer plus a route resumer never overlap in time.
    _validate(tmp_path, ONE_DAG_ONE_ROUTE)


BAD_WHEN = """\
name: bad-when
version: 1
inputs:
  env:
    default: dev
steps:
  - id: a
    prompt: "go"
    when: "inputs.env ==="
"""

GOOD_WHEN = """\
name: good-when
version: 1
inputs:
  env:
    default: dev
steps:
  - id: a
    prompt: "go"
    when: 'inputs.env == "prod"'
"""


def test_malformed_when_rejected(tmp_path: Path):
    # A syntax error in a when-expression must fail validation, not be swallowed.
    with pytest.raises(ValidationError, match="invalid when expression"):
        _validate(tmp_path, BAD_WHEN)


def test_valid_when_passes(tmp_path: Path):
    _validate(tmp_path, GOOD_WHEN)
