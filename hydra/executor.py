"""Execution engine: dataflow scheduling of steps and loops.

One mechanism runs everything. A node (a step, or a whole loop) starts as soon
as every node it depends on has finished. A loop node runs its body as the same
kind of graph, round after round, with its fix step in between. There is no
goto: branching is `when` on upstream verdicts, repetition is a declared loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from .dag import build_schedule, is_glob, topo_order
from .errors import ExecutionError
from .events import EventLog
from .expr import eval_expr, parse_expr
from .inject import SEPARATOR, build_prompt, count_inject, verdict_contract
from .models import DEFAULT_RETRY, LoopDef, Pipeline, StepDef
from .persistence import RunStore
from .provider import ClaudeProvider, Provider, StepResult, StepRunOptions


def parse_verdict(text: str, values: list[str]) -> tuple[str | None, str]:
    """Read a verdict from the last non-empty line of `text`.

    Returns (verdict, line): the declared value named on that line, or None if
    the line names none of the values or more than one. Matching is
    case-insensitive on whole tokens (ACCEPT does not match ACCEPTED). Only the
    last line counts, so a reply that drafts ACCEPT and then corrects itself to
    REJECT further down concludes REJECT.
    """
    line = next((ln.strip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")
    hits = [
        v for v in values
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(v)}(?![A-Za-z0-9_])", line, re.IGNORECASE)
    ]
    return (hits[0] if len(hits) == 1 else None), line


def _abnormal_reason(step: StepDef, result: StepResult) -> str | None:
    """Why `result` is not a usable answer, or None if it is.

    Errored (SDK/API error, timeout, broken stream), or empty when the step is
    contracted to produce a body (it declares `output`, or reads its verdict
    from its own reply). Abnormal outcomes are retried within the step's
    `retry` budget and then fail the pipeline; they never stand in for a verdict.
    """
    if result.is_error or result.api_error_status:
        reason = f"errored (subtype={result.subtype}, api_error_status={result.api_error_status})"
        if result.errors:
            reason += ": " + "; ".join(result.errors)
        return reason
    contracted = bool(step.output) or bool(step.verdict and not step.verdict.prompt)
    if contracted and not (result.output_text or "").strip():
        return "empty output (step is contracted to produce a body)"
    return None


class ExecutionEngine:
    def __init__(
        self,
        pipeline: Pipeline,
        provider: Provider | None = None,
        concurrency: int = 0,
        dry_run: bool = False,
        verbose: bool = False,
        resume_run_id: str | None = None,
        resume_last: bool = False,
    ):
        self.pipeline = pipeline
        self.provider = provider or ClaudeProvider(verbose=verbose)
        self.dry_run = dry_run
        # Prior run to continue: its finished steps/loops are seeded, not re-run.
        # resume_last resolves to the newest run (an explicit id wins).
        self.resume_run_id = resume_run_id
        self.resume_last = resume_last

        self.step_map = {s.id: s for s in pipeline.steps}
        self.loop_map = {lp.id: lp for lp in pipeline.loops}
        self.schedule = build_schedule(pipeline)
        self.producer = {s.output: s.id for s in pipeline.steps if s.output}
        self.when_ast = {s.id: parse_expr(s.when) for s in pipeline.steps if s.when}
        self.until_ast = {lp.id: parse_expr(lp.until) for lp in pipeline.loops}

        # Latest successful result / verdict per step. Loop body entries are
        # cleared at the start of each round, so "has a result" always means
        # "produced in the current round" for loop members.
        self.results: dict[str, StepResult] = {}
        self.verdicts: dict[str, str | None] = {}
        self.skipped: set[str] = set()
        self.execution_counts: dict[str, int] = {}  # → run-N persistence dirs
        self.loop_status: dict[str, str] = {}
        self.seeded_nodes: set[str] = set()  # finished in the resumed-from run

        workdir = pipeline.workdir
        if workdir and not Path(workdir).is_absolute():
            workdir = str(Path.cwd() / workdir)
        self.workdir = workdir

        self._semaphore = asyncio.Semaphore(concurrency) if concurrency > 0 else None
        self.store: RunStore | None = None
        self.events = EventLog()
        # Per resume-target lock: steps resuming one session compact it once.
        self._compact_locks: dict[str, asyncio.Lock] = {}

    # --- Top level ---------------------------------------------------------

    async def run(self) -> str:
        """Execute the pipeline. Returns 'success' or 'failure'."""
        if self.dry_run:
            self._print_dry_run()
            return "success"

        base_dir = self.workdir or str(Path.cwd())
        # Resolve --resume-last before RunStore creates this run's (newer) dir.
        if self.resume_run_id is None and self.resume_last:
            self.resume_run_id = RunStore.find_latest_run(base_dir)
            if self.resume_run_id is None:
                self.events.emit("resume_noop", "--resume-last: no prior run found, starting fresh")

        self.store = RunStore(base_dir, self.pipeline.name, self.pipeline.input_values, self.workdir)
        self.events = EventLog(self.store.run_dir)
        if isinstance(self.provider, ClaudeProvider):
            self.provider.set_trace_file(self.store.run_dir / "agent-trace.log")

        try:
            if self.resume_run_id:
                self._seed_from_prior_run(base_dir)
            await self._run_graph(
                {n: d for n, d in self.schedule.node_deps.items()},
                self._run_node,
                skip=self.seeded_nodes,
            )
        except ExecutionError as e:
            self.store.save_result("failure")
            self.events.pipeline_done(status="failure", reason=str(e))
            return "failure"

        self.store.save_result("success")
        self.events.pipeline_done(status="success", reason=self._terminal_reason())
        return "success"

    async def _run_graph(
        self,
        deps: dict[str, set[str]],
        run_one: Callable[[str], Awaitable[None]],
        skip: set[str] = frozenset(),  # type: ignore[assignment]
    ) -> None:
        """Run every node as soon as its dependencies are done.

        Dataflow rather than layer-by-layer: a slow node never holds back an
        unrelated one. The first failure cancels everything still running and
        propagates.
        """
        done: set[str] = set(skip) & set(deps)
        pending = [n for n in deps if n not in done]
        running: dict[asyncio.Task[None], str] = {}
        try:
            while pending or running:
                for n in [n for n in pending if deps[n] <= done]:
                    pending.remove(n)
                    running[asyncio.create_task(run_one(n))] = n
                if not running:
                    raise ExecutionError(f"Scheduler stalled; unresolved: {pending}")
                finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                errors = []
                for task in finished:
                    node = running.pop(task)
                    if task.exception() is not None:
                        errors.append(task.exception())
                    else:
                        done.add(node)
                if errors:
                    raise errors[0]
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _run_node(self, node: str) -> None:
        if node in self.loop_map:
            await self._run_loop(self.loop_map[node])
        else:
            await self._run_step(self.step_map[node])

    # --- Steps -------------------------------------------------------------

    async def _run_step(self, step: StepDef) -> None:
        """Evaluate `when`, then execute (or skip) one step."""
        execution = self.execution_counts.get(step.id, 0) + 1
        self.execution_counts[step.id] = execution
        self.skipped.discard(step.id)

        if step.when and not eval_expr(
            self.when_ast[step.id], self.pipeline.input_values, self.verdicts
        ):
            self.skipped.add(step.id)
            self.verdicts[step.id] = None
            self.events.skip(step.id, when=step.when, execution=execution)
            if self.store:
                self.store.save_step_skipped(step.id, execution)
            return

        if self._semaphore:
            async with self._semaphore:
                await self._execute(step, execution)
        else:
            await self._execute(step, execution)

    async def _execute(self, step: StepDef, execution: int) -> None:
        loop = self.schedule.node_of[step.id]
        self.events.step_start(
            step.id, execution=execution, resume=step.resume, fork=step.fork,
            inject_count=count_inject(step.inject, self.workdir),
            loop=loop if loop != step.id else None,
        )
        if self.store:
            self.store.save_step_start(step.id, execution)

        self._assert_resumable(step)
        await self._maybe_compact(step)

        budget = DEFAULT_RETRY if step.retry is None else step.retry
        attempt = 0
        while True:
            result, verdict, problem = await self._attempt(step, execution, attempt)
            if problem is None:
                break
            will_retry = attempt < budget
            self.events.step_abnormal(
                step.id, reason=problem, will_retry=will_retry,
                attempt=attempt + 1, budget=budget,
            )
            if not will_retry:
                if self.store:
                    self.store.save_step_result(step.id, result, "failure", attempt, execution)
                raise ExecutionError(
                    f"Step '{step.id}' produced no usable result after "
                    f"{attempt + 1} attempt(s): {problem}"
                )
            attempt += 1

        if step.output:
            self._write_output(step.output, result.output_text)
        self.results[step.id] = result
        self.verdicts[step.id] = verdict
        self._sync_resume_target(step, result)
        if self.store:
            self.store.save_step_result(
                step.id, result, "success", attempt, execution, verdict=verdict
            )

    async def _attempt(
        self, step: StepDef, execution: int, attempt: int
    ) -> tuple[StepResult, str | None, str | None]:
        """One provider run (+ verdict follow-up). Returns (result, verdict, problem)."""
        try:
            prompt = self._build_prompt(step)
        except FileNotFoundError as e:
            raise ExecutionError(f"Step '{step.id}': {e}") from e
        options = self._run_options(step, execution, attempt)
        result = await self.provider.run(prompt, options)
        self.events.step_result(
            step.id, execution=execution,
            provider_status="error" if result.is_error else "ok",
            turns=result.num_turns, duration_ms=result.duration_ms,
            cost_usd=result.cost_usd, subtype=result.subtype,
            stop_reason=result.stop_reason, api_error_status=result.api_error_status,
            output_empty=not (result.output_text or "").strip(),
        )
        problem = _abnormal_reason(step, result)
        if problem or not step.verdict:
            return result, None, problem

        source = result.output_text
        if step.verdict.prompt:
            question = f"{step.verdict.prompt}{SEPARATOR}{verdict_contract(step.verdict.values)}"
            try:
                source = await self.provider.follow_up(result.session_id, question, options)  # type: ignore[arg-type]
            except Exception as e:  # a failed follow-up is an abnormal outcome, not a crash
                return result, None, f"verdict follow-up failed: {e}"
            result.verdict_reply = source
        verdict, line = parse_verdict(source, step.verdict.values)
        if verdict is None:
            return result, None, (
                f"no verdict: last line {line[:120]!r} must name exactly one of "
                f"{step.verdict.values}"
            )
        self.events.verdict(step.id, verdict=verdict, line=line[:200], execution=execution)
        return result, verdict, None

    def _build_prompt(self, step: StepDef) -> str:
        # An exact inject path may be absent only if its producer has not
        # produced yet: it was skipped, or (in a loop) has not run this round,
        # or it is this step's own previous output. Anything else is an error.
        tolerate = {
            p for p in step.inject
            if not is_glob(p) and p in self.producer and self.producer[p] not in self.results
        }
        verdict_inline = step.verdict.values if step.verdict and not step.verdict.prompt else None
        return build_prompt(
            prompt=step.prompt,
            inject_paths=step.inject,
            workdir=self.workdir,
            output_path=step.output,
            verdict_values=verdict_inline,
            tolerate_missing=tolerate,
            preamble=self.pipeline.user_preamble,
        )

    def _base_options(self, step: StepDef) -> StepRunOptions:
        """Run options derived from the step definition alone."""
        return StepRunOptions(
            step_id=step.id,
            model=step.model,
            allowed_tools=step.allowed_tools,
            max_turns=step.max_turns,
            timeout=step.timeout,
            cwd=self.workdir,
            mcp_servers=self.pipeline.mcp_servers or None,
            extra_system_prompt=self.pipeline.system_prompt,
        )

    def _run_options(self, step: StepDef, execution: int, attempt: int) -> StepRunOptions:
        transcript: str | None = None
        if self.store:
            name = "transcript.jsonl" if attempt == 0 else f"transcript.attempt-{attempt}.jsonl"
            transcript = str(self.store.step_dir(step.id, execution) / name)
        return replace(
            self._base_options(step),
            resume_session_id=self.results[step.resume].session_id if step.resume else None,
            fork_session=step.fork,
            transcript_path=transcript,
        )

    def _assert_resumable(self, step: StepDef) -> None:
        """`resume` makes the target a dependency, so it has finished by now —
        but it may have been skipped, or have died before creating a session."""
        if not step.resume:
            return
        target = self.results.get(step.resume)
        if target is None:
            raise ExecutionError(
                f"Step '{step.id}' cannot resume '{step.resume}': it did not run "
                f"(skipped by its `when`). Give '{step.id}' a matching `when`."
            )
        if target.session_id is None:
            raise ExecutionError(
                f"Step '{step.id}' cannot resume '{step.resume}': it produced no session"
            )

    def _sync_resume_target(self, step: StepDef, result: StepResult) -> None:
        """After a non-fork resume, the target's session has grown in place.

        Refresh the target's cached session id and context size so the next
        resumer's compact_before_resume gate sees the real current size, not
        the size the target had when it first finished.
        """
        if not step.resume or step.fork:
            return
        target = self.results[step.resume]
        if result.session_id:
            target.session_id = result.session_id
        if result.input_tokens:
            target.input_tokens = result.input_tokens
        if result.context_window:
            target.context_window = result.context_window

    async def _maybe_compact(self, step: StepDef) -> None:
        """/compact the resume target in place when it is over the threshold.

        The gate is free: the size comes from usage already recorded on the
        target's result. Only when over compact_before_resume do we pay for a
        /compact. A per-target lock makes concurrent (forked) resumers of one
        session compact it once; the others re-read the shrunk size and skip.
        """
        threshold = step.compact_before_resume
        if not threshold or not step.resume:
            return
        lock = self._compact_locks.setdefault(step.resume, asyncio.Lock())
        async with lock:
            target = self.results[step.resume]
            pct = target.context_pct()
            if pct is None or pct < threshold:
                return
            before_tokens = target.input_tokens
            # Compact with the options the target session was built with: a
            # different model/tools/system prompt is a full cache miss.
            after_tokens, window = await self.provider.compact(
                target.session_id,  # type: ignore[arg-type]
                self._base_options(self.step_map[step.resume]),
            )
            if after_tokens:
                target.input_tokens = after_tokens
            if window:
                target.context_window = window
            self.events.compact(
                step.id, target=step.resume, before_pct=pct, threshold=threshold,
                before_tokens=before_tokens, after_tokens=target.input_tokens,
                after_pct=target.context_pct(),
            )

    def _write_output(self, output_path: str, content: str) -> None:
        path = Path(output_path)
        if self.workdir and not path.is_absolute():
            path = Path(self.workdir) / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # --- Loops -------------------------------------------------------------

    async def _run_loop(self, loop: LoopDef) -> None:
        """Rounds of body → until → (fix) until satisfied or out of rounds."""
        body_deps = self.schedule.body_deps[loop.id]
        for rnd in range(1, loop.max_rounds + 1):
            for sid in loop.body:  # "has a result" must mean "this round"
                self.results.pop(sid, None)
                self.verdicts.pop(sid, None)
            await self._run_graph(body_deps, lambda sid: self._run_step(self.step_map[sid]))

            satisfied = eval_expr(
                self.until_ast[loop.id], self.pipeline.input_values, self.verdicts
            )
            self.events.loop_round(
                loop.id, round=rnd, max_rounds=loop.max_rounds,
                until=loop.until, satisfied=satisfied,
            )
            if satisfied:
                self._finish_loop(loop, "converged", rnd)
                return
            if rnd < loop.max_rounds and loop.fix:
                await self._run_step(self.step_map[loop.fix])

        if loop.on_exhausted == "continue":
            self._finish_loop(loop, "exhausted", loop.max_rounds)
            return
        self._finish_loop(loop, "failed", loop.max_rounds)
        raise ExecutionError(
            f"Loop '{loop.id}' did not satisfy `{loop.until}` in "
            f"{loop.max_rounds} round(s) (on_exhausted: fail)"
        )

    def _finish_loop(self, loop: LoopDef, status: str, rounds: int) -> None:
        self.loop_status[loop.id] = status
        self.events.loop_done(loop.id, status=status, rounds=rounds)
        if self.store:
            self.store.save_loop_result(loop.id, status, rounds)

    # --- Resume / reporting -------------------------------------------------

    def _seed_from_prior_run(self, base_dir: str) -> None:
        """Mark the prior run's finished nodes done, restoring their results.

        A plain step is reused if its newest execution succeeded. A loop is
        reused only as a whole, and only if it finished (converged/exhausted);
        a loop interrupted mid-round re-runs from round 1. Steps or loops no
        longer in the pipeline are ignored.
        """
        run_id = self.resume_run_id
        assert run_id is not None
        try:
            loaded = RunStore.load_completed_steps(base_dir, run_id)
            finished_loops = RunStore.load_finished_loops(base_dir, run_id)
        except FileNotFoundError as e:
            raise ExecutionError(str(e)) from e

        seeded: list[str] = []
        for node in self.schedule.node_deps:
            loop = self.loop_map.get(node)
            if loop is not None:
                if node not in finished_loops:
                    continue
                members = [s for s in [*loop.body, loop.fix] if s and s in loaded]
            elif node in loaded:
                members = [node]
            else:
                continue
            for sid in members:
                rs = loaded[sid]
                self.results[sid] = StepResult(session_id=rs.session_id, output_text=rs.output_text)
                self.verdicts[sid] = rs.verdict
                self.execution_counts[sid] = rs.execution
                seeded.append(sid)
            self.seeded_nodes.add(node)

        self.store.record_resumed_from(run_id, seeded)  # type: ignore[union-attr]
        self.events.resumed(run_id, steps=seeded)

    def _terminal_reason(self) -> str:
        ran = len(self.results)
        reason = f"{ran} step(s) completed, {len(self.skipped)} skipped"
        exhausted = [lid for lid, st in self.loop_status.items() if st == "exhausted"]
        if exhausted:
            reason += f"; loop(s) {exhausted} exhausted without satisfying `until`"
        return reason

    def _print_dry_run(self) -> None:
        def describe(sid: str) -> str:
            s = self.step_map[sid]
            bits = [sid]
            if s.verdict:
                bits.append(f"verdict={s.verdict.values}")
            if s.when:
                bits.append(f"when: {s.when}")
            return "  ".join(bits)

        print(f"Pipeline: {self.pipeline.name}")
        print(f"Workdir: {self.workdir or '.'}")
        print(f"Inputs: {self.pipeline.input_values}")
        print("\nSchedule (dependency order):")
        for node in self.schedule.order():
            deps = sorted(self.schedule.node_deps[node])
            after = f"   ← {deps}" if deps else ""
            loop = self.loop_map.get(node)
            if loop is None:
                print(f"  {describe(node)}{after}")
                continue
            print(
                f"  loop {node}  until: {loop.until}  max_rounds={loop.max_rounds}  "
                f"on_exhausted={loop.on_exhausted}{after}"
            )
            for sid in topo_order(self.schedule.body_deps[node], f"loop '{node}'"):
                print(f"    body: {describe(sid)}")
            if loop.fix:
                print(f"    fix:  {describe(loop.fix)}")
