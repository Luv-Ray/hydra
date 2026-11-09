"""Shared test scaffolding: a scripted provider and pipeline loading helpers."""

from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path
from typing import Any

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.models import Pipeline
from hydra.provider import StepResult, StepRunOptions
from hydra.validate import validate_pipeline


class FakeProvider:
    """Scripted stand-in for ClaudeProvider.

    script: step id → reply, or list of replies (one per call; the last one
        repeats). A reply is a str (the output text) or a full StepResult.
        Steps not in the script reply f"out-{id}".
    follow_ups: same shape, for verdict follow-up replies.
    delay: step id → seconds to sleep inside run() (for concurrency tests).

    Session ids mimic the SDK: a non-fork resume continues the same id, a fork
    or a fresh run gets a new one.
    """

    def __init__(
        self,
        script: dict[str, Any] | None = None,
        follow_ups: dict[str, Any] | None = None,
        delay: dict[str, float] | None = None,
        compact_to: int = 5000,
    ):
        self.script = script or {}
        self.follow_ups_script = follow_ups or {}
        self.delay = delay or {}
        self.compact_to = compact_to
        self.calls: list[str] = []
        self.prompts: dict[str, list[str]] = {}
        self.options: dict[str, list[StepRunOptions]] = {}
        self.follow_up_calls: list[tuple[str, str, str]] = []  # (step, session, prompt)
        self.compact_calls: list[tuple[str, str | None, str | None]] = []
        self.spans: dict[str, list[tuple[float, float]]] = {}
        self._count: dict[str, int] = {}
        self._fu_count: dict[str, int] = {}
        self._sid = 0

    @staticmethod
    def _pick(table: dict[str, Any], sid: str, n: int, default: Any) -> Any:
        if sid not in table:
            return default
        v = table[sid]
        if isinstance(v, list):
            return v[min(n, len(v) - 1)]
        return v

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sid = options.step_id
        start = time.monotonic()
        self.calls.append(sid)
        self.prompts.setdefault(sid, []).append(prompt)
        self.options.setdefault(sid, []).append(options)
        n = self._count.get(sid, 0)
        self._count[sid] = n + 1
        if sid in self.delay:
            await asyncio.sleep(self.delay[sid])
        reply = self._pick(self.script, sid, n, f"out-{sid}")
        self.spans.setdefault(sid, []).append((start, time.monotonic()))
        if isinstance(reply, StepResult):
            return reply
        if options.resume_session_id and not options.fork_session:
            session = options.resume_session_id
        else:
            self._sid += 1
            session = f"sess-{sid}-{self._sid}"
        return StepResult(session_id=session, output_text=reply)

    async def follow_up(self, session_id: str, prompt: str, options: StepRunOptions) -> str:
        sid = options.step_id
        self.follow_up_calls.append((sid, session_id, prompt))
        n = self._fu_count.get(sid, 0)
        self._fu_count[sid] = n + 1
        return self._pick(self.follow_ups_script, sid, n, "")

    async def compact(self, session_id: str, options: StepRunOptions) -> tuple[int, int]:
        self.compact_calls.append((session_id, options.model, options.cwd))
        return self.compact_to, 1_000_000


def load(tmp_path: Path, yaml_text: str, vars: dict[str, str] | None = None,
         validate: bool = True) -> Pipeline:
    """Write YAML (dedented) to tmp_path, load it, validate, point workdir there."""
    p = tmp_path / "pipeline.yaml"
    p.write_text(textwrap.dedent(yaml_text))
    pipeline = load_pipeline(p, vars=vars)
    if validate:
        validate_pipeline(pipeline)
    pipeline.workdir = str(tmp_path)
    return pipeline


def run(pipeline: Pipeline, provider: FakeProvider, **kw: Any) -> tuple[str, ExecutionEngine]:
    engine = ExecutionEngine(pipeline=pipeline, provider=provider, **kw)
    return asyncio.run(engine.run()), engine


def errored() -> StepResult:
    return StepResult(session_id="sess-err", output_text="", is_error=True, subtype="error")
