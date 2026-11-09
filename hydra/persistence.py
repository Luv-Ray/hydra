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

    Carries just enough to satisfy downstream steps without re-executing: the
    produced body, the verdict (for `when`/`until`), and the SDK session id (so
    a later `resume:` can continue the conversation). `execution` is the run-N
    it came from, so a re-run in this run continues the numbering.
    """
    step_id: str
    session_id: str | None
    output_text: str
    verdict: str | None = None
    execution: int = 1


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
    verdict: str | None = None
    # Which execution of the step this is (1 = first; run-N/ for N > 1).
    execution: int = 1
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

        Each loop round (and each `when` re-evaluation inside a loop) gets its
        own subdirectory so earlier rounds are never overwritten. The
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
            execution=execution,
        )
        self._write_json(self.step_dir(step_id, execution) / "meta.json", asdict(meta))

    def save_step_result(
        self, step_id: str, result: StepResult, status: str,
        retries_used: int = 0, execution: int = 1, verdict: str | None = None,
    ) -> None:
        d = self.step_dir(step_id, execution)

        if result.output_text:
            (d / "output.md").write_text(result.output_text)
        if result.verdict_reply:
            (d / "verdict.txt").write_text(result.verdict_reply)

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
            verdict=verdict,
            retries_used=retries_used,
            execution=execution,
        )
        self._write_json(d / "meta.json", asdict(meta))

    def save_step_skipped(self, step_id: str, execution: int = 1) -> None:
        meta = StepMeta(step_id=step_id, status="skipped", execution=execution)
        self._write_json(self.step_dir(step_id, execution) / "meta.json", asdict(meta))

    def save_loop_result(self, loop_id: str, status: str, rounds: int) -> None:
        """Record a finished loop: converged / exhausted / failed.

        A resumed run trusts a loop's members only if the loop itself finished
        (converged or exhausted); a loop interrupted mid-round re-runs whole.
        """
        d = self.run_dir / "loops"
        d.mkdir(exist_ok=True)
        self._write_json(d / f"{loop_id}.json", {"status": status, "rounds": rounds})

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
        """Load each step's newest execution from a prior run, if it succeeded.

        A step's newest execution is its highest run-N (or the base dir). Only a
        "success" there is returned: a step left "running" (interrupted) or
        "failure" may have written a partial artifact and must re-run. Whether a
        loop member may be trusted is decided by the caller via
        load_finished_loops.
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
            newest = cls._newest_execution(step_path)
            if newest is None:
                continue
            d, meta = newest
            if meta.get("status") != "success":
                continue
            output_file = d / "output.md"
            completed[step_path.name] = ResumedStep(
                step_id=step_path.name,
                session_id=meta.get("session_id"),
                output_text=output_file.read_text() if output_file.exists() else "",
                verdict=meta.get("verdict"),
                execution=int(meta.get("execution", 1)),
            )
        return completed

    @classmethod
    def load_finished_loops(cls, base_dir: str | Path, run_id: str) -> set[str]:
        """Ids of loops that finished (converged or exhausted) in a prior run."""
        d = cls.runs_root(base_dir) / run_id / "loops"
        finished: set[str] = set()
        if not d.is_dir():
            return finished
        for f in d.glob("*.json"):
            try:
                if json.loads(f.read_text()).get("status") in ("converged", "exhausted"):
                    finished.add(f.stem)
            except (json.JSONDecodeError, OSError):
                continue
        return finished

    @staticmethod
    def _newest_execution(step_path: Path) -> tuple[Path, dict[str, Any]] | None:
        candidates: list[tuple[int, Path]] = [(1, step_path)]
        for sub in step_path.glob("run-*"):
            try:
                candidates.append((int(sub.name.split("-", 1)[1]), sub))
            except ValueError:
                continue
        for _, d in sorted(candidates, reverse=True):
            try:
                return d, json.loads((d / "meta.json").read_text())
            except (json.JSONDecodeError, OSError):
                continue
        return None
