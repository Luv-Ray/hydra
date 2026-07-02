"""File-based content injection for step prompts."""

from __future__ import annotations

import glob as globmod
from pathlib import Path


def build_prompt(
    prompt: str,
    inject_paths: list[str],
    workdir: str | None = None,
    retry_feedback: str | None = None,
    output_path: str | None = None,
) -> str:
    """Assemble the full prompt: retry feedback + inject content + step prompt.

    Args:
        prompt: The step's prompt text (already template-expanded).
        inject_paths: File paths and glob patterns to inject.
        workdir: Working directory for resolving relative paths.
        retry_feedback: Optional feedback from previous attempt to prepend.
        output_path: If set, the step captures the reply body into this file.
            A contract note is appended so the model understands its reply IS
            the artifact, not a chat message about the work.
    """
    parts: list[str] = []

    if retry_feedback:
        parts.append(retry_feedback)

    for pattern in inject_paths:
        resolved = _resolve_path(pattern, workdir)
        if "*" in pattern or "?" in pattern:
            matches = sorted(globmod.glob(resolved, recursive=True))
            for match in matches:
                content = Path(match).read_text()
                display_path = _display_path(match, workdir)
                parts.append(f"[File: {display_path}]\n{content}")
        else:
            path = Path(resolved)
            if not path.exists():
                continue
            content = path.read_text()
            display_path = _display_path(resolved, workdir)
            parts.append(f"[File: {display_path}]\n{content}")

    if parts:
        prefix = "\n\n---\n\n".join(parts)
        full = f"{prefix}\n\n---\n\n{prompt}"
    else:
        full = prompt

    if output_path:
        full = f"{full}\n\n---\n\n{_output_contract(output_path)}"

    return full


def _output_contract(output_path: str) -> str:
    """Note appended when a step captures its reply body into a file.

    Root cause this addresses: by default a model frames its reply as a message
    to a human — adding preambles, progress recaps, and sign-offs. But here the
    reply is saved verbatim as a file and read by later steps; no human reads it
    as a message. This states the document-production frame once, at the
    framework level, so the model emits the artifact itself rather than prose
    about producing it. Kept general on purpose — not a list of things to avoid.
    """
    return (
        f"[Output contract] Your reply to this message is saved verbatim as the "
        f"file `{output_path}` and read by later steps in this pipeline as input. "
        "It is the file's content, not a message to a reader. Output exactly what "
        "that file should contain and nothing else — no preamble, framing, or "
        "commentary around it."
    )



def read_inject_files(
    inject_paths: list[str],
    workdir: str | None = None,
    allow_missing: bool = False,
) -> list[tuple[str, str]]:
    """Read inject files and return (display_path, content) pairs.

    Args:
        allow_missing: If True, skip missing precise files instead of raising.
    """
    results: list[tuple[str, str]] = []

    for pattern in inject_paths:
        resolved = _resolve_path(pattern, workdir)
        if "*" in pattern or "?" in pattern:
            matches = sorted(globmod.glob(resolved, recursive=True))
            for match in matches:
                content = Path(match).read_text()
                results.append((_display_path(match, workdir), content))
        else:
            path = Path(resolved)
            if not path.exists():
                if allow_missing:
                    continue
                raise FileNotFoundError(
                    f"Inject file not found: {pattern} (resolved: {resolved})"
                )
            content = path.read_text()
            results.append((_display_path(resolved, workdir), content))

    return results


def count_inject(inject_paths: list[str], workdir: str | None = None) -> int:
    """Count how many files would actually be injected (globs expanded).

    Used for observability — reports how much context a step pulls in without
    reading the file bodies.
    """
    total = 0
    for pattern in inject_paths:
        resolved = _resolve_path(pattern, workdir)
        if "*" in pattern or "?" in pattern:
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
