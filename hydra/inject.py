"""Prompt assembly: file injection plus the output / verdict contracts."""

from __future__ import annotations

import glob as globmod
from collections.abc import Collection
from pathlib import Path

from .dag import is_glob

SEPARATOR = "\n\n---\n\n"


def build_prompt(
    prompt: str,
    inject_paths: list[str],
    workdir: str | None = None,
    output_path: str | None = None,
    verdict_values: list[str] | None = None,
    tolerate_missing: Collection[str] = (),
    preamble: str | None = None,
) -> str:
    """Assemble preamble + injected files + step prompt + contracts.

    Args:
        inject_paths: exact paths and glob patterns, relative to workdir.
        output_path: set when the reply body is captured into a file; appends
            the output contract.
        verdict_values: set when the verdict is read from this reply; appends
            the verdict contract.
        tolerate_missing: exact inject paths that may legitimately be absent —
            the engine passes the outputs of producers that have not produced
            yet (skipped, or not run yet in the current loop round). Any other
            missing exact path raises, so a typo'd path is never silently
            dropped. Glob patterns with no match are always skipped.
    """
    parts: list[str] = [preamble] if preamble else []

    for pattern in inject_paths:
        resolved = _resolve_path(pattern, workdir)
        if is_glob(pattern):
            for match in sorted(globmod.glob(resolved, recursive=True)):
                parts.append(f"[File: {_display_path(match, workdir)}]\n{Path(match).read_text()}")
            continue
        path = Path(resolved)
        if not path.exists():
            if pattern in tolerate_missing:
                continue
            raise FileNotFoundError(
                f"Inject file not found: {pattern} (resolved: {resolved}). If a step "
                f"declares this as its `output`, it ran but wrote nothing — check its "
                f"step_abnormal event in the run logs rather than the inject path."
            )
        parts.append(f"[File: {_display_path(resolved, workdir)}]\n{path.read_text()}")

    parts.append(prompt)
    if output_path:
        parts.append(output_contract())
    if verdict_values:
        parts.append(verdict_contract(verdict_values))
    return SEPARATOR.join(parts)


def output_contract() -> str:
    """Appended when a step captures its reply body into a file.

    Addresses two failure modes:
    1. Framing — models default to writing a reply *to a human* (preamble,
       recap, sign-off), but the reply is saved verbatim as an artifact.
    2. Self-write — a model that knows the target filename tends to Write /
       shell-redirect the artifact there, then reply with a status line
       ("file is complete, 271 lines"); the framework captures that status
       line and overwrites the good on-disk file. So the contract omits the
       filename and says explicitly not to write the file itself.
    """
    return (
        "[Output contract] The framework saves your reply body verbatim as a "
        "file and feeds it to later steps; no human reads it as a message. So "
        "your reply must BE the file's content — not a Write/redirect to disk "
        "(that copy is discarded and overwritten by your reply), and not a "
        "status note about the work (that note becomes the file). Output exactly "
        "what the file should contain, with no preamble or commentary."
    )


def verdict_contract(values: list[str]) -> str:
    """Appended to whichever reply the verdict is read from.

    The engine parses only the last non-empty line, so the model is told that
    explicitly — a verdict stated mid-reply and then revised further down would
    otherwise be ambiguous.
    """
    return (
        f"[Verdict contract] End your reply with one final line that contains "
        f"exactly one of: {' / '.join(values)}. The engine reads only that last "
        f"line to decide what happens next: put nothing after it, and do not name "
        f"the other values on it. If your reply is saved as a file, this line is "
        f"part of the file."
    )


def count_inject(inject_paths: list[str], workdir: str | None = None) -> int:
    """How many files would be injected right now (globs expanded)."""
    total = 0
    for pattern in inject_paths:
        resolved = _resolve_path(pattern, workdir)
        if is_glob(pattern):
            total += len(globmod.glob(resolved, recursive=True))
        elif Path(resolved).exists():
            total += 1
    return total


def _resolve_path(path: str, workdir: str | None) -> str:
    if workdir and not Path(path).is_absolute():
        return str(Path(workdir) / path)
    return path


def _display_path(path: str, workdir: str | None) -> str:
    if workdir:
        try:
            return str(Path(path).relative_to(Path(workdir)))
        except ValueError:
            pass
    return path
