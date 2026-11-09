"""DSL v2 data models for Hydra pipeline definitions.

Parsing is strict: every mapping rejects keys it does not know, and field types
are checked here. A typo'd key (`outptu:`) or a wrong type fails at load time
instead of being silently dropped. Cross-references (does `after` name a real
step, is the graph acyclic, ...) are checked later in validate.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError

DSL_VERSION = 2

# Engine-wide default for `retry`: one re-run of an abnormal outcome (errored,
# API error, empty body, unparseable verdict). These are transient by nature, so
# one retry recovers the common blip; a second rarely helps and doubles the
# worst-case cost of an expensive step.
DEFAULT_RETRY = 1

ON_EXHAUSTED = ("fail", "continue")


def _check_keys(d: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ValidationError(f"{where}: expected a mapping, got {type(d).__name__}")
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ValidationError(
            f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}"
        )
    return d


def _req(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ValidationError(f"{where}: missing required key '{key}'")
    return d[key]


def _opt(d: dict[str, Any], key: str, types: type | tuple[type, ...], where: str) -> Any:
    v = d.get(key)
    if v is None:
        return None
    # bool is an int subclass; never accept it where a number is meant.
    if isinstance(v, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        raise ValidationError(f"{where}: '{key}' must not be a boolean")
    if not isinstance(v, types):
        raise ValidationError(f"{where}: '{key}' has wrong type {type(v).__name__}")
    return v


def _str_list(d: dict[str, Any], key: str, where: str) -> list[str]:
    """A list of strings; a bare string is accepted as a one-element list."""
    v = d.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValidationError(f"{where}: '{key}' must be a string or list of strings")
    return list(v)


@dataclass
class InputDef:
    description: str = ""
    required: bool = False
    default: str | None = None

    @classmethod
    def from_dict(cls, d: Any, where: str) -> InputDef:
        if d is None:
            return cls()
        _check_keys(d, {"description", "required", "default"}, where)
        default = d.get("default")
        return cls(
            description=str(d.get("description", "")),
            required=bool(d.get("required", False)),
            default=None if default is None else str(default),
        )


@dataclass
class DefaultsDef:
    model: str | None = None
    retry: int = DEFAULT_RETRY
    timeout: int | None = None  # None = no wall-clock limit
    allowed_tools: list[str] | None = None

    @classmethod
    def from_dict(cls, d: Any) -> DefaultsDef:
        if d is None:
            return cls()
        where = "defaults"
        _check_keys(d, {"model", "retry", "timeout", "allowed_tools"}, where)
        retry = _opt(d, "retry", int, where)
        return cls(
            model=_opt(d, "model", str, where),
            retry=DEFAULT_RETRY if retry is None else retry,
            timeout=_opt(d, "timeout", int, where),
            allowed_tools=_str_list(d, "allowed_tools", where) if "allowed_tools" in d else None,
        )


@dataclass
class VerdictDef:
    """The closed set of values a step's reply must end with.

    values: the allowed verdict tokens (e.g. [ACCEPT, REJECT]).
    prompt: optional follow-up asked in the same session; when set, the verdict
        is read from the follow-up reply instead of the step's own reply.
    """
    values: list[str]
    prompt: str | None = None

    @classmethod
    def from_any(cls, v: Any, where: str) -> VerdictDef:
        where = f"{where}.verdict"
        if isinstance(v, list):
            values, prompt = v, None
        else:
            _check_keys(v, {"values", "prompt"}, where)
            values = _req(v, "values", where)
            prompt = _opt(v, "prompt", str, where)
        if isinstance(values, list):
            for x in values:
                if isinstance(x, bool):
                    raise ValidationError(
                        f"{where}: value {x!r} is a YAML boolean — unquoted yes/no/"
                        f"on/off/true/false parse as booleans; quote it, e.g. 'NO'"
                    )
        if not isinstance(values, list) or not values or not all(
            isinstance(x, str) and x.strip() for x in values
        ):
            raise ValidationError(f"{where}: values must be a non-empty list of strings")
        lowered = [x.strip().lower() for x in values]
        if len(set(lowered)) != len(lowered):
            raise ValidationError(f"{where}: duplicate values (case-insensitive) in {values}")
        return cls(values=[x.strip() for x in values], prompt=prompt)


STEP_KEYS = {
    "id", "prompt", "output", "after", "inject", "resume", "fork",
    "compact_before_resume", "model", "retry", "timeout", "allowed_tools",
    "max_turns", "verdict", "when",
}


@dataclass
class StepDef:
    id: str
    prompt: str
    output: str | None = None
    after: list[str] = field(default_factory=list)
    inject: list[str] = field(default_factory=list)
    resume: str | None = None
    # True → resume into a *copy* of the target session (SDK fork_session). The
    # default appends to the target's session in place, so the next resumer of
    # the same target sees this step's turns too.
    fork: bool = False
    compact_before_resume: int | None = None  # % of window; over it, /compact target first
    model: str | None = None
    retry: int | None = None
    timeout: int | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    verdict: VerdictDef | None = None
    when: str | None = None

    @classmethod
    def from_dict(cls, d: Any, index: int) -> StepDef:
        where = f"steps[{index}]"
        _check_keys(d, STEP_KEYS, where)
        sid = _req(d, "id", where)
        if not isinstance(sid, str):
            raise ValidationError(f"{where}: 'id' must be a string")
        where = f"step '{sid}'"
        prompt = _req(d, "prompt", where)
        if not isinstance(prompt, str):
            raise ValidationError(f"{where}: 'prompt' must be a string")
        fork = d.get("fork", False)
        if not isinstance(fork, bool):
            raise ValidationError(f"{where}: 'fork' must be a boolean")
        return cls(
            id=sid,
            prompt=prompt,
            output=_opt(d, "output", str, where),
            after=_str_list(d, "after", where),
            inject=_str_list(d, "inject", where),
            resume=_opt(d, "resume", str, where),
            fork=fork,
            compact_before_resume=_opt(d, "compact_before_resume", int, where),
            model=_opt(d, "model", str, where),
            retry=_opt(d, "retry", int, where),
            timeout=_opt(d, "timeout", int, where),
            allowed_tools=_str_list(d, "allowed_tools", where) if "allowed_tools" in d else None,
            max_turns=_opt(d, "max_turns", int, where),
            verdict=VerdictDef.from_any(d["verdict"], where) if d.get("verdict") is not None else None,
            when=_opt(d, "when", str, where),
        )


@dataclass
class LoopDef:
    """Re-run `body` until `until` holds, running `fix` between rounds.

    Round N: run body (in dependency order) → evaluate until → done, or (if
    rounds remain) run fix and start round N+1. After max_rounds failed rounds:
    on_exhausted=fail aborts the pipeline; continue lets it proceed with the
    last round's artifacts.
    """
    id: str
    body: list[str]
    until: str
    max_rounds: int
    fix: str | None = None
    on_exhausted: str = "fail"

    @classmethod
    def from_dict(cls, d: Any, index: int) -> LoopDef:
        where = f"loops[{index}]"
        _check_keys(d, {"id", "body", "until", "fix", "max_rounds", "on_exhausted"}, where)
        lid = _req(d, "id", where)
        if not isinstance(lid, str):
            raise ValidationError(f"{where}: 'id' must be a string")
        where = f"loop '{lid}'"
        _req(d, "body", where)
        until = _req(d, "until", where)
        if not isinstance(until, str):
            raise ValidationError(f"{where}: 'until' must be a string expression")
        max_rounds = _req(d, "max_rounds", where)
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValidationError(f"{where}: 'max_rounds' must be an integer >= 1")
        on_exhausted = d.get("on_exhausted", "fail")
        if on_exhausted not in ON_EXHAUSTED:
            raise ValidationError(
                f"{where}: 'on_exhausted' must be one of {list(ON_EXHAUSTED)}, got {on_exhausted!r}"
            )
        return cls(
            id=lid,
            body=_str_list(d, "body", where),
            until=until,
            max_rounds=max_rounds,
            fix=_opt(d, "fix", str, where),
            on_exhausted=on_exhausted,
        )


PIPELINE_KEYS = {
    "name", "version", "inputs", "defaults", "workdir", "mcp_servers",
    "system_prompt", "user_preamble", "steps", "loops",
}


@dataclass
class Pipeline:
    name: str
    steps: list[StepDef]
    loops: list[LoopDef] = field(default_factory=list)
    inputs: dict[str, InputDef] = field(default_factory=dict)
    # Resolved input values (CLI --var > --var-file > default). The single
    # source the engine reads, for templates and `when`/`until` alike.
    input_values: dict[str, str] = field(default_factory=dict)
    defaults: DefaultsDef = field(default_factory=DefaultsDef)
    workdir: str | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # system_prompt is appended to the engine's built-in system prompt for every
    # step; user_preamble is prepended to every step's user prompt.
    system_prompt: str | None = None
    user_preamble: str | None = None
