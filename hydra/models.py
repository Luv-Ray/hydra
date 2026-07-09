"""DSL v1 data models for Hydra pipeline definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InputDef:
    description: str = ""
    required: bool = False
    default: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InputDef:
        return cls(
            description=d.get("description", ""),
            required=d.get("required", False),
            default=d.get("default"),
        )


@dataclass
class DefaultsDef:
    model: str | None = None
    retry: int = 0
    timeout: int | None = None  # None = no wall-clock limit; set to opt in
    max_iterations: int = 10
    allowed_tools: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DefaultsDef:
        return cls(
            model=d.get("model"),
            retry=d.get("retry", 0),
            timeout=d.get("timeout"),
            max_iterations=d.get("max_iterations", 10),
            allowed_tools=d.get("allowed_tools"),
        )


@dataclass
class CheckRouteDef:
    match: str
    goto: str | list[str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckRouteDef:
        return cls(match=d["match"], goto=d["goto"])


@dataclass
class CheckDef:
    prompt: str | None = None
    success_pattern: str | None = None
    routes: list[CheckRouteDef] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckDef:
        routes = None
        if "routes" in d:
            routes = [CheckRouteDef.from_dict(r) for r in d["routes"]]
        return cls(
            prompt=d.get("prompt"),
            success_pattern=d.get("success_pattern"),
            routes=routes,
        )


@dataclass
class RouteDef:
    condition: str  # "success" | "failure"
    goto: str | list[str]  # step_id, [step_ids], "$end", "$fail"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RouteDef:
        return cls(condition=d["if"], goto=d["goto"])


@dataclass
class StepDef:
    id: str
    prompt: str
    output: str | None = None
    after: list[str] = field(default_factory=list)
    inject: list[str] = field(default_factory=list)
    resume: str | None = None
    compact_before_resume: int | None = None  # % of context window; over this, /compact the resume target first
    model: str | None = None
    retry: int | None = None
    retry_inject: str | None = None  # "last_output" | "check_output"
    timeout: int | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    max_iterations: int | None = None
    # Explicit loop body: the DAG steps to re-run on this step's back-jump.
    # When set, it replaces the engine's reverse-derivation (_find_subgraph),
    # making the loop body author-declared and validate-checkable rather than
    # inferred. Omit to fall back to derivation. See _resolve_subgraph.
    loop_body: list[str] | None = None
    routes: list[RouteDef] | None = None
    check: CheckDef | None = None
    when: str | None = None
    trigger: str = "dag"  # "dag" | "route"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StepDef:
        after = d.get("after", [])
        if isinstance(after, str):
            after = [after]

        inject = d.get("inject", [])
        if isinstance(inject, str):
            inject = [inject]

        loop_body = d.get("loop_body")
        if isinstance(loop_body, str):
            loop_body = [loop_body]

        routes = None
        if "routes" in d:
            routes = [RouteDef.from_dict(r) for r in d["routes"]]

        check = None
        if "check" in d:
            check = CheckDef.from_dict(d["check"])

        return cls(
            id=d["id"],
            prompt=d["prompt"],
            output=d.get("output"),
            after=after,
            inject=inject,
            resume=d.get("resume"),
            compact_before_resume=d.get("compact_before_resume"),
            model=d.get("model"),
            retry=d.get("retry"),
            retry_inject=d.get("retry_inject"),
            timeout=d.get("timeout"),
            allowed_tools=d.get("allowed_tools"),
            max_turns=d.get("max_turns"),
            max_iterations=d.get("max_iterations"),
            loop_body=loop_body,
            routes=routes,
            check=check,
            when=d.get("when"),
            trigger=d.get("trigger", "dag"),
        )


@dataclass
class Pipeline:
    name: str
    version: int
    steps: list[StepDef]
    inputs: dict[str, InputDef] = field(default_factory=dict)
    defaults: DefaultsDef = field(default_factory=DefaultsDef)
    workdir: str | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # Applied to every step. system_prompt is appended to the engine's built-in
    # automation system prompt (role/discipline that spans the whole run, sent
    # out-of-band from the step instruction). user_preamble is prepended to
    # each step's user prompt (operational context repeated per step). See loader
    # / executor / provider for how each threads through.
    system_prompt: str | None = None
    user_preamble: str | None = None
