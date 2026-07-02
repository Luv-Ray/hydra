"""Tests for success_pattern check semantics.

Regression coverage for the verdict-routing bug: a verdict agent wrote
"ACCEPT" then appended a "REJECT" correction below it. A whole-document
substring search matched the superseded "ACCEPT" and routed to success,
skipping the revise loop. success_pattern must match the final verdict line.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hydra.executor import ExecutionEngine, _last_nonempty_line
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class MockProvider:
    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        self._call_count = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._call_count += 1
        self.calls.append((options.step_id, prompt[:100]))
        text = self.responses.get(options.step_id, f"Output of {options.step_id}")
        return StepResult(
            session_id=f"session-{options.step_id}-{self._call_count}",
            output_text=text,
        )

    async def check(self, session_id: str, prompt: str) -> str:
        return "yes"


def test_last_nonempty_line():
    assert _last_nonempty_line("ACCEPT") == "ACCEPT"
    assert _last_nonempty_line("foo\nREJECT\n\n") == "REJECT"
    assert _last_nonempty_line("  spaced  ") == "spaced"
    assert _last_nonempty_line("") == ""
    assert _last_nonempty_line("\n\n\n") == ""


@pytest.fixture
def verdict_pipeline_yaml(tmp_path: Path) -> Path:
    content = """\
name: test-verdict
version: 1

steps:
  - id: verdict
    prompt: "Judge it"
    output: out/verdict.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: success
        goto: accepted
      - if: failure
        goto: rejected

  - id: accepted
    trigger: route
    prompt: "Accepted path"
    output: out/accepted.md

  - id: rejected
    trigger: route
    prompt: "Rejected path"
    output: out/rejected.md
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def _run(pipeline_yaml: Path, tmp_path: Path, verdict_text: str) -> list[str]:
    pipeline = load_pipeline(pipeline_yaml)
    pipeline.workdir = str(tmp_path)
    provider = MockProvider({"verdict": verdict_text})
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    asyncio.run(engine.run())
    return [c[0] for c in provider.calls]


def test_clean_accept_routes_to_success(verdict_pipeline_yaml, tmp_path):
    steps = _run(verdict_pipeline_yaml, tmp_path, "All good.\n\nACCEPT")
    assert "accepted" in steps
    assert "rejected" not in steps


def test_clean_reject_routes_to_failure(verdict_pipeline_yaml, tmp_path):
    steps = _run(verdict_pipeline_yaml, tmp_path, "Problems found.\n\nREJECT")
    assert "rejected" in steps
    assert "accepted" not in steps


def test_self_corrected_verdict_uses_final_line(verdict_pipeline_yaml, tmp_path):
    """The exact ISS-202606-00052328A failure: ACCEPT then a REJECT correction."""
    text = (
        "均超过裁决阈值。\n\n"
        "ACCEPT\n\n"
        "---\n\n"
        "更正:按判定标准存在任何高严重度问题即 REJECT,最终裁决为:\n\n"
        "REJECT"
    )
    steps = _run(verdict_pipeline_yaml, tmp_path, text)
    assert "rejected" in steps
    assert "accepted" not in steps
