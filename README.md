# Hydra

**Declaratively orchestrate fully autonomous AI-agent workflows in YAML.** You write down how a task splits into steps, how the steps hand off to each other, and when to loop back and redo — the engine runs it to completion, with no human in the loop.

It runs on Anthropic's official [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), and each step's execution quality is aligned with Claude Code: every step is an autonomous agent session with the full tool surface (Read/Write/Bash/Grep/MCP…).

```yaml
name: analyze
steps:
  - id: investigate
    prompt: Read the codebase, find the root cause of this bug, output analysis and a fix.
    output: root-cause.md

  - id: review
    inject: [root-cause.md]        # feed the previous step's artifact into this prompt
    prompt: Review the analysis above. Reply only ACCEPT or REJECT.
    check:
      success_pattern: ACCEPT
    routes:
      - if: failure
        goto: investigate          # REJECT → jump back and redo
```

```bash
hydra run analyze.yaml
```

---

## Core design

Hydra's identity comes from a few deliberate trade-offs. Understand these and you understand the engine.

### 1. Context flows through files, not in-memory messages

Most orchestration frameworks pass agent-to-agent messages in process memory. Hydra inverts that: **every step's output is a file on disk, and the next step explicitly declares which files it reads.**

- `output: path` — the step's reply body, saved verbatim by the framework as that file.
- `inject: [paths]` — the contents of these files are injected at the front of this step's prompt.

Three payoffs follow directly. Context is **inspectable** — what each step produces and what the next consumes are files you can open and read. Runs are **interruptible and resumable** — artifacts live on disk, so a restart loses nothing. And steps are **decoupled** — they collaborate only through a file contract, sharing no in-memory state.

This comes with an **output contract**: for a step that declares `output:`, its reply *is* the file content and the framework does the writing — the agent should not also Write the file itself. The contract is appended to the prompt automatically, so the agent emits the artifact rather than framing its reply as a chat message with preamble and sign-off.

### 2. `resume` for 1:1 session continuation

Beyond the lightweight "read a file" handoff, Hydra can carry **an entire conversation history** forward:

```yaml
  - id: revise
    resume: investigate            # continue investigate's full session
    inject: [review.md]            # then feed in the review feedback
    prompt: Revise your analysis per the review.
```

`resume: step_id` continues the current step from the full context the target step ended with — the agent remembers what it read and where its reasoning stood. This is what makes "analyze → get critiqued → go back and fix it" loops work well: the reviser *is* the original analyst, without having to re-inject all the background. Pair it with `compact_before_resume` to compact the history before continuing and keep context usage in check.

### 3. DAG parallelism + routing + back-jump loops

Dependencies between steps (from `after:`, or implied by `inject:`) are inferred into a DAG and **executed layer by layer in parallel** — independent steps on the same layer run concurrently.

Each step can end with one decision that determines where control goes next:

```yaml
    check:
      success_pattern: ACCEPT      # or use check.prompt to let another agent adjudicate
    routes:
      - if: success
        goto: next_step            # forward jump
      - if: failure
        goto: investigate          # back jump — forms a revise loop
```

- **Forward jumps** hand control to steps outside the DAG (`trigger: route` steps are reachable only via goto).
- **Back jumps** return to an upstream step, forming a "do → review → fail → go back and fix → review again" loop. Each back jump is a fresh session that reads the latest file contents.
- `max_iterations` bounds a loop so it can't reject forever; when it doesn't converge you can route to "emit a report with the last version" instead of failing the whole run.

### 4. Retry with feedback — and "transient failure" split from "semantic rejection"

Two kinds of "didn't succeed" are different things, and Hydra handles them with separate budgets:

- **Semantic rejection** (a reviewer returns REJECT): a meaningful conclusion, not something to blindly re-run. It should route via `routes` to a revise step. Set `retry: 0` on such a step so it routes on its verdict instead of wasting a rerun.
- **Transient / infrastructure failure** (upstream stream interruption, empty output): this *should* be retried. `retry` + `retry_inject` re-run the step carrying the previous failure as feedback rather than starting blind. When the upstream SDK raises mid-stream, the engine converts it into a failed result handed to the retry machinery instead of crashing the whole pipeline.

### 5. Fully autonomous, file-in / file-out

No human-in-the-loop checkpoints. Input is CLI arguments (`--var`) plus files in the working directory; output is files in the working directory. That makes Hydra suited to running long flows unattended — triage, code review, batch analysis. Run state is persisted under `.hydra/runs/`, and `--resume-last` / `--resume RUN_ID` continue an interrupted run.

---

## Install

```bash
pip install -e .          # or: uv pip install -e .
```

Requires Python ≥ 3.10, plus `claude-agent-sdk`, `click`, and `pyyaml`.

## Configuration

Hydra runs on the Claude Agent SDK, which drives the same `claude` CLI that Claude Code uses. **Its configuration is therefore whatever Claude Code already uses on your machine** — there is nothing Hydra-specific to set up:

- **Authentication** — the same credentials Claude Code runs on (a logged-in session, or `ANTHROPIC_API_KEY`). If `claude` works in your terminal, Hydra works.
- **Settings, MCP servers, model access** — inherited from your existing Claude Code / CLI configuration. A pipeline can additionally declare its own `mcp_servers`, which are passed straight through to the SDK.

If you don't have it yet, install and authenticate the Claude CLI first (see the [Claude Code docs](https://docs.claude.com/en/docs/claude-code)), then point Hydra at a pipeline.

## Usage

```bash
hydra run pipeline.yaml                     # execute
hydra run pipeline.yaml --var issue_id=123  # pass an input
hydra run pipeline.yaml --resume-last       # continue the most recent interrupted run
hydra run pipeline.yaml --dry-run           # print the execution plan without running
hydra validate pipeline.yaml                # static checks, no execution
```

## Architecture

```
YAML pipeline
     │
  loader.py     parse + template expansion + defaults merge
     │
  validate.py   static validation
     │
  dag.py        dependency inference + topological sort
     │
  executor.py   DAG execution + routes + retry + goto
     │
     ├── provider.py     Claude Agent SDK calls
     ├── inject.py       file injection / output contract
     ├── events.py       structured event stream (control-flow observability)
     └── persistence.py  .hydra/runs/ persistence
```

## Docs & examples

- Full DSL reference (fields and semantics): [`docs/specs/dsl-reference.md`](docs/specs/dsl-reference.md) — code is the source of truth. *(In Chinese.)*
- Design notes: [`docs/design.md`](docs/design.md) *(In Chinese.)*
- Examples: [`examples/hello-world.yaml`](examples/hello-world.yaml) (minimal skeleton) and [`examples/anr-fc-analysis.yaml`](examples/anr-fc-analysis.yaml) (a real-scale multi-step analysis pipeline with DAG fan-out, resume, back-jump revise loops, and per-step tool scoping).
