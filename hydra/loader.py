"""YAML loading: strict parse, input resolution, defaults merge, templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import TemplateError, ValidationError
from .expr import expand_template
from .models import (
    DSL_VERSION, PIPELINE_KEYS, DefaultsDef, InputDef, LoopDef, Pipeline, StepDef,
    _check_keys,
)


def load_pipeline(
    path: str | Path,
    vars: dict[str, str] | None = None,
    var_file: str | Path | None = None,
) -> Pipeline:
    """Load a pipeline YAML file into a validated-shape Pipeline.

    Cross-reference checks live in validate.validate_pipeline; call it next.
    """
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)
    _check_keys(data, PIPELINE_KEYS, f"pipeline {path.name}")

    version = data.get("version")
    if version != DSL_VERSION:
        raise ValidationError(
            f"Unsupported DSL version {version!r}: this engine only runs "
            f"`version: {DSL_VERSION}` (v1 has been removed)"
        )

    raw_inputs = data.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        raise ValidationError("inputs: expected a mapping")
    input_defs = {
        name: InputDef.from_dict(cfg, f"inputs.{name}") for name, cfg in raw_inputs.items()
    }
    values = _resolve_inputs(input_defs, vars or {}, var_file)
    defaults = DefaultsDef.from_dict(data.get("defaults"))

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValidationError("Pipeline must have a non-empty 'steps' list")
    raw_loops = data.get("loops") or []
    if not isinstance(raw_loops, list):
        raise ValidationError("loops: expected a list")

    try:
        steps = [
            _expand(_apply_defaults(StepDef.from_dict(raw, i), defaults), values)
            for i, raw in enumerate(raw_steps)
        ]
        workdir = data.get("workdir")
        system_prompt = data.get("system_prompt")
        user_preamble = data.get("user_preamble")
        for key, v in (("workdir", workdir), ("system_prompt", system_prompt),
                       ("user_preamble", user_preamble)):
            if v is not None and not isinstance(v, str):
                raise ValidationError(f"'{key}' must be a string")
        workdir = expand_template(workdir, values) if workdir else None
        system_prompt = expand_template(system_prompt, values) if system_prompt else None
        user_preamble = expand_template(user_preamble, values) if user_preamble else None
    except TemplateError as e:
        raise ValidationError(str(e)) from e

    mcp_servers = data.get("mcp_servers") or {}
    if not isinstance(mcp_servers, dict):
        raise ValidationError("mcp_servers: expected a mapping")

    return Pipeline(
        name=str(data.get("name") or path.stem),
        steps=steps,
        loops=[LoopDef.from_dict(raw, i) for i, raw in enumerate(raw_loops)],
        inputs=input_defs,
        input_values=values,
        defaults=defaults,
        workdir=workdir,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
        user_preamble=user_preamble,
    )


def _resolve_inputs(
    defs: dict[str, InputDef], vars: dict[str, str], var_file: str | Path | None
) -> dict[str, str]:
    """Precedence: CLI --var > --var-file > default. Only declared inputs."""
    resolved: dict[str, str] = {}
    if var_file:
        vf = Path(var_file)
        if not vf.exists():
            raise ValidationError(f"Var file not found: {var_file}")
        file_vars: Any = yaml.safe_load(vf.read_text()) or {}
        if not isinstance(file_vars, dict):
            raise ValidationError(f"Var file must be a mapping: {var_file}")
        resolved.update({str(k): str(v) for k, v in file_vars.items()})
    resolved.update(vars)

    undeclared = sorted(set(resolved) - set(defs))
    if undeclared:
        raise ValidationError(
            f"Undeclared input(s) {undeclared}; declare them under 'inputs'"
        )
    for name, idef in defs.items():
        if name not in resolved and idef.default is not None:
            resolved[name] = idef.default
    missing = [n for n, d in defs.items() if d.required and n not in resolved]
    if missing:
        raise ValidationError(f"Missing required inputs: {', '.join(missing)}")
    return resolved


def _apply_defaults(step: StepDef, defaults: DefaultsDef) -> StepDef:
    """Fill fields the step left unset. A set field replaces the default (no merge)."""
    if step.model is None:
        step.model = defaults.model
    if step.retry is None:
        step.retry = defaults.retry
    if step.timeout is None:
        step.timeout = defaults.timeout
    if step.allowed_tools is None:
        step.allowed_tools = defaults.allowed_tools
    return step


def _expand(step: StepDef, values: dict[str, str]) -> StepDef:
    """Expand {{ inputs.x }} in the step's text fields."""
    step.prompt = expand_template(step.prompt, values)
    if step.output:
        step.output = expand_template(step.output, values)
    step.inject = [expand_template(p, values) for p in step.inject]
    if step.verdict and step.verdict.prompt:
        step.verdict.prompt = expand_template(step.verdict.prompt, values)
    return step
