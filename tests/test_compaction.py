"""compact_before_resume: gate on the resume target's context percentage."""

from __future__ import annotations

from helpers import FakeProvider, load, run
from hydra.provider import StepResult

WINDOW = 1_000_000

PIPELINE = """
    version: 2
    steps:
      - {id: analyze, prompt: x, model: "claude-opus-5-5[1m]", output: rc.md}
      - {id: review_a, prompt: x, resume: analyze, fork: true, compact_before_resume: 50}
      - {id: review_b, prompt: x, resume: analyze, fork: true, compact_before_resume: 50}
"""


class Sized(FakeProvider):
    """analyze comes back at `pct` of the window; everything else is small."""

    def __init__(self, pct: float):
        super().__init__()
        self.pct = pct

    async def run(self, prompt, options):
        r = await super().run(prompt, options)
        tokens = int(WINDOW * self.pct / 100) if options.step_id == "analyze" else 1000
        return StepResult(session_id=r.session_id, output_text=r.output_text,
                          input_tokens=tokens, context_window=WINDOW)


def test_compacts_once_when_over_threshold(tmp_path):
    # Two forked reviewers resume analyze in parallel; the per-target lock
    # means /compact runs exactly once.
    provider = Sized(pct=70)
    status, engine = run(load(tmp_path, PIPELINE), provider)
    assert status == "success"
    # Compacted with analyze's own options: its model (a different model is a
    # full cache miss) and the pipeline workdir (the CLI keys sessions by cwd).
    assert provider.compact_calls == [
        (engine.results["analyze"].session_id, "claude-opus-5-5[1m]", str(tmp_path))
    ]


def test_skips_when_under_threshold(tmp_path):
    provider = Sized(pct=30)
    assert run(load(tmp_path, PIPELINE), provider)[0] == "success"
    assert provider.compact_calls == []


def test_forked_resumers_get_their_own_sessions(tmp_path):
    provider = Sized(pct=30)
    _, engine = run(load(tmp_path, PIPELINE), provider)
    a = provider.options["review_a"][0]
    assert a.fork_session and a.resume_session_id == engine.results["analyze"].session_id
    # Forks leave the target's session id alone.
    assert engine.results["review_a"].session_id != engine.results["analyze"].session_id
