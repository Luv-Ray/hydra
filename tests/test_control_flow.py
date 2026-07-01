"""Tests for retry, back-jump, and check routing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hydra.executor import ExecutionEngine
from hydra.errors import ExecutionError
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class FailThenSucceedProvider:
    """Fails N times then succeeds."""

    def __init__(self, fail_count: int = 1):
        self.fail_count = fail_count
        self.calls: list[tuple[str, str]] = []
        self._attempt: dict[str, int] = {}
        self._call_count = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._call_count += 1
        self.calls.append((options.step_id, prompt[:200]))
        attempt = self._attempt.get(options.step_id, 0)
        self._attempt[options.step_id] = attempt + 1

        return StepResult(
            session_id=f"session-{options.step_id}-{self._call_count}",
            output_text=f"Output attempt {attempt + 1}",
        )

    async def check(self, session_id: str, prompt: str) -> str:
        # Extract step_id from session_id
        parts = session_id.split("-")
        step_id = parts[1] if len(parts) > 1 else ""
        attempt = self._attempt.get(step_id, 0)
        if attempt <= self.fail_count:
            return "no"
        return "yes"


@pytest.fixture
def retry_pipeline_yaml(tmp_path: Path) -> Path:
    content = """\
name: test-retry
version: 1

steps:
  - id: flaky
    prompt: "Do flaky work"
    output: out/result.md
    retry: 3
    retry_inject: check_output
    check:
      prompt: "Success? yes/no"
      success_pattern: "yes"
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def backjump_pipeline_yaml(tmp_path: Path) -> Path:
    content = """\
name: test-backjump
version: 1

steps:
  - id: draft
    prompt: "Write draft"
    output: out/draft.md

  - id: review
    after: [draft]
    inject: [out/draft.md]
    prompt: "Review the draft"
    output: out/review.md

  - id: revise
    after: [review]
    inject: [out/draft.md, out/review.md]
    prompt: "Revise based on feedback"
    output: out/draft.md
    max_iterations: 3
    check:
      prompt: "Approved? yes/no"
      success_pattern: "yes"
    routes:
      - if: success
        goto: $end
      - if: failure
        goto: review
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def test_retry_succeeds_after_failures(retry_pipeline_yaml: Path, tmp_path: Path):
    pipeline = load_pipeline(retry_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    provider = FailThenSucceedProvider(fail_count=2)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    # Should have been called 3 times (2 failures + 1 success)
    assert len(provider.calls) == 3


def test_retry_feedback_injected(retry_pipeline_yaml: Path, tmp_path: Path):
    pipeline = load_pipeline(retry_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    provider = FailThenSucceedProvider(fail_count=1)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    # Second call should have retry feedback in prompt
    assert len(provider.calls) >= 2
    second_prompt = provider.calls[1][1]
    assert "Output from previous attempt" in second_prompt or "Retry" in second_prompt


def test_backjump_loop(backjump_pipeline_yaml: Path, tmp_path: Path):
    pipeline = load_pipeline(backjump_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    # Fails twice then succeeds
    provider = FailThenSucceedProvider(fail_count=2)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    # draft(1) + review(1) + revise(fail) + review(2) + revise(fail) + review(3) + revise(success)
    step_ids = [c[0] for c in provider.calls]
    assert step_ids.count("review") >= 2
    assert step_ids.count("revise") >= 2
