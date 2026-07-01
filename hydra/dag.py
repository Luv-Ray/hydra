"""DAG construction and topological sort for Hydra pipelines."""

from __future__ import annotations

from .models import Pipeline, StepDef


def build_dag(pipeline: Pipeline) -> dict[str, list[str]]:
    """Build dependency graph: step_id -> list of dependency step_ids.

    Dependency sources (union):
    1. resume: X → depends on X
    2. after: [X, Y] → explicit dependencies
    3. inject precise file matching a step's output → auto dependency
       (only if producer appears before consumer in YAML order,
        to avoid creating cycles in goto-backjump scenarios)
    """
    output_map = {s.output: s.id for s in pipeline.steps if s.output}
    step_order = {s.id: i for i, s in enumerate(pipeline.steps)}
    deps: dict[str, list[str]] = {}

    for step in pipeline.steps:
        step_deps: list[str] = []

        if step.resume:
            step_deps.append(step.resume)

        step_deps.extend(step.after)

        for path in step.inject:
            if "*" in path or "?" in path:
                continue
            if path in output_map and output_map[path] != step.id:
                dep_id = output_map[path]
                # Only add if producer is declared before consumer (avoids back-edges)
                if step_order[dep_id] < step_order[step.id] and dep_id not in step_deps:
                    step_deps.append(dep_id)

        deps[step.id] = step_deps

    return deps


def topological_layers(
    pipeline: Pipeline, dag: dict[str, list[str]]
) -> list[list[str]]:
    """Return steps grouped into layers that can execute concurrently.

    Only includes steps with trigger=dag. Steps with trigger=route are
    excluded from DAG scheduling (reached only via goto).
    Raises ValueError on circular dependency.
    """
    dag_steps = [s.id for s in pipeline.steps if s.trigger == "dag"]
    resolved: set[str] = set()
    pending = list(dag_steps)
    layers: list[list[str]] = []

    while pending:
        layer = [
            sid
            for sid in pending
            if all(d in resolved or d not in set(dag_steps) for d in dag.get(sid, []))
        ]
        if not layer:
            raise ValueError(f"Circular dependency detected among: {pending}")
        layers.append(layer)
        resolved.update(layer)
        pending = [sid for sid in pending if sid not in resolved]

    return layers
