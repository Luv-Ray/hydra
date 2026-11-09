"""Continuing a prior run (--resume-last / --resume ID).

Plain steps are reused when their newest execution succeeded; loops are reused
only whole, and only if they finished. Each resume writes a fresh run dir that
records its lineage.
"""

from __future__ import annotations

import json

from helpers import FakeProvider, errored, load, run
from hydra.persistence import RunStore

LINEAR = """
    version: 2
    steps:
      - {id: a, prompt: x, output: a.md}
      - {id: b, prompt: x, inject: [a.md], output: b.md}
      - {id: c, prompt: x, inject: [b.md], output: c.md, retry: 0}
"""


def _fail(step: str) -> FakeProvider:
    return FakeProvider(script={step: errored()})


def test_resume_last_skips_completed(tmp_path):
    p = load(tmp_path, LINEAR)
    assert run(p, _fail("c"))[0] == "failure"
    p2 = FakeProvider()
    assert run(load(tmp_path, LINEAR), p2, resume_last=True)[0] == "success"
    assert p2.calls == ["c"]


def test_resume_explicit_run_id_and_lineage(tmp_path):
    run(load(tmp_path, LINEAR), _fail("c"))
    source = RunStore.find_latest_run(str(tmp_path))
    p2 = FakeProvider()
    assert run(load(tmp_path, LINEAR), p2, resume_run_id=source)[0] == "success"
    assert p2.calls == ["c"]
    newest = RunStore.find_latest_run(str(tmp_path))
    assert newest != source
    meta = json.loads((RunStore.runs_root(str(tmp_path)) / newest / "meta.json").read_text())
    assert meta["resumed_from"] == source
    assert set(meta["resumed_steps"]) == {"a", "b"}


def test_resume_last_no_prior_run_starts_fresh(tmp_path):
    p = FakeProvider()
    assert run(load(tmp_path, LINEAR), p, resume_last=True)[0] == "success"
    assert p.calls == ["a", "b", "c"]


def test_resume_unknown_id_errors_cleanly(tmp_path):
    run(load(tmp_path, LINEAR), _fail("c"))
    assert run(load(tmp_path, LINEAR), FakeProvider(), resume_run_id="nope")[0] == "failure"


def test_failed_step_and_dependents_rerun(tmp_path):
    yaml_text = LINEAR.replace("output: b.md}", "output: b.md, retry: 0}")
    run(load(tmp_path, yaml_text), _fail("b"))
    p2 = FakeProvider()
    run(load(tmp_path, yaml_text), p2, resume_last=True)
    assert p2.calls == ["b", "c"]


LOOPED = """
    version: 2
    steps:
      - {id: analyze, prompt: x, output: rc.md}
      - {id: review, prompt: x, inject: [rc.md], verdict: [ACCEPT, REJECT]}
      - {id: revise, prompt: x, resume: analyze, retry: 0}
      - {id: boom, prompt: x, after: [review], retry: 0}
      - {id: gated, prompt: x, after: [boom], when: "steps.review.verdict == 'ACCEPT'"}
    loops:
      - {id: l, body: [review], fix: revise, until: "steps.review.verdict == 'ACCEPT'", max_rounds: 3}
"""


def test_finished_loop_is_reused_with_its_verdict(tmp_path):
    run(load(tmp_path, LOOPED), FakeProvider(
        script={"review": ["REJECT", "ACCEPT"], "boom": errored()}))
    p2 = FakeProvider()
    status, engine = run(load(tmp_path, LOOPED), p2, resume_last=True)
    assert status == "success"
    # The loop is not re-run, and its restored verdict drives `when`.
    assert p2.calls == ["boom", "gated"]
    assert engine.verdicts["review"] == "ACCEPT"
    # revise resumes analyze's original session from the prior run.
    assert engine.results["analyze"].session_id.startswith("sess-analyze")


def test_loop_interrupted_mid_round_reruns_whole(tmp_path):
    run(load(tmp_path, LOOPED), FakeProvider(script={"review": "REJECT", "revise": errored()}))
    p2 = FakeProvider(script={"review": "ACCEPT"})
    status, engine = run(load(tmp_path, LOOPED), p2, resume_last=True)
    assert status == "success"
    # analyze (a finished plain step) is reused; the unfinished loop starts over.
    assert p2.calls == ["review", "boom", "gated"]
    assert engine.loop_status == {"l": "converged"}
