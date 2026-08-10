from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class InputAudit:
    source_rows: int
    normalized_rows: int
    service_rows: int
    symbols: int
    timeframes: int
    duplicate_cells: int

