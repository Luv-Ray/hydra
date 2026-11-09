"""Timeout is opt-in: unset means no wall-clock limit.

Regression guard. timeout was once a dead field (never enforced), so pipelines
carried stale values like `timeout: 600`. When enforcement was added, a nonzero
engine default would have silently capped every step — this locks in that the
absence of a timeout resolves to None (unlimited), not to a hidden default.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from hydra.loader import load_pipeline


def _load(yaml: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent(yaml))
        path = f.name
    return load_pipeline(Path(path))


def test_no_timeout_anywhere_is_unlimited():
    pl = _load(
        """
        name: t
        version: 2
        steps:
          - id: a
            prompt: go
        """
    )
    assert pl.steps[0].timeout is None


def test_defaults_timeout_inherited():
    pl = _load(
        """
        name: t
        version: 2
        defaults:
          timeout: 600
        steps:
          - id: a
            prompt: go
        """
    )
    assert pl.steps[0].timeout == 600


def test_step_timeout_overrides_default():
    pl = _load(
        """
        name: t
        version: 2
        defaults:
          timeout: 600
        steps:
          - id: a
            prompt: go
          - id: b
            prompt: go
            timeout: 60
        """
    )
    got = {s.id: s.timeout for s in pl.steps}
    assert got == {"a": 600, "b": 60}
