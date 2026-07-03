"""Tests for prompt assembly, including the output-capture contract.

When a step declares `output:`, its reply body is saved verbatim as that file
and read by later steps. build_prompt appends a contract note so the model
treats its reply as the artifact itself, not a chat message about the work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra.inject import build_prompt


def test_no_output_no_contract():
    out = build_prompt(prompt="Do the thing", inject_paths=[])
    assert out == "Do the thing"
    assert "Output contract" not in out


def test_output_appends_contract_with_path():
    out = build_prompt(
        prompt="Write the report", inject_paths=[], output_path="final-report.md"
    )
    assert out.startswith("Write the report")
    assert "Output contract" in out
    # The note names the exact target file so the model knows the destination.
    assert out.count("final-report.md") >= 1
    # Contract comes after the step prompt, separated by a rule.
    assert out.index("Write the report") < out.index("Output contract")


def test_contract_follows_injected_files(tmp_path: Path):
    src = tmp_path / "a.md"
    src.write_text("injected body")
    out = build_prompt(
        prompt="Summarize A",
        inject_paths=[str(src)],
        output_path="summary.md",
    )
    # Order: injected file, then step prompt, then the output contract last.
    assert (
        out.index("injected body")
        < out.index("Summarize A")
        < out.index("Output contract")
    )


def test_missing_precise_inject_raises_on_forward_run(tmp_path: Path):
    # A missing precise file on a normal forward run is a config error (e.g. a
    # typo'd path) and must surface, not be silently dropped.
    with pytest.raises(FileNotFoundError, match="ghost.md"):
        build_prompt(
            prompt="use the file",
            inject_paths=["ghost.md"],
            workdir=str(tmp_path),
        )


def test_missing_precise_inject_tolerated_when_allow_missing(tmp_path: Path):
    # In a back-jump loop an upstream artifact may not exist yet on the first
    # pass, so allow_missing skips it silently.
    out = build_prompt(
        prompt="use the file",
        inject_paths=["ghost.md"],
        workdir=str(tmp_path),
        allow_missing=True,
    )
    assert out == "use the file"


def test_missing_glob_never_raises(tmp_path: Path):
    # Globs skip non-matches regardless of allow_missing — no file is "expected".
    out = build_prompt(
        prompt="use the files",
        inject_paths=["*.md"],
        workdir=str(tmp_path),
    )
    assert out == "use the files"
