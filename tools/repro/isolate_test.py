"""A/B test: does loading global settings (superpowers SessionStart hook)
suppress plain text output? Sends a trivial prompt under two configs and
reports whether any assistant TextBlock came back.

Fast — the prompt is trivial, so each leg is seconds, not the 8-min audit.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, TextBlock, ThinkingBlock

from hydra.provider import SYSTEM_PROMPT

PROMPT = "Reply with exactly the word ACCEPT and nothing else."


async def run_leg(label: str, setting_sources) -> None:
    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model="claude-sonnet-5[1m]",
        setting_sources=setting_sources,
    )
    text_chars = thinking_chars = n_assist = 0
    result = None
    async for msg in query(prompt=PROMPT, options=opts):
        if isinstance(msg, AssistantMessage):
            n_assist += 1
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text_chars += len(b.text)
                elif isinstance(b, ThinkingBlock):
                    thinking_chars += len(b.thinking)
        elif type(msg).__name__ == "ResultMessage":
            result = msg.result
    print(f"[{label}] setting_sources={setting_sources!r}: "
          f"assistant_msgs={n_assist} text_chars={text_chars} "
          f"thinking_chars={thinking_chars} result="
          f"{'EMPTY' if not result else repr(result[:80])}", flush=True)


async def main() -> None:
    # Leg A: current behavior — None loads all global settings (superpowers hook).
    await run_leg("A/global", None)
    # Leg B: isolation — no filesystem settings, no superpowers hook.
    await run_leg("B/isolated", [])


if __name__ == "__main__":
    asyncio.run(main())
