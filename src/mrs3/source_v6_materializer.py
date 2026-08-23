"""Stage 2 materialization: READY witnesses never filter source facts."""

from __future__ import annotations

from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Sequence

from .source_v6 import SourceV6Fragment, _canonical_json
from .source_v6_coverage import ReadyInterval, canonical_ready_intervals
from .source_v6_stitch import measure_points
from .source_v6_storage import decode_fragment_slice, fragment_metadata, source_content_digest


@dataclass(frozen=True, slots=True)
class MaterializedFactRef:
    """A published fact identified rather than carried (W3).

    The publisher's `source_database` branch copies payload bytes in SQL and
    reads only these two fields from a scope's facts, so hydrating a fragment
    in order to publish it moves megabytes to learn an id.
    """

    fragment_id: str
    point_key: str


@dataclass(frozen=True, slots=True)
class MaterializedScope:
    scope_key: str
    facts: tuple[SourceV6Fragment, ...] | tuple[MaterializedFactRef, ...]
    ready_witness: ReadyInterval
    # W7: one compact row per parameter combination, ordered by `point_id`.
    # Empty on the hydrated path, which does not compute them.
    analysis_input: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedSourceV6:
    source_content_digest: str
    scopes: tuple[MaterializedScope, ...]
    # Parameter combinations that were tested and produced no trades. They stay
    # in their scopes carrying the flat result the tester declared; this records
    # which ones they were — see the empty-result spec, E2 and E3.
    empty_result_points: tuple[dict[str, object], ...] = ()


def _witness_window(witness: ReadyInterval) -> tuple[int, int]:
    """The witness as the half-open millisecond window analysis will apply."""
    start = datetime.combine(witness.start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(witness.end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _scope(fragment: SourceV6Fragment) -> str:
    point = fragment.point
    return f"{point.symbol}|{point.side}|{point.timeframe}"


def analysis_input_row(
    point_key: str, point: object, metrics: object, fragments: Sequence[SourceV6Fragment],
    window: tuple[int, int],
) -> dict[str, object]:
    """The compact per-combination row analysis needs (W7).

    Every field here was already derived while measuring, and analysis derived
    the identical values a second time by decoding the same payloads again. The
    row holds scalars and the independent event ids; the balance and equity
    series stay in the worker, because nothing downstream reads them.

    The witness containment check belongs here rather than in analysis: it needs
    each event's timestamp, which is a fact, and this is the last place a fact
    is in hand.
    """
    start_ms, end_ms = window
    event_times = {
        event.event_id: event.timestamp_ms
        for fragment in fragments for event in fragment.events
    }
    event_ids = tuple(sorted(set(metrics.events)))
    if any(
        event_id not in event_times or not start_ms <= event_times[event_id] < end_ms
        for event_id in event_ids
    ):
        raise ValueError(f"point events are outside READY witness: {point_key}")
    return {
        "point_id": point_key,
        "symbol": point.symbol,
        "side": point.side,
        "timeframe": point.timeframe,
        "shift_bp": point.shift_bp,
        "open_ma": point.open_ma_length,
        "close_ma": point.close_ma_length,
        "pnl_pct": float(metrics.total_pnl_percent),
        "dd_pct": float(metrics.max_equity_drawdown_percent),
        "trades": metrics.total_trades,
        "wins": metrics.win_trades,
        "losses": metrics.loss_trades,
        "win_rate_pct": float(metrics.win_rate_percent),
        "profit_factor": None if metrics.profit_factor is None else float(metrics.profit_factor),
        "event_ids": list(event_ids),
        "event_ids_hash": sha256("|".join(event_ids).encode("utf-8")).hexdigest(),
        "event_mode": "real_independent_events",
    }


def measure_point_group(
    source_database: str, point_key: str, fragment_ids: tuple[str, ...],
    window: tuple[int, int],
) -> tuple[str, tuple[str, ...], dict[str, object] | None, dict[str, object]]:
    """Measure one parameter combination; safe to run in a worker (W2, W7).

    The whole point of the split: the hydrated fragments live and die here, and
    what crosses back is an id list, at most one empty-result record, and the
    compact analysis row. The E1 decision is the same `measure_points` call the
    serial path makes, over the same witness window — E1 is still answered by
    running the real calculation, never by a predicate over metadata.
    """
    fragments = decode_fragment_slice(source_database, fragment_ids)
    measured, empty = measure_points(fragments, {point_key: window})
    row = analysis_input_row(
        point_key, fragments[0].point, measured[point_key], fragments, window
    )
    return point_key, tuple(fragment_ids), (empty[0] if empty else None), row


def _point_groups(
    metadata: Sequence[object], scope_keys: Sequence[str],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """`scope -> point_key -> sorted fragment ids`, from metadata alone (W1)."""
    groups: dict[str, dict[str, list[str]]] = {key: {} for key in scope_keys}
    for item in metadata:
        scope = _scope(item)
        if scope in groups:
            groups[scope].setdefault(item.point.canonical_key, []).append(str(item.fragment_id))
    return {
        scope: {point: tuple(sorted(ids)) for point, ids in sorted(points.items())}
        for scope, points in groups.items()
    }


def materialize_source_v6_from_database(
    source_database: str | Path, scope_keys: Sequence[str], *,
    metadata: Sequence[object] | None = None,
    workers: int = 1,
    source_content_digest_value: str | None = None,
    progress_callback: Callable[[int, int], object] | None = None,
) -> MaterializedSourceV6:
    """Materialize the selected scopes without decoding a payload here (W1--W5).

    Equivalent to `materialize_source_v6` in everything the artifact records —
    ids, order, witnesses, `empty_result_points` — and different only in where
    the work happens. The serial path hydrates every selected fragment into the
    coordinator to compute metrics it then discards; this one keeps each
    combination's fragments inside the worker that measured them.
    """
    requested = tuple(sorted(set(scope_keys)))
    if not requested:
        raise ValueError("at least one scope is required")
    database = str(Path(source_database))
    views = tuple(metadata) if metadata is not None else fragment_metadata(database)
    witnesses = {item.scope_key: item for item in canonical_ready_intervals(views)}
    groups = _point_groups(views, requested)
    tasks: list[tuple[str, str, tuple[str, ...], tuple[int, int]]] = []
    for scope_key in requested:
        witness = witnesses.get(scope_key)
        if witness is None:
            raise ValueError(f"scope is not READY: {scope_key}")
        if not groups[scope_key]:
            raise ValueError(f"scope has no facts: {scope_key}")
        window = _witness_window(witness)
        tasks.extend(
            (scope_key, point_key, ids, window)
            for point_key, ids in groups[scope_key].items()
        )
    verdicts = _run_measurements(database, tasks, workers, progress_callback)
    empty_results = [empty for empty, _row in verdicts.values() if empty is not None]
    scopes = tuple(
        MaterializedScope(
            scope_key,
            tuple(sorted(
                (
                    MaterializedFactRef(fragment_id, point_key)
                    for point_key, ids in groups[scope_key].items()
                    for fragment_id in ids
                ),
                key=lambda item: item.fragment_id,
            )),
            witnesses[scope_key],
            tuple(verdicts[point_key][1] for point_key in groups[scope_key]),
        )
        for scope_key in requested
    )
    # W5: the lineage of the whole database, exactly as the hydrated path.
    return MaterializedSourceV6(
        source_content_digest_value or source_content_digest(str(item.fragment_id) for item in views),
        scopes,
        tuple(sorted(empty_results, key=lambda item: str(item["point_key"]))),
    )


def _run_measurements(
    database: str,
    tasks: Sequence[tuple[str, str, tuple[str, ...], tuple[int, int]]],
    workers: int,
    progress_callback: Callable[[int, int], object] | None,
) -> dict[str, tuple[dict[str, object] | None, dict[str, object]]]:
    """Collect one verdict per combination, order-independently (W4)."""
    total = len(tasks)
    if progress_callback is not None:
        progress_callback(0, total)
    results: dict[str, tuple[tuple[str, ...], dict[str, object] | None, dict[str, object]]] = {}

    def record(verdict: tuple[str, tuple[str, ...], dict[str, object] | None, dict[str, object]]) -> None:
        point_key, ids, empty, row = verdict
        if point_key in results:
            raise ValueError(f"duplicate measurement verdict: {point_key}")
        results[point_key] = (ids, empty, row)
        if progress_callback is not None:
            progress_callback(len(results), total)

    if max(1, int(workers)) < 2 or total < 2:
        for _scope_key, point_key, ids, window in tasks:
            record(measure_point_group(database, point_key, ids, window))
    else:
        with ProcessPoolExecutor(max_workers=min(int(workers), total)) as executor:
            futures = [
                executor.submit(measure_point_group, database, point_key, ids, window)
                for _scope_key, point_key, ids, window in tasks
            ]
            pending = set(futures)
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_EXCEPTION)
                    for future in done:
                        # E4 and every decode failure surface here, cancelling
                        # the rest: no surface file is created.
                        record(future.result())
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    expected = {point_key: ids for _scope_key, point_key, ids, _window in tasks}
    if set(results) != set(expected):
        raise ValueError("measurement verdicts do not cover the submitted combinations")
    for point_key, (ids, _empty, _row) in results.items():
        if ids != expected[point_key]:
            raise ValueError(f"measurement verdict changed the fragment set: {point_key}")
    return {point_key: (empty, row) for point_key, (_ids, empty, row) in results.items()}


def materialize_source_v6(
    fragments: Sequence[SourceV6Fragment], scope_keys: Sequence[str], *,
    source_content_digest_value: str | None = None,
) -> MaterializedSourceV6:
    """Keep each selected scope's complete observed grid beside its READY witness.

    A parameter combination whose fragments carry no wallet or equity sample
    keeps its cell and carries the flat result the tester declared, which is
    what keeps the canonical grid complete. Measuring it here is what stops
    `run_multiscope_analysis` aborting the whole run one stage later, and each
    such combination is recorded on the result rather than passing unnoticed.
    """
    requested = tuple(sorted(set(scope_keys)))
    if not requested:
        raise ValueError("at least one scope is required")
    if any(not isinstance(item, SourceV6Fragment) for item in fragments):
        raise ValueError("materialization requires hydrated fragments, not metadata views")
    witnesses = {item.scope_key: item for item in canonical_ready_intervals(tuple(fragments))}
    result = []
    empty_results: list[dict[str, object]] = []
    for scope_key in requested:
        witness = witnesses.get(scope_key)
        if witness is None:
            raise ValueError(f"scope is not READY: {scope_key}")
        members = tuple(item for item in fragments if _scope(item) == scope_key)
        # Measured against this scope's own READY witness, which is the window
        # `run_multiscope_analysis` will apply — so a combination unusable there
        # is caught here rather than aborting the scope one stage later.
        window = _witness_window(witness)
        # Only the requested scopes are measured: measuring the whole input
        # would write other symbols' combinations into this surface's record.
        _measured, empty = measure_points(members, {
            item.point.canonical_key: window for item in members
        })
        empty_results.extend(empty)
        facts = tuple(sorted(members, key=lambda item: item.fragment_id))
        if not facts:
            raise ValueError(f"scope has no facts: {scope_key}")
        result.append(MaterializedScope(scope_key, facts, witness))
    # The digest stays over the whole input: it is the lineage of what was
    # materialized *from*, not of what the requested scopes hold.
    return MaterializedSourceV6(
        source_content_digest_value or source_content_digest(item.fragment_id for item in fragments),
        tuple(result),
        tuple(sorted(empty_results, key=lambda item: str(item["point_key"]))),
    )
