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

    async def check(self, session_id: str, prompt: str, cwd: str | None = None) -> str:
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


class NeverConvergeProvider:
    """check always fails, so any convergence loop exhausts max_iterations."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._n += 1
        self.calls.append((options.step_id, prompt[:200]))
        return StepResult(
            session_id=f"session-{options.step_id}-{self._n}",
            output_text=f"out-{options.step_id}",
        )

    async def check(self, session_id: str, prompt: str, cwd: str | None = None) -> str:
        return "no"


def _escape_pipeline(tmp_path: Path, revise_routes: str) -> Path:
    content = f"""\
name: test-escape
version: 1

steps:
  - id: draft
    prompt: "Write draft"
    output: out/draft.md

  - id: revise
    after: [draft]
    inject: [out/draft.md]
    prompt: "Revise"
    output: out/draft.md
    max_iterations: 2
    check:
      prompt: "Approved? yes/no"
      success_pattern: "yes"
    routes:
{revise_routes}

  - id: publish
    trigger: route
    prompt: "Publish the (possibly unconverged) draft"
    output: out/final.md
"""
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def test_max_iterations_escape_route_taken(tmp_path: Path):
    # Loop never converges; the escape route forwards to `publish` instead of
    # failing the run.
    routes = (
        "      - if: success\n        goto: $end\n"
        "      - if: failure\n        goto: revise\n"
        "      - if: max_iterations\n        goto: publish\n"
    )
    pipeline = load_pipeline(_escape_pipeline(tmp_path, routes))
    pipeline.workdir = str(tmp_path)
    provider = NeverConvergeProvider()
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    step_ids = [c[0] for c in provider.calls]
    assert "publish" in step_ids  # escape landed on the forward step


def test_max_iterations_without_escape_still_fails(tmp_path: Path):
    # No `if: max_iterations` route → exhaustion remains a hard failure.
    routes = (
        "      - if: success\n        goto: $end\n"
        "      - if: failure\n        goto: revise\n"
    )
    pipeline = load_pipeline(_escape_pipeline(tmp_path, routes))
    pipeline.workdir = str(tmp_path)
    provider = NeverConvergeProvider()
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "failure"


def test_max_iterations_escape_to_end(tmp_path: Path):
    # Escape to $end stops cleanly on the current (unconverged) result.
    routes = (
        "      - if: success\n        goto: $end\n"
        "      - if: failure\n        goto: revise\n"
        "      - if: max_iterations\n        goto: $end\n"
    )
    pipeline = load_pipeline(_escape_pipeline(tmp_path, routes))
    pipeline.workdir = str(tmp_path)
    provider = NeverConvergeProvider()
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success"
    step_ids = [c[0] for c in provider.calls]
    assert "publish" not in step_ids  # $end stops before the forward step


def test_max_iterations_escape_looping_back_is_rejected(tmp_path: Path):
    # An escape goto that points back into the already-run loop (revise) would
    # just re-arm the exhausted loop — surfaced as a failure, not silently looped.
    routes = (
        "      - if: success\n        goto: $end\n"
        "      - if: failure\n        goto: revise\n"
        "      - if: max_iterations\n        goto: revise\n"
    )
    pipeline = load_pipeline(_escape_pipeline(tmp_path, routes))
    pipeline.workdir = str(tmp_path)
    provider = NeverConvergeProvider()
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "failure"


class TimeoutThenResumeProvider:
    """First step 'times out' (session_id=None, is_error), rest succeed.

    Mirrors a pre-result timeout: the failed step produced no SDK session, so a
    downstream step that resumes it must be rejected rather than handed a fake id.
    """

    def __init__(self):
        self.calls: list[str] = []

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self.calls.append(options.step_id)
        if options.step_id == "analyze":
            return StepResult(session_id=None, output_text="", is_error=True)
        return StepResult(session_id=f"sess-{options.step_id}", output_text="ok")

    async def check(self, session_id, prompt, cwd=None):
        return "yes"

    async def compact(self, session_id, model=None, cwd=None):
        return (0, 0)


def test_resume_of_sessionless_step_is_rejected(tmp_path: Path):
    # analyze times out (no session) but routes its failure forward to review,
    # which resumes analyze. Resuming a dead session must raise, not fabricate.
    content = (
        "name: resume-dead\nversion: 1\n\nsteps:\n"
        "  - id: analyze\n    prompt: \"analyze\"\n"
        "    routes:\n      - if: failure\n        goto: review\n"
        "  - id: review\n    trigger: route\n    resume: analyze\n    prompt: \"review\"\n"
    )
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)
    engine = ExecutionEngine(
        pipeline=pipeline, inputs={}, provider=TimeoutThenResumeProvider()
    )
    result = asyncio.run(engine.run())

    # The resume guard raises ExecutionError inside review, which run() catches
    # and turns into a failed pipeline (not a crash deep in the SDK).
    assert result == "failure"
