"""Static validation for Hydra pipeline definitions (cross-references, graph).

Key/type errors are caught earlier by the strict parsers in models.py; this
module checks what needs the whole pipeline in view. Hard errors raise
ValidationError; soft issues are returned as warnings.
"""

from __future__ import annotations

import re

from .dag import Schedule, _closure, build_schedule, is_glob, topo_order
from .errors import TemplateError, ValidationError
from .expr import expr_refs, parse_expr, verdict_literals
from .models import Pipeline, StepDef

# Step/loop ids must be identifiers so `steps.ID.verdict` parses as an expression.
_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_pipeline(pipeline: Pipeline) -> list[str]:
    """Run all checks. Raises ValidationError on hard errors; returns warnings."""
    warnings: list[str] = []
    steps = {s.id: s for s in pipeline.steps}

    _check_ids(pipeline)
    for s in pipeline.steps:
        _check_step(s, steps, pipeline)
    _check_outputs_unique(pipeline.steps)
    loop_of = _check_loops(pipeline, steps)

    schedule = build_schedule(pipeline)
    schedule.order()  # raises on a cycle between nodes
    for loop_id, deps in schedule.body_deps.items():
        topo_order(deps, f"loop '{loop_id}' body")
    _check_concurrent_resume(pipeline, schedule, loop_of)

    _warn_unused_verdicts(pipeline, warnings)
    _warn_globs_without_after(pipeline.steps, warnings)
    return warnings


def _check_ids(pipeline: Pipeline) -> None:
    seen: set[str] = set()
    for kind, ids in (("step", [s.id for s in pipeline.steps]),
                      ("loop", [lp.id for lp in pipeline.loops])):
        for i in ids:
            if not _ID_RE.match(i):
                raise ValidationError(
                    f"Invalid {kind} id '{i}': use letters, digits and underscores "
                    f"(it must be referable as steps.{i}.verdict)"
                )
            if i in seen:
                raise ValidationError(f"Duplicate id: '{i}' (step and loop ids share one namespace)")
            seen.add(i)


def _check_expr(
    expr: str, where: str, pipeline: Pipeline, steps: dict[str, StepDef]
) -> set[str]:
    """Parse an expression and check every reference. Returns referenced step ids."""
    try:
        node = parse_expr(expr)
    except TemplateError as e:
        raise ValidationError(f"{where}: {e}") from e
    input_refs, step_refs = expr_refs(node)
    for name in sorted(input_refs - set(pipeline.inputs)):
        raise ValidationError(f"{where}: inputs.{name} is not a declared input")
    for sid in sorted(step_refs):
        if sid not in steps:
            raise ValidationError(f"{where}: steps.{sid} is not a step")
        if steps[sid].verdict is None:
            raise ValidationError(f"{where}: steps.{sid}.verdict — step '{sid}' declares no verdict")
    for sid, lit in verdict_literals(node):
        allowed = steps[sid].verdict.values  # type: ignore[union-attr]
        if lit is not None and str(lit) not in allowed:
            raise ValidationError(
                f"{where}: compares steps.{sid}.verdict to {lit!r}, which is not one "
                f"of its values {allowed}"
            )
    return step_refs


def _check_step(s: StepDef, steps: dict[str, StepDef], pipeline: Pipeline) -> None:
    where = f"step '{s.id}'"
    for dep in s.after:
        if dep not in steps:
            raise ValidationError(f"{where}: after references unknown step '{dep}'")
    if s.resume is not None:
        if s.resume not in steps:
            raise ValidationError(f"{where}: resume references unknown step '{s.resume}'")
        if s.resume == s.id:
            raise ValidationError(f"{where}: cannot resume itself")
    else:
        if s.fork:
            raise ValidationError(f"{where}: 'fork' requires 'resume'")
        if s.compact_before_resume is not None:
            raise ValidationError(f"{where}: 'compact_before_resume' requires 'resume'")
    if s.compact_before_resume is not None and not 0 < s.compact_before_resume <= 100:
        raise ValidationError(f"{where}: compact_before_resume must be in 1..100")
    for key in ("timeout", "max_turns"):
        v = getattr(s, key)
        if v is not None and v <= 0:
            raise ValidationError(f"{where}: {key} must be > 0")
    if s.retry is not None and s.retry < 0:
        raise ValidationError(f"{where}: retry must be >= 0")
    if s.when:
        _check_expr(s.when, f"{where} when", pipeline, steps)


def _check_outputs_unique(steps: list[StepDef]) -> None:
    owner: dict[str, str] = {}
    for s in steps:
        if not s.output:
            continue
        if s.output in owner:
            raise ValidationError(
                f"Steps '{owner[s.output]}' and '{s.id}' both write output '{s.output}'"
            )
        owner[s.output] = s.id


def _check_loops(pipeline: Pipeline, steps: dict[str, StepDef]) -> dict[str, str]:
    """Check loop structure. Returns step id → id of the loop it belongs to."""
    loop_of: dict[str, str] = {}
    for loop in pipeline.loops:
        where = f"loop '{loop.id}'"
        if not loop.body:
            raise ValidationError(f"{where}: body must list at least one step")
        if len(set(loop.body)) != len(loop.body):
            raise ValidationError(f"{where}: body lists a step twice")
        if loop.fix in loop.body:
            raise ValidationError(f"{where}: fix step '{loop.fix}' must not also be in body")
        members = list(loop.body) + ([loop.fix] if loop.fix else [])
        for sid in members:
            if sid not in steps:
                raise ValidationError(f"{where}: references unknown step '{sid}'")
            if sid in loop_of:
                raise ValidationError(
                    f"Step '{sid}' belongs to both loop '{loop_of[sid]}' and '{loop.id}'"
                )
            loop_of[sid] = loop.id
        for sid in loop.body:
            if loop.fix and steps[sid].resume == loop.fix:
                raise ValidationError(
                    f"{where}: body step '{sid}' resumes the fix step '{loop.fix}', "
                    f"which has no session in round 1"
                )
        refs = _check_expr(loop.until, f"{where} until", pipeline, steps)
        outside = sorted(refs - set(loop.body))
        if outside:
            raise ValidationError(
                f"{where}: until may only read verdicts of body steps; {outside} "
                f"are not in body"
            )
        if not refs:
            raise ValidationError(
                f"{where}: until must read the verdict of a body step "
                f"(otherwise no round can change its outcome)"
            )
    return loop_of


def _check_concurrent_resume(
    pipeline: Pipeline, schedule: Schedule, loop_of: dict[str, str]
) -> None:
    """Resumers of one target that may run at the same time must all fork.

    A non-fork resume appends to the target's session file in place; two of
    them running concurrently would interleave writes into one transcript.
    Forked resumers each get their own copy, so any number may run in parallel.
    """
    ancestors = schedule.ancestors()
    loops = {lp.id: lp for lp in pipeline.loops}
    body_anc = {lid: _closure(d) for lid, d in schedule.body_deps.items()}

    def may_overlap(a: str, b: str) -> bool:
        na, nb = schedule.node_of[a], schedule.node_of[b]
        if na != nb:
            return na not in ancestors[nb] and nb not in ancestors[na]
        loop = loops.get(na)
        if loop is None or loop.fix in (a, b):
            return False  # fix runs alone, between body rounds
        anc = body_anc[na]
        return a not in anc[b] and b not in anc[a]

    by_target: dict[str, list[StepDef]] = {}
    for s in pipeline.steps:
        if s.resume:
            by_target.setdefault(s.resume, []).append(s)
    for target, resumers in by_target.items():
        for i, a in enumerate(resumers):
            for b in resumers[i + 1:]:
                if (not a.fork or not b.fork) and may_overlap(a.id, b.id):
                    raise ValidationError(
                        f"Steps '{a.id}' and '{b.id}' both resume '{target}' and may run "
                        f"concurrently; without `fork: true` they would write into the "
                        f"same session. Set `fork: true` on both, or order them with `after`."
                    )


def _warn_unused_verdicts(pipeline: Pipeline, warnings: list[str]) -> None:
    read: set[str] = set()
    for s in pipeline.steps:
        if s.when:
            read |= expr_refs(parse_expr(s.when))[1]
    for loop in pipeline.loops:
        read |= expr_refs(parse_expr(loop.until))[1]
    for s in pipeline.steps:
        if s.verdict and s.id not in read:
            warnings.append(
                f"Step '{s.id}' declares a verdict that no when/until reads"
            )


def _warn_globs_without_after(steps: list[StepDef], warnings: list[str]) -> None:
    for s in steps:
        for pattern in s.inject:
            if is_glob(pattern) and not s.after:
                warnings.append(
                    f"Step '{s.id}': injects glob '{pattern}' but has no 'after' — "
                    f"globs don't create dependencies, so matches may not exist yet"
                )
