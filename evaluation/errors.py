"""Shared exceptions for evaluation helpers and the submission exporter."""


class LeakageGuardError(ValueError):
    """Raised when an evaluation-only result is mistaken for an independent submission."""
