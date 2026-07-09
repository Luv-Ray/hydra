"""Core execution engine: DAG traversal + routing + retry + goto."""

from __future__ import annotations

import asyncio
import re
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


# How many times to re-run a step whose outcome was abnormal (errored, hit an
# API error, or produced no body when one was contracted). These are transient
# by nature — an API 429/500/529 or an empty reply usually clears on one retry —
# so they get their own budget, independent of `step.retry` (which the author
# tunes for the semantic REJECT loop and may deliberately set to 0). Kept at 1:
# a single retry recovers the common transient blip, while a second rarely helps
# and would double the worst-case cost on expensive steps (analyze runs ~1h).
ABNORMAL_RETRY_BUDGET = 1


def _classify_outcome(step: StepDef, result: StepResult) -> tuple[str, str]:
    """Sort a step result into ok / abnormal_fatal / abnormal_routable.

    The distinction that matters: a step that errored or returned an empty body
    produced *no verdict*. It must never be fed to a check and turned into a
    "failure" that a REJECT route then acts on — an empty mini-review is not a
    rejection of the analysis, it's a step that didn't run. Nor may a step that
    broke its output contract (declared `output`, wrote nothing) proceed, since
    a downstream `inject` of that missing file would crash the run.

    Returns (outcome, reason):
      - "ok": real body produced; run the check / write output as normal.
      - "abnormal_fatal": errored/empty on a step that has a check (would fake a
        REJECT) or broke its output contract (would crash downstream). Retried
        within ABNORMAL_RETRY_BUDGET, then hard-fails the pipeline — never routed.
      - "abnormal_routable": errored on a step with no check and no output
        contract, e.g. a timeout on a plain step whose `if: failure` route is
        genuine error handling. Keeps the legacy failure-routing behavior.
    """
    empty = not (result.output_text or "").strip()
    # A step is contracted to produce a body when it declares an output file or
    # when its check reads the reply directly (a check with no prompt of its own).
    contracted = bool(step.output) or bool(step.check and not step.check.prompt)
    errored = result.is_error or bool(result.api_error_status)

    if not errored and not (contracted and empty):
        return "ok", ""

    if errored:
        reason = (
            f"errored (is_error={result.is_error}, subtype={result.subtype}, "
            f"api_error_status={result.api_error_status})"
        )
    else:
        reason = "empty output (step was contracted to produce a body)"

    # Fatal when there's a verdict to protect (has a check) or a contract broken
    # (empty body a downstream inject depends on). Otherwise fall back to routing.
    if step.check or (contracted and empty):
        return "abnormal_fatal", reason
    return "abnormal_routable", reason


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
        resume_run_id: str | None = None,
        resume_last: bool = False,
    ):
        self.pipeline = pipeline
        self.inputs = inputs
        self.provider = provider or ClaudeProvider(verbose=verbose)
        self.concurrency = concurrency
        self.dry_run = dry_run
        self.verbose = verbose
        # Resume config. When resolved (in run(), before the new run dir is
        # created), the successful steps of the chosen prior run are rehydrated
        # into self.results so they're skipped and their session_ids stay
        # available to any resume: target. resume_last picks the newest run;
        # resume_run_id names one explicitly (takes precedence). None = fresh.
        self.resume_run_id = resume_run_id
        self.resume_last = resume_last

        self.step_map = {s.id: s for s in pipeline.steps}
        # Dependency graph is a pure function of the (immutable) pipeline, so
        # build it once here rather than recomputing on every run/back-jump/
        # subgraph query.
        self.dag = build_dag(pipeline)
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

        # Resolve --resume-last to a concrete run id NOW, before RunStore below
        # creates a fresh (and thus "latest") run dir. An explicit id wins; if
        # neither yields a run, this is just a normal fresh run.
        if self.resume_run_id is None and self.resume_last:
            self.resume_run_id = RunStore.find_latest_run(base_dir)
            if self.resume_run_id is None:
                self.events.emit(
                    "resume_noop", "  --resume-last: no prior run found, "
                    "starting fresh",
                )

        self.store = RunStore(base_dir, self.pipeline.name, self.inputs, self.workdir)
        self.events = EventLog(self.store.run_dir)
        # Detailed agent logs (prompts, tool/shell calls, check I/O) go to a file
        # in the run dir — kept out of the live stderr stream, which now carries
        # only control-flow events. See ClaudeProvider.set_trace_file.
        if isinstance(self.provider, ClaudeProvider):
            self.provider.set_trace_file(self.store.run_dir / "agent-trace.log")

        # Record when-skipped steps so the run dir reflects the full plan, not
        # just the steps that ran (skipped steps otherwise leave no trace).
        for sid in self.skipped:
            self.store.save_step_skipped(sid)

        # Build DAG and execute
        layers = topological_layers(self.pipeline, self.dag)

        try:
            # Rehydrate a prior run's completed steps into results so they're
            # skipped below. Inside the try so a bad --resume id surfaces as a
            # caught pipeline failure, not a raw traceback.
            if self.resume_run_id:
                self._seed_from_prior_run(base_dir)

            for layer in layers:
                # Skip steps already run via an earlier goto (a forward-goto
                # fan-out or a max_iterations escape can execute a step before
                # the top-level walk reaches its layer). Back-jumps re-run their
                # loop body through their own internal loop, never here, so a
                # step already in results when its layer arrives genuinely ran
                # and must not repeat.
                active = [
                    sid for sid in layer
                    if sid not in self.skipped and sid not in self.results
                ]
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

    def _seed_from_prior_run(self, base_dir: str) -> None:
        """Load a prior run's successful steps into results so they're skipped.

        Only steps that still exist in the current pipeline are seeded — a step
        renamed or removed since the prior run is silently ignored, so a stale
        artifact can't resurrect a step the DAG no longer knows about. The
        top-level walk (and every goto) already skips any step present in
        results, so seeding is all that's needed to make them no-ops.
        """
        run_id = self.resume_run_id
        assert run_id is not None
        try:
            loaded = RunStore.load_completed_steps(base_dir, run_id)
        except FileNotFoundError as e:
            raise ExecutionError(str(e)) from e
        seeded: list[str] = []
        for sid, rs in loaded.items():
            if sid not in self.step_map:
                continue
            self.results[sid] = StepResult(
                session_id=rs.session_id,
                output_text=rs.output_text,
                check_output=rs.check_output,
            )
            # Count the seeded run as this step's first execution, so if a
            # back-jump later re-runs it, its re-execution persists to run-2/
            # rather than clobbering the seed. The step's inject artifacts
            # (output.md written to the workdir by the prior run) already sit in
            # the workdir, where inject resolves them — no need to copy them.
            self.execution_counts[sid] = 1
            seeded.append(sid)

        self.store.record_resumed_from(run_id, seeded)
        self.events.resumed(run_id, steps=seeded)

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

        # A step can only resume a target that actually produced a session. If
        # the target ran but has no session_id (e.g. it timed out before any
        # result), resuming it is impossible — fail loudly here rather than
        # handing a missing session to compact/run and crashing inside the SDK.
        self._assert_resumable(step)

        # Before resuming a heavy session, compact it in place (like /compact in
        # the CLI) so this step continues on a lighter context.
        await self._maybe_compact(step)

        # Two independent retry budgets, because two unrelated things fail here:
        #  · abnormal outcomes (errored / API error / empty body) are transient
        #    infrastructure hiccups — retried on ABNORMAL_RETRY_BUDGET.
        #  · a semantic REJECT (ok body, check says no) is a real verdict —
        #    retried on step.retry, then routed via `if: failure`.
        # An abnormal outcome never becomes a check-failure, so an empty reply
        # can no longer masquerade as a REJECT and trigger a revise loop.
        max_retries = step.retry or 0
        last_result: StepResult | None = None
        reject_retries = 0    # semantic REJECT re-runs used (bounded by max_retries)
        abnormal_retries = 0  # abnormal re-runs used (bounded by ABNORMAL_RETRY_BUDGET)
        run_index = 0         # total provider.run calls; >0 means "inject retry feedback"

        while True:
            prompt = self._build_step_prompt(step, last_result, run_index)
            options = self._build_run_options(step, run_index, execution)

            result = await self.provider.run(prompt, options)
            outcome, reason = _classify_outcome(step, result)
            self.events.step_result(
                step.id, execution=execution,
                provider_status="error" if result.is_error else "ok",
                turns=result.num_turns, duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                subtype=result.subtype, stop_reason=result.stop_reason,
                api_error_status=result.api_error_status,
                output_empty=not (result.output_text or "").strip(),
            )

            # --- Abnormal outcome: no trustworthy answer was produced ---------
            # An errored / empty step produced no verdict. It never becomes a
            # check-failure, so an empty reply can't masquerade as a REJECT and
            # trigger a revise loop.
            if outcome.startswith("abnormal"):
                will_retry = abnormal_retries < ABNORMAL_RETRY_BUDGET
                self.events.step_abnormal(
                    step.id, reason=reason, will_retry=will_retry,
                    attempt=abnormal_retries + 1, budget=ABNORMAL_RETRY_BUDGET,
                )
                if will_retry:
                    abnormal_retries += 1
                    run_index += 1
                    last_result = result
                    continue
                # Budget spent. Persist as a failure either way.
                self.results[step.id] = result
                if self.store:
                    self.store.save_step_result(
                        step.id, result, "failure", abnormal_retries, execution
                    )
                # Fatal abnormal (a check to protect, or a broken output
                # contract) aborts rather than routing a fake REJECT / crashing
                # a downstream inject. A routable one (plain errored step with a
                # real `if: failure` handler) keeps the legacy behavior.
                if outcome == "abnormal_fatal":
                    raise ExecutionError(
                        f"Step '{step.id}' produced no usable output after "
                        f"{abnormal_retries + 1} attempt(s): {reason}. Treated as "
                        f"a step failure, not a rejection of the analysis — so it "
                        f"is not routed to a revise/failure branch."
                    )
                await self._handle_routes(step, "failure")
                return

            # --- Normal outcome: real body produced ---------------------------
            # Write output file if specified
            if step.output and result.output_text:
                self._write_output(step.output, result.output_text)

            # Run check if defined
            status = "success"
            if step.check:
                if step.check.prompt:
                    check_output = await self.provider.check(
                        result.session_id, step.check.prompt, self.workdir
                    )
                else:
                    check_output = result.output_text
                result.check_output = check_output
                status = await self._evaluate_check(step, check_output)

            if status in ("success", "routed"):
                self.results[step.id] = result
                if self.store:
                    self.store.save_step_result(
                        step.id, result, "success", reject_retries, execution
                    )
                # "routed" means check.routes already dispatched the goto.
                if status == "success":
                    await self._handle_routes(step, "success")
                return

            # Semantic REJECT — retry on the author's budget, then route failure.
            last_result = result
            if reject_retries < max_retries:
                reject_retries += 1
                run_index += 1
                self.events.retry(
                    step.id, attempt=reject_retries, max_retries=max_retries
                )
                continue

            self.results[step.id] = result
            if self.store:
                self.store.save_step_result(
                    step.id, result, "failure", max_retries, execution
                )
            await self._handle_routes(step, "failure")
            return

    def _assert_resumable(self, step: StepDef) -> None:
        """Reject a resume whose target ran but produced no session.

        Only fires when the target is present in results (it executed) yet has a
        None session_id — the signature of a step that failed before the SDK
        returned a session (a pre-result timeout). A target that hasn't run yet
        is not an error here: resume degrades to a fresh session in that case
        (see _build_run_options), which is the intended first-pass/back-jump
        behavior.
        """
        if not step.resume:
            return
        target = self.results.get(step.resume)
        if target is not None and target.session_id is None:
            raise ExecutionError(
                f"Step '{step.id}' cannot resume '{step.resume}': the target "
                f"produced no session (it likely failed or timed out). Route the "
                f"failure explicitly instead of resuming a dead session."
            )

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
            # Compact on the same model the target session was built on: /compact
            # replays the transcript through the model, so a mismatch is a full
            # cache miss. step_map carries the defaults-merged model.
            target_step = self.step_map.get(step.resume)
            target_model = target_step.model if target_step else None
            after_tokens, window = await self.provider.compact(
                target.session_id, target_model, self.workdir
            )
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

        # In a back-jump loop, an upstream artifact may legitimately not exist
        # yet on the first pass, so missing precise inject files are tolerated.
        # On a normal forward run a missing file is a config error (see
        # build_prompt) — surfacing it beats silently dropping a typo'd path.
        allow_missing = step.id in self.iteration_counts

        return build_prompt(
            prompt=step.prompt,
            inject_paths=step.inject,
            workdir=self.workdir,
            retry_feedback=retry_feedback,
            output_path=step.output,
            allow_missing=allow_missing,
            preamble=self.pipeline.user_preamble,
        )

    def _build_run_options(
        self, step: StepDef, attempt: int, execution: int
    ) -> StepRunOptions:
        """Build provider run options for a step."""
        resume_session_id: str | None = None
        if step.resume and step.resume in self.results:
            resume_session_id = self.results[step.resume].session_id

        # Full transcript lands in the step's run dir alongside its output/meta.
        # attempt>0 (a retry) gets a suffixed file so an earlier attempt's
        # transcript — often the one that captured the failure — isn't clobbered.
        transcript_path: str | None = None
        if self.store:
            d = self.store.step_dir(step.id, execution)
            name = "transcript.jsonl" if attempt == 0 else f"transcript.attempt-{attempt}.jsonl"
            transcript_path = str(d / name)

        return StepRunOptions(
            step_id=step.id,
            model=step.model,
            allowed_tools=step.allowed_tools,
            max_turns=step.max_turns,
            timeout=step.timeout,
            cwd=self.workdir,
            resume_session_id=resume_session_id,
            mcp_servers=self.pipeline.mcp_servers or None,
            extra_system_prompt=self.pipeline.system_prompt,
            transcript_path=transcript_path,
        )

    async def _evaluate_check(self, step: StepDef, check_output: str) -> str:
        """Evaluate check result. Returns 'success', 'failure', or 'routed'."""
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

            subgraph = self._resolve_subgraph(targets, origin_step)
            self.events.back_jump(
                origin_step.id, targets=targets, subgraph=subgraph,
                iteration=count, max_iterations=max_iter,
            )

            if count >= max_iter:
                # Loop did not converge. If the origin declares an escape route
                # (`if: max_iterations`), take it instead of failing — this lets
                # a pipeline accept the last unconverged result and move on
                # (e.g. goto final_report) rather than aborting the whole run.
                escape = self._max_iter_route(origin_step)
                if escape is not None:
                    self.events.route(
                        origin_step.id, condition="max_iterations",
                        target=escape, kind="escape",
                    )
                    await self._goto_forward_only(origin_step, escape)
                    return
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

    def _max_iter_route(self, step: StepDef) -> str | list[str] | None:
        """The step's `if: max_iterations` escape goto, if it declares one.

        Checked only when a loop exhausts max_iterations (see _goto). Absent →
        exhaustion stays a hard failure (backward compatible).
        """
        for route in step.routes or []:
            if route.condition == "max_iterations":
                return route.goto
        return None

    async def _goto_forward_only(
        self, origin_step: StepDef, target: str | list[str]
    ) -> None:
        """Dispatch an escape goto that must not loop back.

        Reached only from the max_iterations escape. The loop already failed to
        converge, so the target must move the pipeline forward or terminate —
        never re-enter a step whose results still exist (that would just trigger
        another back-jump and re-arm the exhausted loop). $end/$fail terminate;
        any other target must be unexecuted (a forward step). A back-jump target
        here is a pipeline authoring error, surfaced rather than silently looped.
        """
        if target == "$end":
            self.events.route(
                origin_step.id, condition="max_iterations", target="$end", kind="end",
            )
            return
        if target == "$fail":
            self.events.route(
                origin_step.id, condition="max_iterations", target="$fail", kind="fail",
            )
            raise ExecutionError(
                f"Step '{origin_step.id}' routed to $fail after max_iterations"
            )

        targets = target if isinstance(target, list) else [target]
        looping = [t for t in targets if t in self.results]
        if looping:
            raise ExecutionError(
                f"Step '{origin_step.id}': max_iterations escape goto {looping} "
                f"points back into the loop — it must go forward ($end, $fail, "
                f"or an unexecuted step)"
            )
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
        dag = self.dag
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

    def _resolve_subgraph(
        self, targets: list[str], origin: StepDef
    ) -> list[str]:
        """The loop body to re-run on origin's back-jump.

        Prefers origin's author-declared ``loop_body`` (explicit, validate-
        checked). Falls back to reverse-derivation (_find_subgraph) when it is
        omitted, preserving backward compatibility for pipelines that rely on
        inference. Declared bodies are returned in pipeline order so back-jump
        re-execution layers them deterministically.
        """
        if origin.loop_body is not None:
            order = {s.id: i for i, s in enumerate(self.pipeline.steps)}
            return sorted(origin.loop_body, key=lambda x: order.get(x, 0))
        return self._find_subgraph(targets, origin.id)

    def _find_subgraph(self, targets: list[str], origin_id: str) -> list[str]:
        """Find the loop body: DAG steps between targets and origin (topo order).

        Excludes trigger:route steps — they are only reached via explicit goto,
        not as part of subgraph re-execution.

        Stops the walk at *branch steps* (steps carrying routes / check.routes).
        A branch step's DAG dependents are its forward *exit* — reached only when
        its check passes — not part of the loop body. Re-running the branch step
        itself re-issues the goto that closes the loop, so the walk must not
        descend past it. Without this, a cheap gate that fans out to expensive
        downstream steps on success (e.g. mini-review → parallel deep review)
        would drag that whole fan-out into every revise iteration: the very
        reason the fan-out used to be mis-wired to a common upstream dep instead
        of the gate. The branch step itself stays in the subgraph.
        """
        dag = self.dag
        route_steps = {s.id for s in self.pipeline.steps if s.trigger == "route"}
        branch_steps = {
            s.id for s in self.pipeline.steps
            if s.routes or (s.check and s.check.routes)
        }

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
            # Branch step: include it but do not descend into its exit path.
            if current in branch_steps:
                continue
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
        layers = topological_layers(self.pipeline, self.dag)

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
        for sid, deps in self.dag.items():
            if deps:
                print(f"  {sid} ← {deps}")
