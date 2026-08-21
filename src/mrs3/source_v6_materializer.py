"""Stage 2 materialization: READY witnesses never filter source facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .source_v6 import SourceV6Fragment
from .source_v6_coverage import ReadyInterval, canonical_ready_intervals
from .source_v6_storage import source_content_digest


@dataclass(frozen=True, slots=True)
class MaterializedScope:
    scope_key: str
    facts: tuple[SourceV6Fragment, ...]
    ready_witness: ReadyInterval


@dataclass(frozen=True, slots=True)
class MaterializedSourceV6:
    source_content_digest: str
    scopes: tuple[MaterializedScope, ...]


def _scope(fragment: SourceV6Fragment) -> str:
    point = fragment.point
    return f"{point.symbol}|{point.side}|{point.timeframe}"


def materialize_source_v6(
    fragments: Sequence[SourceV6Fragment], scope_keys: Sequence[str]
) -> MaterializedSourceV6:
    """Keep each selected scope's complete observed grid beside its READY witness."""
    requested = tuple(sorted(set(scope_keys)))
    if not requested:
        raise ValueError("at least one scope is required")
    if any(not isinstance(item, SourceV6Fragment) for item in fragments):
        raise ValueError("materialization requires hydrated fragments, not metadata views")
    witnesses = {item.scope_key: item for item in canonical_ready_intervals(tuple(fragments))}
    result = []
    for scope_key in requested:
        facts = tuple(sorted((item for item in fragments if _scope(item) == scope_key), key=lambda item: item.fragment_id))
        if not facts:
            raise ValueError(f"scope has no facts: {scope_key}")
        witness = witnesses.get(scope_key)
        if witness is None:
            raise ValueError(f"scope is not READY: {scope_key}")
        result.append(MaterializedScope(scope_key, facts, witness))
    return MaterializedSourceV6(source_content_digest(item.fragment_id for item in fragments), tuple(result))
