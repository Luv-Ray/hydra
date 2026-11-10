"""The per-step transcript captures every message block, untruncated.

This is the record that lets an empty/abnormal output be diagnosed without
re-running the step — so the test pins that thinking blocks, tool calls, and an
empty final result are all preserved verbatim.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from hydra import provider as provider_mod
from hydra.provider import (
    ClaudeProvider, StepRunOptions, Transcript, _block_to_dict, _message_to_dict,
)
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import (
    AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock,
)


def _empty_result() -> ResultMessage:
    """A ResultMessage in the empty-output failure mode (0 output tokens)."""
    return ResultMessage(
        subtype="success", duration_ms=100, duration_api_ms=90, is_error=False,
        num_turns=2, session_id="s", stop_reason="end_turn", total_cost_usd=0.4,
        usage={"output_tokens": 0}, result="",
        model_usage={"m": {"outputTokens": 0}},
    )


def test_block_serialization_covers_all_kinds():
    assert _block_to_dict(TextBlock(text="hi")) == {"type": "text", "text": "hi"}
    tb = _block_to_dict(ThinkingBlock(thinking="reasoning", signature="sig"))
    assert tb["type"] == "thinking" and tb["thinking"] == "reasoning"
    tu = _block_to_dict(ToolUseBlock(id="1", name="Bash", input={"cmd": "ls"}))
    assert tu["type"] == "tool_use" and tu["name"] == "Bash"


def test_message_dict_preserves_empty_result_and_usage():
    d = _message_to_dict(_empty_result())
    assert d["kind"] == "ResultMessage"
    assert d["result"] == ""
    assert d["stop_reason"] == "end_turn"
    assert d["model_usage"] == {"m": {"outputTokens": 0}}


def test_message_dict_captures_thinking_only_turn():
    # An assistant turn that is thinking-only (no text) — the exact shape that
    # produces "no visible output". Both blocks must survive.
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="deliberating", signature="s")],
        model="claude-sonnet-5",
    )
    d = _message_to_dict(msg)
    assert d["content"] == [{"type": "thinking", "thinking": "deliberating"}]


def test_transcript_file_written_and_valid_jsonl(tmp_path: Path):
    p = tmp_path / "transcript.jsonl"
    tr = Transcript(str(p))
    tr.write({"kind": "prompt", "text": "do the thing"})
    tr.write(_message_to_dict(AssistantMessage(content=[TextBlock(text="ok")], model="m")))
    tr.write(_message_to_dict(_empty_result()))
    tr.close()

    recs = [json.loads(x) for x in p.read_text().strip().splitlines()]
    assert len(recs) == 3
    assert recs[0]["kind"] == "prompt"
    assert recs[1]["content"] == [{"type": "text", "text": "ok"}]
    assert recs[2]["result"] == ""
    assert all("ts" in r for r in recs)  # every record timestamped


def test_transcript_noop_when_no_path():
    tr = Transcript(None)
    tr.write({"kind": "prompt", "text": "x"})  # must not raise
    tr.close()


def test_transcript_handles_unserializable(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    tr = Transcript(str(p))
    tr.write({"kind": "weird", "obj": object()})  # not JSON-native
    tr.close()
    # default=str keeps it serializable rather than crashing the run.
    assert json.loads(p.read_text().strip())["kind"] == "weird"


class _Result:
    def __init__(self, text: str):
        self.session_id, self.result = f"s-{text}", text
        self.is_error, self.subtype, self.stop_reason = False, "success", "end_turn"
        self.total_cost_usd, self.duration_ms, self.num_turns = 0.0, 1, 1
        self.usage, self.model_usage = {}, {}
        self.permission_denials, self.errors, self.api_error_status = [], [], None


def test_concurrent_runs_keep_separate_transcripts(tmp_path: Path, monkeypatch):
    # Regression: the handle lived on the provider, so when parallel steps
    # (e.g. forked reviewers) ran at once, the first to finish closed the
    # others' file and their ResultMessage records were lost.
    async def fake_query(prompt, options):
        await asyncio.sleep(0.2 if prompt == "slow" else 0.01)
        yield _Result(prompt)

    monkeypatch.setattr(provider_mod, "query", fake_query)
    monkeypatch.setattr(provider_mod, "ResultMessage", _Result)
    prov = ClaudeProvider()

    async def both():
        await asyncio.gather(*(
            prov.run(name, StepRunOptions(step_id=name, transcript_path=str(tmp_path / f"{name}.jsonl")))
            for name in ("slow", "fast")
        ))

    asyncio.run(both())
    for name in ("slow", "fast"):
        recs = [json.loads(x) for x in (tmp_path / f"{name}.jsonl").read_text().splitlines()]
        assert [r["kind"] for r in recs] == ["prompt", "_Result"], name
        assert recs[0]["text"] == name and recs[1]["result"] == name
