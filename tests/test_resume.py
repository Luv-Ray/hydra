"""Resuming a prior run: skip its completed steps, re-run the rest.

Covers --resume-last (newest run) and --resume <id> (explicit), the rule that
only status=success steps are skipped (an interrupted/failed step re-runs), and
that a resume writes a fresh run dir recording its lineage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hydra.executor import ExecutionEngine
from hydra.errors import ExecutionError
from hydra.loader import load_pipeline
from hydra.persistence import RunStore
from hydra.provider import StepResult, StepRunOptions


class _CountingProvider:
    """Records which steps actually execute (i.e. weren't skipped)."""

    def __init__(self, fail_step: str | None = None):
        self.calls: list[str] = []
        self.fail_step = fail_step
        self._n = 0

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self._n += 1
        self.calls.append(options.step_id)
        if options.step_id == self.fail_step:
            # Errored → abnormal → step left non-success (interrupted-like).
            return StepResult(session_id=None, output_text="", is_error=True)
        return StepResult(
            session_id=f"sess-{options.step_id}-{self._n}",
            output_text=f"out-{options.step_id}",
        )

    async def check(self, session_id, prompt, cwd=None):
        return "yes"


def _linear_pipeline(tmp_path: Path) -> Path:
    content = (
        "name: linear\nversion: 1\n\nsteps:\n"
        "  - id: a\n    prompt: \"a\"\n    output: a.md\n"
        "  - id: b\n    after: [a]\n    inject: [a.md]\n    prompt: \"b\"\n    output: b.md\n"
        "  - id: c\n    after: [b]\n    inject: [b.md]\n    prompt: \"c\"\n    output: c.md\n"
    )
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    return p


def _engine(tmp_path: Path, provider, **kw) -> ExecutionEngine:
    pipeline = load_pipeline(_linear_pipeline(tmp_path))
    pipeline.workdir = str(tmp_path)
    return ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider, **kw)


def test_resume_last_skips_completed(tmp_path: Path):
    # First run: c fails, so only a + b complete successfully.
    p1 = _CountingProvider(fail_step="c")
    e1 = _engine(tmp_path, p1)
    assert asyncio.run(e1.run()) == "failure"
    assert p1.calls.count("a") == 1 and p1.calls.count("b") == 1

    # Resume: a + b are skipped, only c re-runs (this time it succeeds).
    p2 = _CountingProvider()
    e2 = _engine(tmp_path, p2, resume_last=True)
    assert asyncio.run(e2.run()) == "success"
    assert "a" not in p2.calls
    assert "b" not in p2.calls
    assert "c" in p2.calls


def test_resume_explicit_run_id(tmp_path: Path):
    p1 = _CountingProvider(fail_step="c")
    e1 = _engine(tmp_path, p1)
    asyncio.run(e1.run())
    run_id = RunStore.find_latest_run(str(tmp_path))
    assert run_id is not None

    p2 = _CountingProvider()
    e2 = _engine(tmp_path, p2, resume_run_id=run_id)
    assert asyncio.run(e2.run()) == "success"
    assert p2.calls == ["c"]


def test_resume_records_lineage(tmp_path: Path):
    p1 = _CountingProvider(fail_step="c")
    asyncio.run(_engine(tmp_path, p1).run())
    source = RunStore.find_latest_run(str(tmp_path))

    asyncio.run(_engine(tmp_path, _CountingProvider(), resume_last=True).run())

    # A new run dir was created (newest != source) and it records its parent.
    newest = RunStore.find_latest_run(str(tmp_path))
    assert newest != source
    meta = json.loads(
        (RunStore.runs_root(str(tmp_path)) / newest / "meta.json").read_text()
    )
    assert meta["resumed_from"] == source
    assert set(meta["resumed_steps"]) == {"a", "b"}


def test_resume_last_no_prior_run_starts_fresh(tmp_path: Path):
    # No .hydra/runs yet → --resume-last degrades to a normal run.
    p = _CountingProvider()
    e = _engine(tmp_path, p, resume_last=True)
    assert asyncio.run(e.run()) == "success"
    assert p.calls == ["a", "b", "c"]


def test_resume_unknown_id_errors_cleanly(tmp_path: Path):
    # First make a real run so .hydra/runs exists.
    asyncio.run(_engine(tmp_path, _CountingProvider(fail_step="c")).run())
    e = _engine(tmp_path, _CountingProvider(), resume_run_id="nonexistent-run")
    # Surfaces as a caught pipeline failure, not a raw FileNotFoundError.
    assert asyncio.run(e.run()) == "failure"


def test_interrupted_step_not_skipped(tmp_path: Path):
    # b "interrupted" (fails) in run 1 → must re-run on resume, and so must c.
    p1 = _CountingProvider(fail_step="b")
    asyncio.run(_engine(tmp_path, p1).run())

    p2 = _CountingProvider()
    asyncio.run(_engine(tmp_path, p2, resume_last=True).run())
    assert "a" not in p2.calls   # a completed → skipped
    assert "b" in p2.calls        # b failed → re-run
    assert "c" in p2.calls        # c never ran → run now
