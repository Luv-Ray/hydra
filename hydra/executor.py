"""Core execution engine: DAG traversal + Agent SDK calls."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .dag import build_dag, topological_layers
from .inject import build_prompt
from .models import TaskDef, StepDef


@dataclass
class StepResult:
    session_id: str
    output_text: str


DEFAULT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


async def execute_step(
    step: StepDef,
    results: dict[str, StepResult],
    task: TaskDef,
) -> StepResult:
    """Execute a single step using the Claude Agent SDK."""
    options = ClaudeAgentOptions(
        allowed_tools=step.allowed_tools or DEFAULT_TOOLS,
    )

    # MCP servers
    if task.mcp_servers:
        options.mcp_servers = {
            name: {"command": srv.command, "args": srv.args, "env": srv.env}
            for name, srv in task.mcp_servers.items()
        }

    # Model override
    if step.model:
        options.model = step.model

    # Max turns
    if step.max_turns:
        options.max_turns = step.max_turns

    # Working directory
    if task.workdir:
        options.cwd = str(Path(task.workdir).resolve())

    # fork_from mode: fork the parent session
    if step.fork_from:
        parent = results[step.fork_from]
        options.resume = parent.session_id
        options.fork_session = True

    # Build prompt (handles context + inject + step.prompt)
    prompt = build_prompt(step, results, workdir=task.workdir)

    # Execute
    session_id: str | None = None
    result_text: str | None = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            session_id = msg.session_id
            if msg.subtype == "success":
                result_text = msg.result

    if session_id is None:
        raise RuntimeError(f"Step '{step.id}' did not return a session ID")
    if result_text is None:
        result_text = ""

    # Write output file if specified
    if step.output:
        output_path = step.output
        if task.workdir and not os.path.isabs(output_path):
            output_path = str(Path(task.workdir) / output_path)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(result_text)

    return StepResult(session_id=session_id, output_text=result_text)


async def execute_dag(task: TaskDef) -> dict[str, StepResult]:
    """Execute all steps in topological order, parallelizing where possible."""
    dag = build_dag(task.steps)
    layers = topological_layers(task.steps, dag)
    step_map: dict[str, StepDef] = {s.id: s for s in task.steps}
    results: dict[str, StepResult] = {}

    for layer in layers:
        if len(layer) == 1:
            step = step_map[layer[0]]
            results[layer[0]] = await execute_step(step, results, task)
        else:
            coros = [execute_step(step_map[sid], results, task) for sid in layer]
            layer_results = await asyncio.gather(*coros)
            for sid, result in zip(layer, layer_results):
                results[sid] = result

    return results
