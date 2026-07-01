"""Core execution engine: DAG traversal + routing + retry + goto."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .dag import build_dag, topological_layers
from .errors import ExecutionError
from .expr import eval_when
from .inject import build_prompt
from .models import Pipeline, StepDef
from .persistence import RunStore
from .provider import ClaudeProvider, Provider, StepResult, StepRunOptions


class ExecutionEngine:
    """Stateful engine that handles DAG execution + routing + loops."""

    def __init__(
        self,
        pipeline: Pipeline,
        inputs: dict[str, str],
        provider: Provider | None = None,
        concurrency: int = 0,
        dry_run: bool = False,
        verbose: bool = False,
    ):
        self.pipeline = pipeline
        self.inputs = inputs
        self.provider = provider or ClaudeProvider(verbose=verbose)
        self.concurrency = concurrency
        self.dry_run = dry_run
        self.verbose = verbose

        self.step_map = {s.id: s for s in pipeline.steps}
        self.results: dict[str, StepResult] = {}
        self.skipped: set[str] = set()
        self.iteration_counts: dict[str, int] = {}

        workdir = pipeline.workdir
        if workdir and not Path(workdir).is_absolute():
            workdir = str(Path.cwd() / workdir)
        self.workdir = workdir

        self._semaphore = asyncio.Semaphore(concurrency) if concurrency > 0 else None
        self.store: RunStore | None = None

    async def run(self) -> str:
        """Execute the pipeline. Returns 'success' or 'failure'."""
        # Evaluate when conditions and mark skipped steps
        self._evaluate_when_conditions()

        if self.dry_run:
            self._print_dry_run()
            return "success"

        # Initialize persistence
        base_dir = self.workdir or str(Path.cwd())
        self.store = RunStore(base_dir, self.pipeline.name, self.inputs, self.workdir)

        # Build DAG and execute
        dag = build_dag(self.pipeline)
        layers = topological_layers(self.pipeline, dag)

        try:
            for layer in layers:
                active = [sid for sid in layer if sid not in self.skipped]
                if not active:
                    continue
                await self._execute_layer(active)
        except ExecutionError:
            self.store.save_result("failure")
            return "failure"

        self.store.save_result("success")
        return "success"

    async def _execute_layer(self, step_ids: list[str]) -> None:
        """Execute a layer of steps concurrently."""
        if len(step_ids) == 1:
            await self._execute_step(step_ids[0])
        else:
            coros = [self._execute_step(sid) for sid in step_ids]
            await asyncio.gather(*coros)

    async def _execute_step(self, step_id: str) -> None:
        """Execute a single step with retry support."""
        step = self.step_map[step_id]

        if self._semaphore:
            async with self._semaphore:
                await self._execute_step_inner(step)
        else:
            await self._execute_step_inner(step)

    async def _execute_step_inner(self, step: StepDef) -> None:
        """Core step execution logic."""
        print(f"[hydra] running: {step.id}", file=sys.stderr)
        if self.store:
            self.store.save_step_start(step.id)

        max_retries = step.retry or 0
        last_result: StepResult | None = None

        for attempt in range(max_retries + 1):
            prompt = self._build_step_prompt(step, last_result, attempt)
            options = self._build_run_options(step, attempt)

            result = await self.provider.run(prompt, options)

            # Write output file if specified
            if step.output and result.output_text:
                self._write_output(step.output, result.output_text)

            # Run check if defined
            status = "success"
            if step.check and not result.is_error:
                check_output = await self.provider.check(
                    result.session_id, step.check.prompt
                )
                result.check_output = check_output
                status = await self._evaluate_check(step, check_output)

            if result.is_error:
                status = "failure"

            if status == "success":
                self.results[step.id] = result
                if self.store:
                    self.store.save_step_result(step.id, result, "success", attempt)
                await self._handle_routes(step, "success")
                print(f"[hydra] done: {step.id} (success)", file=sys.stderr)
                return

            # Failed — retry or give up
            last_result = result
            if attempt < max_retries:
                print(
                    f"[hydra] retry {attempt + 1}/{max_retries}: {step.id}",
                    file=sys.stderr,
                )
                continue

        # All retries exhausted
        self.results[step.id] = last_result  # type: ignore[assignment]
        if self.store and last_result:
            self.store.save_step_result(step.id, last_result, "failure", max_retries)
        print(f"[hydra] done: {step.id} (failure)", file=sys.stderr)
        await self._handle_routes(step, "failure")

    def _build_step_prompt(
        self, step: StepDef, last_result: StepResult | None, attempt: int
    ) -> str:
        """Build the full prompt for a step execution attempt."""
        retry_feedback: str | None = None
        if attempt > 0 and last_result and step.retry_inject:
            if step.retry_inject == "last_output":
                feedback_content = last_result.output_text
            elif step.retry_inject == "check_output":
                feedback_content = last_result.check_output or ""
            else:
                feedback_content = ""

            if feedback_content:
                retry_feedback = (
                    f"[Output from previous attempt]\n{feedback_content}\n\n"
                    f"[Retry with the above feedback]"
                )

        # Determine if missing inject files are allowed (goto back-jump scenario)
        allow_missing = step.id in self.iteration_counts

        return build_prompt(
            prompt=step.prompt,
            inject_paths=step.inject,
            workdir=self.workdir,
            retry_feedback=retry_feedback,
        )

    def _build_run_options(self, step: StepDef, attempt: int) -> StepRunOptions:
        """Build provider run options for a step."""
        resume_session_id: str | None = None
        if step.resume and step.resume in self.results:
            resume_session_id = self.results[step.resume].session_id

        return StepRunOptions(
            step_id=step.id,
            model=step.model,
            allowed_tools=step.allowed_tools,
            max_turns=step.max_turns,
            timeout=step.timeout,
            cwd=self.workdir,
            resume_session_id=resume_session_id,
            mcp_servers=self.pipeline.mcp_servers or None,
        )

    async def _evaluate_check(self, step: StepDef, check_output: str) -> str:
        """Evaluate check result. Returns 'success', 'failure', or 'routed'."""
        import re

        check = step.check
        if not check:
            return "success"

        if check.success_pattern:
            if re.search(check.success_pattern, check_output, re.IGNORECASE):
                return "success"
            return "failure"

        if check.routes:
            for cr in check.routes:
                if re.search(cr.match, check_output, re.IGNORECASE):
                    await self._goto(step, cr.goto)
                    return "routed"

        return "failure"

    async def _handle_routes(self, step: StepDef, status: str) -> None:
        """Process step-level routes based on status."""
        if not step.routes:
            if status == "failure":
                raise ExecutionError(f"Step '{step.id}' failed with no failure route")
            return

        for route in step.routes:
            if route.condition == status:
                await self._goto(step, route.goto)
                return

        # No matching route — if failure, raise
        if status == "failure":
            raise ExecutionError(f"Step '{step.id}' failed with no matching route")

    async def _goto(self, origin_step: StepDef, target: str | list[str]) -> None:
        """Handle goto: execute target steps (may be back-jump)."""
        if target == "$end":
            return
        if target == "$fail":
            raise ExecutionError(
                f"Step '{origin_step.id}' routed to $fail"
            )

        targets = target if isinstance(target, list) else [target]

        # Check if this is a back-jump (target already executed)
        is_back_jump = any(t in self.results for t in targets)
        if is_back_jump:
            # Increment iteration counter
            count = self.iteration_counts.get(origin_step.id, 0) + 1
            self.iteration_counts[origin_step.id] = count
            max_iter = origin_step.max_iterations or 10
            if count >= max_iter:
                raise ExecutionError(
                    f"Step '{origin_step.id}' exceeded max_iterations ({max_iter})"
                )

            # Re-execute the subgraph from targets to origin
            await self._execute_back_jump(origin_step, targets)
        else:
            # Forward goto — execute targets that haven't run yet
            await self._execute_forward_goto(targets)

    async def _execute_back_jump(
        self, origin: StepDef, targets: list[str]
    ) -> None:
        """Re-execute subgraph from targets through origin."""
        # Find all steps between targets and origin (inclusive of targets, exclusive of origin)
        subgraph = self._find_subgraph(targets, origin.id)

        # Clear results for subgraph steps
        for sid in subgraph:
            self.results.pop(sid, None)

        # Re-execute in topological order
        dag = build_dag(self.pipeline)
        # Filter dag to subgraph only
        sub_steps = [s for s in self.pipeline.steps if s.id in subgraph]
        resolved = {sid for sid in self.results}
        pending = list(subgraph)

        while pending:
            ready = [
                sid for sid in pending
                if all(d in resolved or d not in subgraph for d in dag.get(sid, []))
            ]
            if not ready:
                raise ExecutionError(f"Stuck in back-jump re-execution: {pending}")
            await self._execute_layer(ready)
            resolved.update(ready)
            pending = [sid for sid in pending if sid not in resolved]

        # Re-execute origin itself
        await self._execute_step(origin.id)

    async def _execute_forward_goto(self, targets: list[str]) -> None:
        """Execute forward-goto targets (trigger:route steps)."""
        for target in targets:
            if target not in self.results and target not in self.skipped:
                await self._execute_step(target)

    def _find_subgraph(self, targets: list[str], origin_id: str) -> list[str]:
        """Find all DAG steps between targets and origin (topological order)."""
        dag = build_dag(self.pipeline)
        # BFS forward from targets, stop at origin
        visited: set[str] = set()
        queue = list(targets)

        # Build reverse DAG (step → its dependents)
        reverse: dict[str, list[str]] = {s.id: [] for s in self.pipeline.steps}
        for sid, deps in dag.items():
            for dep in deps:
                reverse.setdefault(dep, []).append(sid)

        # Forward walk from targets through dependents, stop at origin
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            if current == origin_id:
                continue
            visited.add(current)
            for dependent in reverse.get(current, []):
                if dependent != origin_id and dependent not in visited:
                    queue.append(dependent)

        # Return in pipeline order
        order = {s.id: i for i, s in enumerate(self.pipeline.steps)}
        return sorted(visited, key=lambda x: order.get(x, 0))

    def _write_output(self, output_path: str, content: str) -> None:
        """Write step output to file."""
        if self.workdir and not Path(output_path).is_absolute():
            output_path = str(Path(self.workdir) / output_path)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(content)

    def _evaluate_when_conditions(self) -> None:
        """Evaluate when conditions and mark skipped steps."""
        for step in self.pipeline.steps:
            if step.when:
                if not eval_when(step.when, self.inputs):
                    self.skipped.add(step.id)

    def _print_dry_run(self) -> None:
        """Print DAG and execution plan without running anything."""
        dag = build_dag(self.pipeline)
        layers = topological_layers(self.pipeline, dag)

        print(f"Pipeline: {self.pipeline.name}")
        print(f"Workdir: {self.workdir or '.'}")
        print(f"Inputs: {self.inputs}")
        print(f"Skipped (when=false): {self.skipped or 'none'}")
        print()
        print("Execution layers:")
        for i, layer in enumerate(layers):
            print(f"  Layer {i}: {layer}")
        print()
        print("Route-only steps:", [s.id for s in self.pipeline.steps if s.trigger == "route"])
        print()
        print("Dependencies:")
        for sid, deps in dag.items():
            if deps:
                print(f"  {sid} ← {deps}")
