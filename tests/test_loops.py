"""Loops: body → until → fix rounds, exhaustion, and the review/revise patterns."""

from __future__ import annotations

import json

from helpers import FakeProvider, load, run
from hydra.provider import StepResult

REVIEW_LOOP = """
    version: 2
    steps:
      - {{id: analyze, prompt: x, output: rc.md}}
      - {{id: review, prompt: x, inject: [rc.md, notes.md], output: review.md, verdict: [ACCEPT, REJECT]}}
      - {{id: revise, prompt: x, resume: analyze, inject: [review.md], output: notes.md}}
      - {{id: report, prompt: x, inject: [rc.md], after: [review]}}
    loops:
      - id: review_loop
        body: [review]
        fix: revise
        until: "steps.review.verdict == 'ACCEPT'"
        max_rounds: {rounds}
        on_exhausted: {on_exhausted}
"""


def test_loop_converges(tmp_path):
    p = load(tmp_path, REVIEW_LOOP.format(rounds=3, on_exhausted="fail"))
    provider = FakeProvider(script={"review": ["REJECT", "REJECT", "ACCEPT"]})
    status, engine = run(p, provider)
    assert status == "success"
    assert provider.calls == [
        "analyze", "review", "revise", "review", "revise", "review", "report",
    ]
    assert engine.loop_status == {"review_loop": "converged"}
    # Each round persisted separately, not overwritten.
    steps = engine.store.run_dir / "steps" / "review"
    verdicts = [json.loads((d / "meta.json").read_text())["verdict"]
                for d in (steps, steps / "run-2", steps / "run-3")]
    assert verdicts == ["REJECT", "REJECT", "ACCEPT"]
    loop_rec = json.loads((engine.store.run_dir / "loops" / "review_loop.json").read_text())
    assert loop_rec == {"status": "converged", "rounds": 3}


def test_round_one_tolerates_fix_output_missing(tmp_path):
    # review injects notes.md, written by revise — which has not run in round 1.
    p = load(tmp_path, REVIEW_LOOP.format(rounds=2, on_exhausted="fail"))
    provider = FakeProvider(script={"review": ["REJECT", "ACCEPT"]})
    assert run(p, provider)[0] == "success"
    assert "notes.md" not in provider.prompts["review"][0]
    assert "[File: notes.md]\nout-revise" in provider.prompts["review"][1]


def test_exhausted_fail_aborts(tmp_path):
    p = load(tmp_path, REVIEW_LOOP.format(rounds=2, on_exhausted="fail"))
    provider = FakeProvider(script={"review": "REJECT"})
    status, engine = run(p, provider)
    assert status == "failure"
    # No pointless fix after the last round; downstream never runs.
    assert provider.calls == ["analyze", "review", "revise", "review"]
    assert engine.loop_status == {"review_loop": "failed"}


def test_exhausted_continue_proceeds_with_last_round(tmp_path):
    p = load(tmp_path, REVIEW_LOOP.format(rounds=2, on_exhausted="continue"))
    provider = FakeProvider(script={"review": "REJECT"})
    status, engine = run(p, provider)
    assert status == "success"
    assert provider.calls[-1] == "report"
    assert engine.loop_status == {"review_loop": "exhausted"}
    assert "exhausted" in engine._terminal_reason()


def test_append_resume_continues_the_target_session(tmp_path):
    # Two revisions both resume analyze without fork: the second continues the
    # session the first appended to, so the reviser remembers its own edits.
    p = load(tmp_path, REVIEW_LOOP.format(rounds=3, on_exhausted="fail"))
    provider = FakeProvider(script={"review": ["REJECT", "REJECT", "ACCEPT"]})
    _, engine = run(p, provider)
    analyze_session = provider.options["analyze"][0]
    first, second = provider.options["revise"]
    assert first.resume_session_id == second.resume_session_id == engine.results["analyze"].session_id
    assert not first.fork_session
    assert analyze_session.resume_session_id is None


def test_resume_target_size_refreshed_so_compaction_sees_growth(tmp_path):
    # analyze finishes small (30%); the first revise grows the session to 70%.
    # The second revise must compact: v1 kept gating on the stale 30%.
    yaml_text = REVIEW_LOOP.format(rounds=3, on_exhausted="fail").replace(
        "resume: analyze,", "resume: analyze, compact_before_resume: 50,"
    )
    p = load(tmp_path, yaml_text)
    window = 1_000_000

    class Growing(FakeProvider):
        async def run(self, prompt, options):
            r = await super().run(prompt, options)
            pct = {"analyze": 30, "revise": 70}.get(options.step_id, 1)
            return StepResult(session_id=r.session_id, output_text=r.output_text,
                              input_tokens=window * pct // 100, context_window=window)

    provider = Growing(script={"review": ["REJECT", "REJECT", "ACCEPT"]})
    assert run(p, provider)[0] == "success"
    assert len(provider.compact_calls) == 1  # before the second revise only


PANEL = """
    version: 2
    steps:
      - {id: analyze, prompt: x, output: rc.md}
      - {id: mini_review, prompt: x, inject: [rc.md], verdict: [ACCEPT, REJECT]}
      - {id: mini_revise, prompt: x, resume: analyze}
      - {id: r_evidence, prompt: x, inject: [rc.md], output: r1.md, after: [mini_review]}
      - {id: r_reasoning, prompt: x, inject: [rc.md], output: r2.md, after: [mini_review]}
      - {id: verdict, prompt: x, inject: [r1.md, r2.md], output: v.md, verdict: [ACCEPT, REJECT]}
      - {id: revise, prompt: x, resume: analyze, inject: [v.md]}
      - {id: final, prompt: x, inject: [rc.md, v.md]}
    loops:
      - id: gate
        body: [mini_review]
        fix: mini_revise
        until: "steps.mini_review.verdict == 'ACCEPT'"
        max_rounds: 3
        on_exhausted: continue
      - id: panel
        body: [r_evidence, r_reasoning, verdict]
        fix: revise
        until: "steps.verdict.verdict == 'ACCEPT'"
        max_rounds: 2
        on_exhausted: continue
"""


def test_gate_then_parallel_panel(tmp_path):
    # The two v1 hacks this replaces: the expensive panel must not run during
    # the cheap gate's rounds (v1 needed branch-step cut-offs + loop_body), and
    # the panel's reviewers run in parallel each round.
    p = load(tmp_path, PANEL)
    provider = FakeProvider(
        script={"mini_review": ["REJECT", "ACCEPT"], "verdict": ["REJECT", "ACCEPT"]},
        delay={"r_evidence": 0.2, "r_reasoning": 0.2},
    )
    status, engine = run(p, provider)
    assert status == "success"
    calls = provider.calls
    # Panel starts only after the gate converged.
    assert calls.index("r_evidence") > calls.index("mini_revise")
    assert calls.count("mini_review") == 2 and calls.count("r_evidence") == 2
    assert calls.count("verdict") == 2 and calls.count("revise") == 1
    assert calls[-1] == "final"
    # Reviewers overlap within a round.
    (e_start, e_end), (r_start, r_end) = provider.spans["r_evidence"][0], provider.spans["r_reasoning"][0]
    assert e_start < r_end and r_start < e_end
    assert engine.loop_status == {"gate": "converged", "panel": "converged"}


def test_self_inject_in_loop_tolerated_on_first_round(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: draft, prompt: x, inject: [draft.md], output: draft.md, verdict: [DONE, MORE]}
        loops:
          - {id: refine, body: [draft], until: "steps.draft.verdict == 'DONE'", max_rounds: 3}
    """)
    provider = FakeProvider(script={"draft": ["v1\nMORE", "v2\nDONE"]})
    assert run(p, provider)[0] == "success"
    assert "draft.md" not in provider.prompts["draft"][0]
    assert "[File: draft.md]\nv1\nMORE" in provider.prompts["draft"][1]
