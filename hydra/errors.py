"""Hydra exception types."""

from __future__ import annotations


class HydraError(Exception):
    """Base exception for all Hydra errors."""


class ValidationError(HydraError):
    """DSL validation failure (exit code 2)."""


class ExecutionError(HydraError):
    """Runtime execution failure (exit code 1)."""


class TemplateError(HydraError):
    """Template expansion or expression evaluation failure."""
