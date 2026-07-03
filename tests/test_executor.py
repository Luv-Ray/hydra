"""Tests for the execution engine using a mock provider."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class MockProvider:
    """Mock provider that returns canned responses."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        self.check_calls: list[tuple[str, str]] = []
        self._call_count = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._call_count += 1
        self.calls.append((options.step_id, prompt[:100]))
        text = self.responses.get(options.step_id, f"Output of {options.step_id}")
        return StepResult(
            session_id=f"session-{options.step_id}-{self._call_count}",
            output_text=text,
        )

    async def check(self, session_id: str, prompt: str, cwd: str | None = None) -> str:
        self.check_calls.append((session_id, prompt))
        return "yes"


@pytest.fixture
def simple_pipeline_yaml(tmp_path: Path) -> Path:
    content = """\
name: test-simple
version: 1

steps:
  - id: step_a
    prompt: "Do step A"
    output: out/a.md

  - id: step_b
    inject: [out/a.md]
    prompt: "Do step B using A's output"
    output: out/b.md
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def route_pipeline_yaml(tmp_path: Path) -> Path:
    content = """\
name: test-routes
version: 1

steps:
  - id: work
    prompt: "Do work"
    output: out/work.md
    check:
      prompt: "Success? yes/no"
      success_pattern: "yes"
    routes:
      - if: success
        goto: done
      - if: failure
        goto: fallback

  - id: done
    trigger: route
    prompt: "Generate success report"
    output: out/done.md

  - id: fallback
    trigger: route
    prompt: "Generate failure report"
    output: out/fallback.md
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def test_simple_dag_execution(simple_pipeline_yaml: Path, tmp_path: Path):
    pipeline = load_pipeline(simple_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    provider = MockProvider({"step_a": "Result A", "step_b": "Result B"})
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    assert len(provider.calls) == 2
    assert provider.calls[0][0] == "step_a"
    assert provider.calls[1][0] == "step_b"

    # Verify output files were written
    assert (tmp_path / "out" / "a.md").read_text() == "Result A"
    assert (tmp_path / "out" / "b.md").read_text() == "Result B"

    # Verify inject: step_b prompt should include step_a's output
    step_b_prompt = provider.calls[1][1]
    assert "Result A" in step_b_prompt or "[File:" in step_b_prompt


def test_route_success(route_pipeline_yaml: Path, tmp_path: Path):
    pipeline = load_pipeline(route_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    provider = MockProvider({"work": "Done", "done": "Success report"})
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    # work + done should run, not fallback
    step_ids = [c[0] for c in provider.calls]
    assert "work" in step_ids
    assert "done" in step_ids
    assert "fallback" not in step_ids


def test_dry_run(simple_pipeline_yaml: Path, tmp_path: Path, capsys):
    pipeline = load_pipeline(simple_pipeline_yaml)
    pipeline.workdir = str(tmp_path)

    provider = MockProvider()
    engine = ExecutionEngine(
        pipeline=pipeline, inputs={}, provider=provider, dry_run=True
    )
    result = asyncio.run(engine.run())

    assert result == "success"
    assert len(provider.calls) == 0  # Nothing executed

    captured = capsys.readouterr()
    assert "Execution layers:" in captured.out
