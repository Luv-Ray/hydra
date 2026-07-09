"""An abnormal step outcome (errored / empty body) must not become a REJECT.

The failure this guards against: a gate step (mini-review) returns an empty
reply — is_error=False, result=""  — which used to (a) write no output file and
(b) fail its success_pattern check against the empty string, routing to the
revise loop as if the model had rejected the analysis. The revise step then
injected the never-written output file and crashed. These tests pin the fix:
empty/errored outcomes retry on their own budget, then hard-fail — never route.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hydra.executor import (
    ExecutionEngine,
    ABNORMAL_RETRY_BUDGET,
    _classify_outcome,
)
from hydra.errors import ExecutionError
from hydra.loader import load_pipeline
from hydra.models import StepDef
from hydra.provider import StepResult, StepRunOptions


class _ScriptedProvider:
    """Returns queued StepResults per step; records every run call."""

    def __init__(self, script: dict[str, list[StepResult]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self._n = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._n += 1
        self.calls.append(options.step_id)
        queue = self.script.get(options.step_id)
        if queue:
            r = queue.pop(0)
            # Give it a session id if the script left it None-by-default ok body.
            if r.session_id is None and not r.is_error and r.output_text:
                r.session_id = f"sess-{options.step_id}-{self._n}"
            return r
        return StepResult(
            session_id=f"sess-{options.step_id}-{self._n}",
            output_text=f"out-{options.step_id}",
        )

    async def check(self, session_id, prompt, cwd=None):
        return "yes"


def _gate_pipeline(tmp_path: Path) -> Path:
    # gate has an inline success_pattern check and routes failure to a revise
    # step that injects gate's output — exactly the mini-review shape.
    content = (
        "name: gate-empty\nversion: 1\n\nsteps:\n"
        "  - id: analyze\n    prompt: \"analyze\"\n    output: root.md\n"
        "  - id: gate\n    after: [analyze]\n    inject: [root.md]\n"
        "    prompt: \"review\"\n    output: gate.md\n    retry: 0\n"
        "    check:\n      success_pattern: \"ACCEPT\"\n"
        "    routes:\n      - if: failure\n        goto: revise\n"
        "  - id: revise\n    trigger: route\n    resume: analyze\n"
        "    inject: [gate.md]\n    prompt: \"revise\"\n"
        "    routes:\n      - if: success\n        goto: gate\n"
    )
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def _run(tmp_path: Path, provider) -> str:
    pipeline = load_pipeline(_gate_pipeline(tmp_path))
    pipeline.workdir = str(tmp_path)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    return asyncio.run(engine.run())


def test_empty_gate_output_aborts_not_rejects(tmp_path: Path):
    # gate always returns an empty body. It must retry on the abnormal budget,
    # then hard-fail — NOT route to revise (which would crash on missing gate.md).
    empty = lambda: StepResult(session_id="s", output_text="")  # noqa: E731
    provider = _ScriptedProvider({
        "analyze": [StepResult(session_id="a", output_text="root text")],
        "gate": [empty() for _ in range(ABNORMAL_RETRY_BUDGET + 1)],
    })
    result = _run(tmp_path, provider)

    assert result == "failure"
    # revise must never have run — the abort happened before any routing.
    assert "revise" not in provider.calls
    # gate tried once + its abnormal retries, all before giving up.
    assert provider.calls.count("gate") == ABNORMAL_RETRY_BUDGET + 1


def test_empty_gate_recovers_on_retry(tmp_path: Path):
    # First gate attempt is empty (transient); the retry returns a real ACCEPT.
    provider = _ScriptedProvider({
        "analyze": [StepResult(session_id="a", output_text="root text")],
        "gate": [
            StepResult(session_id="s", output_text=""),          # abnormal
            StepResult(session_id="s2", output_text="ACCEPT"),   # recovers
        ],
    })
    result = _run(tmp_path, provider)

    assert result == "success"
    assert provider.calls.count("gate") == 2
    assert "revise" not in provider.calls
    # The recovered body was written to the output file.
    assert (tmp_path / "gate.md").read_text() == "ACCEPT"


def test_api_error_status_treated_as_abnormal(tmp_path: Path):
    # is_error=False but api_error_status set (e.g. a 529) is still abnormal.
    provider = _ScriptedProvider({
        "analyze": [StepResult(session_id="a", output_text="root text")],
        "gate": [
            StepResult(
                session_id="s", output_text="ACCEPT",
                is_error=False, api_error_status=529,
            )
            for _ in range(ABNORMAL_RETRY_BUDGET + 1)
        ],
    })
    result = _run(tmp_path, provider)

    assert result == "failure"
    assert "revise" not in provider.calls


def test_genuine_reject_still_routes(tmp_path: Path):
    # A real body that fails the check IS a REJECT and must still route to revise.
    # gate returns a non-ACCEPT body (check fails), revise then succeeds → gate
    # is retried and this time accepts.
    provider = _ScriptedProvider({
        "analyze": [StepResult(session_id="a", output_text="root text")],
        "gate": [
            StepResult(session_id="s1", output_text="REJECT: needs work"),
            StepResult(session_id="s2", output_text="ACCEPT"),
        ],
        "revise": [StepResult(session_id="r1", output_text="revised")],
    })
    result = _run(tmp_path, provider)

    assert result == "success"
    # The REJECT genuinely routed through revise and back to gate.
    assert "revise" in provider.calls
    assert provider.calls.count("gate") == 2


def test_stream_error_text_surfaces_in_reason():
    # A stream interruption arrives as is_error=True with the real error text in
    # `errors` (put there by provider.run's mid-stream branch). The classify
    # reason must carry that text, not just the misleading subtype "success".
    step = StepDef(id="challenge_alternatives", prompt="x", output="review.md")
    result = StepResult(
        session_id="s",
        output_text="",
        is_error=True,
        subtype="success",  # the misleading CLI subtype
        errors=["API Error: 上游响应流中断，响应不完整: error decoding response body"],
    )
    outcome, reason = _classify_outcome(step, result)
    assert outcome == "abnormal_fatal"  # contracted (has output:) + errored
    assert "上游响应流中断" in reason
