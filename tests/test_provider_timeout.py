"""ClaudeProvider.run enforces the step's wall-clock timeout.

The Agent SDK has no per-turn timeout, so provider.run wraps message consumption
in asyncio.wait_for. On expiry it returns a failed StepResult (is_error=True)
rather than raising, so retry/route handling treats it like any other failure.
"""

from __future__ import annotations

import asyncio

import pytest

from hydra import provider as provider_mod
from hydra.provider import ClaudeProvider, StepRunOptions


class _FakeResult:
    """Minimal stand-in for the SDK ResultMessage."""

    def __init__(self):
        self.session_id = "sess-x"
        self.is_error = False
        self.total_cost_usd = 0.0
        self.duration_ms = 1
        self.num_turns = 1
        self.result = "done"
        self.model_usage = {}
        self.usage = {}
        self.permission_denials = []
        self.errors = []
        self.subtype = "success"
        self.stop_reason = "end_turn"
        self.api_error_status = None


def _install_slow_query(monkeypatch, delay: float):
    async def slow_query(prompt, options):
        await asyncio.sleep(delay)
        yield _FakeResult()
        # isinstance checks in run() rely on the real ResultMessage type; patch
        # that too so the fake is recognized.

    monkeypatch.setattr(provider_mod, "query", slow_query)
    monkeypatch.setattr(provider_mod, "ResultMessage", _FakeResult)
    monkeypatch.setattr(provider_mod, "AssistantMessage", type("A", (), {}))


def test_run_times_out(monkeypatch):
    # Query sleeps 5s; the step budget is 1s → timeout fires first.
    _install_slow_query(monkeypatch, delay=5.0)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="s", timeout=1)
    result = asyncio.run(provider.run("go", opts))
    assert result.is_error is True


def test_run_completes_under_timeout(monkeypatch):
    # Query returns immediately; well under the 5s budget → normal result.
    _install_slow_query(monkeypatch, delay=0.0)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="s", timeout=5)
    result = asyncio.run(provider.run("go", opts))
    assert result.is_error is False
    assert result.output_text == "done"
