"""Provider protocol and Claude Agent SDK implementation."""

from __future__ import annotations

import sys
import json
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query
from claude_agent_sdk.types import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass
class StepRunOptions:
    step_id: str
    model: str | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    timeout: int | None = None
    cwd: str | None = None
    resume_session_id: str | None = None
    # With resume: continue in a copy of the session (SDK fork_session) instead
    # of appending to it in place.
    fork_session: bool = False
    mcp_servers: dict[str, Any] | None = None
    # Pipeline-wide system_prompt, appended to the built-in automation prompt.
    extra_system_prompt: str | None = None
    # Where to write the full, untruncated message transcript for this run. One
    # JSON object per SDK message (every content block: text/thinking/tool_use/
    # tool_result, plus system/user-injected messages). Always written when set,
    # independent of verbose — this is the record that lets an empty/abnormal
    # output be diagnosed without re-running the step. None disables it.
    transcript_path: str | None = None


@dataclass
class StepResult:
    # None when the step produced no real SDK session (e.g. it timed out before
    # any ResultMessage arrived). Downstream resume treats a None target session
    # as a hard error rather than fabricating a fake id that the SDK can't find.
    session_id: str | None
    output_text: str
    # Reply to the verdict follow-up (verdict.prompt), when the step has one.
    verdict_reply: str | None = None
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int = 0
    num_turns: int = 0
    # Context accounting — drives compact_before_resume. input_tokens is the
    # point-in-time occupancy (the last turn's input side, incl. cache), NOT the
    # run's cumulative token spend; context_window is the model's window size.
    # See run()'s last_turn_input_tokens and context_pct.
    input_tokens: int = 0
    context_window: int = 0
    # Termination diagnostics from the SDK ResultMessage. Recorded for every
    # step (not just verbose runs) so an empty/abnormal output can be told apart
    # from a genuine model reply after the fact — e.g. subtype "error_max_turns"
    # or a non-None api_error_status (429/500/529) means the step never produced
    # a verdict. See executor._abnormal_reason.
    subtype: str | None = None
    stop_reason: str | None = None
    api_error_status: int | None = None
    errors: list[str] | None = None

    def context_pct(self) -> float | None:
        """Fraction of the model context window consumed on entry, 0-100.

        None when the window is unknown (e.g. stub providers in tests).
        """
        if not self.context_window:
            return None
        return 100.0 * self.input_tokens / self.context_window


# The built-in tools a step gets when it declares no allowed_tools of its own.
# This is the *visibility* set (SDK `tools=`), not a permission whitelist — see
# run() for why the distinction matters.
DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


def _extract_context(msg: ResultMessage) -> tuple[int, int]:
    """Pull (input_tokens, context_window) from a ResultMessage.

    Prefers model_usage (carries the per-model contextWindow, e.g. 1_000_000
    for the 1M opus), falling back to the flat usage dict. Returns (0, 0) when
    neither is present so callers treat context as unknown rather than zero.

    IMPORTANT: the returned input_tokens is model_usage.inputTokens, which is the
    SUM of every turn's input tokens across the whole run — NOT current context
    occupancy. Do not divide it by context_window (that yields >100% on
    multi-turn runs). It's used here only for the context_window value and as a
    last-resort fallback; occupancy comes from the last turn's per-turn usage
    (see run()'s last_turn_input_tokens). See context_pct.
    """
    model_usage = msg.model_usage or {}
    if model_usage:
        # One entry per model touched; the step's own model dominates. Take the
        # entry with the largest inputTokens (the resumed transcript load).
        entry = max(model_usage.values(), key=lambda e: e.get("inputTokens", 0))
        return int(entry.get("inputTokens", 0)), int(entry.get("contextWindow", 0))
    usage = msg.usage or {}
    return int(usage.get("input_tokens", 0)), 0

def _block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize one assistant/user content block, untruncated.

    Covers the block types that matter for diagnosing an empty/abnormal output:
    text (the visible reply), thinking (reasoning that produced no visible
    text), tool_use (what it tried to call), tool_result. Unknown block types
    fall back to a repr so nothing is silently dropped.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", None),
            "is_error": getattr(block, "is_error", None),
        }
    return {"type": type(block).__name__, "repr": repr(block)[:2000]}


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """Serialize any SDK message to a plain dict for the transcript.

    AssistantMessage/UserMessage carry content blocks; ResultMessage carries the
    termination summary; system/hook messages are captured by attribute dump so
    an injected nudge (e.g. "your previous response had no visible output") is
    preserved. Everything is JSON-serializable via a str() fallback.
    """
    kind = type(msg).__name__
    out: dict[str, Any] = {"kind": kind}
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        out["content"] = [_block_to_dict(b) for b in content]
    elif content is not None:
        out["content"] = content
    if isinstance(msg, ResultMessage):
        out.update(
            subtype=msg.subtype,
            is_error=msg.is_error,
            stop_reason=msg.stop_reason,
            num_turns=msg.num_turns,
            duration_ms=msg.duration_ms,
            total_cost_usd=msg.total_cost_usd,
            result=msg.result,
            usage=msg.usage,
            model_usage=msg.model_usage,
            api_error_status=getattr(msg, "api_error_status", None),
            errors=msg.errors,
        )
    else:
        # For non-Assistant/Result messages (system init, hook events), dump the
        # raw data dict if present so nothing about the environment is lost.
        data = getattr(msg, "data", None)
        if data is not None and "content" not in out:
            out["data"] = data
        subtype = getattr(msg, "subtype", None)
        if subtype is not None:
            out["subtype"] = subtype
    return out


class Transcript:
    """Full untruncated JSONL record of one provider.run() call.

    Opened with truncation, so a retry's file reflects that attempt only. A
    no-op when path is None.
    """

    def __init__(self, path: str | None):
        self._f: TextIO | None = Path(path).open("w") if path else None

    def write(self, record: dict[str, Any]) -> None:
        if not self._f:
            return
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = json.dumps(
                {"ts": record["ts"], "kind": record.get("kind", "?"),
                 "unserializable": repr(record)[:2000]},
                ensure_ascii=False,
            )
        self._f.write(line + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


SYSTEM_PROMPT = (
    "You are running in a fully automated pipeline. "
    "There is no human operator to answer questions or confirm decisions. "
    "Do not ask for clarification, confirmation, or permission — "
    "make reasonable decisions autonomously and continue to completion. "
    "If you encounter ambiguity, choose the most likely interpretation and proceed."
)


class Provider(Protocol):
    async def run(self, prompt: str, options: StepRunOptions) -> StepResult: ...

    # `options` are those of the step that built the session (same tools,
    # system prompt, model, cwd), so the follow-up stays on its cached prefix.
    async def follow_up(
        self, session_id: str, prompt: str, options: StepRunOptions
    ) -> str: ...

    async def compact(
        self, session_id: str, options: StepRunOptions
    ) -> tuple[int, int]: ...


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

    @staticmethod
    def _sdk_options(options: StepRunOptions) -> ClaudeAgentOptions:
        """The one place StepRunOptions becomes ClaudeAgentOptions.

        run(), check() and compact() all build from this, so a follow-up or a
        /compact on a session uses the same tools, system prompt and model the
        session was built with. Prompt caching keys on that prefix (tools →
        system → messages); a follow-up with a different toolset or system
        prompt would re-read the whole transcript uncached.
        """
        system_prompt = SYSTEM_PROMPT
        if options.extra_system_prompt:
            system_prompt = f"{SYSTEM_PROMPT}\n\n{options.extra_system_prompt}"
        # A step's allowed_tools must restrict what the model can *call*, so it
        # maps to the SDK's `tools` (the visibility set), NOT the SDK's
        # `allowed_tools`. Those are different fields: SDK `allowed_tools` is a
        # permission whitelist ("run these without prompting"), and under
        # permission_mode="bypassPermissions" every tool is already auto-allowed,
        # so passing our restriction there is a silent no-op — the model still
        # sees the full default toolset (Bash/Write/Edit/Task/...). Feeding it to
        # `tools` is what actually removes the others from the model's context.
        # (MCP tools come from the CLI's setting_sources and are unaffected by
        # `tools`; this bounds only built-ins, which is where step misuse lives.)
        tools = options.allowed_tools or DEFAULT_TOOLS
        sdk_options = ClaudeAgentOptions(
            tools=tools,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
        )

        if options.model:
            sdk_options.model = options.model
        if options.max_turns:
            sdk_options.max_turns = options.max_turns
        if options.cwd:
            sdk_options.cwd = options.cwd
        if options.resume_session_id:
            sdk_options.resume = options.resume_session_id
            sdk_options.fork_session = options.fork_session
        if options.mcp_servers:
            sdk_options.mcp_servers = options.mcp_servers
        return sdk_options

    async def run(self, prompt: str, options: StepRunOptions) -> StepResult:
        sdk_options = self._sdk_options(options)
        self._log_prompt(prompt, options)

        # Full untruncated transcript for this run (see StepRunOptions). Record
        # the prompt we sent first, then every message as it streams in.
        # Local to this call: concurrent steps share one provider, so a handle
        # on self would be closed/overwritten by whichever step finishes first.
        tr = Transcript(options.transcript_path)
        tr.write({"kind": "prompt", "text": prompt})

        session_id: str | None = None
        result_text: str | None = None
        is_error = False
        cost_usd: float | None = None
        duration_ms = 0
        num_turns = 0
        input_tokens = 0
        context_window = 0
        # Point-in-time context occupancy: the input side of the LAST turn's
        # usage (input + both cache buckets — cached tokens still occupy the
        # window). This is what context_pct must divide by the window, NOT the
        # ResultMessage's model_usage.inputTokens, which is the SUM of every
        # turn's input tokens across the whole agentic run (42 turns → 2M) and
        # produces nonsense like "209% of a 1M window". Each AssistantMessage
        # overwrites this, so after the loop it holds the final turn's value.
        last_turn_input_tokens = 0
        subtype: str | None = None
        stop_reason: str | None = None
        api_error_status: int | None = None
        errors: list[str] | None = None

        async def _consume() -> None:
            nonlocal session_id, result_text, is_error, cost_usd
            nonlocal duration_ms, num_turns, input_tokens, context_window
            nonlocal last_turn_input_tokens
            nonlocal subtype, stop_reason, api_error_status, errors
            async for msg in query(prompt=prompt, options=sdk_options):
                tr.write(_message_to_dict(msg))
                if isinstance(msg, AssistantMessage):
                    self._log_assistant(msg, options.step_id)
                    u = msg.usage or {}
                    if u:
                        last_turn_input_tokens = (
                            int(u.get("input_tokens", 0))
                            + int(u.get("cache_read_input_tokens", 0))
                            + int(u.get("cache_creation_input_tokens", 0))
                        )
                elif isinstance(msg, ResultMessage):
                    session_id = msg.session_id
                    is_error = msg.is_error
                    cost_usd = msg.total_cost_usd
                    duration_ms = msg.duration_ms
                    num_turns = msg.num_turns
                    # Take the window size from model_usage (correct), but the
                    # occupancy from the per-turn point value captured above —
                    # fall back to _extract_context's cumulative value only if no
                    # turn carried usage, so we're never worse than before.
                    cumulative_tokens, context_window = _extract_context(msg)
                    input_tokens = last_turn_input_tokens or cumulative_tokens
                    subtype = msg.subtype
                    stop_reason = msg.stop_reason
                    api_error_status = getattr(msg, "api_error_status", None)
                    errors = msg.errors
                    if msg.result:
                        result_text = msg.result
                    # Always record the result line (not just on denials/errors) so
                    # every step leaves a trace of how it terminated — an empty
                    # output is impossible to diagnose after the fact otherwise.
                    self._log_result(msg, options.step_id)

        # The SDK exposes no per-turn timeout, so enforce the step's wall-clock
        # budget here. On expiry we surface it as a failed StepResult (not a
        # raise) so retry/route handling treats it like any other step failure.
        try:
            if options.timeout:
                await asyncio.wait_for(_consume(), timeout=options.timeout)
            else:
                await _consume()
        except asyncio.TimeoutError:
            self._log(f"step '{options.step_id}' timed out after {options.timeout}s")
            tr.write(
                {"kind": "timeout", "after_s": options.timeout}
            )
            tr.close()
            # session_id may be None here if the timeout fired before any
            # ResultMessage arrived. Surface that honestly rather than inventing
            # a "timeout-<id>" string — a fake id would sail through resume and
            # only blow up deep in the SDK with "No conversation found".
            return StepResult(
                session_id=session_id,
                output_text=result_text or "",
                is_error=True,
                duration_ms=(options.timeout or 0) * 1000,
                subtype="timeout",
                stop_reason=stop_reason,
                api_error_status=api_error_status,
                errors=errors,
            )
        except Exception as exc:
            # The CLI can emit a ResultMessage with is_error=True (e.g. an
            # upstream stream interruption: "上游响应流中断，响应不完整") and
            # then exit non-zero. The SDK turns that non-zero exit into a raise
            # from inside `async for msg in query(...)` — so this except fires
            # *after* _consume already captured the failing ResultMessage into
            # the nonlocals above. If we let it propagate, run() never returns
            # and the executor's abnormal-outcome retry/classification never
            # runs — the whole pipeline crashes on a transient blip. Convert it
            # to a failed StepResult, exactly like the timeout branch, so retry
            # handling treats it as the transient error it is.
            #
            # asyncio.CancelledError is a BaseException on 3.8+, so `except
            # Exception` already lets genuine cancellation propagate.
            self._log(f"step '{options.step_id}' errored mid-stream: {exc}")
            tr.write({"kind": "error", "error": str(exc)})
            tr.close()
            # Prefer the structured error text the CLI already reported (in
            # `errors`); fall back to the raised exception message. This is what
            # surfaces in the step_abnormal event / ExecutionError reason, so
            # the operator sees "上游响应流中断" rather than a bare "success"
            # (the SDK's ProcessError-replacement text falls back to subtype
            # when errors is empty, which is misleading on its own).
            err_list = errors if errors else [str(exc)]
            return StepResult(
                session_id=session_id,
                output_text=result_text or "",
                is_error=True,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                num_turns=num_turns,
                input_tokens=input_tokens,
                context_window=context_window,
                # Distinguish a mid-stream transport failure from a clean model
                # reply when the CLI never gave us a subtype of its own.
                subtype=subtype or "stream_error",
                stop_reason=stop_reason,
                api_error_status=api_error_status,
                errors=err_list,
            )

        tr.close()

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
            subtype=subtype,
            stop_reason=stop_reason,
            api_error_status=api_error_status,
            errors=errors,
        )

    @staticmethod
    def _session_options(session_id: str, options: StepRunOptions) -> ClaudeAgentOptions:
        """Options for one more turn appended to `session_id` itself (no fork).

        cwd must match the directory the session was created under — the CLI
        keys sessions by project dir, so a different cwd fails with "No
        conversation found". Carrying the building step's options keeps it so.
        """
        sdk_options = ClaudeProvider._sdk_options(
            replace(options, resume_session_id=session_id, fork_session=False)
        )
        sdk_options.max_turns = 1
        return sdk_options

    async def follow_up(
        self, session_id: str, prompt: str, options: StepRunOptions
    ) -> str:
        """Ask one more question in the same session; return the reply text."""
        self._log(f"  [{options.step_id}] follow-up: {prompt[:100]}")
        result_text = ""
        async for msg in query(prompt=prompt, options=self._session_options(session_id, options)):
            if isinstance(msg, ResultMessage):
                if msg.result:
                    result_text = msg.result
                self._log(f"  [{options.step_id}] follow-up reply: {result_text[:100]}")
        return result_text

    async def compact(
        self, session_id: str, options: StepRunOptions
    ) -> tuple[int, int]:
        """Compact a session in place, the way `/compact` does in the CLI.

        Resumes the session in a throwaway connection, sends the `/compact`
        slash command (the CLI summarizes the conversation so far), and
        disconnects. The compaction persists to the session on disk, so the
        next step that resumes this id inherits the lighter transcript.

        `options` are the resume *target's* run options. `/compact` feeds the
        whole transcript back through the model, so it must run on the model,
        tools and system prompt the session was built with — any mismatch is a
        full cache miss over the entire transcript. cwd must match too (see
        _session_options).

        Returns (input_tokens, context_window) after compaction — measured via
        get_context_usage() so the caller can update its cached size and log
        the reduction. Returns (0, 0) if the size could not be read.
        """
        client = ClaudeSDKClient(options=self._session_options(session_id, options))
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
        segments = [
            s for s in prompt.split("\n\n---\n\n")
            if not s.startswith(("[Output contract]", "[Verdict contract]"))
        ]
        instruction = (segments[-1] if segments else prompt).strip()
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
        # Whether the reply carried a body — an empty result on an ok status is
        # exactly the failure mode this line exists to make visible.
        parts.append("result=empty" if not msg.result else "result=present")
        if msg.stop_reason:
            parts.append(f"stop_reason={msg.stop_reason}")
        api_status = getattr(msg, "api_error_status", None)
        if api_status:
            parts.append(f"api_error_status={api_status}")
        if msg.permission_denials:
            parts.append(f"permission_denials={len(msg.permission_denials)}")
        if msg.errors:
            parts.append(f"errors={msg.errors}")
        self._log(f"  [{step_id}] result: {', '.join(parts)}")
