"""CLI entry point for Hydra pipeline execution."""

from __future__ import annotations

import asyncio
import sys

import click

from .errors import ExecutionError, HydraError, ValidationError
from .executor import ExecutionEngine
from .loader import load_pipeline
from .validate import validate_pipeline


@click.group()
def cli() -> None:
    """Hydra — AI agent orchestration engine."""


@cli.command()
@click.argument("pipeline_file", type=click.Path(exists=True))
@click.option("--var", multiple=True, help="Input variable: key=value")
@click.option("--var-file", type=click.Path(exists=True), help="YAML file with input variables")
@click.option("--concurrency", type=int, default=0, help="Max concurrent steps (0=unlimited)")
@click.option("--dry-run", is_flag=True, help="Show execution plan without running")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging (include assistant text)")
@click.option(
    "--resume-last", is_flag=True,
    help="Continue the most recent prior run: skip its successfully-completed "
    "steps and re-run the rest (the interrupted/failed step onward).",
)
@click.option(
    "--resume", "resume_run_id", metavar="RUN_ID", default=None,
    help="Continue a specific prior run by id (see .hydra/runs/). "
    "Takes precedence over --resume-last.",
)
def run(
    pipeline_file: str,
    var: tuple[str, ...],
    var_file: str | None,
    concurrency: int,
    dry_run: bool,
    verbose: bool,
    resume_last: bool,
    resume_run_id: str | None,
) -> None:
    """Execute a pipeline."""
    vars_dict = _parse_vars(var)

    try:
        pipeline = load_pipeline(pipeline_file, vars=vars_dict, var_file=var_file)
        warnings = validate_pipeline(pipeline)
        for w in warnings:
            click.echo(f"[hydra] warning: {w}", err=True)

        engine = ExecutionEngine(
            pipeline=pipeline,
            concurrency=concurrency,
            dry_run=dry_run,
            verbose=verbose,
            resume_run_id=resume_run_id,
            resume_last=resume_last,
        )
        result = asyncio.run(engine.run())

        if result == "failure":
            sys.exit(1)

    except ValidationError as e:
        click.echo(f"[hydra] validation error: {e}", err=True)
        sys.exit(2)
    except ExecutionError as e:
        click.echo(f"[hydra] execution error: {e}", err=True)
        sys.exit(1)
    except HydraError as e:
        click.echo(f"[hydra] error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("pipeline_file", type=click.Path(exists=True))
@click.option("--var", multiple=True, help="Input variable: key=value")
@click.option("--var-file", type=click.Path(exists=True), help="YAML file with input variables")
def validate(
    pipeline_file: str,
    var: tuple[str, ...],
    var_file: str | None,
) -> None:
    """Validate a pipeline file without executing."""
    vars_dict = _parse_vars(var)

    try:
        pipeline = load_pipeline(pipeline_file, vars=vars_dict, var_file=var_file)
        warnings = validate_pipeline(pipeline)

        click.echo(f"Pipeline '{pipeline.name}' is valid.")
        click.echo(f"  Steps: {len(pipeline.steps)}")
        click.echo(f"  Loops: {len(pipeline.loops)}")

        if warnings:
            click.echo()
            for w in warnings:
                click.echo(f"  warning: {w}")

    except ValidationError as e:
        click.echo(f"Validation error: {e}", err=True)
        sys.exit(2)
    except HydraError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)


def _parse_vars(var_args: tuple[str, ...]) -> dict[str, str]:
    """Parse --var key=value arguments into a dict."""
    result: dict[str, str] = {}
    for item in var_args:
        if "=" not in item:
            raise click.BadParameter(f"Invalid --var format: '{item}' (expected key=value)")
        key, value = item.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def main() -> None:
    cli()
