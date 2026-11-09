# Hydra

**Declaratively orchestrate fully autonomous AI-agent workflows in YAML.** You write down how a task splits into steps, how the steps hand off to each other, and what to redo until it is good enough. The engine runs it to completion, with no human in the loop.

It runs on Anthropic's official [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), and each step's execution quality is aligned with Claude Code: every step is an autonomous agent session with the full tool surface (Read/Write/Bash/Grep/MCP…).

```yaml
version: 2
steps:
  - id: investigate
    prompt: Read the codebase, find the root cause of this bug, and propose a fix.
    output: root-cause.md

  - id: review
    inject: [root-cause.md]          # feed the previous step's artifact into this prompt
    prompt: Review the analysis above. List every problem you find.
    output: review.md
    verdict: [ACCEPT, REJECT]        # the reply's last line decides

  - id: revise
    resume: investigate              # continue the original analyst's session
    inject: [review.md]
    prompt: Fix every problem in the review, then overwrite root-cause.md.

loops:
  - id: review_loop
    body: [review]
    fix: revise                      # runs between rounds
    until: "steps.review.verdict == 'ACCEPT'"
    max_rounds: 3
```

```bash
hydra run analyze.yaml
```

---

## Core design

Hydra's identity comes from a few deliberate trade-offs. Understand these and you understand the engine.

### 1. Context flows through files, not in-memory messages

Most orchestration frameworks pass agent-to-agent messages in process memory. Hydra inverts that: **every step's output is a file on disk, and the next step explicitly declares which files it reads.**

- `output: path`: the step's reply body, saved verbatim by the framework as that file.
- `inject: [paths]`: the contents of these files are injected at the front of this step's prompt.

Context is **inspectable** (you can open what each step produced and consumed), runs are **resumable** (artifacts live on disk), and steps are **decoupled** (they share nothing but files). Each step is its own agent session that sees only its own prompt and the files it injects, so prompts should be self-contained.

A step that declares `output:` gets an **output contract** appended to its prompt: its reply *is* the file content, written by the framework, so the agent should neither frame it as a chat message nor write the file itself.

### 2. `resume` continues a whole conversation

```yaml
  - id: revise
    resume: investigate              # append to investigate's session
  - id: second_opinion
    resume: investigate
    fork: true                       # continue in a copy, leave the original alone
```

`resume: step_id` continues from the full context the target ended with: the agent remembers what it read and where its reasoning stood. That is what makes "analyze → get critiqued → fix it" work well: the reviser *is* the original analyst. By default the turns are appended to the target's session, so a second revision remembers the first. `fork: true` branches a copy instead, which is what parallel readers of one session need (the validator requires it when they may run concurrently). `compact_before_resume` compacts a heavy session first.

### 3. Three control-flow concepts, nothing else

- **Dependencies** come from `after:`, `resume:`, exact-path `inject:` matching another step's `output:`, and verdicts read in `when:`. Scheduling is **dataflow**: each step starts the moment everything it depends on is done, so independent branches run in parallel and a slow branch never holds up an unrelated one.
- **Branching is `when:`** over inputs or upstream verdicts. A `verdict:` declares a closed set of values; the engine reads exactly one of them from the last line of the reply.

  ```yaml
  - {id: triage, verdict: [fixable, refactor, wontfix], prompt: ...}
  - {id: fix, when: "steps.triage.verdict == 'fixable'", prompt: ...}
  ```

- **Repetition is a declared loop**: re-run `body` until `until` holds, running `fix` between rounds, at most `max_rounds` times. When it never converges, `on_exhausted: continue` carries the last round forward instead of failing the run.

There is no goto. Every edge is visible in the YAML, and `hydra validate` checks all of it statically: unknown keys, dangling references, cycles, verdict values compared against a typo, unsafe concurrent resumes.

### 4. A verdict is an answer; a failure is not

A reviewer replying REJECT gave a real answer, and loops act on it. A step that errored, timed out, returned an empty body, or ended without a readable verdict gave **no** answer. That is retried (`retry`, default 1) and then fails the run. It is never mistaken for REJECT, so a transient API blip can't trigger a pointless revise round.

### 5. Fully autonomous, file-in / file-out

No human-in-the-loop checkpoints. Input is CLI arguments (`--var`) plus files in the working directory; output is files in the working directory. That suits long unattended flows: triage, code review, batch analysis. Run state is persisted under `.hydra/runs/`, and `--resume-last` / `--resume RUN_ID` continue an interrupted run, reusing every finished step and finished loop.

---

## Install

```bash
pip install -e .          # or: uv pip install -e .
```

Requires Python ≥ 3.10, plus `claude-agent-sdk`, `click`, and `pyyaml`.

## Configuration

Hydra runs on the Claude Agent SDK, which drives the same `claude` CLI that Claude Code uses. **Its configuration is therefore whatever Claude Code already uses on your machine.** There is nothing Hydra-specific to set up:

- **Authentication**: the same credentials Claude Code runs on (a logged-in session, or `ANTHROPIC_API_KEY`). If `claude` works in your terminal, Hydra works.
- **Settings, MCP servers, model access**: inherited from your existing Claude Code / CLI configuration. A pipeline can additionally declare its own `mcp_servers`, which are passed straight through to the SDK.

If you don't have it yet, install and authenticate the Claude CLI first (see the [Claude Code docs](https://docs.claude.com/en/docs/claude-code)), then point Hydra at a pipeline.

## Usage

```bash
hydra run pipeline.yaml                     # execute
hydra run pipeline.yaml --var issue_id=123  # pass an input
hydra run pipeline.yaml --resume-last       # continue the most recent interrupted run
hydra run pipeline.yaml --dry-run           # print the schedule without running
hydra validate pipeline.yaml                # static checks, no execution
```

## Architecture

```
YAML pipeline
     │
  loader.py + models.py   strict parse, inputs, defaults, templates
     │
  validate.py             references, expressions, loops, cycles, resume safety
     │
  dag.py                  dependency inference → scheduling graph (steps + loop nodes)
     │
  executor.py             dataflow scheduler, when, loops, verdicts, retries
     │
     ├── provider.py      Claude Agent SDK calls (run / verdict follow-up / compact)
     ├── inject.py        file injection, output & verdict contracts
     ├── events.py        structured event stream (control-flow observability)
     └── persistence.py   .hydra/runs/ persistence and resume
```

## Docs & examples

- Full DSL reference (fields and semantics): [`docs/specs/dsl-reference.md`](docs/specs/dsl-reference.md). Code is the source of truth. *(In Chinese.)*
- Design notes: [`docs/design.md`](docs/design.md) *(In Chinese.)*
- Examples: [`examples/hello-world.yaml`](examples/hello-world.yaml) (minimal) and [`examples/bug-analysis.yaml`](examples/bug-analysis.yaml) (a cheap gate loop before a parallel review-panel loop, resume-based revision, `when` on a loop's outcome, per-step tool scoping).
