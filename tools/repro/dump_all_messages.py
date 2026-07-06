"""Repro #2: dump EVERY message fully (no truncation), including raw dicts.

Repro #1 showed assistant_msgs=0 and an SDK-injected UserMessage saying the
previous response "had no visible output". That means the model's turn-1 output
was non-visible (thinking-only? a hook stripped it?). This dumps everything raw
to see turn-1 content, the full injected nudge, and any hook interference.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from claude_agent_sdk import ClaudeAgentOptions, query

from hydra.inject import build_prompt
from hydra.provider import SYSTEM_PROMPT

WORKDIR = "/home/ray/Workspace/pipelines/ISS-202607-00006887A"
PIPELINE = "/home/ray/Workspace/pipelines/anr-fc-analysis.yaml"


def _pretty(obj) -> str:
    def default(o):
        if hasattr(o, "__dict__"):
            return {"__type__": type(o).__name__, **o.__dict__}
        return repr(o)
    try:
        return json.dumps(obj, default=default, ensure_ascii=False, indent=2)
    except Exception:
        return repr(obj)


async def main() -> None:
    data = yaml.safe_load(Path(PIPELINE).read_text())
    step = next(s for s in data["steps"] if s["id"] == "mini-review")
    model = data["defaults"]["model"]

    prompt = build_prompt(
        prompt=step["prompt"],
        inject_paths=step.get("inject", []),
        workdir=WORKDIR,
        output_path=step.get("output"),
    )
    print(f"=== model={model} prompt_len={len(prompt)}", flush=True)

    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        cwd=WORKDIR,
    )

    i = 0
    async for msg in query(prompt=prompt, options=opts):
        i += 1
        cls = type(msg).__name__
        print(f"\n########## MSG {i}: {cls} ##########", flush=True)
        # Dump the message's own attributes fully.
        attrs = getattr(msg, "__dict__", None)
        if attrs is not None:
            print(_pretty(attrs), flush=True)
        else:
            print(repr(msg), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
