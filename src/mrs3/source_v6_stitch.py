"""Deterministic overlap ownership and interval metrics for Source v6 facts."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Mapping, Sequence

from .source_v6 import NormalizedAction, NormalizedCycle, NormalizedSample, SourceV6Fragment, _canonical_json


MIN_OVERLAP_HOURS = 96


class SourceV6StitchError(ValueError):
    pass


# Why no sample survived. Only the first may become a flat result: it is the
# only one in which the tester actually reported nothing.
GENUINE_ZERO_ACTIVITY = "GENUINE_ZERO_ACTIVITY"
OPEN_TAIL_FILTER_REMOVED_ALL_DATA = "OPEN_TAIL_FILTER_REMOVED_ALL_DATA"
WINDOW_EXCLUDES_MEASURABLE_DATA = "WINDOW_EXCLUDES_MEASURABLE_DATA"
SEAM_OR_CYCLE_RECONSTRUCTION_FAILURE = "SEAM_OR_CYCLE_RECONSTRUCTION_FAILURE"


class SourceV6EmptySeriesError(SourceV6StitchError):
    """No wallet/equity sample survives to be measured, and why.

    Its own type because callers need to tell "this parameter combination
    cannot be measured" from every other stitch failure without matching on a
    message. Whether a combination is measurable depends on visibility
    filtering, seam boundaries, tail truncation and the selected window, so the
    only exact test is attempting the calculation.

    `reason` exists because the causes are not interchangeable and treating them
    as one published a corpus of false zeros: every combination of two real
    surfaces was declared idle, while each fragment held thousands of samples,
    hundreds of events and hundreds of closed cycles. An emptied series is not
    an idle bot. `diagnostic` carries what the fragments actually held, so a
    rejection can say that instead of "0 candidates".
    """

    def __init__(
        self, message: str, *, reason: str = SEAM_OR_CYCLE_RECONSTRUCTION_FAILURE,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.diagnostic = dict(diagnostic or {})


@dataclass(frozen=True, slots=True)
class Ownership:
    fragment_id: str
    incoming_start_ms: int
    overlap_hours: Decimal
    status: str
    reason: str | None = None
    boundary_ms: int | None = None


@dataclass(frozen=True, slots=True)
class BatchResolution:
    status: str
    active_fragments: tuple[SourceV6Fragment, ...]
    decisions: tuple[Ownership, ...]


@dataclass(frozen=True, slots=True)
class BridgeFacts:
    cycle_ids: tuple[str, ...]
    action_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    sample_timestamps: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RoundTrip:
    round_trip_id: str
    timestamp_ms: int
    entry_action_ids: tuple[str, ...]
    realizing_action_ids: tuple[str, ...]
    realized_pnl: Decimal
    realized_size: Decimal
    peak_position_size: Decimal

    @property
    def weighted_trades(self) -> Decimal:
        return self.realized_size / self.peak_position_size if self.peak_position_size else Decimal("0")


@dataclass(frozen=True, slots=True)
class CanonicalMetrics:
    total_pnl: Decimal
    total_pnl_percent: Decimal
    profit_factor: Decimal | None
    total_trades: int
    win_trades: int
    loss_trades: int
    win_rate_percent: Decimal
    max_equity_drawdown: Decimal
    max_equity_drawdown_percent: Decimal
    max_realized_drawdown: Decimal
    max_realized_drawdown_percent: Decimal
    balance_series: tuple[NormalizedSample, ...]
    equity_series: tuple[NormalizedSample, ...]
    events: tuple[str, ...]
    period_metrics: tuple["PeriodMetrics", ...] = ()
    weighted_trades: Decimal = Decimal("0")
    round_trip_ids: tuple[str, ...] = ()
    round_trips: tuple[RoundTrip, ...] = ()
    max_equity_drawdown_source: str = "SERIES"
    max_equity_drawdown_declared_fragment_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PeriodMetrics:
    fragment_id: str
    anchor_balance: Decimal
    final_balance: Decimal
    total_pnl: Decimal
    total_pnl_percent: Decimal
    profit_factor: Decimal | None
    balance_series: tuple[NormalizedSample, ...]
    equity_series: tuple[NormalizedSample, ...]
    max_realized_drawdown: Decimal
    max_equity_drawdown: Decimal
    max_realized_drawdown_percent: Decimal
    max_equity_drawdown_percent: Decimal


def owner_for_timestamp(timestamp_ms: int, outgoing: SourceV6Fragment, incoming: SourceV6Fragment) -> str | None:
    """Return exact fact ownership; UTC-day labels are never used here."""
    if timestamp_ms < outgoing.report_end_ms:
        return outgoing.fragment_id if outgoing.report_start_ms <= timestamp_ms < outgoing.report_end_ms else None
    return incoming.fragment_id if incoming.report_start_ms <= timestamp_ms < incoming.report_end_ms else None


def _seam_cycle_sets(outgoing: SourceV6Fragment, incoming: SourceV6Fragment) -> tuple[set[str], set[str], set[str]]:
    """Return incoming excluded cycles, retained boundary cycles and old open cycles."""
    boundary = outgoing.report_end_ms
    excluded = {
        cycle.cycle_id
        for cycle in incoming.cycles
        if cycle.open_timestamp_ms < boundary and (not cycle.closed or cycle.close_timestamp_ms < boundary)
    }
    retained = {
        cycle.cycle_id
        for cycle in incoming.cycles
        if cycle.cycle_id not in excluded
    }
    old_open = {cycle.cycle_id for cycle in outgoing.cycles if cycle.open_timestamp_ms < boundary and not cycle.closed}
    return excluded, retained, old_open


def _batch_order_key(fragment: object) -> tuple[int, bool, str]:
    """Batch order: by start, then facts before emptiness, then id.

    `persist_batch_resolution` re-derives the outgoing side by bisecting its own
    ordering, so it must sort by exactly this key. If the two drift apart, a
    decision computed against one outgoing fragment is persisted against
    another whenever two fragments share a start.
    """
    return (
        fragment.report_start_ms,
        not _carries_facts(fragment),
        fragment.fragment_id,
    )


def _carries_facts(fragment: object) -> bool:
    """Whether this fragment holds any fact at all (ADR-0016, Z5).

    `resolve_batch` accepts `SourceV6FragmentMetadata` as well as decoded
    fragments — the merge passes metadata for a single-member group and decodes
    only when a group has more than one. Metadata carries no fact collections,
    so emptiness cannot be determined from it; that case answers `True`, which
    is the behaviour that predates this rule. It is never the deciding answer:
    both places the rule changes an outcome — the seam in `resolve_ownership`
    and the tie-break below — are reached only with two or more members, and the
    merge has decoded by then.
    """
    actions = getattr(fragment, "actions", None)
    if actions is None:
        return True
    return bool(actions or fragment.cycles or fragment.events)


def resolve_ownership(outgoing: SourceV6Fragment, incoming: SourceV6Fragment, *, min_overlap_hours: int = MIN_OVERLAP_HOURS) -> Ownership:
    if outgoing.point != incoming.point:
        return Ownership(incoming.fragment_id, incoming.report_start_ms, Decimal("0"), "UNRESOLVED", "INCOMPATIBLE_POINT")
    if outgoing.settings_fingerprint != incoming.settings_fingerprint:
        return Ownership(incoming.fragment_id, incoming.report_start_ms, Decimal("0"), "UNRESOLVED", "INCOMPATIBLE_CONTRACT")
    if outgoing.stitchability != "STITCHABLE_FIXED_LOT" or incoming.stitchability != "STITCHABLE_FIXED_LOT":
        return Ownership(incoming.fragment_id, incoming.report_start_ms, Decimal("0"), "UNRESOLVED", "NON_STITCHABLE")
    overlap_start = max(outgoing.report_start_ms, incoming.report_start_ms)
    overlap_end = min(outgoing.report_end_ms, incoming.report_end_ms)
    overlap_ms = max(0, overlap_end - overlap_start)
    overlap_hours = Decimal(overlap_ms) / Decimal(3_600_000)
    if overlap_hours < Decimal(min_overlap_hours):
        return Ownership(incoming.fragment_id, incoming.report_start_ms, overlap_hours, "UNRESOLVED", "OVERLAP_BELOW_MINIMUM")
    if overlap_end <= overlap_start:
        return Ownership(incoming.fragment_id, incoming.report_start_ms, overlap_hours, "UNRESOLVED", "NO_OVERLAP")
    if not _carries_facts(incoming) and any(not cycle.closed for cycle in outgoing.cycles):
        # ADR-0016 Z5, mirrored. Seam exclusion drops the outgoing's open tail
        # because the incoming is assumed to carry its continuation; an empty
        # incoming carries nothing, so the tail would be deleted with nothing
        # replacing it. ADR-0010 already names this outcome: "a fragment whose
        # start cannot cover the outgoing tail cycle is marked
        # BRIDGE_NOT_COVERED ... contributes to PARTIAL". An UNRESOLVED decision
        # writes no fact rows, so the outgoing keeps everything, and
        # `calculate_metrics` already understands this reason.
        return Ownership(
            incoming.fragment_id, incoming.report_start_ms, overlap_hours,
            "UNRESOLVED", "BRIDGE_NOT_COVERED",
        )
    if not _carries_facts(outgoing):
        # ADR-0016 Z5. Seam exclusion exists to stop the same fact being counted
        # twice: ADR-0013 measured 1,121 matching closed cycles across 681
        # overlapping pairs, and gives the overlap to the old report because it
        # already carries them. A zero-activity outgoing carries nothing, so the
        # premise is absent and excluding the incoming's overlap cycles would
        # delete evidence that nothing replaces — silently, under a COMMITTED
        # batch. Hand over at the incoming's own start instead, which is the
        # existing non-seam rule and keeps every incoming fact.
        return Ownership(
            incoming.fragment_id, incoming.report_start_ms, overlap_hours,
            "RESOLVED", "EMPTY_OUTGOING_NOTHING_TO_EXCLUDE",
        )
    return Ownership(incoming.fragment_id, outgoing.report_end_ms, overlap_hours, "USE_OLD_WITH_SEAM_EXCLUSION", "INCOMPLETE_SEAM_CYCLE_EXCLUDED", outgoing.report_end_ms)


EMPTY_RESULT_REASON = "NO_WALLET_OR_EQUITY_SAMPLES"


def _fragment_diagnostic(fragments: Sequence[SourceV6Fragment]) -> dict[str, object]:
    """What the fragments held, for a rejection that can name it."""
    return {
        "fragments": len(fragments),
        "wallet_samples": sum(len(item.wallet_samples) for item in fragments),
        "equity_samples": sum(len(item.equity_samples) for item in fragments),
        "actions": sum(len(item.actions) for item in fragments),
        "events": sum(len(item.events) for item in fragments),
        "closed_cycles": sum(1 for item in fragments for cycle in item.cycles if cycle.closed),
        "open_cycles": sum(1 for item in fragments for cycle in item.cycles if not cycle.closed),
    }


def _empty_series_error(
    fragments: Sequence[SourceV6Fragment], *, windowed: bool
) -> SourceV6EmptySeriesError:
    """Name the cause, because only one of them may become a flat result.

    Decided from the facts rather than from where the emptiness was noticed: a
    combination that reported nothing has no samples to begin with, while one
    emptied by the open-tail cutoff has plenty. The corpus published on
    2026-08-22 was entirely the second kind and was recorded as the first.
    """
    diagnostic = _fragment_diagnostic(fragments)
    if not diagnostic["wallet_samples"] and not diagnostic["equity_samples"]:
        return SourceV6EmptySeriesError(
            "wallet/equity series are required",
            reason=GENUINE_ZERO_ACTIVITY, diagnostic=diagnostic,
        )
    cutoff = min(
        (
            cycle.open_timestamp_ms
            for item in fragments for cycle in item.cycles
            if not cycle.closed and cycle.open_timestamp_ms < item.report_end_ms
        ),
        default=None,
    )
    earliest = min(
        (sample.timestamp_ms for item in fragments for sample in item.wallet_samples),
        default=None,
    )
    if cutoff is not None and earliest is not None and cutoff <= earliest:
        return SourceV6EmptySeriesError(
            "the open-tail cutoff hides every wallet/equity sample",
            reason=OPEN_TAIL_FILTER_REMOVED_ALL_DATA,
            diagnostic={**diagnostic, "open_tail_cutoff_ms": cutoff, "first_sample_ms": earliest},
        )
    if windowed:
        return SourceV6EmptySeriesError(
            "the selected window hides every wallet/equity sample",
            reason=WINDOW_EXCLUDES_MEASURABLE_DATA, diagnostic=diagnostic,
        )
    return SourceV6EmptySeriesError(
        "wallet/equity series are required",
        reason=SEAM_OR_CYCLE_RECONSTRUCTION_FAILURE, diagnostic=diagnostic,
    )


def flat_result_metrics() -> CanonicalMetrics:
    """The metrics of a parameter combination that was tested and never traded.

    Not invented. The tester states them and the importer already verified they
    agree with each other before admitting the report (ADR-0016, Z1): total PnL
    zero, gross profit and loss zero, drawdown zero, and the balance unchanged
    from first to last. What is genuinely undefined is the ratio set, and
    ADR-0006 settled that those stay `None` rather than becoming a number.

    Keeping the combination measurable is what keeps the canonical grid whole.
    Removing it published a 113-of-114 grid that the pipeline adapter then
    rejected with `INCOMPLETE_GRID`, naming neither the reason nor the cell.
    """
    zero = Decimal("0")
    return CanonicalMetrics(
        total_pnl=zero,
        total_pnl_percent=zero,
        profit_factor=None,
        total_trades=0,
        win_trades=0,
        loss_trades=0,
        win_rate_percent=zero,
        max_equity_drawdown=zero,
        max_equity_drawdown_percent=zero,
        max_realized_drawdown=zero,
        max_realized_drawdown_percent=zero,
        balance_series=(),
        equity_series=(),
        events=(),
    )


def measure_points(
    fragments: Sequence[SourceV6Fragment],
    intervals: dict[str, tuple[int, int]] | None = None,
) -> tuple[dict[str, CanonicalMetrics], list[dict[str, object]]]:
    """Measure every parameter combination, including those that never traded.

    Measurability is decided by attempting the calculation rather than by a
    predicate mirroring it. `calculate_metrics` merges the series, filters them
    for visibility, drops samples at or after an open tail, drops a later
    fragment's pre-boundary samples, may truncate on `BRIDGE_NOT_COVERED`, and
    applies the selected window — all before it checks that anything remains.
    Two attempts to restate that as a predicate were wrong; the calculation is
    the only exact answer.

    A combination that never traded gets `flat_result_metrics` and stays in the
    grid. Every other way of ending up with no samples raises, naming the
    combination and its reason: a window that hides real data, an open tail that
    hides it, or a seam that could not be resolved. Collapsing those into a flat
    result published two entire surfaces of zeros over real trading histories.

    The metrics are returned, so deciding measurability and measuring are the
    same pass.
    """
    by_point: dict[str, list[SourceV6Fragment]] = {}
    for fragment in fragments:
        by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
    measured: dict[str, CanonicalMetrics] = {}
    empty: list[dict[str, object]] = []
    for point_key in sorted(by_point):
        members = tuple(by_point[point_key])
        window = (intervals or {}).get(point_key)
        try:
            measured[point_key] = (
                calculate_metrics(members, start_ms=window[0], end_ms=window[1])
                if window
                else calculate_metrics(members)
            )
            continue
        except SourceV6EmptySeriesError as error:
            # Only one cause may become a flat result. Treating them as one
            # published two whole surfaces of false zeros: every combination was
            # recorded as idle while its fragments held thousands of samples and
            # hundreds of closed cycles.
            if error.reason != GENUINE_ZERO_ACTIVITY:
                raise SourceV6EmptySeriesError(
                    f"{error} for {point_key}",
                    reason=error.reason, diagnostic=error.diagnostic,
                ) from error
        # Reaching here means `GENUINE_ZERO_ACTIVITY`: no fragment held a
        # sample to begin with. Telling that from a windowing artefact used to
        # need a second, unwindowed measurement of every empty combination;
        # `_empty_series_error` now decides it from the facts in one pass.
        measured[point_key] = flat_result_metrics()
        empty.append({
            "point_key": point_key,
            "fragment_ids": sorted(item.fragment_id for item in members),
            "reason": EMPTY_RESULT_REASON,
        })
    return measured, empty


def resolve_batch(fragments: Sequence[SourceV6Fragment], *, min_overlap_hours: int = MIN_OVERLAP_HOURS) -> BatchResolution:
    unique: dict[str, SourceV6Fragment] = {}
    for fragment in fragments:
        unique.setdefault(fragment.fragment_id, fragment)
    # ADR-0016 Z5: the middle term only breaks ties between fragments sharing a
    # start, and only when one of them has no facts. `active` is seeded with
    # `ordered[0]`, so without it an empty fragment could take a window from a
    # fragment carrying real actions purely because its id sorted lower, and the
    # fragment with the data would be the one reported `AMBIGUOUS_INCOMING`.
    # Among fragments that all carry facts the term is constant and the order is
    # unchanged.
    ordered = tuple(sorted(unique.values(), key=_batch_order_key))
    if not ordered:
        raise SourceV6StitchError("at least one fragment is required")
    active = [ordered[0]]
    decisions: list[Ownership] = []
    unresolved = False
    for incoming in ordered[1:]:
        ambiguous = [candidate for candidate in ordered if candidate is not incoming and candidate.point == incoming.point and candidate.report_start_ms == incoming.report_start_ms and candidate.report_end_ms == incoming.report_end_ms]
        if ambiguous:
            decision = Ownership(incoming.fragment_id, incoming.report_start_ms, Decimal("0"), "UNRESOLVED", "AMBIGUOUS_INCOMING")
            decisions.append(decision)
            unresolved = True
            continue
        decision = resolve_ownership(active[-1], incoming, min_overlap_hours=min_overlap_hours)
        decisions.append(decision)
        if decision.status in {"RESOLVED", "USE_OLD_WITH_SEAM_EXCLUSION"}:
            active.append(incoming)
        else:
            unresolved = True
    return BatchResolution("PARTIAL" if unresolved and active else ("UNRESOLVED" if unresolved else "COMMITTED"), tuple(active), tuple(decisions))


def select_bridge_facts(outgoing: SourceV6Fragment, incoming: SourceV6Fragment) -> BridgeFacts:
    """Select the complete incoming fact set for tails opened by outgoing."""
    tail_orders = {cycle.order_id for cycle in outgoing.cycles if not cycle.closed}
    bridge_cycles = [cycle for cycle in incoming.cycles if cycle.order_id in tail_orders and cycle.closed]
    cycle_ids = tuple(cycle.cycle_id for cycle in bridge_cycles)
    action_ids = tuple(action_id for cycle in bridge_cycles for action_id in cycle.action_ids)
    event_ids = tuple(event.event_id for event in incoming.events if event.action_id in set(action_ids))
    if tail_orders and not bridge_cycles:
        raise SourceV6StitchError("BRIDGE_NOT_COVERED")
    sample_timestamps = tuple(sorted({sample.timestamp_ms for sample in incoming.wallet_samples if sample.timestamp_ms >= incoming.report_start_ms}))
    return BridgeFacts(cycle_ids, action_ids, event_ids, sample_timestamps)


def persist_resolution(
    database: str,
    outgoing: SourceV6Fragment,
    incoming: SourceV6Fragment,
    *,
    status: str,
    reason: str | None = None,
) -> BridgeFacts | None:
    """Persist one resolver decision and exact fact ownership atomically.

    The resolver remains pure; this explicit boundary is called only after
    both fragments have been committed to v6 storage.  It records every
    timestamped action/sample/event owner and the bridge cycle's complete
    incoming membership before updating fragment/day activation state.
    """
    from .source_v6_storage import persist_fragment_resolutions

    bridge, request = _resolution_request(outgoing, incoming, status=status, reason=reason)
    persist_fragment_resolutions(database, (request,))
    return bridge


def _resolution_request(
    outgoing: SourceV6Fragment,
    incoming: SourceV6Fragment,
    *,
    status: str,
    reason: str | None = None,
) -> tuple[BridgeFacts | None, dict[str, object]]:
    """Compute one resolver decision's fact rows without touching the database."""
    bridge = select_bridge_facts(outgoing, incoming) if status == "RESOLVED" else None
    boundary = outgoing.report_end_ms
    excluded_cycles, retained_cycles, old_open_cycles = _seam_cycle_sets(outgoing, incoming)
    incoming_cycles = {cycle.cycle_id: cycle for cycle in incoming.cycles}
    incoming_actions = {action.action_id: action for action in incoming.actions}
    excluded_action_ids = {
        action_id
        for cycle_id in excluded_cycles
        for action_id in incoming_cycles[cycle_id].action_ids
    }
    old_open_action_ids = {
        action_id
        for cycle_id in old_open_cycles
        for action_id in next((cycle.action_ids for cycle in outgoing.cycles if cycle.cycle_id == cycle_id), ())
    }
    fact_rows: list[tuple[str, str, str, str | None, bool, str | None, str | None]] = []
    seam_status = status == "USE_OLD_WITH_SEAM_EXCLUSION"
    # On the non-seam path no seam exclusion happened, so labelling a row with
    # `INCOMPLETE_SEAM_CYCLE_EXCLUDED` would be false audit evidence. The
    # handover there is at the incoming's own start.
    inactive_reason = "INCOMPLETE_SEAM_CYCLE_EXCLUDED" if seam_status else "OUTSIDE_OWNED_INTERVAL"
    for action in outgoing.actions:
        active = action.timestamp_ms < boundary and (action.action_id not in old_open_action_ids if seam_status else action.timestamp_ms < incoming.report_start_ms)
        fact_rows.append(("action", action.action_id, outgoing.fragment_id, outgoing.fragment_id, active, None if active else inactive_reason, None))
    for event in outgoing.events:
        active = event.timestamp_ms < boundary and (event.action_id not in old_open_action_ids if seam_status else event.timestamp_ms < incoming.report_start_ms)
        fact_rows.append(("event", event.event_id, outgoing.fragment_id, outgoing.fragment_id, active, None if active else inactive_reason, None))
    for cycle in outgoing.cycles:
        active = cycle.open_timestamp_ms < boundary and (cycle.cycle_id not in old_open_cycles if seam_status else True)
        fact_rows.append(("cycle", cycle.cycle_id, outgoing.fragment_id, outgoing.fragment_id, active, None if active else inactive_reason, None))
    for action in incoming.actions:
        if seam_status:
            active = action.action_id not in excluded_action_ids and (action.timestamp_ms >= boundary or action.action_id in {item for cycle_id in retained_cycles for item in incoming_cycles[cycle_id].action_ids})
        else:
            active = action.timestamp_ms >= incoming.report_start_ms
        fact_rows.append(("action", action.action_id, incoming.fragment_id, incoming.fragment_id, active, None if active else inactive_reason, None))
    active_incoming_action_ids = {
        row[1]
        for row in fact_rows
        if row[0] == "action" and row[2] == incoming.fragment_id and row[4]
    }
    for event in incoming.events:
        action = incoming_actions.get(event.action_id)
        active = bool(action and action.action_id in active_incoming_action_ids) if seam_status else event.timestamp_ms >= incoming.report_start_ms
        fact_rows.append(("event", event.event_id, incoming.fragment_id, incoming.fragment_id, active, None if active else inactive_reason, None))
    for cycle in incoming.cycles:
        active = cycle.cycle_id not in excluded_cycles if seam_status else cycle.closed
        fact_rows.append(("cycle", cycle.cycle_id, incoming.fragment_id, incoming.fragment_id, active, None if active else inactive_reason, None))
    for fragment, series in ((outgoing, "wallet"), (outgoing, "equity"), (incoming, "wallet"), (incoming, "equity")):
        owned = [f"{series}:{sample.timestamp_ms}" for sample in getattr(fragment, f"{series}_samples") if (fragment is outgoing and (sample.timestamp_ms < boundary if seam_status else sample.timestamp_ms < incoming.report_start_ms)) or (fragment is incoming and sample.timestamp_ms >= (boundary if seam_status else incoming.report_start_ms))]
        fact_rows.extend((f"{series}_sample", fact_id, fragment.fragment_id, fragment.fragment_id, True, None, None) for fact_id in owned)
    excluded_event_ids = [event.event_id for event in incoming.events if event.action_id in excluded_action_ids]
    old_cutoff = min((cycle.open_timestamp_ms for cycle in outgoing.cycles if cycle.cycle_id in old_open_cycles), default=boundary)
    old_anchors = [sample for sample in outgoing.wallet_samples if sample.timestamp_ms < old_cutoff]
    retained_boundary = [cycle for cycle in incoming.cycles if cycle.cycle_id in retained_cycles and cycle.open_timestamp_ms < boundary]
    new_cutoff = min((cycle.open_timestamp_ms for cycle in retained_boundary), default=boundary)
    new_anchors = [sample for sample in incoming.wallet_samples if sample.timestamp_ms < new_cutoff]
    correction = sum((incoming_cycles[cycle_id].realized_pnl - incoming_cycles[cycle_id].fees for cycle_id in excluded_cycles), Decimal("0"))
    evidence = json.dumps({
        "boundary_ms": boundary,
        "excluded_cycle_ids": sorted(excluded_cycles),
        "excluded_action_ids": sorted(excluded_action_ids),
        "excluded_event_ids": sorted(excluded_event_ids),
        "retained_boundary_cycle_ids": sorted(cycle.cycle_id for cycle in retained_boundary),
        "old_open_cycle_ids": sorted(old_open_cycles),
        "old_anchor_timestamp_ms": old_anchors[-1].timestamp_ms if old_anchors else None,
        "new_anchor_timestamp_ms": new_anchors[-1].timestamp_ms if new_anchors else None,
        "excluded_net_effect": str(correction),
    }, sort_keys=True, separators=(",", ":"))
    return bridge, {
        "outgoing_fragment_id": outgoing.fragment_id,
        "incoming_fragment_id": incoming.fragment_id,
        "status": status,
        "fact_rows": fact_rows,
        "reason": reason,
        "boundary_ms": boundary if seam_status else None,
        "evidence_json": evidence if seam_status else None,
    }


def persist_batch_resolution(database: str, fragments: Sequence[SourceV6Fragment], resolution: BatchResolution) -> BatchResolution:
    """Apply every adjacent batch decision in one transaction after import."""
    from .source_v6_storage import persist_fragment_resolutions

    by_id = {fragment.fragment_id: fragment for fragment in fragments}
    # Ordered by the same key `resolve_batch` used, so the outgoing side is the
    # last entry starting before the incoming one — and is the same fragment the
    # decision was computed against.
    ordered_active = sorted(resolution.active_fragments, key=_batch_order_key)
    starts = [fragment.report_start_ms for fragment in ordered_active]
    requests: list[dict[str, object]] = []
    for decision in resolution.decisions:
        incoming = by_id[decision.fragment_id]
        index = bisect_left(starts, incoming.report_start_ms)
        if index == 0:
            continue
        outgoing = ordered_active[index - 1]
        _bridge, request = _resolution_request(
            outgoing, incoming, status=decision.status, reason=decision.reason
        )
        requests.append(request)
    if requests:
        persist_fragment_resolutions(database, requests)
    return resolution


def _merge_samples(
    fragments: Sequence[SourceV6Fragment],
    attr: str,
    *,
    shared_seam_offsets: list[Decimal] | None = None,
) -> tuple[NormalizedSample, ...]:
    if not fragments:
        return ()
    offset = Decimal("0")
    merged: dict[int, NormalizedSample] = {}
    for index, fragment in enumerate(fragments):
        samples: Sequence[NormalizedSample] = getattr(fragment, attr)
        if not samples:
            continue
        if index:
            previous = getattr(fragments[index - 1], attr)
            if previous:
                outgoing_overlap_timestamps = [timestamp for timestamp in merged if fragment.report_start_ms <= timestamp < fragment.report_end_ms]
                if not outgoing_overlap_timestamps:
                    raise SourceV6StitchError(f"{attr} has no outgoing sample in the overlap seam")
                seam_timestamp = max(outgoing_overlap_timestamps)
                overlap = [sample for sample in samples if fragment.report_start_ms <= sample.timestamp_ms <= seam_timestamp]
                if not overlap:
                    raise SourceV6StitchError(f"{attr} has no sample in the overlap seam")
                exact = [sample for sample in overlap if sample.timestamp_ms == seam_timestamp]
                anchor = exact[-1] if exact else min(overlap, key=lambda sample: (abs(sample.timestamp_ms - seam_timestamp), sample.timestamp_ms))
                seam_offset = merged[seam_timestamp].value - (anchor.value + offset)
                if shared_seam_offsets is not None and attr == "equity_samples":
                    seam_offset = shared_seam_offsets[index - 1]
                elif shared_seam_offsets is not None and attr == "wallet_samples":
                    shared_seam_offsets.append(seam_offset)
                offset += seam_offset
                for timestamp in tuple(merged):
                    if timestamp >= fragment.report_start_ms:
                        del merged[timestamp]
        for sample in samples:
            timestamp = sample.timestamp_ms
            if index and timestamp < fragment.report_start_ms:
                continue
            rebased = sample.value + offset
            merged[timestamp] = NormalizedSample(timestamp, rebased, sample.upnl)
    return tuple(NormalizedSample(timestamp, merged[timestamp].value, merged[timestamp].upnl) for timestamp in sorted(merged))


def _drawdown(series: Sequence[NormalizedSample]) -> tuple[Decimal, Decimal]:
    maximum, peak_value = _drawdown_details(series)
    return maximum, (maximum / peak_value * Decimal("100")) if peak_value else Decimal("0")


def _drawdown_details(series: Sequence[NormalizedSample]) -> tuple[Decimal, Decimal]:
    if not series:
        raise SourceV6StitchError("required metric series is empty")
    peak = series[0].value
    maximum = Decimal("0")
    peak_value = peak
    for sample in series:
        if sample.value > peak:
            peak = sample.value
        drawdown = peak - sample.value
        if drawdown > maximum:
            maximum = drawdown
            peak_value = peak
    return maximum, peak_value


def _round_trip_peak(
    action_ids: Sequence[str], actions_by_id: Mapping[str, NormalizedAction],
    cycles_by_action: Mapping[str, NormalizedCycle],
) -> Decimal:
    """Return the largest observed position size for the involved position."""
    cycle_ids = {cycles_by_action[action_id].cycle_id for action_id in action_ids if action_id in cycles_by_action}
    relevant = [
        action for action in actions_by_id.values()
        if action.action_id in action_ids
        or (cycles_by_action.get(action.action_id) is not None and cycles_by_action[action.action_id].cycle_id in cycle_ids)
    ]
    position = Decimal("0")
    peak = Decimal("0")
    for action in sorted(relevant, key=lambda item: (item.timestamp_ms, item.action_id)):
        if action.post_size is not None:
            position = abs(action.post_size)
        elif action.size is not None:
            amount = abs(action.size)
            if action.action in {"opened", "increased"}:
                position += amount
            else:
                position = max(Decimal("0"), position - amount)
        peak = max(peak, position)
    return peak


def derive_round_trips(
    actions: Sequence[NormalizedAction], cycles: Sequence[NormalizedCycle], point_key: str,
) -> tuple[RoundTrip, ...]:
    """Build deterministic entry-run/realisation-run decisions from filtered facts."""
    ordered = tuple(sorted(actions, key=lambda item: (item.timestamp_ms, item.action_id)))
    actions_by_id = {item.action_id: item for item in ordered}
    cycles_by_action = {
        action_id: cycle
        for cycle in cycles
        for action_id in cycle.action_ids
        if action_id in actions_by_id
    }
    result: list[RoundTrip] = []
    entries: list[NormalizedAction] = []
    realizing: list[NormalizedAction] = []

    def finish() -> None:
        if not entries or not realizing:
            return
        entry_ids = tuple(item.action_id for item in entries)
        realizing_ids = tuple(item.action_id for item in realizing)
        realized_pnl = sum((item.pnl for item in realizing), Decimal("0"))
        realized_size = sum((abs(item.size) for item in realizing if item.size is not None), Decimal("0"))
        peak = _round_trip_peak((*entry_ids, *realizing_ids), actions_by_id, cycles_by_action)
        payload = {
            "version": "source-v6-round-trip-v1",
            "point_key": point_key,
            "entry_action_ids": list(entry_ids),
            "realizing_action_ids": list(realizing_ids),
        }
        result.append(RoundTrip(
            sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
            realizing[0].timestamp_ms, entry_ids, realizing_ids, realized_pnl, realized_size, peak,
        ))

    for action in ordered:
        if action.action in {"opened", "increased"}:
            if realizing:
                finish()
                entries, realizing = [], []
            entries.append(action)
        elif action.action in {"decreased", "closed"} and entries:
            realizing.append(action)
    finish()
    return tuple(result)


def calculate_metrics(
    fragments: Sequence[SourceV6Fragment],
    *,
    truncate_unresolved_tail: bool = True,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> CanonicalMetrics:
    if not fragments:
        raise SourceV6StitchError("at least one fragment is required")
    if start_ms is not None and end_ms is not None and end_ms <= start_ms:
        raise SourceV6StitchError("selected interval must be non-empty")
    if any(fragment.point != fragments[0].point for fragment in fragments[1:]):
        raise SourceV6StitchError("canonical metrics require one point identity")
    if any(fragment.stitchability != "STITCHABLE_FIXED_LOT" for fragment in fragments):
        raise SourceV6StitchError("non-stitchable fragment cannot form canonical metrics")
    ordered = tuple(sorted(fragments, key=lambda fragment: (fragment.report_start_ms, fragment.fragment_id)))
    admitted: list[SourceV6Fragment] = [ordered[0]]
    for index in range(1, len(ordered)):
        decision = resolve_ownership(admitted[-1], ordered[index])
        if decision.status not in {"RESOLVED", "USE_OLD_WITH_SEAM_EXCLUSION"}:
            if truncate_unresolved_tail and decision.reason == "BRIDGE_NOT_COVERED":
                break
            raise SourceV6StitchError(f"cannot calculate metrics for unresolved seam: {decision.reason}")
        admitted.append(ordered[index])
    ordered = tuple(admitted)

    seam_boundaries = [ordered[index - 1].report_end_ms for index in range(1, len(ordered))]
    excluded_by_fragment: list[set[str]] = [set()]
    retained_by_fragment: list[set[str]] = [set()]
    old_open_by_fragment: list[set[str]] = []
    for index, fragment in enumerate(ordered):
        old_open_by_fragment.append({cycle.cycle_id for cycle in fragment.cycles if not cycle.closed and cycle.open_timestamp_ms < fragment.report_end_ms})
        if index:
            excluded, retained, _ = _seam_cycle_sets(ordered[index - 1], fragment)
            excluded_by_fragment.append(excluded)
            retained_by_fragment.append(retained)
    adjusted_views: list[dict[str, tuple[NormalizedSample, ...]]] = []
    prior_corrections: list[tuple[int, Decimal]] = []
    for index, fragment in enumerate(ordered):
        current_corrections = [(cycle.close_timestamp_ms, cycle.realized_pnl - cycle.fees) for cycle in fragment.cycles if cycle.cycle_id in excluded_by_fragment[index] and cycle.close_timestamp_ms is not None]
        corrections = prior_corrections + current_corrections
        prior_corrections.extend(current_corrections)
        views: dict[str, tuple[NormalizedSample, ...]] = {}
        for attr in ("wallet_samples", "equity_samples"):
            adjusted: list[NormalizedSample] = []
            for sample in getattr(fragment, attr):
                value = sample.value
                for close_timestamp, net_effect in corrections:
                    if sample.timestamp_ms >= close_timestamp:
                        value -= net_effect
                adjusted.append(NormalizedSample(sample.timestamp_ms, value, sample.upnl))
            views[attr] = tuple(adjusted)
        adjusted_views.append(views)

    visible_views: list[dict[str, tuple[NormalizedSample, ...]]] = []
    for index, fragment in enumerate(ordered):
        if index == 0:
            cutoff = min((cycle.open_timestamp_ms for cycle in fragment.cycles if cycle.cycle_id in old_open_by_fragment[index]), default=None)
            visible = {attr: tuple(sample for sample in adjusted_views[index][attr] if cutoff is None or sample.timestamp_ms < cutoff) for attr in ("wallet_samples", "equity_samples")}
        else:
            boundary = seam_boundaries[index - 1]
            visible = {attr: tuple(sample for sample in adjusted_views[index][attr] if sample.timestamp_ms >= boundary) for attr in ("wallet_samples", "equity_samples")}
        visible_views.append(visible)

    seam_deltas: list[Decimal] = []

    def merge(attr: str) -> tuple[NormalizedSample, ...]:
        offset = Decimal("0")
        merged: dict[int, NormalizedSample] = {}
        for index, fragment in enumerate(ordered):
            samples = visible_views[index][attr]
            if index and samples:
                previous = ordered[index - 1]
                candidates = [timestamp for timestamp in merged if previous.report_start_ms <= timestamp < previous.report_end_ms]
                if not candidates:
                    candidates = list(merged)
                if not candidates:
                    raise SourceV6StitchError(f"{attr} has no outgoing sample in the overlap seam")
                seam_timestamp = max(candidates)
                relevant_cycles = [cycle for cycle in fragment.cycles if cycle.open_timestamp_ms < seam_boundaries[index - 1]]
                stop = min((cycle.open_timestamp_ms for cycle in relevant_cycles), default=seam_boundaries[index - 1])
                anchors = [sample for sample in adjusted_views[index][attr] if sample.timestamp_ms < stop]
                if not anchors:
                    anchors = [sample for sample in adjusted_views[index][attr] if sample.timestamp_ms < seam_boundaries[index - 1]]
                if not anchors:
                    anchors = list(adjusted_views[index][attr])
                if not anchors:
                    raise SourceV6StitchError(f"{attr} has no incoming sample in the overlap seam")
                anchor = anchors[-1]
                seam_delta = merged[seam_timestamp].value - (anchor.value + offset)
                if attr == "equity_samples" and index - 1 < len(seam_deltas):
                    seam_delta = seam_deltas[index - 1]
                elif attr == "wallet_samples":
                    seam_deltas.append(seam_delta)
                offset += seam_delta
                for timestamp in tuple(merged):
                    if timestamp >= seam_boundaries[index - 1]:
                        del merged[timestamp]
            for sample in samples:
                merged[sample.timestamp_ms] = NormalizedSample(sample.timestamp_ms, sample.value + offset, sample.upnl)
        return tuple(merged[timestamp] for timestamp in sorted(merged))

    raw_balance = merge("wallet_samples")
    raw_equity = merge("equity_samples")
    balance = raw_balance
    equity = raw_equity
    if raw_balance:
        initial_offset = ordered[0].initial_balance - raw_balance[0].value
        if initial_offset:
            balance = tuple(NormalizedSample(sample.timestamp_ms, sample.value + initial_offset, sample.upnl) for sample in raw_balance)
            equity = tuple(NormalizedSample(sample.timestamp_ms, sample.value + initial_offset, sample.upnl) for sample in raw_equity)
    raw_window_balance = raw_balance
    raw_window_equity = raw_equity
    if start_ms is not None:
        raw_window_balance = tuple(sample for sample in raw_window_balance if sample.timestamp_ms >= start_ms)
        raw_window_equity = tuple(sample for sample in raw_window_equity if sample.timestamp_ms >= start_ms)
    if end_ms is not None:
        raw_window_balance = tuple(sample for sample in raw_window_balance if sample.timestamp_ms < end_ms)
        raw_window_equity = tuple(sample for sample in raw_window_equity if sample.timestamp_ms < end_ms)
    if start_ms is not None:
        balance = tuple(sample for sample in balance if sample.timestamp_ms >= start_ms)
        equity = tuple(sample for sample in equity if sample.timestamp_ms >= start_ms)
    if end_ms is not None:
        balance = tuple(sample for sample in balance if sample.timestamp_ms < end_ms)
        equity = tuple(sample for sample in equity if sample.timestamp_ms < end_ms)
    if not raw_window_balance or not raw_window_equity:
        raise _empty_series_error(ordered, windowed=start_ms is not None or end_ms is not None)
    full_window = start_ms is None or start_ms <= ordered[0].report_start_ms
    pnl_anchor = ordered[0].initial_balance if full_window else raw_window_balance[0].value
    total_pnl = raw_window_balance[-1].value - pnl_anchor
    total_pnl_percent = total_pnl / pnl_anchor * Decimal("100") if pnl_anchor else Decimal("0")
    action_by_fact: dict[str, NormalizedAction] = {}
    period_actions: list[list[NormalizedAction]] = [[] for _ in ordered]
    cycle_by_fact: dict[str, NormalizedCycle] = {}
    event_by_fact: dict[str, str] = {}
    for index, fragment in enumerate(ordered):
        boundary = fragment.report_end_ms if index == 0 else seam_boundaries[index - 1]
        excluded = old_open_by_fragment[index] if index == 0 else excluded_by_fragment[index]
        excluded_actions = {action_id for cycle in fragment.cycles if cycle.cycle_id in excluded for action_id in cycle.action_ids}
        retained_actions = {action_id for cycle in fragment.cycles if index and cycle.cycle_id in retained_by_fragment[index] for action_id in cycle.action_ids}
        for action in fragment.actions:
            if action.action_id in excluded_actions or action.timestamp_ms >= fragment.report_end_ms or (index and action.timestamp_ms < boundary and action.action_id not in retained_actions):
                continue
            if (start_ms is None or action.timestamp_ms >= start_ms) and (end_ms is None or action.timestamp_ms < end_ms):
                action_by_fact[action.action_id] = action
                period_actions[index].append(action)
        for cycle in fragment.cycles:
            if cycle.cycle_id in excluded or cycle.open_timestamp_ms >= fragment.report_end_ms or (index and cycle.open_timestamp_ms < boundary and cycle.cycle_id not in retained_by_fragment[index]):
                continue
            if (start_ms is None or cycle.open_timestamp_ms >= start_ms) and (end_ms is None or cycle.open_timestamp_ms < end_ms):
                cycle_by_fact[cycle.cycle_id] = cycle
        for event in fragment.events:
            if event.action_id in excluded_actions or event.timestamp_ms >= fragment.report_end_ms or (index and event.timestamp_ms < boundary and event.action_id not in retained_actions):
                continue
            if (start_ms is None or event.timestamp_ms >= start_ms) and (end_ms is None or event.timestamp_ms < end_ms):
                event_by_fact[event.event_id] = event.event_id
    actions = list(action_by_fact.values())
    cycles = list(cycle_by_fact.values())
    event_ids = list(event_by_fact.values())
    # Seam ownership and the selected half-open window are applied above before
    # either PF or round-trip derivation, so no out-of-window realization can
    # leak into the published facts.
    realizing_actions = [action for action in actions if action.action in {"decreased", "closed"}]
    positive = sum((action.pnl for action in realizing_actions if action.pnl > 0), Decimal("0"))
    negative = sum((action.pnl for action in realizing_actions if action.pnl < 0), Decimal("0"))
    profit_factor = positive / abs(negative) if negative else None
    round_trips = derive_round_trips(actions, cycles, ordered[0].point.canonical_key)
    wins = sum(1 for trip in round_trips if trip.realized_pnl > 0)
    losses = sum(1 for trip in round_trips if trip.realized_pnl < 0)
    total = len(round_trips)
    period_metrics: list[PeriodMetrics] = []
    for index, fragment in enumerate(ordered):
        if index == 0:
            period_balance = tuple(sample for sample in adjusted_views[index]["wallet_samples"] if sample.timestamp_ms < (min((cycle.open_timestamp_ms for cycle in fragment.cycles if cycle.cycle_id in old_open_by_fragment[index]), default=fragment.report_end_ms)))
            period_equity = tuple(sample for sample in adjusted_views[index]["equity_samples"] if sample.timestamp_ms < (min((cycle.open_timestamp_ms for cycle in fragment.cycles if cycle.cycle_id in old_open_by_fragment[index]), default=fragment.report_end_ms)))
        else:
            boundary = seam_boundaries[index - 1]
            retained = [cycle for cycle in fragment.cycles if cycle.cycle_id in retained_by_fragment[index] and cycle.open_timestamp_ms < boundary]
            stop = min((cycle.open_timestamp_ms for cycle in retained), default=boundary)
            anchor_balance = [sample for sample in adjusted_views[index]["wallet_samples"] if sample.timestamp_ms < stop]
            anchor_equity = [sample for sample in adjusted_views[index]["equity_samples"] if sample.timestamp_ms < stop]
            period_balance = tuple(anchor_balance[-1:] + [sample for sample in adjusted_views[index]["wallet_samples"] if sample.timestamp_ms >= boundary])
            period_equity = tuple(anchor_equity[-1:] + [sample for sample in adjusted_views[index]["equity_samples"] if sample.timestamp_ms >= boundary])
        if period_balance and period_equity:
            anchor = period_balance[0].value
            final = period_balance[-1].value
            balance_dd, balance_dd_pct = _drawdown(period_balance)
            equity_dd, equity_dd_pct = _drawdown(period_equity)
            pnl = final - anchor
            positive_period = sum((action.pnl for action in period_actions[index] if action.action in {"decreased", "closed"} and action.pnl > 0), Decimal("0"))
            negative_period = sum((action.pnl for action in period_actions[index] if action.action in {"decreased", "closed"} and action.pnl < 0), Decimal("0"))
            period_pf = positive_period / abs(negative_period) if negative_period else None
            period_metrics.append(PeriodMetrics(fragment.fragment_id, anchor, final, pnl, pnl / anchor * Decimal("100") if anchor else Decimal("0"), period_pf, period_balance, period_equity, balance_dd, equity_dd, balance_dd_pct, equity_dd_pct))
    balance_dd, balance_peak = _drawdown_details(raw_window_balance)
    balance_dd_pct = balance_dd / balance_peak * Decimal("100") if balance_peak else Decimal("0")
    series_equity_dd, series_equity_peak = _drawdown_details(raw_window_equity)
    declared_candidates: list[tuple[Decimal, str, Decimal]] = []
    window_start = start_ms if start_ms is not None else min(fragment.report_start_ms for fragment in ordered)
    window_end = end_ms if end_ms is not None else max(fragment.report_end_ms for fragment in ordered)
    for index, fragment in enumerate(ordered):
        if fragment.stitchability != "STITCHABLE_FIXED_LOT":
            continue
        if fragment.report_start_ms < window_start or fragment.report_end_ms > window_end:
            continue
        if index == 0:
            cutoff = min((cycle.open_timestamp_ms for cycle in fragment.cycles if cycle.cycle_id in old_open_by_fragment[index]), default=None)
            if cutoff is not None and any(sample.timestamp_ms >= cutoff for sample in fragment.equity_samples):
                continue
        else:
            boundary = seam_boundaries[index - 1]
            if excluded_by_fragment[index] or any(sample.timestamp_ms < boundary for sample in fragment.equity_samples):
                continue
        try:
            declared = Decimal(str(fragment.metrics.get("Max Drawdown", "")))
        except Exception:
            continue
        if not declared.is_finite() or declared < 0:
            continue
        _sampled_dd, sampled_peak = _drawdown_details(adjusted_views[index]["equity_samples"])
        declared_candidates.append((declared, fragment.fragment_id, sampled_peak))
    # M6 is explicitly max(merged-series DD, admissible declared DD); an exact
    # maximum tie is still DECLARED and retains every tied fragment id.
    best_equity_dd = max([series_equity_dd, *(item[0] for item in declared_candidates)])
    tied_declared = tuple(sorted(item[1] for item in declared_candidates if item[0] == best_equity_dd))
    if tied_declared:
        selected_declared = next(item for item in sorted(declared_candidates, key=lambda item: item[1]) if item[1] == tied_declared[0])
        equity_dd = best_equity_dd
        equity_peak = selected_declared[2]
        equity_source = "DECLARED"
    else:
        equity_dd = series_equity_dd
        equity_peak = series_equity_peak
        equity_source = "SERIES"
    equity_dd_pct = equity_dd / equity_peak * Decimal("100") if equity_peak else Decimal("0")
    classified_trades = wins + losses
    win_rate = (Decimal(wins) * Decimal("100") / Decimal(classified_trades)) if classified_trades else Decimal("0")
    return CanonicalMetrics(
        total_pnl, total_pnl_percent, profit_factor, total, wins, losses, win_rate,
        equity_dd, equity_dd_pct, balance_dd, balance_dd_pct, balance, equity,
        tuple(dict.fromkeys(event_ids)), tuple(period_metrics),
        sum((trip.weighted_trades for trip in round_trips), Decimal("0")),
        tuple(trip.round_trip_id for trip in round_trips), round_trips,
        equity_source, tied_declared,
    )
