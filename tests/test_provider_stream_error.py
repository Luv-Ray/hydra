"""A mid-stream SDK error must return a failed StepResult, not raise.

The failure this guards against: the CLI emits a ResultMessage with
is_error=True (e.g. an upstream stream interruption) and then exits non-zero.
The SDK turns that non-zero exit into a bare Exception raised from inside
`async for msg in query(...)`. If provider.run lets it propagate, run() never
returns and the executor's abnormal-outcome retry/classification never runs —
the whole pipeline crashes on a transient blip. provider.run must instead
convert it to StepResult(is_error=True), exactly like the timeout branch.
"""

from __future__ import annotations

import asyncio

from hydra import provider as provider_mod
from hydra.provider import ClaudeProvider, StepRunOptions


class _FakeResult:
    """SDK ResultMessage stand-in for a mid-stream error result."""

    def __init__(self):
        self.session_id = "sess-x"
        self.is_error = True
        self.total_cost_usd = 1.5
        self.duration_ms = 813946
        self.num_turns = 6
        self.result = "API Error: 上游响应流中断，响应不完整: error decoding response body"
        self.model_usage = {}
        self.usage = {}
        self.permission_denials = []
        self.errors = None
        self.subtype = "success"  # the misleading subtype the real CLI emits
        self.stop_reason = "stop_sequence"
        self.api_error_status = None


def _install_erroring_query(monkeypatch, *, yield_result: bool):
    async def erroring_query(prompt, options):
        if yield_result:
            # The CLI yielded the error ResultMessage first, THEN exited
            # non-zero — the real ordering. _consume captures it, then the SDK
            # raises as the process exits.
            yield _FakeResult()
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(provider_mod, "query", erroring_query)
    monkeypatch.setattr(provider_mod, "ResultMessage", _FakeResult)
    monkeypatch.setattr(provider_mod, "AssistantMessage", type("A", (), {}))


def test_mid_stream_error_after_result_becomes_failed_result(monkeypatch):
    # ResultMessage(is_error=True) captured, then SDK raises on non-zero exit.
    _install_erroring_query(monkeypatch, yield_result=True)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="challenge_alternatives")
    result = asyncio.run(provider.run("go", opts))

    assert result.is_error is True
    # Captured session/diagnostics from the ResultMessage survive.
    assert result.session_id == "sess-x"
    assert result.subtype == "success"
    # The actual error text is carried through so the operator sees it, rather
    # than a bare "success". Falls back to the raised exception text when the
    # SDK left `errors` empty.
    assert result.errors
    assert any("error result" in e or "响应" in e for e in result.errors)


def test_mid_stream_error_before_any_result_becomes_failed_result(monkeypatch):
    # The stream breaks before any ResultMessage arrives — session_id stays
    # None (honest), and run() still returns rather than raising.
    _install_erroring_query(monkeypatch, yield_result=False)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="analyze")
    result = asyncio.run(provider.run("go", opts))

    assert result.is_error is True
    assert result.session_id is None
    assert result.subtype == "stream_error"  # no CLI subtype → our marker
    assert result.errors  # the raised exception text
