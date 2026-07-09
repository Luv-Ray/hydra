"""context_pct must reflect point-in-time occupancy, not cumulative token spend.

The bug this guards against: ClaudeProvider.run used the ResultMessage's
model_usage.inputTokens for occupancy, but that field is the SUM of every
turn's input tokens across the whole agentic run. On a 42-turn run it reached
2,087,845 against a 1,000,000 window → context_pct() reported 209%, and the
compact_before_resume gate was deciding on a meaningless ratio. The fix reads
the LAST turn's per-turn usage (input + both cache buckets) instead.
"""

from __future__ import annotations

import asyncio

from hydra import provider as provider_mod
from hydra.provider import ClaudeProvider, StepRunOptions


class _FakeAssistant:
    """SDK AssistantMessage stand-in carrying one turn's raw usage dict."""

    def __init__(self, usage: dict | None):
        self.content = []
        self.usage = usage


class _FakeResult:
    """SDK ResultMessage stand-in with a cumulative model_usage."""

    def __init__(self, *, cumulative_input: int, window: int):
        self.session_id = "sess-x"
        self.is_error = False
        self.total_cost_usd = 1.0
        self.duration_ms = 1
        self.num_turns = 3
        self.result = "done"
        self.usage = {"input_tokens": cumulative_input}
        self.model_usage = {
            "claude-sonnet-4.6[1m]": {
                "inputTokens": cumulative_input,
                "contextWindow": window,
            }
        }
        self.permission_denials = []
        self.errors = []
        self.subtype = "success"
        self.stop_reason = "end_turn"
        self.api_error_status = None


def _install_query(monkeypatch, turns: list[dict | None], result: _FakeResult):
    async def fake_query(prompt, options):
        for u in turns:
            yield _FakeAssistant(u)
        yield result

    monkeypatch.setattr(provider_mod, "query", fake_query)
    monkeypatch.setattr(provider_mod, "ResultMessage", _FakeResult)
    monkeypatch.setattr(provider_mod, "AssistantMessage", _FakeAssistant)


def test_context_pct_uses_last_turn_not_cumulative(monkeypatch):
    # Three turns with growing input; cumulative model_usage = 2M > 1M window.
    turns = [
        {"input_tokens": 100_000},
        {"input_tokens": 300_000},
        {"input_tokens": 500_000},  # last turn = true occupancy
    ]
    result = _FakeResult(cumulative_input=2_000_000, window=1_000_000)
    _install_query(monkeypatch, turns, result)

    r = asyncio.run(ClaudeProvider().run("go", StepRunOptions(step_id="analyze")))
    assert r.input_tokens == 500_000  # last turn, NOT 2M cumulative
    assert r.context_window == 1_000_000
    assert r.context_pct() == 50.0  # not 200%


def test_context_pct_counts_cache_tokens(monkeypatch):
    # Cached input still occupies the window: input + cache_read + cache_creation.
    turns = [
        {
            "input_tokens": 10_000,
            "cache_read_input_tokens": 400_000,
            "cache_creation_input_tokens": 90_000,
        }
    ]
    result = _FakeResult(cumulative_input=10_000, window=1_000_000)
    _install_query(monkeypatch, turns, result)

    r = asyncio.run(ClaudeProvider().run("go", StepRunOptions(step_id="analyze")))
    assert r.input_tokens == 500_000
    assert r.context_pct() == 50.0


def test_context_pct_fallback_when_no_turn_usage(monkeypatch):
    # No AssistantMessage carried usage → fall back to cumulative so we're never
    # worse than before (and never crash / return None for a known window).
    turns = [None, {}]  # no usable per-turn usage
    result = _FakeResult(cumulative_input=750_000, window=1_000_000)
    _install_query(monkeypatch, turns, result)

    r = asyncio.run(ClaudeProvider().run("go", StepRunOptions(step_id="analyze")))
    assert r.input_tokens == 750_000  # fell back to cumulative
    assert r.context_pct() == 75.0


def test_context_pct_none_when_window_unknown(monkeypatch):
    # No window → occupancy is unknowable; context_pct returns None (pin
    # existing behavior so the compact gate treats it as "unknown", not 0).
    turns = [{"input_tokens": 123}]
    result = _FakeResult(cumulative_input=123, window=0)
    _install_query(monkeypatch, turns, result)

    r = asyncio.run(ClaudeProvider().run("go", StepRunOptions(step_id="analyze")))
    assert r.context_pct() is None
