"""Structured event stream for pipeline observability.

Writes an append-only events.jsonl (always on, cheap) into the run dir and
mirrors each event as a concise human-readable line on stderr. This is the
authoritative record of control-flow decisions — check evaluations, routing,
back-jumps, iteration counts, and termination — which stdout logs alone can't
reconstruct after the fact.
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
        self, step_id: str, *, trigger: str, execution: int,
        resume: str | None, inject_count: int,
    ) -> None:
        extra = []
        if execution > 1:
            extra.append(f"run #{execution}")
        if resume:
            extra.append(f"resume={resume}")
        if inject_count:
            extra.append(f"inject={inject_count}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        self.emit(
            "step_start", f"→ {step_id}{suffix}",
            step=step_id, trigger=trigger, execution=execution,
            resume=resume, inject_count=inject_count,
        )

    def step_result(
        self, step_id: str, *, execution: int, provider_status: str,
        turns: int, duration_ms: int, cost_usd: float | None,
    ) -> None:
        cost = f", ${cost_usd:.4f}" if cost_usd else ""
        self.emit(
            "step_result",
            f"  {step_id}: agent {provider_status} "
            f"({turns} turns, {duration_ms}ms{cost})",
            step=step_id, execution=execution, provider_status=provider_status,
            turns=turns, duration_ms=duration_ms, cost_usd=cost_usd,
        )

    def check(
        self, step_id: str, *, mode: str, pattern: str | None,
        matched: bool, verdict: str, target: str | None = None,
    ) -> None:
        if mode == "success_pattern":
            hit = "matched" if matched else "not matched"
            human = f"  check {step_id}: /{pattern}/ {hit} → {verdict}"
        elif mode == "routes" and target:
            human = f"  check {step_id}: /{pattern}/ matched → goto {target}"
        else:
            human = f"  check {step_id}: no pattern matched → {verdict}"
        self.emit(
            "check", human,
            step=step_id, mode=mode, pattern=pattern,
            matched=matched, verdict=verdict, target=target,
        )

    def route(
        self, step_id: str, *, condition: str, target: Any, kind: str,
    ) -> None:
        self.emit(
            "route", f"  route {step_id}: {condition} → {kind} {target}",
            step=step_id, condition=condition, target=target, kind=kind,
        )

    def back_jump(
        self, origin: str, *, targets: list[str], subgraph: list[str],
        iteration: int, max_iterations: int,
    ) -> None:
        self.emit(
            "back_jump",
            f"  back-jump {origin} → {targets} "
            f"(iteration {iteration}/{max_iterations}, re-run {subgraph})",
            origin=origin, targets=targets, subgraph=subgraph,
            iteration=iteration, max_iterations=max_iterations,
        )

    def retry(self, step_id: str, *, attempt: int, max_retries: int) -> None:
        self.emit(
            "retry", f"  retry {step_id}: attempt {attempt}/{max_retries}",
            step=step_id, attempt=attempt, max_retries=max_retries,
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

    def pipeline_done(self, *, status: str, reason: str) -> None:
        self.emit(
            "pipeline_done", f"pipeline {status}: {reason}",
            status=status, reason=reason,
        )
