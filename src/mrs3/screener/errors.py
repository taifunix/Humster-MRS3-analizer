"""Shared exception for the screener evaluation pipeline."""

from __future__ import annotations


class ScreenerEvaluationError(ValueError):
    """A whole screener evaluation call must fail, not just one pair's verdict."""
