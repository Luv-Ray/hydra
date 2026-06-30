"""Assemble inject content into a prompt prefix."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .executor import StepResult
    from .models import InjectDef, StepDef


def build_prompt(
    step: "StepDef",
    results: dict[str, "StepResult"],
    workdir: str | None = None,
) -> str:
    """Build the full prompt for a step: inject prefix + context prefix + step prompt."""
    parts: list[str] = []

    # output_from mode: inject parent's output as context
    if step.output_from:
        parent = results[step.output_from]
        parts.append(f"[Output of step: {step.output_from}]\n{parent.output_text}")

    # inject content
    for inj in step.inject:
        from .models import InjectType

        if inj.type == InjectType.OUTPUT_OF:
            ref = results[inj.value]
            parts.append(f"[Output of step: {inj.value}]\n{ref.output_text}")
        elif inj.type == InjectType.FILE:
            file_path = inj.value
            if workdir and not os.path.isabs(file_path):
                file_path = str(Path(workdir) / file_path)
            content = Path(file_path).read_text()
            parts.append(f"[File: {inj.value}]\n{content}")

    if parts:
        prefix = "\n\n---\n\n".join(parts)
        return f"{prefix}\n\n---\n\n{step.prompt}"
    return step.prompt
