"""The per-step transcript captures every message block, untruncated.

This is the record that lets an empty/abnormal output be diagnosed without
re-running the step — so the test pins that thinking blocks, tool calls, and an
empty final result are all preserved verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from hydra.provider import _block_to_dict, _message_to_dict, ClaudeProvider
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
    prov = ClaudeProvider()
    prov._transcript_open(str(p))
    prov._transcript_write({"kind": "prompt", "text": "do the thing"})
    prov._transcript_write(
        _message_to_dict(
            AssistantMessage(content=[TextBlock(text="ok")], model="m")
        )
    )
    prov._transcript_write(_message_to_dict(_empty_result()))
    prov._transcript_close()

    lines = p.read_text().strip().splitlines()
    assert len(lines) == 3
    recs = [json.loads(x) for x in lines]  # all valid JSON
    assert recs[0]["kind"] == "prompt"
    assert recs[1]["content"] == [{"type": "text", "text": "ok"}]
    assert recs[2]["result"] == ""
    assert all("ts" in r for r in recs)  # every record timestamped


def test_transcript_noop_when_no_path():
    # No path set → writes are silently dropped, no crash.
    prov = ClaudeProvider()
    prov._transcript_open(None)
    prov._transcript_write({"kind": "prompt", "text": "x"})  # must not raise
    prov._transcript_close()


def test_transcript_handles_unserializable(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    prov = ClaudeProvider()
    prov._transcript_open(str(p))
    prov._transcript_write({"kind": "weird", "obj": object()})  # not JSON-native
    prov._transcript_close()
    rec = json.loads(p.read_text().strip())
    # default=str keeps it serializable rather than crashing the run.
    assert rec["kind"] == "weird"
