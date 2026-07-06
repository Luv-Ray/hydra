"""Faithful repro of the mini-review step, dumping EVERY message block raw.

Goal: determine whether the model's audit body lands in AssistantMessage
TextBlocks (which provider.run discards, reading only ResultMessage.result) or
whether the model genuinely produces nothing. Reconstructs the exact prompt the
pipeline sent (inject root-cause.md + mini-review prompt + output contract) and
streams the SDK query, printing the type and a preview of every block.

Run:  .venv/bin/python tmp_repro_minireview.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from claude_agent_sdk.types import (
    AssistantMessage, SystemMessage, TextBlock, ThinkingBlock,
    ToolUseBlock, ToolResultBlock, UserMessage,
)

from hydra.inject import build_prompt
from hydra.provider import SYSTEM_PROMPT

WORKDIR = "/home/ray/Workspace/pipelines/ISS-202607-00006887A"
PIPELINE = "/home/ray/Workspace/pipelines/anr-fc-analysis.yaml"


def _mini_review_step() -> dict:
    data = yaml.safe_load(Path(PIPELINE).read_text())
    for s in data["steps"]:
        if s["id"] == "mini-review":
            return s
    raise SystemExit("mini-review step not found")


async def main() -> None:
    step = _mini_review_step()
    model = yaml.safe_load(Path(PIPELINE).read_text())["defaults"]["model"]

    prompt = build_prompt(
        prompt=step["prompt"],
        inject_paths=step.get("inject", []),
        workdir=WORKDIR,
        output_path=step.get("output"),
    )
    print(f"=== model: {model}", flush=True)
    print(f"=== prompt length: {len(prompt)} chars", flush=True)

    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        cwd=WORKDIR,
    )

    text_chars = 0
    thinking_chars = 0
    n_assistant = 0
    n_text_blocks = 0

    async for msg in query(prompt=prompt, options=opts):
        cls = type(msg).__name__
        if isinstance(msg, AssistantMessage):
            n_assistant += 1
            for b in msg.content:
                bt = type(b).__name__
                if isinstance(b, TextBlock):
                    n_text_blocks += 1
                    text_chars += len(b.text)
                    preview = b.text[:300].replace("\n", " ")
                    print(f"[{cls}] TextBlock ({len(b.text)} chars): {preview}", flush=True)
                elif isinstance(b, ThinkingBlock):
                    thinking_chars += len(b.thinking)
                    preview = b.thinking[:200].replace("\n", " ")
                    print(f"[{cls}] ThinkingBlock ({len(b.thinking)} chars): {preview}", flush=True)
                elif isinstance(b, ToolUseBlock):
                    print(f"[{cls}] ToolUseBlock: {b.name}({str(b.input)[:120]})", flush=True)
                elif isinstance(b, ToolResultBlock):
                    print(f"[{cls}] ToolResultBlock", flush=True)
                else:
                    print(f"[{cls}] {bt}: {str(b)[:120]}", flush=True)
        elif isinstance(msg, ResultMessage):
            print("=" * 60, flush=True)
            print(f"[ResultMessage] subtype={msg.subtype} is_error={msg.is_error} "
                  f"stop_reason={msg.stop_reason} num_turns={msg.num_turns}", flush=True)
            r = msg.result
            print(f"[ResultMessage] result: {'EMPTY' if not r else repr(r[:300])}", flush=True)
        else:
            print(f"[{cls}] {str(msg)[:120]}", flush=True)

    print("=" * 60, flush=True)
    print(f"SUMMARY: assistant_msgs={n_assistant} text_blocks={n_text_blocks} "
          f"text_chars={text_chars} thinking_chars={thinking_chars}", flush=True)
    if text_chars and not r:
        print(">>> CONFIRMED: model produced TextBlock output that ResultMessage.result dropped.", flush=True)
    elif not text_chars and thinking_chars:
        print(">>> Model produced ONLY thinking, no final text.", flush=True)
    elif not text_chars and not thinking_chars:
        print(">>> Model produced no text at all.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
