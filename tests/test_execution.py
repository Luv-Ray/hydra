"""Scheduling, `when` branching, verdict parsing, and abnormal-outcome retries."""

from __future__ import annotations

from helpers import FakeProvider, errored, load, run
from hydra.executor import parse_verdict


# --- verdict parsing --------------------------------------------------------

def test_parse_verdict_reads_last_line_whole_tokens():
    vals = ["ACCEPT", "REJECT"]
    assert parse_verdict("analysis...\n\nVerdict: accept\n\n", vals)[0] == "ACCEPT"
    # Self-correction: the last line wins.
    assert parse_verdict("ACCEPT\nwait, actually\nREJECT", vals)[0] == "REJECT"
    # Whole tokens only: v1's substring regex matched ACCEPT inside these.
    assert parse_verdict("NOT ACCEPTED", vals)[0] is None
    assert parse_verdict("UNACCEPTABLE", vals)[0] is None
    # Naming both on the final line is ambiguous.
    assert parse_verdict("ACCEPT or REJECT?", vals)[0] is None
    assert parse_verdict("", vals) == (None, "")
    assert parse_verdict("**REJECT**", vals)[0] == "REJECT"


# --- scheduling ---------------------------------------------------------------

def test_inject_infers_dependency_regardless_of_declaration_order(tmp_path):
    # Consumer declared before producer: v1 silently dropped this edge.
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: report, prompt: x, inject: [rc.md]}
          - {id: analyze, prompt: x, output: rc.md}
    """)
    provider = FakeProvider()
    status, _ = run(p, provider)
    assert status == "success"
    assert provider.calls == ["analyze", "report"]
    assert "[File: rc.md]\nout-analyze" in provider.prompts["report"][0]


def test_independent_branches_run_concurrently(tmp_path):
    # Dataflow, not layers: fast_2 starts when fast_1 ends, not when slow ends.
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: slow, prompt: x}
          - {id: fast_1, prompt: x}
          - {id: fast_2, prompt: x, after: [fast_1]}
    """)
    provider = FakeProvider(delay={"slow": 0.4, "fast_1": 0.05, "fast_2": 0.05})
    status, _ = run(p, provider)
    assert status == "success"
    slow_end = provider.spans["slow"][0][1]
    assert provider.spans["fast_2"][0][0] < slow_end


def test_failure_cancels_running_siblings(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: doomed, prompt: x, retry: 0}
          - {id: slow, prompt: x}
          - {id: later, prompt: x, after: [slow]}
    """)
    provider = FakeProvider(script={"doomed": errored()}, delay={"slow": 5})
    status, _ = run(p, provider)
    assert status == "failure"
    assert "slow" not in provider.spans  # cancelled before it finished
    assert "later" not in provider.calls


# --- when branching -------------------------------------------------------------

BRANCH = """
    version: 2
    inputs: {{mode: {{default: {mode}}}}}
    steps:
      - {{id: triage, prompt: x, output: t.md, verdict: [fixable, refactor, wontfix]}}
      - {{id: fix, prompt: x, output: fix.md, when: "steps.triage.verdict == 'fixable'"}}
      - {{id: redesign, prompt: x, output: rd.md, when: "steps.triage.verdict == 'refactor'"}}
      - {{id: deep, prompt: x, when: "inputs.mode == 'deep'"}}
      - {{id: summary, prompt: x, inject: [fix.md, rd.md]}}
"""


def test_when_branches_on_verdict_and_join_tolerates_skipped_producer(tmp_path):
    p = load(tmp_path, BRANCH.format(mode="quick"))
    provider = FakeProvider(script={"triage": "looks simple\nfixable"})
    status, engine = run(p, provider)
    assert status == "success"
    assert "fix" in provider.calls
    assert "redesign" not in provider.calls and "deep" not in provider.calls
    # rd.md's producer was skipped, so its absence is tolerated; fix.md is injected.
    assert "[File: fix.md]" in provider.prompts["summary"][0]
    assert "rd.md" not in provider.prompts["summary"][0]
    assert engine.skipped == {"redesign", "deep"}


def test_when_sees_input_defaults(tmp_path):
    # v1 bug: when only saw --var values, never defaults or --var-file.
    p = load(tmp_path, BRANCH.format(mode="deep"))
    provider = FakeProvider(script={"triage": "wontfix"})
    assert run(p, provider)[0] == "success"
    assert "deep" in provider.calls


def test_missing_inject_with_no_producer_still_fails(tmp_path):
    p = load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, inject: [typo.md]}]\n")
    provider = FakeProvider()
    assert run(p, provider)[0] == "failure"
    assert provider.calls == []


# --- verdicts and abnormal outcomes ---------------------------------------------

def test_inline_verdict_appends_contract(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: gate, prompt: x, output: g.md, verdict: [OK, BAD]}
          - {id: next, prompt: x, when: "steps.gate.verdict == 'OK'"}
    """)
    provider = FakeProvider(script={"gate": "fine\nOK"})
    status, engine = run(p, provider)
    assert status == "success" and engine.verdicts["gate"] == "OK"
    assert "[Verdict contract]" in provider.prompts["gate"][0]
    assert "OK / BAD" in provider.prompts["gate"][0]
    # The verdict line is part of the saved file.
    assert (tmp_path / "g.md").read_text() == "fine\nOK"


def test_verdict_prompt_asks_follow_up_in_same_session(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - id: test
            prompt: run the tests
            verdict: {values: [pass, fail], prompt: "Did every test pass?"}
          - {id: ship, prompt: x, when: "steps.test.verdict == 'pass'"}
    """)
    provider = FakeProvider(follow_ups={"test": "All green.\nPASS"})
    status, engine = run(p, provider)
    assert status == "success" and engine.verdicts["test"] == "pass"
    step, session, question = provider.follow_up_calls[0]
    assert step == "test" and session == engine.results["test"].session_id
    assert question.startswith("Did every test pass?") and "[Verdict contract]" in question
    # Contract goes on the follow-up, not the main prompt.
    assert "[Verdict contract]" not in provider.prompts["test"][0]
    assert "ship" in provider.calls


def test_unparseable_verdict_is_abnormal_and_retried(tmp_path):
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: gate, prompt: x, verdict: [OK, BAD], retry: 1}
          - {id: next, prompt: x, when: "steps.gate.verdict == 'OK'"}
    """)
    provider = FakeProvider(script={"gate": ["I think it's fine", "OK"]})
    status, engine = run(p, provider)
    assert status == "success"
    assert provider.calls.count("gate") == 2 and engine.verdicts["gate"] == "OK"


def test_abnormal_outcome_fails_after_retry_budget(tmp_path):
    # Empty body on a step with a verdict: no answer, so never treated as BAD.
    p = load(tmp_path, """
        version: 2
        steps:
          - {id: gate, prompt: x, verdict: [OK, BAD]}
          - {id: on_bad, prompt: x, when: "steps.gate.verdict == 'BAD'"}
    """)
    provider = FakeProvider(script={"gate": ""})
    status, _ = run(p, provider)
    assert status == "failure"
    assert provider.calls == ["gate", "gate"]  # default retry: 1
    assert "on_bad" not in provider.calls


def test_errored_step_recovers_on_retry(tmp_path):
    p = load(tmp_path, "version: 2\nsteps: [{id: a, prompt: x, output: a.md}]\n")
    provider = FakeProvider(script={"a": [errored(), "body"]})
    assert run(p, provider)[0] == "success"
    assert (tmp_path / "a.md").read_text() == "body"


def test_failed_follow_up_is_abnormal_not_a_crash(tmp_path):
    class Boom(FakeProvider):
        async def follow_up(self, *a, **k):
            raise RuntimeError("stream broke")

    p = load(tmp_path, """
        version: 2
        steps:
          - id: t
            prompt: x
            retry: 0
            verdict: {values: [pass, fail], prompt: "?"}
          - {id: u, prompt: x, when: "steps.t.verdict == 'pass'"}
    """)
    assert run(p, Boom())[0] == "failure"
