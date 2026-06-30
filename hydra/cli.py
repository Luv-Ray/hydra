"""CLI entry point for Hydra."""

from __future__ import annotations

import asyncio
import sys
import time

from .executor import execute_dag
from .models import TaskDef


async def run(task_file: str) -> None:
    task = TaskDef.from_yaml(task_file)
    print(f"[hydra] Running task: {task.name}")
    print(f"[hydra] Steps: {[s.id for s in task.steps]}")

    start = time.time()
    results = await execute_dag(task)
    elapsed = time.time() - start

    print(f"\n[hydra] Completed {len(results)} steps in {elapsed:.1f}s")
    for step_id, result in results.items():
        preview = result.output_text[:80].replace("\n", " ")
        print(f"  {step_id}: {preview}...")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: hydra <task.yaml>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
