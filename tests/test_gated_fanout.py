"""Gate-before-parallel-fanout topology (mirrors anr-fc-analysis mini-review gate).

A cheap gate step (mini_review) must fully converge — including any REJECT →
revise → back-jump rounds — BEFORE the expensive parallel fan-out (the deep
reviews) runs, and the fan-out must then run in parallel exactly once. This is
the bug the run 20260703-085402 exposed: the fan-out was wired to a common
upstream dep, so it landed in the same layer as the gate and ran concurrently
with it, ignoring the gate entirely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions


class GateProvider:
    """mini_review REJECTs until N revises happened, then ACCEPTs.

    Records the order steps start so we can assert the gate blocks the fan-out.
    verdict always ACCEPTs (we only exercise the mini-review gate loop here).
    """

    def __init__(self, gate_reject_rounds: int = 2):
        self.gate_reject_rounds = gate_reject_rounds
        self.calls: list[str] = []
        self.revise_count = 0
        # concurrency probe: how many fan-out steps were in flight at once
        self._in_flight = 0
        self.max_fanout_concurrency = 0
        self._fanout = {
            "challenge_a",
            "challenge_b",
            "challenge_c",
        }

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sid = options.step_id
        self.calls.append(sid)
        if sid == "mini_revise":
            self.revise_count += 1

        if sid in self._fanout:
            self._in_flight += 1
            self.max_fanout_concurrency = max(
                self.max_fanout_concurrency, self._in_flight
            )
            await asyncio.sleep(0.01)  # hold so siblings overlap if truly parallel
            self._in_flight -= 1

        if sid == "mini_review":
            text = "ACCEPT" if self.revise_count >= self.gate_reject_rounds else "REJECT"
        elif sid == "verdict":
            text = "ACCEPT"
        else:
            text = f"out-{sid}"
        return StepResult(session_id=f"sess-{sid}", output_text=text)

    async def check(self, session_id: str, prompt: str, cwd: str | None = None) -> str:
        return ""

    async def compact(self, session_id, model=None, cwd=None):
        return (0, 0)


PIPELINE = """\
name: gated-fanout
version: 1

defaults:
  retry: 0

steps:
  - id: analyze
    prompt: "analyze"
    output: root-cause.md

  - id: mini_review
    after: [analyze]
    inject: [root-cause.md]
    prompt: "gate"
    output: mini-challenge.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: failure
        goto: mini_revise

  - id: mini_revise
    trigger: route
    resume: analyze
    inject: [mini-challenge.md]
    max_iterations: 3
    prompt: "revise"
    routes:
      - if: success
        goto: mini_review

  - id: challenge_a
    after: [mini_review]
    inject: [root-cause.md]
    prompt: "review a"
    output: review-a.md

  - id: challenge_b
    after: [mini_review]
    inject: [root-cause.md]
    prompt: "review b"
    output: review-b.md

  - id: challenge_c
    after: [mini_review]
    inject: [root-cause.md]
    prompt: "review c"
    output: review-c.md

  - id: verdict
    after: [challenge_a, challenge_b, challenge_c]
    inject: [review-a.md, review-b.md, review-c.md]
    prompt: "verdict"
    output: challenge.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: success
        goto: final_report

  - id: final_report
    trigger: route
    inject: [root-cause.md, challenge.md]
    prompt: "final"
    output: final-report.md
"""


def _run(tmp_path: Path, gate_reject_rounds: int) -> GateProvider:
    p = tmp_path / "pipeline.yaml"
    p.write_text(PIPELINE)
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)
    provider = GateProvider(gate_reject_rounds=gate_reject_rounds)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())
    assert result == "success", provider.calls
    return provider


def test_gate_blocks_fanout_until_converged(tmp_path: Path):
    # ACCEPT once revise_count>=2: gate rejects twice, mini_revise runs twice,
    # then the 3rd mini_review ACCEPTs.
    provider = _run(tmp_path, gate_reject_rounds=2)
    calls = provider.calls
    fanout = {"challenge_a", "challenge_b", "challenge_c"}

    # No fan-out step may start before the gate's final (ACCEPT) run.
    # The last mini_review call is the converging one.
    last_gate_idx = max(i for i, c in enumerate(calls) if c == "mini_review")
    first_fanout_idx = min(i for i, c in enumerate(calls) if c in fanout)
    assert first_fanout_idx > last_gate_idx, (
        f"fan-out started before gate converged: {calls}"
    )

    # Gate looped: mini_review ran (reject rounds + 1) times, revise ran reject times.
    assert calls.count("mini_review") == 3, calls
    assert calls.count("mini_revise") == 2, calls


def test_fanout_runs_in_parallel_exactly_once(tmp_path: Path):
    # Gate accepts immediately (no reject rounds).
    provider = _run(tmp_path, gate_reject_rounds=1)
    calls = provider.calls

    # Each fan-out step runs exactly once (no duplicate from goto + layer).
    for sid in ("challenge_a", "challenge_b", "challenge_c"):
        assert calls.count(sid) == 1, (sid, calls)

    # They actually overlapped (parallel), not serialized.
    assert provider.max_fanout_concurrency == 3, provider.max_fanout_concurrency

    # verdict fanned in after all three, then final_report.
    assert calls.count("verdict") == 1, calls
    assert calls[-1] == "final_report", calls


def test_gate_reject_does_not_rerun_fanout(tmp_path: Path):
    # The whole point: the expensive fan-out must NOT be dragged into the gate's
    # revise back-jump. With gate rejecting twice, fan-out still runs once each.
    provider = _run(tmp_path, gate_reject_rounds=2)
    for sid in ("challenge_a", "challenge_b", "challenge_c"):
        assert provider.calls.count(sid) == 1, (sid, provider.calls)
