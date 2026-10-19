"""Pair registry: one source of truth per exchange pair (docs/specs/2026-10-18-pair-registry.md)."""

from .liquidity import read_liquidity

__all__ = ["read_liquidity"]
