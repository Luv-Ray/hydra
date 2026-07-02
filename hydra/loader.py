"""YAML loading, defaults merging, and template expansion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationError
from .expr import expand_template
from .models import DefaultsDef, InputDef, Pipeline, StepDef


def load_pipeline(
    path: str | Path,
    vars: dict[str, str] | None = None,
    var_file: str | Path | None = None,
) -> Pipeline:
    """Load a pipeline YAML file, merge defaults, expand templates."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValidationError(f"Pipeline file must be a YAML mapping: {path}")

    # Parse inputs definitions
    input_defs = {
        name: InputDef.from_dict(cfg) if isinstance(cfg, dict) else InputDef()
        for name, cfg in data.get("inputs", {}).items()
    }

    # Resolve input values
    resolved_inputs = _resolve_inputs(input_defs, vars or {}, var_file)

    # Parse defaults
    defaults = DefaultsDef.from_dict(data.get("defaults", {}))

    # Parse steps
    raw_steps = data.get("steps", [])
    if not raw_steps:
        raise ValidationError("Pipeline must have at least one step")

    steps: list[StepDef] = []
    for raw in raw_steps:
        step = StepDef.from_dict(raw)
        step = _apply_defaults(step, defaults)
        step = _expand_step_templates(step, resolved_inputs)
        steps.append(step)

    # Expand workdir template
    workdir = data.get("workdir")
    if workdir:
        workdir = expand_template(workdir, resolved_inputs)

    return Pipeline(
        name=data.get("name", path.stem),
        version=data.get("version", 1),
        steps=steps,
        inputs=input_defs,
        defaults=defaults,
        workdir=workdir,
        mcp_servers=data.get("mcp_servers", {}),
    )


def _resolve_inputs(
    defs: dict[str, InputDef],
    vars: dict[str, str],
    var_file: str | Path | None,
) -> dict[str, str]:
    """Merge var-file + CLI vars, then validate required inputs."""
    resolved: dict[str, str] = {}

    # Load var file first (CLI vars override)
    if var_file:
        vf = Path(var_file)
        if not vf.exists():
            raise ValidationError(f"Var file not found: {var_file}")
        with open(vf) as f:
            file_vars = yaml.safe_load(f) or {}
        if isinstance(file_vars, dict):
            resolved.update({k: str(v) for k, v in file_vars.items()})

    # CLI vars override file vars
    resolved.update(vars)

    # Apply defaults for missing inputs
    for name, idef in defs.items():
        if name not in resolved and idef.default is not None:
            resolved[name] = str(idef.default)

    # Check required inputs
    missing = [
        name for name, idef in defs.items() if idef.required and name not in resolved
    ]
    if missing:
        raise ValidationError(f"Missing required inputs: {', '.join(missing)}")

    return resolved


def _apply_defaults(step: StepDef, defaults: DefaultsDef) -> StepDef:
    """Apply defaults to step fields that weren't explicitly set."""
    if step.model is None:
        step.model = defaults.model
    if step.retry is None:
        step.retry = defaults.retry
    if step.timeout is None:
        step.timeout = defaults.timeout
    if step.max_iterations is None:
        step.max_iterations = defaults.max_iterations
    if step.allowed_tools is None:
        step.allowed_tools = defaults.allowed_tools
    return step


def _expand_step_templates(step: StepDef, inputs: dict[str, str]) -> StepDef:
    """Expand {{ inputs.xxx }} in prompt and output fields."""
    step.prompt = expand_template(step.prompt, inputs)
    if step.output:
        step.output = expand_template(step.output, inputs)
    return step
