"""A/B on the FULL mini-review prompt: isolated vs global settings.

repro showed output_tokens=0 (model emitted nothing) with 82047 input tokens.
This tests whether removing the global superpowers SessionStart injection
(setting_sources=[]) changes the outcome, and prints output_tokens for each.

Each leg can take ~8 min (matches the observed duration), so run in background.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, TextBlock, ThinkingBlock

from hydra.inject import build_prompt
from hydra.provider import SYSTEM_PROMPT

WORKDIR = "/home/ray/Workspace/pipelines/ISS-202607-00006887A"
PIPELINE = "/home/ray/Workspace/pipelines/anr-fc-analysis.yaml"


def _prompt() -> str:
    data = yaml.safe_load(Path(PIPELINE).read_text())
    step = next(s for s in data["steps"] if s["id"] == "mini-review")
    return build_prompt(
        prompt=step["prompt"],
        inject_paths=step.get("inject", []),
        workdir=WORKDIR,
        output_path=step.get("output"),
    )


async def leg(label: str, setting_sources) -> None:
    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model="claude-sonnet-5[1m]",
        setting_sources=setting_sources,
    )
    prompt = _prompt()
    n_assist = text_chars = thinking_chars = 0
    result = None
    in_tok = out_tok = 0
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            n_assist += 1
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text_chars += len(b.text)
                elif isinstance(b, ThinkingBlock):
                    thinking_chars += len(b.thinking)
        elif type(msg).__name__ == "ResultMessage":
            result = msg.result
            u = (msg.model_usage or {}).get("claude-sonnet-5[1m]", {})
            in_tok = u.get("inputTokens", 0)
            out_tok = u.get("outputTokens", 0)
    print(f"[{label}] ss={setting_sources!r} assist={n_assist} "
          f"text={text_chars} thinking={thinking_chars} "
          f"in_tok={in_tok} out_tok={out_tok} "
          f"result={'EMPTY' if not result else repr(result[:80])}", flush=True)


async def leg_model(label: str, model: str) -> None:
    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        setting_sources=[],
    )
    prompt = _prompt()
    n_assist = text_chars = 0
    result = None
    in_tok = out_tok = 0
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            n_assist += 1
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text_chars += len(b.text)
        elif type(msg).__name__ == "ResultMessage":
            result = msg.result
            u = (msg.model_usage or {})
            entry = next(iter(u.values()), {}) if u else {}
            in_tok = entry.get("inputTokens", 0)
            out_tok = entry.get("outputTokens", 0)
    print(f"[{label}] model={model} assist={n_assist} text={text_chars} "
          f"in_tok={in_tok} out_tok={out_tok} "
          f"result={'EMPTY' if not result else repr(result[:80])}", flush=True)


async def main() -> None:
    # Leg C first (cheapest signal): same big prompt, NO [1m] tier suffix.
    await leg_model("C/no-1m", "claude-sonnet-5")
    await leg("A/global", None)
    await leg("B/isolated", [])


if __name__ == "__main__":
    asyncio.run(main())
