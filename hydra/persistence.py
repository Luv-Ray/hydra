"""Run persistence — .hydra/runs/<timestamp>/ directory management."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .provider import StepResult


@dataclass
class RunMeta:
    pipeline_name: str
    started_at: str
    inputs: dict[str, str]
    workdir: str | None = None


@dataclass
class StepMeta:
    step_id: str
    status: str  # "pending" | "running" | "success" | "failure" | "skipped"
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0
    cost_usd: float | None = None
    num_turns: int = 0
    session_id: str | None = None
    iteration: int = 0
    retries_used: int = 0


class RunStore:
    """Manages a single pipeline run's persistence directory."""

    def __init__(self, base_dir: str | Path, pipeline_name: str, inputs: dict[str, str], workdir: str | None = None):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(base_dir) / ".hydra" / "runs" / ts
        self.run_dir.mkdir(parents=True, exist_ok=True)

        meta = RunMeta(
            pipeline_name=pipeline_name,
            started_at=datetime.now(timezone.utc).isoformat(),
            inputs=inputs,
            workdir=workdir,
        )
        self._write_json(self.run_dir / "meta.json", asdict(meta))

    def step_dir(self, step_id: str) -> Path:
        d = self.run_dir / "steps" / step_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_step_start(self, step_id: str) -> None:
        meta = StepMeta(
            step_id=step_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write_json(self.step_dir(step_id) / "meta.json", asdict(meta))

    def save_step_result(self, step_id: str, result: StepResult, status: str, retries_used: int = 0) -> None:
        d = self.step_dir(step_id)

        if result.output_text:
            (d / "output.md").write_text(result.output_text)

        meta = StepMeta(
            step_id=step_id,
            status=status,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            session_id=result.session_id,
            retries_used=retries_used,
        )
        self._write_json(d / "meta.json", asdict(meta))

    def save_step_skipped(self, step_id: str) -> None:
        meta = StepMeta(step_id=step_id, status="skipped")
        self._write_json(self.step_dir(step_id) / "meta.json", asdict(meta))

    def save_result(self, status: str) -> None:
        self._write_json(
            self.run_dir / "result.json",
            {"status": status, "finished_at": datetime.now(timezone.utc).isoformat()},
        )

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
