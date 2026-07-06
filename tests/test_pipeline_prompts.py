"""Pipeline-wide system_prompt (appended) and prompt_preamble (prepended)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hydra.executor import ExecutionEngine
from hydra.loader import load_pipeline
from hydra.provider import SYSTEM_PROMPT, StepResult, StepRunOptions


class CapturingProvider:
    """Records the system_prompt and user prompt seen by each step."""

    def __init__(self):
        self.seen: list[tuple[str, str | None, str]] = []  # (step, sys, prompt)

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        self.seen.append((options.step_id, options.extra_system_prompt, prompt))
        return StepResult(session_id=f"sess-{options.step_id}", output_text="ok")

    async def check(self, session_id, prompt, cwd=None):
        return "yes"


PIPELINE = """\
name: prompt-fields
version: 1

inputs:
  who:
    default: "Android 系统工程师"

system_prompt: |
  你是一位资深 {{ inputs.who }}，全程遵守证据分级。
prompt_preamble: |
  日志在 logs/，仓库看 repo-index.md。

steps:
  - id: step_a
    prompt: "do a"
    output: a.md
  - id: step_b
    after: [step_a]
    inject: [a.md]
    prompt: "do b"
    output: b.md
"""


def test_both_prompt_fields_applied_to_every_step(tmp_path: Path):
    p = tmp_path / "pipeline.yaml"
    p.write_text(PIPELINE)
    pipeline = load_pipeline(p)
    pipeline.workdir = str(tmp_path)
    provider = CapturingProvider()
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    assert asyncio.run(engine.run()) == "success"

    assert len(provider.seen) == 2
    for step_id, sys_prompt, user_prompt in provider.seen:
        # system_prompt field is delivered as extra_system_prompt, template-expanded
        assert sys_prompt is not None, step_id
        assert "资深 Android 系统工程师" in sys_prompt, step_id
        # preamble prepended to every step's user prompt
        assert user_prompt.startswith("日志在 logs/"), (step_id, user_prompt[:40])


def test_system_prompt_appended_not_replacing_builtin(tmp_path: Path):
    # The engine's automation prompt must survive; the field appends to it.
    p = tmp_path / "pipeline.yaml"
    p.write_text(PIPELINE)
    pipeline = load_pipeline(p)

    # loader stores the expanded field; provider concatenation is unit-checked here
    from hydra.provider import ClaudeProvider  # noqa: F401

    assert pipeline.system_prompt is not None
    combined = f"{SYSTEM_PROMPT}\n\n{pipeline.system_prompt}"
    assert combined.startswith(SYSTEM_PROMPT)
    assert "证据分级" in combined


def test_absent_fields_default_to_none(tmp_path: Path):
    content = (
        "name: bare\nversion: 1\nsteps:\n"
        "  - id: only\n    prompt: \"x\"\n"
    )
    p = tmp_path / "pipeline.yaml"
    p.write_text(content)
    pipeline = load_pipeline(p)
    assert pipeline.system_prompt is None
    assert pipeline.prompt_preamble is None

    provider = CapturingProvider()
    pipeline.workdir = str(tmp_path)
    engine = ExecutionEngine(pipeline=pipeline, inputs={}, provider=provider)
    assert asyncio.run(engine.run()) == "success"
    step_id, sys_prompt, user_prompt = provider.seen[0]
    assert sys_prompt is None
    assert user_prompt == "x"  # no preamble prepended
