"""DSL data models for Hydra task definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

import yaml


class InjectType(Enum):
    OUTPUT_OF = "output_of"
    FILE = "file"


@dataclass
class InjectDef:
    type: InjectType
    value: str

    @classmethod
    def from_dict(cls, d: dict) -> "InjectDef":
        if "output_of" in d:
            return cls(type=InjectType.OUTPUT_OF, value=d["output_of"])
        elif "file" in d:
            return cls(type=InjectType.FILE, value=d["file"])
        raise ValueError(f"Unknown inject type: {d}")


@dataclass
class StepDef:
    id: str
    prompt: str
    fork_from: str | None = None
    output_from: str | None = None
    after: list[str] = field(default_factory=list)
    inject: list[InjectDef] = field(default_factory=list)
    compact: bool = False
    output: str | None = None
    model: str | None = None
    max_turns: int | None = None
    allowed_tools: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "StepDef":
        injects = [InjectDef.from_dict(i) for i in d.get("inject", [])]
        return cls(
            id=d["id"],
            prompt=d["prompt"],
            fork_from=d.get("fork_from"),
            output_from=d.get("output_from"),
            after=(
                d.get("after", [])
                if isinstance(d.get("after"), list)
                else [d["after"]] if d.get("after") else []
            ),
            inject=injects,
            compact=d.get("compact", False),
            output=d.get("output"),
            model=d.get("model"),
            max_turns=d.get("max_turns"),
            allowed_tools=d.get("allowed_tools"),
        )


@dataclass
class McpServerDef:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "McpServerDef":
        return cls(
            command=d["command"],
            args=d.get("args", []),
            env=d.get("env", {}),
        )


@dataclass
class TaskDef:
    name: str
    steps: list[StepDef]
    workdir: str | None = None
    mcp_servers: dict[str, McpServerDef] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "TaskDef":
        with open(path) as f:
            data = yaml.safe_load(f)
        steps = [StepDef.from_dict(s) for s in data["steps"]]
        mcp = {
            name: McpServerDef.from_dict(cfg)
            for name, cfg in data.get("mcp_servers", {}).items()
        }
        return cls(
            name=data["name"],
            steps=steps,
            workdir=data.get("workdir"),
            mcp_servers=mcp,
        )
