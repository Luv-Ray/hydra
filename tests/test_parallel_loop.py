"""Repro of the parallel-review back-jump loop (mirrors anr-fc-analysis.yaml)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class VerdictProvider:
    """verdict returns REJECT until N revisions have happened, then ACCEPT.

    check() with no prompt matches against output_text, so verdict's
    output IS the ACCEPT/REJECT signal (success_pattern: ACCEPT).
    """

    def __init__(self, reject_rounds: int = 2):
        self.reject_rounds = reject_rounds
        self.calls: list[str] = []
        self.revise_count = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sid = options.step_id
        self.calls.append(sid)
        if sid == "revise":
            self.revise_count += 1
        # verdict outputs ACCEPT once we've revised enough
        if sid == "verdict":
            text = "ACCEPT" if self.revise_count >= self.reject_rounds else "REJECT"
        else:
            text = f"output-{sid}"
        return StepResult(session_id=f"sess-{sid}", output_text=text)

    async def check(self, session_id: str, prompt: str) -> str:
        return ""


PIPELINE = """\
name: parallel-review-loop
version: 1

defaults:
  retry: 0

steps:
  - id: analyze
    prompt: "analyze"
    output: root-cause.md

  - id: challenge_evidence
    after: [analyze]
    inject: [root-cause.md]
    prompt: "review evidence"
    output: review-evidence.md

  - id: challenge_alternatives
    after: [analyze]
    inject: [root-cause.md]
    prompt: "review alternatives"
    output: review-alternatives.md

  - id: challenge_fix
    after: [analyze]
    inject: [root-cause.md]
    prompt: "review fix"
    output: review-fix.md

  - id: verdict
    after: [challenge_evidence, challenge_alternatives, challenge_fix]
    inject: [review-evidence.md, review-alternatives.md, review-fix.md]
    prompt: "verdict"
    output: challenge.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: success
        goto: final_report
      - if: failure
        goto: revise

  - id: revise
    trigger: route
    resume: analyze
    inject: [challenge.md]
    max_iterations: 3
    prompt: "revise"
    routes:
      - if: success
        goto: [challenge_evidence, challenge_alternatives, challenge_fix]

  - id: final_report
    trigger: route
    inject: [root-cause.md, challenge.md]
    prompt: "final report"
    output: final-report.md
"""


def test_parallel_review_loop(tmp_path: Path):
    p = tmp_path / "pipeline.yaml"
    p.write_text(PIPELINE)
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)

    provider = VerdictProvider(reject_rounds=2)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    print("CALLS:", provider.calls)
    assert result == "success"
    # verdict should run multiple times (initial + re-runs after revise)
    assert provider.calls.count("verdict") >= 2, provider.calls
    assert provider.calls.count("revise") >= 1, provider.calls
    # final_report must run at the end
    assert "final_report" in provider.calls, provider.calls
    # each challenge should re-run per loop
    assert provider.calls.count("challenge_evidence") >= 2, provider.calls
