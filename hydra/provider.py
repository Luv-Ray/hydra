"""Provider protocol and Claude Agent SDK implementation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query
from claude_agent_sdk.types import AssistantMessage, ToolUseBlock, TextBlock


@dataclass
class StepRunOptions:
    step_id: str
    model: str | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    timeout: int | None = None
    cwd: str | None = None
    resume_session_id: str | None = None
    mcp_servers: dict[str, Any] | None = None


@dataclass
class StepResult:
    session_id: str
    output_text: str
    check_output: str | None = None
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int = 0
    num_turns: int = 0
    # Context accounting (from model_usage) — drives compact_before_resume.
    input_tokens: int = 0
    context_window: int = 0

    def context_pct(self) -> float | None:
        """Fraction of the model context window consumed on entry, 0-100.

        None when the window is unknown (e.g. stub providers in tests).
        """
        if not self.context_window:
            return None
        return 100.0 * self.input_tokens / self.context_window


DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


def _extract_context(msg: ResultMessage) -> tuple[int, int]:
    """Pull (input_tokens, context_window) from a ResultMessage.

    Prefers model_usage (carries the per-model contextWindow, e.g. 1_000_000
    for the 1M opus), falling back to the flat usage dict. Returns (0, 0) when
    neither is present so callers treat context as unknown rather than zero.
    """
    model_usage = msg.model_usage or {}
    if model_usage:
        # One entry per model touched; the step's own model dominates. Take the
        # entry with the largest inputTokens (the resumed transcript load).
        entry = max(model_usage.values(), key=lambda e: e.get("inputTokens", 0))
        return int(entry.get("inputTokens", 0)), int(entry.get("contextWindow", 0))
    usage = msg.usage or {}
    return int(usage.get("input_tokens", 0)), 0

SYSTEM_PROMPT = (
    "You are running in a fully automated pipeline. "
    "There is no human operator to answer questions or confirm decisions. "
    "Do not ask for clarification, confirmation, or permission — "
    "make reasonable decisions autonomously and continue to completion. "
    "If you encounter ambiguity, choose the most likely interpretation and proceed."
)


class Provider(Protocol):
    async def run(self, prompt: str, options: StepRunOptions) -> StepResult: ...

    async def check(self, session_id: str, prompt: str) -> str: ...

    async def compact(self, session_id: str) -> tuple[int, int]: ...


class ClaudeProvider:
    """Provider implementation using Claude Agent SDK."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._trace: TextIO | None = None

    def set_trace_file(self, path: Path | None) -> None:
        """Route the detailed agent trace (prompts, tool calls, check I/O) to a
        file instead of stderr. These are category-2 logs — useful for tuning a
        workflow later, but noise during a live run. When set, stderr stays quiet
        (unless verbose=True, which mirrors to both). Control-flow lines are
        owned by EventLog, not this provider.
        """
        if self._trace:
            self._trace.close()
        self._trace = path.open("a") if path else None

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sdk_options = ClaudeAgentOptions(
            allowed_tools=options.allowed_tools or DEFAULT_TOOLS,
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
        )

        if options.model:
            sdk_options.model = options.model
        if options.max_turns:
            sdk_options.max_turns = options.max_turns
        if options.cwd:
            sdk_options.cwd = options.cwd
        if options.resume_session_id:
            sdk_options.resume = options.resume_session_id
        if options.mcp_servers:
            sdk_options.mcp_servers = options.mcp_servers

        self._log_prompt(prompt, options)

        session_id: str | None = None
        result_text: str | None = None
        is_error = False
        cost_usd: float | None = None
        duration_ms = 0
        num_turns = 0
        input_tokens = 0
        context_window = 0

        async for msg in query(prompt=prompt, options=sdk_options):
            if isinstance(msg, AssistantMessage):
                self._log_assistant(msg, options.step_id)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                is_error = msg.is_error
                cost_usd = msg.total_cost_usd
                duration_ms = msg.duration_ms
                num_turns = msg.num_turns
                input_tokens, context_window = _extract_context(msg)
                if msg.result:
                    result_text = msg.result
                if msg.permission_denials or msg.errors:
                    self._log_result(msg, options.step_id)

        if session_id is None:
            raise RuntimeError(f"Step '{options.step_id}' did not return a session ID")

        return StepResult(
            session_id=session_id,
            output_text=result_text or "",
            is_error=is_error,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            input_tokens=input_tokens,
            context_window=context_window,
        )

    async def check(self, session_id: str, prompt: str) -> str:
        """Send a follow-up check prompt in the same session."""
        self._log(f"  check: {prompt[:100]}")
        sdk_options = ClaudeAgentOptions(
            resume=session_id,
            permission_mode="bypassPermissions",
            max_turns=1,
        )

        result_text = ""
        async for msg in query(prompt=prompt, options=sdk_options):
            if isinstance(msg, ResultMessage):
                if msg.result:
                    result_text = msg.result
                self._log(f"  check result: {result_text[:100]}")

        return result_text

    async def compact(self, session_id: str) -> tuple[int, int]:
        """Compact a session in place, the way `/compact` does in the CLI.

        Resumes the session in a throwaway connection, sends the `/compact`
        slash command (the CLI summarizes the conversation so far), and
        disconnects. The compaction persists to the session on disk, so the
        next step that resumes this id inherits the lighter transcript.

        Returns (input_tokens, context_window) after compaction — measured via
        get_context_usage() so the caller can update its cached size and log
        the reduction. Returns (0, 0) if the size could not be read.
        """
        options = ClaudeAgentOptions(
            resume=session_id,
            permission_mode="bypassPermissions",
            max_turns=1,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query("/compact")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    break
            try:
                usage = await client.get_context_usage()
                return int(usage.get("totalTokens", 0)), int(usage.get("rawMaxTokens", 0))
            except Exception:
                return 0, 0
        finally:
            await client.disconnect()

    def _log(self, message: str) -> None:
        """Write a detail line to the agent trace file (category 2).

        Falls back to stderr when no trace file is set (e.g. tests, dry runs).
        With verbose=True it mirrors to stderr even when a trace file exists.
        """
        line = f"[hydra] {message}"
        if self._trace:
            self._trace.write(line + "\n")
            self._trace.flush()
            if self.verbose:
                print(line, file=sys.stderr)
        else:
            print(line, file=sys.stderr)

    def _log_prompt(self, prompt: str, options: StepRunOptions) -> None:
        """Log the step's actual instruction, not the injected file headers.

        build_prompt() prepends injected file contents before the instruction,
        joined by '\\n\\n---\\n\\n'. The real instruction is the last segment;
        showing it (instead of prompt[:120]) makes each step's log distinct.
        """
        instruction = prompt.rsplit("\n\n---\n\n", 1)[-1].strip()
        preview = instruction[:160].replace("\n", " ")
        ellipsis = "..." if len(instruction) > 160 else ""
        self._log(f"  [{options.step_id}] instruction: {preview}{ellipsis}")

    def _log_assistant(self, msg: AssistantMessage, step_id: str) -> None:
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                input_preview = str(block.input)[:80]
                self._log(f"  [{step_id}] tool: {block.name}({input_preview})")
            elif isinstance(block, TextBlock) and self.verbose:
                text_preview = block.text[:100].replace("\n", " ")
                self._log(f"  [{step_id}] text: {text_preview}")

    def _log_result(self, msg: ResultMessage, step_id: str) -> None:
        status = "error" if msg.is_error else msg.subtype
        parts = [f"status={status}", f"turns={msg.num_turns}", f"{msg.duration_ms}ms"]
        if msg.total_cost_usd:
            parts.append(f"${msg.total_cost_usd:.4f}")
        if msg.permission_denials:
            parts.append(f"permission_denials={len(msg.permission_denials)}")
        if msg.errors:
            parts.append(f"errors={msg.errors}")
        self._log(f"  [{step_id}] result: {', '.join(parts)}")
