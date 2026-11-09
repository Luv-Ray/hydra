"""Structured event stream for pipeline observability.

Writes an append-only events.jsonl (always on, cheap) into the run dir and
mirrors each event as a concise human-readable line on stderr. This is the
authoritative record of control-flow decisions, which stdout logs alone can't
reconstruct after the fact: step starts/results, abnormal outcomes, verdicts,
skips, loop rounds, compactions, and termination.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventLog:
    """Emits control-flow events to events.jsonl and stderr."""

    def __init__(self, run_dir: Path | None = None):
        self.path = (run_dir / "events.jsonl") if run_dir else None

    def emit(self, event: str, human: str, **fields: Any) -> None:
        """Record one event: structured to JSONL, human-readable to stderr."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[hydra] {human}", file=sys.stderr)

    # --- Control-flow event helpers -------------------------------------

    def step_start(
        self, step_id: str, *, execution: int, resume: str | None,
        fork: bool, inject_count: int, loop: str | None = None,
    ) -> None:
        extra = []
        if execution > 1:
            extra.append(f"run #{execution}")
        if resume:
            extra.append(f"{'fork' if fork else 'resume'}={resume}")
        if inject_count:
            extra.append(f"inject={inject_count}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        self.emit(
            "step_start", f"→ {step_id}{suffix}",
            step=step_id, execution=execution, resume=resume, fork=fork,
            inject_count=inject_count, loop=loop,
        )

    def skip(self, step_id: str, *, when: str, execution: int) -> None:
        self.emit(
            "skip", f"  skip {step_id} (when: {when})",
            step=step_id, when=when, execution=execution,
        )

    def step_result(
        self, step_id: str, *, execution: int, provider_status: str,
        turns: int, duration_ms: int, cost_usd: float | None,
        subtype: str | None = None, stop_reason: str | None = None,
        api_error_status: int | None = None, output_empty: bool = False,
    ) -> None:
        cost = f", ${cost_usd:.4f}" if cost_usd else ""
        self.emit(
            "step_result",
            f"  {step_id}: agent {provider_status} "
            f"({turns} turns, {duration_ms}ms{cost})",
            step=step_id, execution=execution, provider_status=provider_status,
            turns=turns, duration_ms=duration_ms, cost_usd=cost_usd,
            # Termination diagnostics — always recorded so an empty/abnormal
            # output can be diagnosed from events.jsonl without a verbose rerun.
            subtype=subtype, stop_reason=stop_reason,
            api_error_status=api_error_status, output_empty=output_empty,
        )

    def step_abnormal(
        self, step_id: str, *, reason: str, will_retry: bool,
        attempt: int, budget: int,
    ) -> None:
        """A step produced no trustworthy answer (errored, empty, no verdict).

        Distinct from a verdict like REJECT, which is a real answer. This either
        retries within the step's `retry` budget or fails the pipeline.
        """
        tail = (
            f"retry {attempt}/{budget}" if will_retry
            else "no retries left → abort"
        )
        self.emit(
            "step_abnormal",
            f"  {step_id}: abnormal outcome ({reason}) → {tail}",
            step=step_id, reason=reason, will_retry=will_retry,
            attempt=attempt, budget=budget,
        )

    def verdict(
        self, step_id: str, *, verdict: str, line: str, execution: int,
    ) -> None:
        self.emit(
            "verdict", f"  verdict {step_id}: {verdict}",
            step=step_id, verdict=verdict, line=line, execution=execution,
        )

    def loop_round(
        self, loop_id: str, *, round: int, max_rounds: int,
        until: str, satisfied: bool,
    ) -> None:
        tail = "until satisfied" if satisfied else "until not satisfied"
        self.emit(
            "loop_round",
            f"  loop {loop_id}: round {round}/{max_rounds} → {tail}",
            loop=loop_id, round=round, max_rounds=max_rounds,
            until=until, satisfied=satisfied,
        )

    def loop_done(self, loop_id: str, *, status: str, rounds: int) -> None:
        self.emit(
            "loop_done", f"  loop {loop_id}: {status} after {rounds} round(s)",
            loop=loop_id, status=status, rounds=rounds,
        )

    def compact(
        self, step_id: str, *, target: str, before_pct: float,
        threshold: int, before_tokens: int, after_tokens: int,
        after_pct: float | None,
    ) -> None:
        after = f"{after_pct:.0f}%" if after_pct is not None else "?"
        self.emit(
            "compact",
            f"  compact {target} (before {step_id}): "
            f"{before_pct:.0f}% > {threshold}% → {after} "
            f"(tokens {before_tokens} → {after_tokens})",
            step=step_id, target=target, before_pct=before_pct,
            threshold=threshold, before_tokens=before_tokens,
            after_tokens=after_tokens, after_pct=after_pct,
        )

    def resumed(self, source_run_id: str, *, steps: list[str]) -> None:
        joined = ", ".join(steps) if steps else "(none)"
        self.emit(
            "resumed",
            f"resuming from {source_run_id}: skipping {len(steps)} "
            f"completed step(s) [{joined}]",
            source_run_id=source_run_id, steps=steps,
        )

    def pipeline_done(self, *, status: str, reason: str) -> None:
        self.emit(
            "pipeline_done", f"pipeline {status}: {reason}",
            status=status, reason=reason,
        )
