"""compact_before_resume: gate on the resume target's context percentage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class SizedProvider:
    """Reports a configurable context size for the resume target.

    `analyze` comes back large (over the window fraction we set as threshold);
    every other step is small. compact() records the call and shrinks the
    target so re-checks under the lock skip.
    """

    def __init__(self, analyze_pct: float, window: int = 1_000_000):
        self.window = window
        self.analyze_tokens = int(window * analyze_pct / 100)
        self.compact_calls: list[str] = []
        self.run_calls: list[str] = []

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sid = options.step_id
        self.run_calls.append(sid)
        tokens = self.analyze_tokens if sid == "analyze" else 1000
        return StepResult(
            session_id=f"sess-{sid}",
            output_text=f"out-{sid}",
            input_tokens=tokens,
            context_window=self.window,
        )

    async def check(self, session_id: str, prompt: str) -> str:
        return ""

    async def compact(self, session_id: str) -> tuple[int, int]:
        self.compact_calls.append(session_id)
        # Simulate /compact shrinking the transcript to a small size.
        return 5000, self.window


PIPELINE = """\
name: compact-test
version: 1

defaults:
  retry: 0

steps:
  - id: analyze
    prompt: "analyze"
    output: root-cause.md

  - id: review_a
    after: [analyze]
    resume: analyze
    compact_before_resume: 50
    prompt: "review a"

  - id: review_b
    after: [analyze]
    resume: analyze
    compact_before_resume: 50
    prompt: "review b"
"""


def _run(tmp_path: Path, analyze_pct: float) -> SizedProvider:
    p = tmp_path / "pipeline.yaml"
    p.write_text(PIPELINE)
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)
    provider = SizedProvider(analyze_pct=analyze_pct)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())
    assert result == "success"
    return provider


def test_compacts_once_when_over_threshold(tmp_path: Path):
    # analyze at 70% > 50% threshold: two parallel reviewers resume it, but the
    # per-target lock means /compact runs exactly once.
    provider = _run(tmp_path, analyze_pct=70)
    assert provider.compact_calls == ["sess-analyze"], provider.compact_calls


def test_skips_when_under_threshold(tmp_path: Path):
    # analyze at 30% < 50%: never compact.
    provider = _run(tmp_path, analyze_pct=30)
    assert provider.compact_calls == [], provider.compact_calls
