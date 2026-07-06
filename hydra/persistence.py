"""Run persistence — .hydra/runs/<timestamp>/ directory management."""

from __future__ import annotations

import json
import secrets
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
class ResumedStep:
    """A successful step loaded from a prior run, used to seed a resume.

    Carries just enough to satisfy downstream steps without re-executing:
    the produced body (for `inject`), the check output, and the SDK session id
    (so a later `resume:` target can continue the original conversation).
    """
    step_id: str
    session_id: str | None
    output_text: str
    check_output: str | None = None


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
        # Microsecond precision so lexical sort of run ids equals chronological
        # order — find_latest_run relies on this to pick the true newest run.
        # The random suffix is only a same-microsecond tiebreak for uniqueness.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        run_id = f"{ts}-{secrets.token_hex(3)}"
        self.run_dir = Path(base_dir) / ".hydra" / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        meta = RunMeta(
            pipeline_name=pipeline_name,
            started_at=datetime.now(timezone.utc).isoformat(),
            inputs=inputs,
            workdir=workdir,
        )
        self._write_json(self.run_dir / "meta.json", asdict(meta))

    def step_dir(self, step_id: str, execution: int = 1) -> Path:
        """Directory for a step's Nth execution.

        Loop iterations (back-jumps, retries that re-run the step) each get
        their own subdirectory so earlier rounds are never overwritten. The
        first execution lives directly under steps/<id>/; subsequent runs go
        to steps/<id>/run-<N>/.
        """
        base = self.run_dir / "steps" / step_id
        d = base if execution <= 1 else base / f"run-{execution}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record_resumed_from(self, source_run_id: str, steps: list[str]) -> None:
        """Note in this run's meta.json which prior run it continued from.

        Keeps run dirs independent (each resume writes a fresh dir) while
        preserving the lineage so a chain of resumes stays traceable.
        """
        meta_path = self.run_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            meta = {}
        meta["resumed_from"] = source_run_id
        meta["resumed_steps"] = steps
        self._write_json(meta_path, meta)

    def save_step_start(self, step_id: str, execution: int = 1) -> None:
        meta = StepMeta(
            step_id=step_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            iteration=execution,
        )
        self._write_json(self.step_dir(step_id, execution) / "meta.json", asdict(meta))

    def save_step_result(
        self, step_id: str, result: StepResult, status: str,
        retries_used: int = 0, execution: int = 1,
    ) -> None:
        d = self.step_dir(step_id, execution)

        if result.output_text:
            (d / "output.md").write_text(result.output_text)
        if result.check_output:
            (d / "check.txt").write_text(result.check_output)

        # Carry the start time forward from save_step_start so meta.json keeps
        # a real wall-clock span instead of overwriting started_at with null.
        started_at = self._read_started_at(d)

        meta = StepMeta(
            step_id=step_id,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            session_id=result.session_id,
            retries_used=retries_used,
            iteration=execution,
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

    def _read_started_at(self, step_dir: Path) -> str | None:
        """Recover started_at from an existing meta.json, if present."""
        meta_path = step_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text()).get("started_at")
        except (json.JSONDecodeError, OSError):
            return None

    # --- Resuming a prior run -------------------------------------------

    @staticmethod
    def runs_root(base_dir: str | Path) -> Path:
        return Path(base_dir) / ".hydra" / "runs"

    @classmethod
    def find_latest_run(cls, base_dir: str | Path) -> str | None:
        """Return the id of the most recent run under base_dir, or None.

        Run ids are timestamp-prefixed (YYYYMMDD-HHMMSS-hex), so lexical sort
        equals chronological sort — the max name is the newest run.
        """
        root = cls.runs_root(base_dir)
        if not root.is_dir():
            return None
        ids = [d.name for d in root.iterdir() if d.is_dir()]
        return max(ids) if ids else None

    @classmethod
    def load_completed_steps(
        cls, base_dir: str | Path, run_id: str
    ) -> dict[str, "ResumedStep"]:
        """Load the successful steps of a prior run for resuming.

        Only steps whose meta.json status is "success" are returned: a step
        left "running" (interrupted mid-flight) or "failure" may have written a
        partial/empty artifact and must be re-executed, not trusted. Loop
        iterations (run-N subdirs) are ignored — resume rehydrates the step's
        first-execution result, and any convergence loop re-runs from scratch.
        """
        run_dir = cls.runs_root(base_dir) / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run '{run_id}' not found under {run_dir}")

        steps_dir = run_dir / "steps"
        completed: dict[str, ResumedStep] = {}
        if not steps_dir.is_dir():
            return completed

        for step_path in sorted(steps_dir.iterdir()):
            if not step_path.is_dir():
                continue
            meta_path = step_path / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("status") != "success":
                continue
            output_file = step_path / "output.md"
            output_text = output_file.read_text() if output_file.exists() else ""
            check_file = step_path / "check.txt"
            check_output = check_file.read_text() if check_file.exists() else None
            completed[step_path.name] = ResumedStep(
                step_id=step_path.name,
                session_id=meta.get("session_id"),
                output_text=output_text,
                check_output=check_output,
            )
        return completed
