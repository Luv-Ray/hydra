"""Dependency inference and the scheduling graph.

Scheduling unit = a *node*: either a plain step, or a whole loop (its body and
fix run inside the loop node). Dependencies are inferred from four sources:

  1. `after: [X]`                     explicit
  2. `resume: X`                      needs X's session
  3. `inject: [path]` (exact path)    the step whose `output` is that path
  4. `when` referencing steps.X       needs X's verdict

Edges are not order-dependent: YAML declaration order has no meaning. A step
depending on a loop member depends on the whole loop (it sees the final round).
Inside a loop, edges between body steps order the body; edges involving the
loop's `fix` step are dropped (fix always runs after the body, then the body
re-runs). Any remaining cycle is an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationError
from .expr import expr_refs, parse_expr
from .models import Pipeline, StepDef


def is_glob(path: str) -> bool:
    return "*" in path or "?" in path


def step_deps(step: StepDef, output_map: dict[str, str]) -> set[str]:
    """Step ids `step` depends on, from all four sources. Never includes itself."""
    deps: set[str] = set(step.after)
    if step.resume:
        deps.add(step.resume)
    for path in step.inject:
        if not is_glob(path) and path in output_map:
            deps.add(output_map[path])
    if step.when:
        deps |= expr_refs(parse_expr(step.when))[1]
    deps.discard(step.id)
    return deps


@dataclass
class Schedule:
    # node id (step id or loop id) → node ids it waits for
    node_deps: dict[str, set[str]]
    # step id → node id it is scheduled under (itself, or its loop's id)
    node_of: dict[str, str]
    # loop id → {body step id → body step ids it waits for (within the body)}
    body_deps: dict[str, dict[str, set[str]]] = field(default_factory=dict)

    def order(self) -> list[str]:
        return topo_order(self.node_deps, "pipeline")

    def ancestors(self) -> dict[str, set[str]]:
        return _closure(self.node_deps)


def build_schedule(pipeline: Pipeline) -> Schedule:
    output_map = {s.output: s.id for s in pipeline.steps if s.output}
    loop_of: dict[str, str] = {}
    for loop in pipeline.loops:
        for sid in loop.body:
            loop_of[sid] = loop.id
        if loop.fix:
            loop_of[loop.fix] = loop.id
    node_of = {s.id: loop_of.get(s.id, s.id) for s in pipeline.steps}

    node_deps: dict[str, set[str]] = {}
    for s in pipeline.steps:
        node_deps.setdefault(node_of[s.id], set())
    body_deps: dict[str, dict[str, set[str]]] = {
        loop.id: {sid: set() for sid in loop.body} for loop in pipeline.loops
    }
    for loop in pipeline.loops:
        node_deps.setdefault(loop.id, set())

    for s in pipeline.steps:
        node = node_of[s.id]
        for dep in step_deps(s, output_map):
            if dep not in node_of:
                continue  # unknown id: reported by validate
            dep_node = node_of[dep]
            if dep_node != node:
                node_deps[node].add(dep_node)
            elif node in body_deps and s.id in body_deps[node] and dep in body_deps[node]:
                body_deps[node][s.id].add(dep)
            # else: an edge to/from the loop's fix — ordering is fixed by the loop

    for loop in pipeline.loops:
        until_steps = expr_refs(parse_expr(loop.until))[1]
        for dep in until_steps:
            if dep in node_of and node_of[dep] != loop.id:
                node_deps[loop.id].add(node_of[dep])

    return Schedule(node_deps=node_deps, node_of=node_of, body_deps=body_deps)


def topo_order(deps: dict[str, set[str]], what: str) -> list[str]:
    """Deterministic topological order (ties broken by insertion order)."""
    order: list[str] = []
    done: set[str] = set()
    pending = list(deps)
    while pending:
        ready = [n for n in pending if deps[n] <= done]
        if not ready:
            raise ValidationError(f"Circular dependency in {what} among: {pending}")
        order.extend(ready)
        done.update(ready)
        pending = [n for n in pending if n not in done]
    return order


def _closure(deps: dict[str, set[str]]) -> dict[str, set[str]]:
    """node → all transitive ancestors."""
    memo: dict[str, set[str]] = {}

    def visit(n: str) -> set[str]:
        if n not in memo:
            memo[n] = set()
            acc: set[str] = set()
            for d in deps.get(n, ()):
                acc.add(d)
                acc |= visit(d)
            memo[n] = acc
        return memo[n]

    for n in deps:
        visit(n)
    return memo
