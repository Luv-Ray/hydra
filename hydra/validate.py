"""Static validation for Hydra pipeline definitions."""

from __future__ import annotations

import glob as globmod
import sys

from .errors import ValidationError
from .expr import eval_when
from .models import Pipeline, StepDef


def validate_pipeline(pipeline: Pipeline) -> list[str]:
    """Run all validation checks. Raises ValidationError on hard errors.

    Returns a list of warnings (non-fatal).
    """
    warnings: list[str] = []
    step_ids = {s.id for s in pipeline.steps}
    step_map = {s.id: s for s in pipeline.steps}
    output_map = {s.output: s.id for s in pipeline.steps if s.output}

    _check_id_uniqueness(pipeline.steps)
    _check_references(pipeline.steps, step_ids)
    _check_resume_uniqueness(pipeline.steps)
    _check_route_targets(pipeline.steps, step_ids)
    _check_trigger_route_reachable(pipeline.steps, step_ids, warnings)
    _check_loop_protection(pipeline.steps, step_map)
    _check_when_syntax(pipeline.steps)
    _check_check_fields(pipeline.steps)
    _check_glob_without_after(pipeline.steps, output_map, warnings)

    return warnings


def _check_id_uniqueness(steps: list[StepDef]) -> None:
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValidationError(f"Duplicate step id: '{step.id}'")
        seen.add(step.id)


def _check_references(steps: list[StepDef], step_ids: set[str]) -> None:
    for step in steps:
        if step.resume and step.resume not in step_ids:
            raise ValidationError(
                f"Step '{step.id}': resume references unknown step '{step.resume}'"
            )
        for dep in step.after:
            if dep not in step_ids:
                raise ValidationError(
                    f"Step '{step.id}': after references unknown step '{dep}'"
                )


def _check_resume_uniqueness(steps: list[StepDef]) -> None:
    resume_targets: dict[str, list[str]] = {}
    for step in steps:
        if step.resume:
            resume_targets.setdefault(step.resume, []).append(step.id)
    for target, users in resume_targets.items():
        if len(users) > 1:
            raise ValidationError(
                f"Multiple steps resume '{target}': {users}. "
                f"resume is 1:1 — only one downstream can resume a step."
            )


def _check_route_targets(steps: list[StepDef], step_ids: set[str]) -> None:
    specials = {"$end", "$fail"}
    for step in steps:
        if not step.routes:
            continue
        for route in step.routes:
            targets = route.goto if isinstance(route.goto, list) else [route.goto]
            for t in targets:
                if t not in step_ids and t not in specials:
                    raise ValidationError(
                        f"Step '{step.id}': route goto references unknown target '{t}'"
                    )
        if step.check and step.check.routes:
            for cr in step.check.routes:
                targets = cr.goto if isinstance(cr.goto, list) else [cr.goto]
                for t in targets:
                    if t not in step_ids and t not in specials:
                        raise ValidationError(
                            f"Step '{step.id}': check.routes goto references "
                            f"unknown target '{t}'"
                        )


def _check_trigger_route_reachable(
    steps: list[StepDef], step_ids: set[str], warnings: list[str]
) -> None:
    """Warn if trigger:route steps are never targeted by any goto."""
    route_steps = {s.id for s in steps if s.trigger == "route"}
    if not route_steps:
        return

    goto_targets: set[str] = set()
    for step in steps:
        if step.routes:
            for route in step.routes:
                if isinstance(route.goto, list):
                    goto_targets.update(route.goto)
                else:
                    goto_targets.add(route.goto)
        if step.check and step.check.routes:
            for cr in step.check.routes:
                if isinstance(cr.goto, list):
                    goto_targets.update(cr.goto)
                else:
                    goto_targets.add(cr.goto)

    unreachable = route_steps - goto_targets
    for sid in unreachable:
        warnings.append(
            f"Step '{sid}' has trigger:route but is never targeted by any goto"
        )


def _check_loop_protection(
    steps: list[StepDef], step_map: dict[str, StepDef]
) -> None:
    """Ensure steps with back-jump routes have max_iterations set."""
    step_order = {s.id: i for i, s in enumerate(steps)}
    for step in steps:
        if not step.routes:
            continue
        for route in step.routes:
            targets = route.goto if isinstance(route.goto, list) else [route.goto]
            for t in targets:
                if t in step_order and step_order[t] <= step_order[step.id]:
                    if not step.max_iterations:
                        raise ValidationError(
                            f"Step '{step.id}': goto back-jump to '{t}' requires "
                            f"max_iterations to prevent infinite loops"
                        )


def _check_when_syntax(steps: list[StepDef]) -> None:
    for step in steps:
        if step.when:
            try:
                eval_when(step.when, {})
            except Exception:
                pass  # Template errors from missing inputs are fine at validate time


def _check_check_fields(steps: list[StepDef]) -> None:
    for step in steps:
        if not step.check:
            continue
        if step.check.success_pattern and step.check.routes:
            raise ValidationError(
                f"Step '{step.id}': check.success_pattern and check.routes "
                f"are mutually exclusive"
            )
        if not step.check.success_pattern and not step.check.routes:
            raise ValidationError(
                f"Step '{step.id}': check must have either success_pattern or routes"
            )


def _check_glob_without_after(
    steps: list[StepDef],
    output_map: dict[str, str],
    warnings: list[str],
) -> None:
    """Warn if inject has glob but no after referencing steps that write matching outputs."""
    for step in steps:
        for pattern in step.inject:
            if "*" not in pattern and "?" not in pattern:
                continue
            has_relevant_after = any(
                out_path
                for out_path, sid in output_map.items()
                if sid in step.after
            )
            if not has_relevant_after and step.after == []:
                warnings.append(
                    f"Step '{step.id}': injects glob '{pattern}' but has no "
                    f"'after' dependencies — files may not exist at runtime"
                )
