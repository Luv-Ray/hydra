"""Provider protocol and Claude Agent SDK implementation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
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


DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]

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


class ClaudeProvider:
    """Provider implementation using Claude Agent SDK."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

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

        self._log(f"  prompt: {prompt[:120]}{'...' if len(prompt) > 120 else ''}")
        if options.resume_session_id:
            self._log(f"  resume: {options.resume_session_id}")

        session_id: str | None = None
        result_text: str | None = None
        is_error = False
        cost_usd: float | None = None
        duration_ms = 0
        num_turns = 0

        async for msg in query(prompt=prompt, options=sdk_options):
            if isinstance(msg, AssistantMessage):
                self._log_assistant(msg, options.step_id)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                is_error = msg.is_error
                cost_usd = msg.total_cost_usd
                duration_ms = msg.duration_ms
                num_turns = msg.num_turns
                if msg.result:
                    result_text = msg.result
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

    def _log(self, message: str) -> None:
        print(f"[hydra] {message}", file=sys.stderr)

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
