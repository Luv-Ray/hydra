"""DAG construction and topological sort from step references."""

from __future__ import annotations

from .models import InjectType, StepDef


def build_dag(steps: list[StepDef]) -> dict[str, list[str]]:
    """Build dependency graph: step_id -> list of dependency step_ids."""
    deps: dict[str, list[str]] = {}
    for step in steps:
        step_deps: list[str] = []
        if step.fork_from:
            step_deps.append(step.fork_from)
        if step.output_from:
            step_deps.append(step.output_from)
        step_deps.extend(step.after)
        for inj in step.inject:
            if inj.type == InjectType.OUTPUT_OF:
                step_deps.append(inj.value)
        deps[step.id] = step_deps
    return deps


def topological_layers(
    steps: list[StepDef], dag: dict[str, list[str]]
) -> list[list[str]]:
    """Return steps grouped into layers that can execute concurrently.

    Each layer contains step IDs whose dependencies are all in prior layers.
    Raises ValueError on circular dependency.
    """
    resolved: set[str] = set()
    pending = [s.id for s in steps]
    layers: list[list[str]] = []

    while pending:
        layer = [sid for sid in pending if all(d in resolved for d in dag.get(sid, []))]
        if not layer:
            raise ValueError(f"Circular dependency detected among: {pending}")
        layers.append(layer)
        resolved.update(layer)
        pending = [sid for sid in pending if sid not in resolved]

    return layers
