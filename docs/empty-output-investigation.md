# Empty-output investigation (mini-review, sonnet-5)

Record of a debugging session. Kept for the follow-up experiments we deferred.

## Symptom

`mini-review` step reported `agent ok (2 turns, 484s, $0.41)` but produced an
empty body. Repeated 3× identically (same cost to the cent, same ~484s). The
empty reply cascaded into a crash before the abnormal-outcome fix landed.

## What the raw SDK data showed

From `ResultMessage` (repro dumps):

- `output_tokens: 0` — the model emitted **nothing** (thinking is counted as
  output too, so it wasn't "thinking hard" either).
- `input_tokens: 82047`, `cost 0.410235` = exactly $5.00/Mtok input (the sonnet
  1M long-context input price). The whole cost was input; zero output cost.
- `stop_reason: end_turn`, `is_error: false`, `api_error_status: null`.
- `duration_ms: 483844` (8 min) with 0 output → request was held/queued
  server-side, not computing.
- The SDK injected a UserMessage: `[Your previous response had no visible
  output. Please continue and produce a user-visible response.]` between the two
  turns — turn 1 produced nothing visible, the nudge didn't help, turn 2 also
  empty → end_turn.

## Ruled out (with evidence)

- API error / rate limit / 5xx: `api_error_status=null`.
- Token cap: `stop_reason=end_turn`, not `max_tokens`.
- Timeout.
- Content hidden in TextBlock or the result field: `text_blocks=0`,
  `output_tokens=0`.
- `[1m]` long-context tier capacity: dropping `[1m]` still empty (leg C).
- superpowers SessionStart hook directly: isolating settings still empty when
  the tool set stayed at 5 (leg B).

## The decisive variable: the tool set

| config | setting_sources | tools | out_tok | result |
|---|---|---|---|---|
| pipeline as-was | None | R,W,B,G,Grep | 0 | EMPTY |
| A/global | None | R,W,B,G,Grep | 0 | EMPTY |
| C/no-1m | None | R,W,B,G,Grep | 14 | EMPTY |
| B/isolated | [] | R,W,B,G,Grep | 15 | EMPTY |
| **decisive** | **[]** | **Read only** | **2024** | **full report ✅** |

`setting_sources` is NOT the deciding factor (B and decisive are both `[]`, one
empty one fine). The **only** variable that flips empty → success is the tool
set: 5 tools → empty, `[Read]` only → a complete 2024-token audit report.

## Working hypothesis (not yet fully bisected)

`mini-review`'s prompt explicitly says the step is pure-logic and must NOT use
logs/tools ("拿不到日志、无法也不应去核对"). Handing it Bash/Grep/Glob/Write
contradicts that: a task told "no tools" while holding a toolbox — sonnet-5
stalls in the contradiction and emits 0 tokens. The 4 challenge_* steps don't
hit this: their prompts DO ask them to read logs, so tools match the task.

Suspected specific trigger: `Bash` (the "should I go verify?" tension), but the
bisection (T2 `[Read,Bash]`, T3 `[Read,Write,Glob,Grep]`) was deferred.

## Fix applied

`mini-review` given `allowed_tools: [Read]` in anr-fc-analysis.yaml — the
proven-good config. Matches the step's own "pure logic, no lookup" design.

Note: the engine's abnormal-outcome handling already caught the empty output
correctly (retried, hard-failed cleanly, no crash) — the empty output itself
was the thing to fix, via the tool set.

## Deferred experiments (repro scripts in tools/repro/)

- Tool bisection: T2 `[Read,Bash]`, T3 `[Read,Write,Glob,Grep]` (isolated,
  sonnet-5, full mini-review prompt) to pin whether Bash specifically is the
  trigger.
- Whether the same 5-tool empty-output reproduces on opus (model sensitivity).
- Each leg is ~8 min (server-held), so batch them.
