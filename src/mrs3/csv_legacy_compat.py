"""Wire constants and point-local trade slots for CSV Direct analysis artifacts.

CSV Direct writes the fresh analysis schema that the unchanged shortlist and
READY JSON consumers already read. Without PRETEST A/B evidence that is the
legacy v1 fresh contract, where ``pretest_ab`` is optional.

``countslot`` values are reproducible ordinal labels for one point's completed
trade count. They are not market events: they carry no time, order or
cross-point identity and must never be used for deduplication. They exist only
because the point schema stores the count as ``_event_ids`` + ``event_ids_hash``.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Sequence

LEGACY_ANALYSIS_FINGERPRINT = "analysis-v6-fresh-compact-v1"
LEGACY_SURFACE_FINGERPRINT = "surface-v6-fresh-compact-v2"
LEGACY_EVENT_MODE = "real_independent_events"
TRADE_COUNT_CONTRACT = "completed_positions-v1"
EVENT_ID_SEMANTICS = "per_point_count_ordinal_only"


class CsvCompatibilityError(ValueError):
    """The CSV analytics cannot be represented faithfully in the legacy schema."""


def countslot_ids(point_id: str, trades: int) -> list[str]:
    """Encode one completed-trade count as sorted, point-local ordinal names."""
    if not isinstance(point_id, str) or not point_id.strip():
        raise CsvCompatibilityError("point_id must be a nonempty string")
    if type(trades) is not int or trades < 0:
        raise CsvCompatibilityError("trades must be a nonnegative integer")
    prefix = sha256(point_id.encode("utf-8")).hexdigest()
    return [f"countslot:{prefix}:{number:012d}" for number in range(trades)]


def event_ids_hash(ids: Sequence[str]) -> str:
    """The point-schema hash: SHA-256 of the sorted, '|'-joined identifiers."""
    return sha256("|".join(sorted(set(ids))).encode("utf-8")).hexdigest()
