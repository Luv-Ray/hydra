"""File-based content injection for step prompts."""

from __future__ import annotations

import glob as globmod
from pathlib import Path


def build_prompt(
    prompt: str,
    inject_paths: list[str],
    workdir: str | None = None,
    retry_feedback: str | None = None,
) -> str:
    """Assemble the full prompt: retry feedback + inject content + step prompt.

    Args:
        prompt: The step's prompt text (already template-expanded).
        inject_paths: File paths and glob patterns to inject.
        workdir: Working directory for resolving relative paths.
        retry_feedback: Optional feedback from previous attempt to prepend.
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
        return f"{prefix}\n\n---\n\n{prompt}"
    return prompt


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
