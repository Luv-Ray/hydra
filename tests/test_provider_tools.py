"""A step's allowed_tools must bound what the model can call.

Regression guard for a silent misconfiguration: the restriction was fed to the
SDK's `allowed_tools` (a permission whitelist) instead of `tools` (the
visibility set). Under permission_mode="bypassPermissions" every tool is already
auto-allowed, so the whitelist was a no-op and the model saw the full default
toolset regardless of what a step declared. The fix maps our allowed_tools to
the SDK's `tools`; these tests pin that mapping so it can't regress.
"""

from __future__ import annotations

import asyncio

from hydra import provider as provider_mod
from hydra.provider import ClaudeProvider, StepRunOptions, DEFAULT_TOOLS


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


def _capture_options(monkeypatch) -> dict:
    """Patch query() to record the ClaudeAgentOptions run() built, then finish."""
    captured: dict = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield _FakeResult()

    monkeypatch.setattr(provider_mod, "query", fake_query)
    monkeypatch.setattr(provider_mod, "ResultMessage", _FakeResult)
    monkeypatch.setattr(provider_mod, "AssistantMessage", type("A", (), {}))
    return captured


def test_allowed_tools_maps_to_sdk_tools(monkeypatch):
    # A restrictive step (e.g. mini-review) must have its set land on the SDK's
    # `tools` — the field that actually removes other tools from the model.
    captured = _capture_options(monkeypatch)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="mini-review", allowed_tools=["Read"])
    asyncio.run(provider.run("go", opts))

    sdk_opts = captured["options"]
    assert sdk_opts.tools == ["Read"]
    # And it must NOT be smuggled into the SDK allowed_tools whitelist, where
    # bypassPermissions would make it a no-op. Empty default = SDK default.
    assert sdk_opts.allowed_tools == []


def test_no_allowed_tools_falls_back_to_default_set(monkeypatch):
    # A step that declares nothing gets the default built-in set on `tools`.
    captured = _capture_options(monkeypatch)
    provider = ClaudeProvider()
    opts = StepRunOptions(step_id="analyze", allowed_tools=None)
    asyncio.run(provider.run("go", opts))

    assert captured["options"].tools == DEFAULT_TOOLS


def test_declared_set_is_passed_through_verbatim(monkeypatch):
    # fetch_issue declares the default set plus Monitor; it must arrive intact.
    captured = _capture_options(monkeypatch)
    provider = ClaudeProvider()
    tools = ["Read", "Write", "Bash", "Glob", "Grep", "Monitor"]
    opts = StepRunOptions(step_id="fetch_issue", allowed_tools=tools)
    asyncio.run(provider.run("go", opts))

    assert captured["options"].tools == tools
