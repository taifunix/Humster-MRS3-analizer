"""Build a fresh Source v6 target from a base DB plus coverage-only patch rows."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from mrs3.source_v6_merge import _copy_fragments_from_inputs, _read_input, merge_source_v6
from mrs3.source_v6_storage import compact_v6_database, create_v6_database, validate_source_v6_database


def _key(item: object) -> tuple[object, ...]:
    point = item.point
    return (
        point.symbol, point.side, point.timeframe,
        point.open_ma_type, point.open_ma_source, point.open_ma_length,
        point.close_ma_type, point.close_ma_source, point.close_ma_length,
    )


def patch_merge(base: Path, patch: Path, target: Path, *, workers: int) -> int:
    if target.exists():
        raise ValueError(f"target already exists: {target}")
    filtered = Path(f"{target}.patch-filter.source-v6.duckdb")
    packed = Path(f"{filtered}.packed")
    if filtered.exists():
        raise ValueError(f"temporary patch target already exists: {filtered}")

    base_input, patch_input = _read_input(base), _read_input(patch)
    shifts: dict[tuple[object, ...], set[int]] = defaultdict(set)
    ranges: dict[tuple[object, ...], tuple[int, int]] = {}
    for item in base_input.fragments:
        item_key = _key(item)
        shifts[item_key].add(item.point.shift_bp)
        start, end = ranges.get(item_key, (item.report_start_ms, item.report_end_ms))
        ranges[item_key] = min(start, item.report_start_ms), max(end, item.report_end_ms)
    selected = tuple(
        item for item in patch_input.fragments
        if _key(item) in shifts
        and item.point.shift_bp not in shifts[_key(item)]
        and item.report_start_ms <= ranges[_key(item)][0]
        and item.report_end_ms >= ranges[_key(item)][1]
    )
    if not selected:
        raise ValueError("patch contains no missing shifts that cover the base interval")

    try:
        create_v6_database(filtered)
        _copy_fragments_from_inputs(filtered, selected, {item.fragment_id: patch for item in selected}, None)
        validate_source_v6_database(filtered)
        compact_v6_database(filtered, packed)
        validate_source_v6_database(packed)
        packed.replace(filtered)
        result = merge_source_v6((base, filtered), target, workers=workers)
    finally:
        for path in (filtered, Path(f"{filtered}.wal"), Path(f"{filtered}.tmp"), packed):
            path.unlink(missing_ok=True)
    print(f"COMMITTED {result.target_path} accepted={result.accepted_count} patch_fragments={len(selected)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("patch", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    return patch_merge(args.base.resolve(), args.patch.resolve(), args.target.resolve(), workers=args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
