"""Explicit loop_body: author-declared loop body replaces reverse-derivation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hydra.errors import ValidationError
from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import StepResult, StepRunOptions
from hydra.validate import validate_pipeline


class GateProvider:
    """gate REJECTs until N revises, then ACCEPTs. Records call order."""

    def __init__(self, reject_rounds: int = 2):
        self.reject_rounds = reject_rounds
        self.calls: list[str] = []
        self.revise_count = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sid = options.step_id
        self.calls.append(sid)
        if sid == "revise":
            self.revise_count += 1
        if sid == "gate":
            text = "ACCEPT" if self.revise_count >= self.reject_rounds else "REJECT"
        elif sid == "verdict":
            text = "ACCEPT"  # this test exercises the gate loop, not the verdict loop
        else:
            text = f"out-{sid}"
        return StepResult(session_id=f"sess-{sid}", output_text=text)

    async def check(self, session_id, prompt, cwd=None):
        return ""

    async def compact(self, session_id, model=None, cwd=None):
        return (0, 0)


def _pipeline(loop_body_line: str) -> str:
    return f"""\
name: loop-body-test
version: 1

defaults:
  retry: 0

steps:
  - id: analyze
    prompt: "analyze"
    output: root-cause.md

  - id: gate
    after: [analyze]
    inject: [root-cause.md]
    prompt: "gate"
    output: mini-challenge.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: failure
        goto: revise

  - id: revise
    trigger: route
    resume: analyze
    inject: [mini-challenge.md]
    max_iterations: 3
{loop_body_line}
    prompt: "revise"
    routes:
      - if: success
        goto: gate

  - id: challenge_a
    after: [gate]
    inject: [root-cause.md]
    prompt: "a"
    output: a.md

  - id: challenge_b
    after: [gate]
    inject: [root-cause.md]
    prompt: "b"
    output: b.md

  - id: verdict
    after: [challenge_a, challenge_b]
    inject: [a.md, b.md]
    prompt: "verdict"
    output: challenge.md
    check:
      success_pattern: "ACCEPT"
    routes:
      - if: success
        goto: final
    max_iterations: 2

  - id: final
    trigger: route
    inject: [challenge.md]
    prompt: "final"
    output: final.md
"""


def test_loop_body_restricts_rerun_to_declared_members(tmp_path: Path):
    # revise declares loop_body: [gate] — so its back-jump re-runs ONLY gate,
    # never the expensive fan-out (challenge_a/b) even though they are DAG
    # dependents of gate. This is the crux: without loop_body, reverse-derivation
    # would (pre-branch-step-fix) have pulled them in.
    p = tmp_path / "pipeline.yaml"
    p.write_text(_pipeline("    loop_body: [gate]"))
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)
    provider = GateProvider(reject_rounds=2)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    result = asyncio.run(engine.run())

    assert result == "success", provider.calls
    # gate looped: rejected twice (revise x2), accepted on 3rd gate run
    assert provider.calls.count("gate") == 3, provider.calls
    assert provider.calls.count("revise") == 2, provider.calls
    # fan-out ran exactly once — NOT dragged into the gate loop
    assert provider.calls.count("challenge_a") == 1, provider.calls
    assert provider.calls.count("challenge_b") == 1, provider.calls
    # and only after the gate converged
    last_gate = max(i for i, c in enumerate(provider.calls) if c == "gate")
    first_fanout = min(
        i for i, c in enumerate(provider.calls) if c in ("challenge_a", "challenge_b")
    )
    assert first_fanout > last_gate, provider.calls


def test_loop_body_unknown_member_rejected(tmp_path: Path):
    p = tmp_path / "pipeline.yaml"
    p.write_text(_pipeline("    loop_body: [gate, nonexistent]"))
    pipeline = load_pipeline(p)
    with pytest.raises(ValidationError, match="unknown step 'nonexistent'"):
        validate_pipeline(pipeline)


def test_loop_body_route_member_rejected(tmp_path: Path):
    # a trigger:route step cannot be a loop body member
    p = tmp_path / "pipeline.yaml"
    p.write_text(_pipeline("    loop_body: [gate, final]"))
    pipeline = load_pipeline(p)
    with pytest.raises(ValidationError, match="trigger:route"):
        validate_pipeline(pipeline)


def test_loop_body_without_backjump_rejected(tmp_path: Path):
    # verdict declares loop_body but only routes forward (to final) — dead config.
    p = tmp_path / "pipeline.yaml"
    body = _pipeline("    loop_body: [gate]")  # valid revise body
    # inject a loop_body on verdict, which has no back-jump route
    body = body.replace(
        "        goto: final\n    max_iterations: 2",
        "        goto: final\n    max_iterations: 2\n    loop_body: [challenge_a]",
    )
    p.write_text(body)
    pipeline = load_pipeline(p)
    with pytest.raises(ValidationError, match="no back-jump route"):
        validate_pipeline(pipeline)
