"""Core execution engine: DAG traversal + routing + retry + goto."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .dag import build_dag, topological_layers
from .errors import ExecutionError
from .events import EventLog
from .expr import eval_when
from .inject import build_prompt, count_inject
from .models import Pipeline, StepDef
from .persistence import RunStore
from .provider import ClaudeProvider, Provider, StepResult, StepRunOptions


def _last_nonempty_line(text: str) -> str:
    """Return the last non-blank line of ``text`` (stripped), or "" if none."""
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


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
        # Total times each step has been executed (for non-overwriting persistence)
        self.execution_counts: dict[str, int] = {}

        workdir = pipeline.workdir
        if workdir and not Path(workdir).is_absolute():
            workdir = str(Path.cwd() / workdir)
        self.workdir = workdir

        self._semaphore = asyncio.Semaphore(concurrency) if concurrency > 0 else None
        self.store: RunStore | None = None
        self.events = EventLog()
        # Serialize compaction per resume-target so parallel steps that resume
        # the same session compact it at most once (see _maybe_compact).
        self._compact_locks: dict[str, asyncio.Lock] = {}

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
        self.events = EventLog(self.store.run_dir)
        # Detailed agent logs (prompts, tool/shell calls, check I/O) go to a file
        # in the run dir — kept out of the live stderr stream, which now carries
        # only control-flow events. See ClaudeProvider.set_trace_file.
        if isinstance(self.provider, ClaudeProvider):
            self.provider.set_trace_file(self.store.run_dir / "agent-trace.log")

        # Build DAG and execute
        dag = build_dag(self.pipeline)
        layers = topological_layers(self.pipeline, dag)

        try:
            for layer in layers:
                active = [sid for sid in layer if sid not in self.skipped]
                if not active:
                    continue
                await self._execute_layer(active)
        except ExecutionError as e:
            self.store.save_result("failure")
            self.events.pipeline_done(status="failure", reason=str(e))
            return "failure"

        self.store.save_result("success")
        self.events.pipeline_done(
            status="success", reason=self._terminal_reason()
        )
        return "success"

    def _terminal_reason(self) -> str:
        """Describe how the pipeline reached its successful end."""
        # The last route-triggered step (e.g. final_report) is the natural end.
        route_done = [
            s.id for s in self.pipeline.steps
            if s.trigger == "route" and s.id in self.results
        ]
        if route_done:
            return f"reached {route_done[-1]}"
        return "all steps completed"

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
        execution = self.execution_counts.get(step.id, 0) + 1
        self.execution_counts[step.id] = execution

        resume_id = step.resume if (step.resume and step.resume in self.results) else None
        self.events.step_start(
            step.id, trigger=step.trigger, execution=execution,
            resume=resume_id, inject_count=count_inject(step.inject, self.workdir),
        )
        if self.store:
            self.store.save_step_start(step.id, execution)

        # Before resuming a heavy session, compact it in place (like /compact in
        # the CLI) so this step continues on a lighter context.
        await self._maybe_compact(step)

        max_retries = step.retry or 0
        last_result: StepResult | None = None

        for attempt in range(max_retries + 1):
            prompt = self._build_step_prompt(step, last_result, attempt)
            options = self._build_run_options(step, attempt)

            result = await self.provider.run(prompt, options)
            self.events.step_result(
                step.id, execution=execution,
                provider_status="error" if result.is_error else "ok",
                turns=result.num_turns, duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
            )

            # Write output file if specified
            if step.output and result.output_text:
                self._write_output(step.output, result.output_text)

            # Run check if defined
            status = "success"
            if step.check and not result.is_error:
                if step.check.prompt:
                    check_output = await self.provider.check(
                        result.session_id, step.check.prompt
                    )
                else:
                    check_output = result.output_text
                result.check_output = check_output
                status = await self._evaluate_check(step, check_output)

            if result.is_error:
                status = "failure"

            if status in ("success", "routed"):
                self.results[step.id] = result
                if self.store:
                    self.store.save_step_result(
                        step.id, result, "success", attempt, execution
                    )
                # "routed" means check.routes already dispatched the goto.
                if status == "success":
                    await self._handle_routes(step, "success")
                return

            # Failed — retry or give up
            last_result = result
            if attempt < max_retries:
                self.events.retry(step.id, attempt=attempt + 1, max_retries=max_retries)
                continue

        # All retries exhausted
        self.results[step.id] = last_result  # type: ignore[assignment]
        if self.store and last_result:
            self.store.save_step_result(
                step.id, last_result, "failure", max_retries, execution
            )
        await self._handle_routes(step, "failure")

    async def _maybe_compact(self, step: StepDef) -> None:
        """Compact the resume target in place when it exceeds the step's threshold.

        Gate is free: we read the target's context percentage from usage already
        captured on its StepResult — no extra API call. Only when it's over
        compact_before_resume do we pay for an actual /compact.

        Parallel-safe: several steps may resume the same session concurrently
        (e.g. three reviewers resuming `analyze`). A per-target lock ensures the
        first compacts once and rewrites the cached size; the others re-read the
        now-small size inside the lock and skip.
        """
        threshold = step.compact_before_resume
        if not threshold or not step.resume:
            return
        target = self.results.get(step.resume)
        if target is None:
            return

        lock = self._compact_locks.setdefault(step.resume, asyncio.Lock())
        async with lock:
            target = self.results[step.resume]  # re-read under lock
            pct = target.context_pct()
            if pct is None or pct < threshold:
                return

            before_tokens = target.input_tokens
            after_tokens, window = await self.provider.compact(target.session_id)
            # Rewrite the cached size so sibling steps (and later resumes) see the
            # compacted transcript and don't compact again.
            if after_tokens:
                target.input_tokens = after_tokens
            if window:
                target.context_window = window
            after_pct = target.context_pct()
            self.events.compact(
                step.id, target=step.resume, before_pct=pct, threshold=threshold,
                before_tokens=before_tokens, after_tokens=target.input_tokens,
                after_pct=after_pct,
            )

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
            output_path=step.output,
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
            # Match against the final verdict line, not the whole document. A
            # verdict agent often narrates or self-corrects before concluding
            # (e.g. writes "ACCEPT" then appends a "REJECT" correction below),
            # so a whole-document search would match superseded draft text. The
            # last non-empty line is the actual conclusion.
            last_line = _last_nonempty_line(check_output)
            matched = bool(re.search(check.success_pattern, last_line, re.IGNORECASE))
            verdict = "success" if matched else "failure"
            self.events.check(
                step.id, mode="success_pattern",
                pattern=check.success_pattern, matched=matched, verdict=verdict,
            )
            return verdict

        if check.routes:
            for cr in check.routes:
                if re.search(cr.match, check_output, re.IGNORECASE):
                    self.events.check(
                        step.id, mode="routes", pattern=cr.match,
                        matched=True, verdict="routed", target=cr.goto,
                    )
                    await self._goto(step, cr.goto)
                    return "routed"
            self.events.check(
                step.id, mode="routes", pattern=None,
                matched=False, verdict="failure",
            )

        return "failure"

    async def _handle_routes(self, step: StepDef, status: str) -> None:
        """Process step-level routes based on status."""
        if not step.routes:
            if status == "failure":
                raise ExecutionError(
                    f"Step '{step.id}' failed and has no failure route"
                )
            return

        for route in step.routes:
            if route.condition == status:
                await self._goto(step, route.goto, condition=status)
                return

        # No matching route — if failure, raise
        if status == "failure":
            raise ExecutionError(
                f"Step '{step.id}' failed and no route matches condition 'failure'"
            )

    async def _goto(
        self, origin_step: StepDef, target: str | list[str],
        condition: str = "check",
    ) -> None:
        """Handle goto: execute target steps (may be back-jump)."""
        if target == "$end":
            self.events.route(
                origin_step.id, condition=condition, target="$end", kind="end",
            )
            return
        if target == "$fail":
            self.events.route(
                origin_step.id, condition=condition, target="$fail", kind="fail",
            )
            raise ExecutionError(f"Step '{origin_step.id}' routed to $fail")

        targets = target if isinstance(target, list) else [target]

        # Check if this is a back-jump (target already executed)
        is_back_jump = any(t in self.results for t in targets)
        if is_back_jump:
            # Increment iteration counter
            count = self.iteration_counts.get(origin_step.id, 0) + 1
            self.iteration_counts[origin_step.id] = count
            max_iter = origin_step.max_iterations or 10

            subgraph = self._find_subgraph(targets, origin_step.id)
            self.events.back_jump(
                origin_step.id, targets=targets, subgraph=subgraph,
                iteration=count, max_iterations=max_iter,
            )

            if count >= max_iter:
                raise ExecutionError(
                    f"Step '{origin_step.id}' exceeded max_iterations "
                    f"({max_iter}) — loop did not converge"
                )

            # Re-execute the subgraph from targets to origin
            await self._execute_back_jump(origin_step, targets, subgraph)
        else:
            self.events.route(
                origin_step.id, condition=condition, target=targets, kind="forward",
            )
            # Forward goto — execute targets that haven't run yet
            await self._execute_forward_goto(targets)

    async def _execute_back_jump(
        self, origin: StepDef, targets: list[str], subgraph: list[str]
    ) -> None:
        """Re-execute subgraph from targets through origin.

        The subgraph includes all DAG steps between targets and origin
        (exclusive of origin). After re-execution, if the subgraph's last step
        routed elsewhere (e.g. forward to origin or a different target), origin
        may already have been re-executed. Otherwise, re-execute origin.
        """
        # Clear results for subgraph steps AND origin
        for sid in subgraph:
            self.results.pop(sid, None)
        self.results.pop(origin.id, None)

        # Re-execute subgraph in topological order
        dag = build_dag(self.pipeline)
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

        # Re-execute origin only when it is a normal DAG step that naturally
        # follows the subgraph (e.g. a step whose own check/routes drive the
        # loop). When origin is trigger:route, it was reached via an explicit
        # goto and the subgraph's last step (which carries the check/routes)
        # already decided the next action — re-running origin would loop forever.
        if origin.trigger != "route" and origin.id not in self.results:
            await self._execute_step(origin.id)

    async def _execute_forward_goto(self, targets: list[str]) -> None:
        """Execute forward-goto targets (trigger:route steps)."""
        for target in targets:
            if target not in self.results and target not in self.skipped:
                await self._execute_step(target)

    def _find_subgraph(self, targets: list[str], origin_id: str) -> list[str]:
        """Find all DAG steps between targets and origin (topological order).

        Excludes trigger:route steps — they are only reached via explicit goto,
        not as part of subgraph re-execution.
        """
        dag = build_dag(self.pipeline)
        route_steps = {s.id for s in self.pipeline.steps if s.trigger == "route"}

        # Build reverse DAG (step → its dependents)
        reverse: dict[str, list[str]] = {s.id: [] for s in self.pipeline.steps}
        for sid, deps in dag.items():
            for dep in deps:
                reverse.setdefault(dep, []).append(sid)

        # Forward walk from targets through dependents, stop at origin
        visited: set[str] = set()
        queue = list(targets)
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            if current == origin_id:
                continue
            visited.add(current)
            for dependent in reverse.get(current, []):
                if dependent == origin_id or dependent in visited:
                    continue
                if dependent in route_steps:
                    continue
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
