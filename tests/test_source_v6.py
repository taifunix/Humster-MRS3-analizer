from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
import pytest

from mrs3.performance import parse_performance_report


_FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _report(name: str):
    return parse_performance_report((_FIXTURES / name).read_bytes())


def _max_drawdown(series: tuple[tuple[int, Decimal], ...]) -> tuple[Decimal, Decimal]:
    peak = series[0][1]
    maximum = Decimal("0")
    peak_at_maximum = peak
    for _, value in series:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > maximum:
            maximum = drawdown
            peak_at_maximum = peak
    return maximum, maximum / peak_at_maximum * Decimal("100")


def _execution_fingerprint(report) -> str:
    settings = report.settings
    basic = settings["basic"]
    strategy = settings[basic["strategy"]]
    side = basic["side"]
    open_ma = strategy[f"ma_{side.lower()}"]
    close_ma = strategy[f"ma_close_{side.lower()}"]
    payload = {
        "symbol": basic["symbol"],
        "timeframe": basic["time_frame"],
        "side": side,
        "shift_bp": int(abs(Decimal("1") - Decimal(str(open_ma["multiplier"]))) * Decimal("10000")),
        "open_ma": {key: open_ma[key] for key in ("type", "source", "len")},
        "close_ma": {key: close_ma[key] for key in ("type", "source", "len")},
        "initial_balance": report.metrics["Initial balance"],
        "fixed_order_balance": basic["my_fix_balance"],
        "balance_percentage": basic[f"balance_percentage_{side.lower()}"] ,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "execution_compatibility_fingerprint_v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_source_timestamp_accepts_tester_display_timestamp_as_explicit_utc():
    from mrs3.source_v6 import _timestamp

    assert _timestamp("2026-07-27 00:03:00") == datetime(2026, 7, 27, 0, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-07-27T00:03:00", "2026-07-27 00:03:00.123456"])
def test_source_timestamp_rejects_other_naive_formats(value):
    from mrs3.source_v6 import SourceV6Error, _timestamp

    with pytest.raises(SourceV6Error, match="timezone-aware UTC"):
        _timestamp(value)


def test_effective_report_period_retains_real_tester_terminal_endpoint():
    from mrs3.source_v6 import _effective_report_period

    endpoint = 1767916800000  # 2026-01-09T00:00:00Z
    report = SimpleNamespace(
        metrics={"Report range": "2026-01-01 - 2026-01-09"},
        actions=({"Timestamp": "2026-01-09 00:00:00"},),
        wallet_series=((endpoint, Decimal("100")),),
        equity_series=((endpoint, Decimal("100")),),
    )

    start, end = _effective_report_period(report)
    assert start == 1767225600000
    assert end == 1768003200000  # 2026-01-10T00:00:00Z, still half-open


def test_effective_report_period_keeps_strict_end_without_terminal_sample():
    from mrs3.source_v6 import _effective_report_period

    report = SimpleNamespace(
        metrics={"Report range": "2026-01-01 - 2026-01-09"},
        actions=({"Timestamp": "2026-01-08 23:59:00"},),
        wallet_series=((1767878340000, Decimal("100")),),
        equity_series=((1767878340000, Decimal("100")),),
    )

    assert _effective_report_period(report) == (1767225600000, 1767916800000)


def test_effective_report_period_rejects_data_after_terminal_endpoint():
    from mrs3.source_v6 import SourceV6Error, _effective_report_period

    endpoint = 1767916800000
    report = SimpleNamespace(
        metrics={"Report range": "2026-01-01 - 2026-01-09"},
        actions=({"Timestamp": "2026-01-09 00:00:00"}, {"Timestamp": "2026-01-09 00:01:00"}),
        wallet_series=((endpoint, Decimal("100")),),
        equity_series=((endpoint, Decimal("100")),),
    )

    with pytest.raises(SourceV6Error, match="exceeds the date-only terminal endpoint"):
        _effective_report_period(report)


def test_effective_report_period_rejects_data_after_end_without_boundary_sample():
    from mrs3.source_v6 import SourceV6Error, _effective_report_period

    report = SimpleNamespace(
        metrics={"Report range": "2026-01-01 - 2026-01-09"},
        actions=({"Timestamp": "2026-01-09 00:01:00"},),
        wallet_series=((1767916860000, Decimal("100")),),
        equity_series=((1767916860000, Decimal("100")),),
    )

    with pytest.raises(SourceV6Error, match="exceeds the date-only terminal endpoint"):
        _effective_report_period(report)


def test_task0_fixed_lot_golden_references_balance_equity_and_action_pnl() -> None:
    report = _report("source_v6_fixed_lot_overlap_a.html")

    balance_pnl = report.wallet_series[-1][1] - report.wallet_series[0][1]
    closed = [action for action in report.actions if action["Action"] == "closed"]
    gross_profit = sum((Decimal(action["PnL"]) for action in closed if Decimal(action["PnL"]) > 0), Decimal())
    gross_loss = sum((Decimal(action["PnL"]) for action in closed if Decimal(action["PnL"]) < 0), Decimal())
    max_dd, max_dd_percent = _max_drawdown(report.equity_series)

    assert balance_pnl == Decimal("5.8")
    assert balance_pnl == Decimal(report.metrics["Total PnL"])
    assert sum(Decimal(action["Fee"]) for action in closed) == Decimal("0.2")
    assert gross_profit / abs(gross_loss) == Decimal("2.5")
    assert Decimal(report.metrics["Profit Factor"]) == Decimal("2.5")
    assert max_dd == Decimal("30")
    assert max_dd_percent.quantize(Decimal("0.0001")) == Decimal(report.metrics["Max Drawdown, %"])
    assert len(closed) == int(report.metrics["Total Trades"])


def test_task0_fixed_lot_pair_has_96_hour_overlap_and_covering_tail() -> None:
    outgoing = _report("source_v6_fixed_lot_overlap_a.html")
    incoming = _report("source_v6_fixed_lot_overlap_b.html")

    outgoing_start, outgoing_end = (date.fromisoformat(value.strip()) for value in outgoing.metrics["Report range"].split(" - "))
    incoming_start, incoming_end = (date.fromisoformat(value.strip()) for value in incoming.metrics["Report range"].split(" - "))

    assert (outgoing_end - incoming_start).days * 24 == 96
    assert incoming_end > outgoing_end > incoming_start > outgoing_start
    assert outgoing.settings["exchange"]["use_upnl"] is False
    assert outgoing.settings["basic"]["use_fix"] is True
    assert any(action["Order ID"] == "3" and action["Action"] == "opened" for action in outgoing.actions)
    assert any(
        action["Order ID"] == "3"
        and action["Action"] == "decreased"
        and action["Post Size"] == "0.5"
        for action in outgoing.actions
    )
    assert any(action["Order ID"] == "3" and action["Action"] == "closed" for action in incoming.actions)


def test_task0_header_is_half_open_and_initial_balance_matches_first_wallet_sample() -> None:
    from mrs3.source_v6 import normalize_source_v6

    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        parsed = _report(name)
        fragment = normalize_source_v6((_FIXTURES / name).read_bytes())
        start, end = fragment.report_start_ms, fragment.report_end_ms
        assert fragment.wallet_samples[0].timestamp_ms >= start
        assert fragment.wallet_samples[-1].timestamp_ms < end
        assert fragment.initial_balance == fragment.wallet_samples[0].value


def test_task0_three_representative_reports_reconcile_wallet_equity_and_upnl() -> None:
    from mrs3.source_v6 import normalize_source_v6

    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html", "source_v6_legacy_nonstitchable.html"):
        fragment = normalize_source_v6((_FIXTURES / name).read_bytes())
        assert len(fragment.wallet_samples) == len(fragment.equity_samples) >= 2
        assert all(equity.value - wallet.value == equity.upnl for wallet, equity in zip(fragment.wallet_samples, fragment.equity_samples))
        assert fragment.initial_balance == fragment.wallet_samples[0].value


def test_task0_legacy_fixture_is_importable_but_not_stitchable_by_contract() -> None:
    report = _report("source_v6_legacy_nonstitchable.html")

    assert report.settings["exchange"]["use_upnl"] is True
    assert report.settings["basic"]["use_fix"] is False
    assert report.inventory.trade_row_count == 1


def test_task1_legacy_normalizes_as_non_stitchable_position_sizing() -> None:
    from mrs3.source_v6 import normalize_source_v6

    fragment = normalize_source_v6((_FIXTURES / "source_v6_legacy_nonstitchable.html").read_bytes())
    assert fragment.stitchability == "NON_STITCHABLE_POSITION_SIZING"


def test_task1_malformed_and_upnl_sized_reports_fail_closed_for_stitching() -> None:
    from mrs3.source_v6 import SourceV6Error, normalize_source_v6
    from mrs3.source_v6_stitch import SourceV6StitchError, calculate_metrics

    legacy = normalize_source_v6((_FIXTURES / "source_v6_legacy_nonstitchable.html").read_bytes())
    with pytest.raises(SourceV6StitchError, match="non-stitchable"):
        calculate_metrics((legacy,))
    with pytest.raises(SourceV6Error):
        normalize_source_v6(b"<html><body>not a tester report</body></html>")


def test_task0_execution_fingerprint_covers_strategy_and_sizing_contract() -> None:
    fixed_a = _report("source_v6_fixed_lot_overlap_a.html")
    fixed_b = _report("source_v6_fixed_lot_overlap_b.html")
    legacy = _report("source_v6_legacy_nonstitchable.html")

    assert _execution_fingerprint(fixed_a) == _execution_fingerprint(fixed_b)
    assert _execution_fingerprint(fixed_a) == _execution_fingerprint(legacy)
    assert fixed_a.settings["exchange"]["use_upnl"] is False
    assert legacy.settings["exchange"]["use_upnl"] is True
    assert _execution_fingerprint(fixed_a).startswith("execution_compatibility_fingerprint_v1:")


def test_task1_normalizes_fixed_lot_fragment_into_stable_facts() -> None:
    from mrs3.source_v6 import normalize_source_v6

    first = normalize_source_v6(
        (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(),
        source_name="first.html",
    )
    second = normalize_source_v6(
        (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(),
        source_name="renamed.html",
    )

    assert first.stitchability == "STITCHABLE_FIXED_LOT"
    assert first.point.canonical_key == "ONUSDT|LONG|1h|50|SMA|ohlc4|7|SMA|ohlc4|3"
    assert first.fragment_id == second.fragment_id
    assert first.source_name == "first.html"
    assert first.actions[0].action_id == second.actions[0].action_id
    assert first.cycles[0].cycle_id == second.cycles[0].cycle_id
    assert len(first.events) == len({event.event_id for event in first.events})
    assert first.wallet_samples[-1].value == Decimal("1005.8")
    assert first.equity_samples[2].upnl == Decimal("-25.8")
    assert first.open_tail_cycle_ids == (first.cycles[-1].cycle_id,)
    assert tuple(event.action_id for event in first.events) == tuple(action.action_id for action in first.actions)
    assert all(event.event_id == event.action_id for event in first.events)
    assert set(first.open_tail_cycle_ids) == {cycle.cycle_id for cycle in first.cycles if not cycle.closed}


def test_task0_wallet_equity_upnl_contract_is_reconciled_across_three_reports() -> None:
    from mrs3.source_v6 import normalize_source_v6

    paths = (
        _FIXTURES / "source_v6_fixed_lot_overlap_a.html",
        _FIXTURES / "source_v6_fixed_lot_overlap_b.html",
        _FIXTURES / "source_v6_legacy_nonstitchable.html",
    )
    reports = tuple(normalize_source_v6(path.read_bytes()) for path in paths)
    for report in reports:
        assert report.wallet_samples[0].value == report.initial_balance
        assert report.wallet_samples[0].upnl == Decimal("0")
        assert tuple(sample.timestamp_ms for sample in report.wallet_samples) == tuple(sample.timestamp_ms for sample in report.equity_samples)
        assert all(sample.upnl == sample.value - wallet.value for sample, wallet in zip(report.equity_samples, report.wallet_samples, strict=True))


def test_task1_windows_and_debian_source_paths_do_not_change_canonical_hash() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes()
    windows = normalize_source_v6(source, source_name=r"C:\reports\a.html")
    debian = normalize_source_v6(source, source_name="/srv/reports/a.html")
    assert windows.fragment_id == debian.fragment_id
    assert windows.settings_fingerprint == debian.settings_fingerprint


def test_task1_action_ids_do_not_depend_on_html_row_order() -> None:
    from mrs3.source_v6 import normalize_source_v6

    original = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes()
    text = original.decode("utf-8")
    rows = re.findall(r"<tr><td>2026-01-[^<]+</td>.*?</tr>", text)
    assert len(rows) == 4
    table_start = text.index("<table><thead><tr><th>Timestamp")
    table_end = text.index("</tbody></table>", table_start)
    trade_table = text[table_start:table_end]
    for row in rows:
        trade_table = trade_table.replace(row, "", 1)
    reordered = text[:table_start] + trade_table + "".join(reversed(rows)) + text[table_end:]

    first = normalize_source_v6(original, source_name="original.html")
    second = normalize_source_v6(reordered.encode("utf-8"), source_name="reordered.html")

    assert [action.action_id for action in first.actions] == [action.action_id for action in second.actions]
    assert [event.event_id for event in first.events] == [event.event_id for event in second.events]
    assert first.fragment_id == second.fragment_id


def test_task1_rejects_shifted_wallet_equity_timestamps() -> None:
    from mrs3.source_v6 import SourceV6Error, normalize_source_v6

    source = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_text(encoding="utf-8")
    shifted = source.replace("equitySeries = [[1767225600000", "equitySeries = [[1767225600001", 1)
    with pytest.raises(SourceV6Error, match="wallet/equity"):
        normalize_source_v6(shifted.encode("utf-8"))


def test_task1_point_key_includes_ma_type_and_source() -> None:
    from mrs3.source_v6 import normalize_source_v6

    source = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_text(encoding="utf-8")
    changed = source.replace('"type":"SMA","source":"ohlc4"', '"type":"EMA","source":"close"', 1)
    variant = normalize_source_v6(changed.encode("utf-8"))
    original = normalize_source_v6(source.encode("utf-8"))
    assert variant.point.canonical_key != original.point.canonical_key


def test_task1_rejects_fractional_ma_lengths() -> None:
    from mrs3.source_v6 import SourceV6Error, normalize_source_v6

    source = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_text(encoding="utf-8")
    fractional = source.replace('"len":7', '"len":7.5', 1)
    with pytest.raises(SourceV6Error, match="positive integer"):
        normalize_source_v6(fractional.encode("utf-8"))


def test_task3_exact_overlap_ownership_and_task4_metrics() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics, resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(), source_name="a.html")
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes(), source_name="b.html")

    ownership = resolve_ownership(outgoing, incoming)
    assert ownership.status == "USE_OLD_WITH_SEAM_EXCLUSION"
    assert ownership.overlap_hours == Decimal("96")
    metrics = calculate_metrics((outgoing, incoming))
    assert metrics.total_pnl == Decimal("5.8")
    # Leading closes are one orphan round trip; the later entry-only tail is
    # intentionally not a round trip.
    assert metrics.total_trades == 1
    assert metrics.profit_factor == Decimal("2.5")
    assert (metrics.win_trades, metrics.loss_trades) == (1, 0)
    assert metrics.win_rate_percent == Decimal("100")
    assert metrics.balance_series[-1].value == Decimal("1005.8")


def test_task4_missing_primary_series_fails_visibly() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import SourceV6StitchError, calculate_metrics

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    with pytest.raises(SourceV6StitchError, match="series"):
        calculate_metrics((replace(report, wallet_samples=(), equity_samples=()),))


def test_task4_selected_interval_excludes_boundary_cycle_opened_before_start() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    start = report.wallet_samples[2].timestamp_ms
    metrics = calculate_metrics((report,), start_ms=start, end_ms=report.report_end_ms)
    assert all(sample.timestamp_ms >= start for sample in metrics.balance_series)
    # At +49h the remaining action is an orphan realization; A's +180h
    # opened/+181h decreased tail never realizes.
    assert metrics.total_trades == 1
    assert metrics.round_trips[0].entry_action_ids == ()


def test_task4_stitched_metrics_match_independent_decimal_reference() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    metrics = calculate_metrics((outgoing, incoming))
    canonical_balance = Decimal("1000")
    expected_final = Decimal("1005.8")
    assert metrics.total_pnl == expected_final - canonical_balance
    action_pnls = [action.pnl for action in outgoing.actions if action.action == "closed"]
    assert sum((value for value in action_pnls if value > 0), Decimal("0")) / abs(sum((value for value in action_pnls if value < 0), Decimal("0"))) == metrics.profit_factor


def test_task3_midday_boundary_keeps_pre_boundary_outgoing_fact() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import owner_for_timestamp

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    incoming = replace(incoming, report_start_ms=incoming.report_start_ms + 14 * 3_600_000)
    before = incoming.report_start_ms - 1
    at = incoming.report_start_ms
    assert owner_for_timestamp(before, outgoing, incoming) == outgoing.fragment_id
    assert owner_for_timestamp(at, outgoing, incoming) == outgoing.fragment_id
    assert owner_for_timestamp(outgoing.report_end_ms, outgoing, incoming) == incoming.fragment_id


def test_task4_unresolved_open_tail_is_excluded_until_covering_fragment() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    assert calculate_metrics((outgoing,)).total_pnl == Decimal("5.8")


def test_task3_non_aligned_overlap_uses_incoming_boundary_ownership() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    shifted_wallet = tuple(NormalizedSample(sample.timestamp_ms + 3_600_000, sample.value, sample.upnl) for sample in incoming.wallet_samples)
    shifted_equity = tuple(NormalizedSample(sample.timestamp_ms + 3_600_000, sample.value, sample.upnl) for sample in incoming.equity_samples)
    incoming = replace(incoming, wallet_samples=shifted_wallet, equity_samples=shifted_equity)
    metrics = calculate_metrics((outgoing, incoming))
    assert metrics.balance_series == tuple(sorted(metrics.balance_series, key=lambda sample: sample.timestamp_ms))
    assert all(sample.timestamp_ms < incoming.report_start_ms or sample.timestamp_ms >= incoming.report_start_ms + 3_600_000 for sample in metrics.balance_series)


def test_task3_uncovered_tail_is_partial_with_automatic_reason() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_batch, resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    incoming = replace(incoming, actions=(), cycles=(), events=())
    # This constructs an incoming that carries nothing while the outgoing has an
    # open tail. It used to assert `COMMITTED` with the seam applied, which
    # contradicted both this test's own name and ADR-0010: "a fragment whose
    # start cannot cover the outgoing tail cycle is marked BRIDGE_NOT_COVERED
    # ... contributes to PARTIAL". Until ADR-0016 the shape was unreachable from
    # real data, because a report with no facts was quarantined.
    decision = resolve_ownership(outgoing, incoming)
    assert decision.reason == "BRIDGE_NOT_COVERED"
    batch = resolve_batch((outgoing, incoming))
    assert batch.status == "PARTIAL"
    assert batch.active_fragments == (outgoing,)


def test_task3_non_overlapping_interval_is_not_resolved() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    non_overlapping = replace(
        incoming,
        report_start_ms=outgoing.report_end_ms + 3_600_000,
        report_end_ms=outgoing.report_end_ms + 9 * 86_400_000,
    )
    decision = resolve_ownership(outgoing, non_overlapping)
    assert decision.status == "UNRESOLVED"
    assert decision.reason in {"NO_OVERLAP", "OVERLAP_BELOW_MINIMUM"}


def test_task3_compatible_overlap_without_tail_extends_active_interval() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_ownership, resolve_batch

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    outgoing = replace(outgoing, cycles=tuple(cycle for cycle in outgoing.cycles if cycle.closed), open_tail_cycle_ids=())
    assert resolve_ownership(outgoing, incoming).status == "USE_OLD_WITH_SEAM_EXCLUSION"
    batch = resolve_batch((outgoing, incoming))
    assert batch.status == "COMMITTED"
    assert batch.active_fragments == (outgoing, incoming)


def test_task3_same_interval_conflicting_batch_versions_remain_ambiguous() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import resolve_batch

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    conflicting = replace(report, fragment_id="conflicting-fragment", wallet_samples=tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("1"), sample.upnl) for sample in report.wallet_samples))
    batch = resolve_batch((report, conflicting))
    assert batch.status == "PARTIAL"
    assert batch.decisions[0].reason == "AMBIGUOUS_INCOMING"


def test_task3_bridge_selection_contains_cycle_actions_and_events() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import select_bridge_facts

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    bridge = select_bridge_facts(outgoing, incoming)
    assert bridge.cycle_ids
    assert bridge.action_ids
    assert bridge.event_ids
    assert bridge.sample_timestamps
    assert all(timestamp >= incoming.report_start_ms for timestamp in bridge.sample_timestamps)


def test_task3_different_contract_is_unresolved_not_silently_stitched() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    legacy = normalize_source_v6((_FIXTURES / "source_v6_legacy_nonstitchable.html").read_bytes())
    assert resolve_ownership(outgoing, legacy).reason in {"INCOMPATIBLE_POINT", "INCOMPATIBLE_CONTRACT", "NON_STITCHABLE"}


def test_task4_independent_decimal_reference_reconciles_pnl_dd_pf_and_trades() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    metrics = calculate_metrics((report,))
    balances = [sample.value for sample in report.wallet_samples]
    reference_pnl = balances[-1] - balances[0]
    wins = [action.pnl for action in report.actions if action.action == "closed" and action.pnl > 0]
    losses = [action.pnl for action in report.actions if action.action == "closed" and action.pnl < 0]
    reference_pf = sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
    peak = balances[0]
    drawdowns = []
    for value in balances:
        peak = max(peak, value)
        drawdowns.append(peak - value)
    assert metrics.total_pnl == reference_pnl
    assert metrics.profit_factor == reference_pf
    # A's +1h/+49h closes are one orphan realization run and its
    # +180h/+181h actions are an entry-only tail.
    assert metrics.total_trades == 1
    assert metrics.round_trips[0].entry_action_ids == ()
    assert metrics.max_realized_drawdown == max(drawdowns)


def test_task4_constant_incoming_wallet_offset_does_not_change_metrics() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    shifted_wallet = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("123"), sample.upnl) for sample in incoming.wallet_samples)
    shifted_equity = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("123"), sample.upnl) for sample in incoming.equity_samples)
    shifted = replace(incoming, wallet_samples=shifted_wallet, equity_samples=shifted_equity)
    assert calculate_metrics((outgoing, incoming)).total_pnl == calculate_metrics((outgoing, shifted)).total_pnl


def test_task4_balance_series_is_rebased_to_initial_balance() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    source = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    shifted_wallet = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("25"), sample.upnl) for sample in source.wallet_samples)
    shifted_equity = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("25"), sample.upnl) for sample in source.equity_samples)
    shifted = replace(source, wallet_samples=shifted_wallet, equity_samples=shifted_equity)
    assert calculate_metrics((shifted,)).balance_series[0].value == shifted.initial_balance


def test_task4_equity_uses_the_same_seam_offset_as_balance() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    shifted_wallet = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("10"), sample.upnl) for sample in incoming.wallet_samples)
    shifted_equity = tuple(NormalizedSample(sample.timestamp_ms, sample.value + Decimal("10"), sample.upnl) for sample in incoming.equity_samples)
    metrics = calculate_metrics((outgoing, replace(incoming, wallet_samples=shifted_wallet, equity_samples=shifted_equity)))
    by_time = {sample.timestamp_ms: sample.value for sample in metrics.balance_series}
    assert all(sample.value - by_time[sample.timestamp_ms] == sample.upnl for sample in metrics.equity_series)


def test_task7_old_owned_overlap_excludes_closed_incoming_cycle_and_corrects_later_samples() -> None:
    from mrs3.source_v6 import NormalizedSample, normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics, owner_for_timestamp, resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    excluded = incoming.cycles[0]
    excluded_action = incoming.actions[0]
    incoming = replace(
        incoming,
        actions=(replace(excluded_action, pnl=Decimal("-10"), fee=Decimal("0.9"), balance=Decimal("989.1")),),
        cycles=(replace(excluded, realized_pnl=Decimal("-10"), fees=Decimal("0.9")),),
        wallet_samples=tuple(
            NormalizedSample(sample.timestamp_ms, Decimal("989.1"), Decimal("0"))
            if sample.timestamp_ms >= excluded.open_timestamp_ms else sample
            for sample in incoming.wallet_samples
        ),
        equity_samples=tuple(
            NormalizedSample(sample.timestamp_ms, Decimal("989.1"), Decimal("0"))
            if sample.timestamp_ms >= excluded.open_timestamp_ms else sample
            for sample in incoming.equity_samples
        ),
    )

    decision = resolve_ownership(outgoing, incoming)
    assert decision.status == "USE_OLD_WITH_SEAM_EXCLUSION"
    assert decision.reason == "INCOMPLETE_SEAM_CYCLE_EXCLUDED"
    assert owner_for_timestamp(outgoing.report_end_ms - 1, outgoing, incoming) == outgoing.fragment_id
    assert owner_for_timestamp(outgoing.report_end_ms, outgoing, incoming) == incoming.fragment_id

    metrics = calculate_metrics((outgoing, incoming))
    # A has one orphan realization run and a +180h opened/+181h decreased tail;
    # the incoming orphan close is removed by old-owned seam exclusion.
    assert metrics.total_trades == 1
    assert metrics.round_trips[0].entry_action_ids == ()
    assert metrics.profit_factor == Decimal("2.5")
    assert all(excluded_action.action_id != action_id for action_id in metrics.events)
    old_period, new_period = metrics.period_metrics
    assert old_period.anchor_balance == Decimal("1000")
    assert old_period.total_pnl == Decimal("5.8")
    assert old_period.total_pnl_percent == Decimal("0.58")
    assert old_period.profit_factor == Decimal("2.5")
    assert old_period.max_realized_drawdown == Decimal("4.1")
    assert old_period.max_equity_drawdown == Decimal("30")
    assert new_period.anchor_balance == Decimal("1000")
    assert new_period.total_pnl == Decimal("0")
    assert new_period.total_pnl_percent == Decimal("0")
    assert new_period.profit_factor is None
    assert new_period.max_realized_drawdown == Decimal("0")
    assert new_period.max_equity_drawdown == Decimal("0")

    expected_balance = tuple(sample.value for sample in metrics.balance_series)
    expected_equity = tuple(sample.value for sample in metrics.equity_series)
    assert expected_balance == (Decimal("1000"), Decimal("1009.9"), Decimal("1005.8"), Decimal("1005.8"))
    assert expected_equity == (Decimal("1000"), Decimal("1010"), Decimal("980"), Decimal("1005.8"))
    assert metrics.max_realized_drawdown == Decimal("4.1")
    assert metrics.max_equity_drawdown == Decimal("30")


def test_task7_boundary_crossing_incoming_cycle_is_retained_as_new_period() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import calculate_metrics, resolve_ownership

    outgoing = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    incoming = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    boundary = outgoing.report_end_ms
    cycle = incoming.cycles[0]
    action = incoming.actions[0]
    event = incoming.events[0]
    incoming = replace(
        incoming,
        actions=(replace(action, timestamp_ms=boundary, balance=Decimal("1000.9")),),
        cycles=(replace(cycle, close_timestamp_ms=boundary, action_ids=(action.action_id,)),),
        events=(replace(event, timestamp_ms=boundary),),
    )

    assert resolve_ownership(outgoing, incoming).status == "USE_OLD_WITH_SEAM_EXCLUSION"
    metrics = calculate_metrics((outgoing, incoming))
    # The boundary mutation retains B's +180h close, but A's +180h open is at
    # the outgoing half-open endpoint, so the retained realization is orphaned.
    assert metrics.total_trades == 1
    assert metrics.round_trips[0].entry_action_ids == ()
    assert metrics.profit_factor == Decimal("2.75")
    assert action.action_id in metrics.events
    old_period, new_period = metrics.period_metrics
    assert old_period.total_pnl == Decimal("5.8")
    assert old_period.total_pnl_percent == Decimal("0.58")
    assert old_period.profit_factor == Decimal("2.5")
    assert new_period.anchor_balance == Decimal("1000")
    assert new_period.total_pnl == Decimal("0.9")
    assert new_period.total_pnl_percent == Decimal("0.09")
    assert new_period.profit_factor is None


def test_task5_coverage_uses_report_header_days_and_deterministic_gap_export() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import coverage_csv, coverage_json, coverage_cells, missing_cells

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    cells = coverage_cells((report,))
    assert len(cells) == 8
    gaps = missing_cells((report,), start=date(2026, 1, 1), end=date(2026, 1, 11))
    assert [cell.utc_day.isoformat() for cell in gaps] == ["2026-01-09", "2026-01-10"]
    assert coverage_csv(cells) == coverage_csv(tuple(reversed(cells)))
    assert coverage_json(cells).startswith(b"[{\"point_key\":\"ONUSDT|LONG|1h|50|SMA|ohlc4|7|SMA|ohlc4|3\"")
    assert missing_cells((report,), start=date(2026, 1, 1), end=date(2026, 1, 2), symbols=("NOPE",)) == ()
    from mrs3.source_v6_coverage import ready_intervals
    interval = ready_intervals((report,))[0]
    assert interval.scope_key == "ONUSDT|LONG|1h"
    assert interval.start == date(2026, 1, 1)
    assert interval.end == date(2026, 1, 8)


def test_task5_ready_interval_groups_ma_variants_by_pair_side_timeframe() -> None:
    from dataclasses import replace
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ready_intervals

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    variant = replace(report, point=replace(report.point, shift_bp=Decimal("75")))
    intervals = ready_intervals((report, variant))
    assert len(intervals) == 1
    assert intervals[0].scope_key == "ONUSDT|LONG|1h"


def test_task5_readiness_grid_requires_every_shift_and_close_ma_variant() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ready_intervals

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    variants = [
        replace(report, point=replace(report.point, shift_bp=shift, close_ma_length=close))
        for shift in (30, 40)
        for close in (2, 3)
    ]
    assert ready_intervals(variants, required_shifts=(30, 40), required_close_lengths=(2, 3))[0].start == date(2026, 1, 1)
    assert ready_intervals(variants[:-1], required_shifts=(30, 40), required_close_lengths=(2, 3)) == ()


def test_task5_canonical_readiness_uses_operational_six_by_nineteen_contract() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP, canonical_ready_intervals
    from mrs3.duckdb_direct import CoverageInterval, canonical_coverage_from_rows

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    variants = [
        replace(report, point=replace(report.point, shift_bp=shift, close_ma_length=close))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    ]
    intervals = canonical_ready_intervals(variants)
    assert intervals and intervals[0].scope_key == "ONUSDT|LONG|1h"
    assert (intervals[0].start, intervals[0].end) == (date(2026, 1, 1), date(2026, 1, 8))
    assert canonical_ready_intervals(variants[:-1]) == ()

    rows = [{
        "report_id": fragment.fragment_id,
        "source_hash": fragment.source_sha256,
        "canonical_point_key": fragment.point.canonical_key,
        "symbol": fragment.point.symbol,
        "side": fragment.point.side,
        "timeframe": fragment.point.timeframe,
        "shift_bp": fragment.point.shift_bp,
        "open_ma_len": fragment.point.open_ma_length,
        "close_ma_len": fragment.point.close_ma_length,
        "report_period_start_ms": fragment.report_start_ms,
        "report_period_end_ms": fragment.report_end_ms,
        "start_timestamp_ms": fragment.report_start_ms,
        "end_timestamp_ms": fragment.report_end_ms,
    } for fragment in variants]
    coverage = canonical_coverage_from_rows(rows)
    assert coverage.intervals and all(isinstance(item, CoverageInterval) for item in coverage.intervals)
    assert all(type(item.selectable) is bool for item in coverage.intervals)


def test_task5_canonical_readiness_drops_unselectable_zero_length_duplicate(monkeypatch) -> None:
    from mrs3.source_v6_coverage import ReadyInterval, canonical_ready_intervals

    scope = SimpleNamespace(symbol="ONUSDT", side="LONG", timeframe="1h")
    hidden_scope = SimpleNamespace(symbol="BTCUSDT", side="LONG", timeframe="1h")
    selected = SimpleNamespace(
        scope=scope,
        start_utc="2026-01-01T00:00:00.000+00:00",
        end_utc="2026-01-05T00:00:00.000+00:00",
        selectable=True,
    )
    hidden_longer = SimpleNamespace(
        scope=scope,
        start_utc="2025-12-01T00:00:00.000+00:00",
        end_utc="2026-01-20T00:00:00.000+00:00",
        selectable=False,
    )
    hidden_only = SimpleNamespace(
        scope=hidden_scope,
        start_utc="2026-01-09T00:00:00.000+00:00",
        end_utc="2026-01-09T00:00:00.000+00:00",
        selectable=False,
    )
    degenerate = SimpleNamespace(
        scope=SimpleNamespace(symbol="ETHUSDT", side="LONG", timeframe="1h"),
        start_utc="2026-01-09T00:00:00.000+00:00",
        end_utc="2026-01-09T00:00:00.000+00:00",
        selectable=True,
    )
    calls = []

    def coverage(rows):
        calls.append(rows)
        return SimpleNamespace(intervals=(selected, hidden_longer, hidden_only, degenerate))

    # The production helper defers this import to avoid a module cycle.
    monkeypatch.setattr(
        "mrs3.duckdb_direct.canonical_coverage_from_rows",
        coverage,
    )

    result = canonical_ready_intervals(())
    assert calls
    assert canonical_ready_intervals(()) == (
        ReadyInterval("ONUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 4)),
    )
    assert result == canonical_ready_intervals(())


def test_task5_canonical_readiness_collapses_real_disjoint_fragment_windows() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ReadyInterval, canonical_ready_intervals
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    variants = tuple(
        replace(report, point=replace(report.point, shift_bp=shift, close_ma_length=close))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    )
    period = report.report_end_ms - report.report_start_ms
    second_start = report.report_end_ms + 86_400_000
    second = tuple(replace(item, report_start_ms=second_start, report_end_ms=second_start + period) for item in variants)

    assert canonical_ready_intervals((*variants, *second)) == (
        ReadyInterval("ONUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 8)),
    )


def test_task5_canonical_readiness_keeps_earliest_longest_selectable_interval(monkeypatch) -> None:
    from mrs3.source_v6_coverage import ReadyInterval, canonical_ready_intervals

    scope = SimpleNamespace(symbol="ONUSDT", side="LONG", timeframe="1h")
    late_longer = SimpleNamespace(
        scope=scope,
        start_utc="2026-01-05T00:00:00.000+00:00",
        end_utc="2026-01-20T00:00:00.000+00:00",
        selectable=True,
    )
    early_shorter = SimpleNamespace(
        scope=scope,
        start_utc="2026-01-01T00:00:00.000+00:00",
        end_utc="2026-01-03T00:00:00.000+00:00",
        selectable=True,
    )
    tie_scope = SimpleNamespace(symbol="BTCUSDT", side="LONG", timeframe="1h")
    tie_late = SimpleNamespace(
        scope=tie_scope,
        start_utc="2026-02-03T00:00:00.000+00:00",
        end_utc="2026-02-08T00:00:00.000+00:00",
        selectable=True,
    )
    tie_early = SimpleNamespace(
        scope=tie_scope,
        start_utc="2026-02-01T00:00:00.000+00:00",
        end_utc="2026-02-06T00:00:00.000+00:00",
        selectable=True,
    )

    calls = []

    def coverage(rows):
        calls.append(rows)
        return SimpleNamespace(intervals=(late_longer, early_shorter, tie_late, tie_early))

    monkeypatch.setattr(
        "mrs3.duckdb_direct.canonical_coverage_from_rows",
        coverage,
    )
    expected = (
        ReadyInterval("BTCUSDT|LONG|1h", date(2026, 2, 1), date(2026, 2, 5)),
        ReadyInterval("ONUSDT|LONG|1h", date(2026, 1, 5), date(2026, 1, 19)),
    )
    assert canonical_ready_intervals(()) == expected
    assert calls

    monkeypatch.setattr(
        "mrs3.duckdb_direct.canonical_coverage_from_rows",
        lambda rows: SimpleNamespace(intervals=(tie_early, tie_late, early_shorter, late_longer)),
    )
    assert canonical_ready_intervals(()) == expected


def test_task5_canonical_readiness_keeps_single_day_interval(monkeypatch) -> None:
    from mrs3.source_v6_coverage import ReadyInterval, canonical_ready_intervals

    scope = SimpleNamespace(symbol="SOLUSDT", side="SHORT", timeframe="1h")
    one_day = SimpleNamespace(
        scope=scope,
        start_utc="2026-03-01T00:00:00.000+00:00",
        end_utc="2026-03-02T00:00:00.000+00:00",
        selectable=True,
    )
    monkeypatch.setattr(
        "mrs3.duckdb_direct.canonical_coverage_from_rows",
        lambda rows: SimpleNamespace(intervals=(one_day,)),
    )

    assert canonical_ready_intervals(()) == (
        ReadyInterval("SOLUSDT|SHORT|1h", date(2026, 3, 1), date(2026, 3, 1)),
    )


def test_task5_selected_interval_must_be_inside_ready_bounds() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ready_intervals, select_ready_interval

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    intervals = ready_intervals((report,))
    selected = select_ready_interval(intervals, scope_key="ONUSDT|LONG|1h", start=date(2026, 1, 2), end=date(2026, 1, 5))
    assert (selected.start, selected.end) == (date(2026, 1, 2), date(2026, 1, 4))
    with pytest.raises(ValueError, match="outside READY"):
        select_ready_interval(intervals, scope_key="ONUSDT|LONG|1h", start=date(2025, 12, 1), end=date(2026, 1, 5))


def test_task5_header_coverage_survives_empty_trade_day_and_is_deterministic() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import coverage_cells, coverage_csv, coverage_json

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    empty = replace(report, actions=(), cycles=(), events=())
    cells = coverage_cells((empty,))
    assert len(cells) == 8
    assert coverage_csv(cells) == coverage_csv(tuple(reversed(cells)))
    assert coverage_json(cells) == coverage_json(tuple(reversed(cells)))


def test_task6_surface_publish_is_self_contained_and_atomic(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface, read_surface, scan_surfaces, scan_surface_diagnostics, load_source_v6_pipeline_input

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    interval = (report.report_start_ms, report.report_end_ms)
    scope = f"{report.point.symbol}|{report.point.side}|{report.point.timeframe}"
    surface = publish_surface(tmp_path, (report,), intervals={report.point.canonical_key: interval})
    payload = read_surface(surface)
    assert surface.parent == tmp_path
    assert payload["schema_version"] == 6
    assert payload["surface_id"]
    assert payload["fragment_ids"] == [report.fragment_id]
    with pytest.raises(Exception, match="complete canonical point grid"):
        load_source_v6_pipeline_input(surface, scope=scope, start=interval[0], end=interval[1])
    assert not list(tmp_path.glob("*.staging"))
    assert len(scan_surfaces(tmp_path)) == 1
    tampered = tmp_path / "source-v6-tampered.json"
    tampered.write_text(surface.read_text(encoding="utf-8").replace('"schema_version":6', '"schema_version":7'), encoding="utf-8")
    assert len(scan_surfaces(tmp_path)) == 1
    statuses = {item.status for item in scan_surface_diagnostics(tmp_path)}
    assert statuses == {"VALID", "MALFORMED"}
    from mrs3.source_v6_surface import publish_surface_db, append_analysis_run, load_source_v6_pipeline_input, read_surface_db, verify_surface_frozen_facts
    db_surface = publish_surface_db(tmp_path, (report,), intervals={report.point.canonical_key: interval})
    db_surface_2 = publish_surface_db(tmp_path, (report,), intervals={report.point.canonical_key: interval})
    assert db_surface_2 != db_surface
    assert read_surface_db(db_surface)["surface_id"] == payload["surface_id"]
    assert verify_surface_frozen_facts(db_surface)
    with pytest.raises(Exception, match="complete canonical point grid"):
        load_source_v6_pipeline_input(db_surface, scope=scope, start=interval[0], end=interval[1])
    assert len(scan_surfaces(tmp_path)) == 3
    append_analysis_run(db_surface, "run-1", {"plateaus": 0})
    import duckdb
    connection = duckdb.connect(str(db_surface), read_only=True)
    try:
        manifest_before = connection.execute("select value from manifest where key='manifest_sha256'").fetchone()[0]
        assert connection.execute("select count(*) from frozen_events").fetchone()[0] == len(report.events)
        assert connection.execute("select count(*) from analysis_runs").fetchone()[0] == 1
        assert connection.execute("select value from manifest where key='manifest_sha256'").fetchone()[0] == manifest_before
    finally:
        connection.close()


def test_task6_multi_point_surface_keeps_point_metrics_separate(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface, read_surface

    first_bytes = (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes()
    second_bytes = first_bytes.replace(b'"symbol":"ONUSDT"', b'"symbol":"BTCUSDT"')
    first = normalize_source_v6(first_bytes)
    second = normalize_source_v6(second_bytes)
    payload = read_surface(publish_surface(tmp_path, (first, second)))
    rows = {row["point_key"]: row for row in payload["point_metrics"]}
    assert set(rows) == {first.point.canonical_key, second.point.canonical_key}
    assert all(row["point_event_count"] == 2 for row in rows.values())


def test_task7_plateau_report_is_rebuilt_from_persisted_surface(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_analysis import export_plateau_report
    from mrs3.source_v6_surface import publish_surface_db

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    surface = publish_surface_db(tmp_path, (report,))
    output = tmp_path / "plateau-report"
    manifest = export_plateau_report(surface, output)
    assert manifest["surface_id"]
    assert (output / "plateau_report.xlsx").exists()
    assert {"plateaus.csv", "plateau_members.csv", "before_after.csv", "closema_profiles.csv", "lineage.csv", "diagnostics.csv"}.issubset({path.name for path in output.iterdir()})


def test_task7_plateau_facts_are_stable_under_fragment_order_permutation(tmp_path) -> None:
    from hashlib import sha256
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_analysis import export_plateau_report
    from mrs3.source_v6_surface import publish_surface_db

    first = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    second = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    left = tmp_path / "left"
    right = tmp_path / "right"
    export_plateau_report(publish_surface_db(tmp_path / "surface-left", (first, second)), left)
    export_plateau_report(publish_surface_db(tmp_path / "surface-right", (second, first)), right)
    for name in ("plateaus.csv", "plateau_members.csv", "before_after.csv", "closema_profiles.csv", "lineage.csv", "diagnostics.csv"):
        assert sha256((left / name).read_bytes()).hexdigest() == sha256((right / name).read_bytes()).hexdigest()


def test_task6_surface_identity_is_input_order_independent(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface, read_surface

    first = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    second = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    left = read_surface(publish_surface(tmp_path / "left", (first, second)))
    right = read_surface(publish_surface(tmp_path / "right", (second, first)))
    assert left["surface_id"] == right["surface_id"]


def test_task6_duckdb_surface_order_and_manifest_tamper_are_rejected(tmp_path) -> None:
    import duckdb
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface_db, read_surface_db

    first = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    second = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    left = publish_surface_db(tmp_path / "left", (first, second))
    right = publish_surface_db(tmp_path / "right", (second, first))
    assert read_surface_db(left)["surface_id"] == read_surface_db(right)["surface_id"]
    connection = duckdb.connect(str(left))
    try:
        raw = connection.execute("select value from manifest where key='surface_manifest_json'").fetchone()[0]
        payload = json.loads(raw)
        payload["points"] = ["tampered-point"]
        connection.execute("update manifest set value=? where key='surface_manifest_json'", (json.dumps(payload),))
    finally:
        connection.close()
    with pytest.raises(Exception, match="manifest hash"):
        read_surface_db(left)


def test_task6_surface_persists_overlap_tail_decisions(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface_db, read_surface_db

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    surface = publish_surface_db(tmp_path, (report,), overlap_tail_decisions=({"fragment_id": report.fragment_id, "status": "RESOLVED", "reason": None},))
    assert read_surface_db(surface)["overlap_tail_decisions"] == [{"fragment_id": report.fragment_id, "status": "RESOLVED", "reason": None}]


def test_task6_overlap_decisions_contribute_to_identity_deterministically(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface_db, read_surface_db

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    first = {"fragment_id": report.fragment_id, "status": "RESOLVED", "reason": "A"}
    second = {"fragment_id": report.fragment_id, "status": "PARTIAL", "reason": "B"}
    left = read_surface_db(publish_surface_db(tmp_path / "left", (report,), overlap_tail_decisions=(first, second)))
    reordered = read_surface_db(publish_surface_db(tmp_path / "reordered", (report,), overlap_tail_decisions=(second, first)))
    changed = read_surface_db(publish_surface_db(tmp_path / "changed", (report,), overlap_tail_decisions=(first,)))
    assert left["surface_id"] == reordered["surface_id"]
    assert left["surface_id"] != changed["surface_id"]


def test_task6_surface_records_selected_scope_interval_in_identity(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import publish_surface, read_surface

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    scope = report.point.canonical_key
    left = read_surface(publish_surface(tmp_path / "left", (report,), intervals={scope: (report.report_start_ms, report.report_end_ms)}))
    right = read_surface(publish_surface(tmp_path / "right", (report,), intervals={scope: (report.report_start_ms + 3_600_000, report.report_end_ms)}))
    assert left["surface_id"] != right["surface_id"]
    assert right["selected_intervals"][0]["start_ms"] == report.report_start_ms + 3_600_000


def test_task8_selected_ready_interval_applies_to_every_point_in_scope(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_surface import publish_surface, read_surface

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    variants = [replace(report, point=replace(report.point, shift_bp=shift, close_ma_length=close)) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    start, end = report.report_start_ms, report.report_start_ms + 2 * 86_400_000
    intervals = {fragment.point.canonical_key: (start, end) for fragment in variants}
    payload = read_surface(publish_surface(tmp_path, variants, intervals=intervals))
    assert len(payload["point_metrics"]) == 114
    assert len(payload["selected_intervals"]) == 114
    assert {item["scope_key"] for item in payload["selected_intervals"]} == {fragment.point.canonical_key for fragment in variants}
    assert all(item["TotalTrades"] <= 2 for item in payload["point_metrics"])


def test_task6_duckdb_frozen_digest_rejects_tamper_and_append_preserves_it(tmp_path) -> None:
    import duckdb
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import append_analysis_run, publish_surface_db, read_surface_db, verify_surface_frozen_facts

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    surface = publish_surface_db(tmp_path, (report,))
    before = verify_surface_frozen_facts(surface)
    append_analysis_run(surface, "run-immutable", {"ok": True})
    assert verify_surface_frozen_facts(surface) == before
    duckdb.connect(str(surface)).execute("update frozen_point_facts set facts_json='tampered'").close()
    with pytest.raises(Exception, match="before analysis append"):
        append_analysis_run(surface, "run-after-tamper", {"ok": False})
    check = duckdb.connect(str(surface), read_only=True)
    try:
        assert check.execute("select count(*) from analysis_runs where run_id='run-after-tamper'").fetchone()[0] == 0
    finally:
        check.close()
    with pytest.raises(Exception, match="frozen surface facts hash mismatch"):
        read_surface_db(surface)


def _canonical_v6_surface_fixture(tmp_path):
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_surface import publish_surface

    base = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    fragments = [
        replace(
            base,
            point=replace(base.point, shift_bp=shift, close_ma_length=close),
            fragment_id=f"{base.fragment_id}-{shift}-{close}",
        )
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    ]
    interval = (base.report_start_ms, base.report_end_ms)
    selected = {fragment.point.canonical_key: interval for fragment in fragments}
    return publish_surface(tmp_path, fragments, intervals=selected), interval, fragments


def test_task1_v6_adapter_requires_explicit_scope_and_interval(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    surface = publish_surface(tmp_path, (report,), intervals={report.point.canonical_key: (report.report_start_ms, report.report_end_ms)})
    with pytest.raises(Exception, match="selected scope and non-empty UTC"):
        load_source_v6_pipeline_input(surface)
    with pytest.raises(Exception, match="selected scope and non-empty UTC"):
        load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h")


def test_task1_v6_adapter_maps_published_json_without_fragment_bounds(tmp_path) -> None:
    from mrs3.source_v6_surface import load_source_v6_pipeline_input

    surface, (start, end), _fragments = _canonical_v6_surface_fixture(tmp_path / "json")
    selected = load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    assert len(selected.points) == 114
    assert selected.points["point_id"].str.len().min() > len("ONUSDT|LONG|1h|110|7|2")
    assert selected.points["report_start"].eq(selected.points.attrs["selected_interval"]["start"]).all()
    assert selected.points["report_end"].eq(selected.points.attrs["selected_interval"]["end"]).all()
    assert all(isinstance(item, tuple) for item in selected.points["_event_ids"])


def test_task1_v6_adapter_maps_published_duckdb_with_same_contract(tmp_path) -> None:
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface_db

    _json_surface, (start, end), fragments = _canonical_v6_surface_fixture(tmp_path / "json")
    selected_surface = publish_surface_db(
        tmp_path / "db",
        fragments,
        intervals={fragment.point.canonical_key: (start, end) for fragment in fragments},
    )
    selected = load_source_v6_pipeline_input(selected_surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    assert len(selected.points) == 114
    assert selected.points.attrs["source_surface_id"]


def test_task1_v6_adapter_rejects_incomplete_canonical_grid(tmp_path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface

    report = normalize_source_v6((_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    surface = publish_surface(tmp_path, (report,), intervals={report.point.canonical_key: (report.report_start_ms, report.report_end_ms)})
    with pytest.raises(Exception, match="complete canonical point grid"):
        load_source_v6_pipeline_input(
            surface,
            scope="ONUSDT|LONG|1h",
            start=report.report_start_ms,
            end=report.report_end_ms,
        )


def _rewrite_surface_manifest(path: Path, payload: dict[str, object]) -> None:
    from hashlib import sha256
    from mrs3.source_v6 import _canonical_json

    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    unsigned.pop("created_at_utc", None)
    payload["manifest_sha256"] = sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def test_task1_v6_adapter_rejects_fragment_event_mode_mismatch(tmp_path) -> None:
    from mrs3.source_v6_surface import _payload_frozen_digest, load_source_v6_pipeline_input, read_surface

    surface, (start, end), _fragments = _canonical_v6_surface_fixture(tmp_path)
    payload = read_surface(surface)
    payload["fragments"][0]["event_mode"] = "legacy_trades_proxy"
    payload["frozen_facts_sha256"] = _payload_frozen_digest(payload)
    _rewrite_surface_manifest(surface, payload)
    with pytest.raises(Exception, match="UNSUPPORTED_EVENT_MODE"):
        load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)


def test_task1_v6_adapter_requires_primary_equity_drawdown_metric(tmp_path) -> None:
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, read_surface

    surface, (start, end), _fragments = _canonical_v6_surface_fixture(tmp_path)
    payload = read_surface(surface)
    metric = payload["point_metrics"][0]
    metric.pop("MaxEquityDrawdownPercent", None)
    metric["MaxRealizedDrawdownPercent"] = "0"
    _rewrite_surface_manifest(surface, payload)
    with pytest.raises(Exception, match="MaxEquityDrawdownPercent"):
        load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)


def test_task1_v6_adapter_utc_millis_handles_far_future_boundary() -> None:
    from mrs3.source_v6_surface import _utc_millis

    value = datetime(2100, 1, 1, 0, 0, 0, 123_000, tzinfo=timezone.utc)
    assert _utc_millis(value, "start") == 4_102_444_800_123


def test_task2_v6_analysis_run_is_deterministic_idempotent_and_complete(tmp_path) -> None:
    import duckdb
    from datetime import date
    from hashlib import sha256
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import _canonical_json
    from mrs3.source_v6_surface import (
        load_source_v6_pipeline_input,
        publish_surface_db,
        run_source_v6_analysis,
    )

    _json_surface, (start, end), fragments = _canonical_v6_surface_fixture(tmp_path / "surface")
    surface = publish_surface_db(tmp_path / "surface-db", fragments, intervals={fragment.point.canonical_key: (start, end) for fragment in fragments})
    selected = load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    snapshot = {"ONUSDT": date(2026, 1, 1)}
    snapshot_hash = sha256(_canonical_json({"ONUSDT": "2026-01-01"}).encode("utf-8")).hexdigest()

    first = run_source_v6_analysis(
        surface,
        selected,
        AlgorithmConfig.defaults(),
        algorithm_version="0.7-canonical-phase1",
        listing_dates=snapshot,
        listing_dates_sha256=snapshot_hash,
    )
    second = run_source_v6_analysis(
        surface,
        selected,
        AlgorithmConfig.defaults(),
        algorithm_version="0.7-canonical-phase1",
        listing_dates=snapshot,
        listing_dates_sha256=snapshot_hash,
    )
    assert first["state"] == "COMMITTED"
    assert first["analysis_run_id"] == second["analysis_run_id"]
    assert first["event_mode"] == "real_independent_events"
    assert first["selected_interval"] == {"start_ms": start, "end_ms": end}

    connection = duckdb.connect(str(surface), read_only=True)
    try:
        row = connection.execute(
            "select state, metadata_json from analysis_runs where run_id = ?",
            [first["analysis_run_id"]],
        ).fetchone()
        assert row is not None
        assert row[0] == "COMMITTED"
        metadata = json.loads(row[1])
        assert metadata["surface_id"] == selected.points.attrs["source_surface_id"]
        assert metadata["manifest_sha256"] == selected.points.attrs["source_manifest_sha256"]
        assert metadata["frozen_facts_sha256"] == selected.points.attrs["source_frozen_facts_sha256"]
        assert metadata["listing_dates_sha256"] == snapshot_hash
        assert connection.execute(
            "select count(*) from analysis_run_facts where run_id = ?",
            [first["analysis_run_id"]],
        ).fetchone()[0] >= 5
        assert connection.execute(
            "select count(*) from analysis_run_attempts where run_id = ?",
            [first["analysis_run_id"]],
        ).fetchone()[0] == 6
        assert connection.execute(
            "select count(*) from analysis_run_attempts where run_id = ? and reason='IDEMPOTENT_REUSE'",
            [first["analysis_run_id"]],
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_task2_v6_analysis_requires_algorithm_version_and_supports_cancellation(tmp_path) -> None:
    import duckdb
    from datetime import date
    from hashlib import sha256
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import _canonical_json
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface_db, run_source_v6_analysis

    _json_surface, (start, end), fragments = _canonical_v6_surface_fixture(tmp_path / "surface")
    surface = publish_surface_db(tmp_path / "surface-db", fragments, intervals={fragment.point.canonical_key: (start, end) for fragment in fragments})
    selected = load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    snapshot = {"ONUSDT": date(2026, 1, 1)}
    snapshot_hash = sha256(_canonical_json({"ONUSDT": "2026-01-01"}).encode("utf-8")).hexdigest()
    with pytest.raises(TypeError):
        run_source_v6_analysis(surface, selected, AlgorithmConfig.defaults(), listing_dates=snapshot, listing_dates_sha256=snapshot_hash)
    with pytest.raises(Exception, match="RUN_CANCELLED"):
        run_source_v6_analysis(
            surface,
            selected,
            AlgorithmConfig.defaults(),
            algorithm_version="0.7-canonical-phase1",
            listing_dates=snapshot,
            listing_dates_sha256=snapshot_hash,
            cancel_check=lambda: True,
        )
    connection = duckdb.connect(str(surface), read_only=True)
    try:
        assert connection.execute("select count(*) from analysis_runs").fetchone()[0] == 0
        failed = connection.execute("select state, metadata_json from analysis_run_attempts where state='CANCELLED'").fetchone()
        assert failed is not None
        assert json.loads(failed[1])["attempt_state"] == failed[0]
        assert json.loads(failed[1])["state"] == failed[0]
        assert connection.execute("select count(*) from analysis_run_attempts where state='REQUESTED'").fetchone()[0] == 1
        assert connection.execute("select count(*) from analysis_run_attempts where state='CANCELLED'").fetchone()[0] == 1
    finally:
        connection.close()


def test_task2_v6_analysis_run_id_is_database_unique(tmp_path) -> None:
    import duckdb
    from datetime import date
    from hashlib import sha256
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import _canonical_json
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface_db, run_source_v6_analysis

    _json_surface, (start, end), fragments = _canonical_v6_surface_fixture(tmp_path / "surface")
    surface = publish_surface_db(tmp_path / "surface-db", fragments, intervals={fragment.point.canonical_key: (start, end) for fragment in fragments})
    selected = load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    snapshot = {"ONUSDT": date(2026, 1, 1)}
    snapshot_hash = sha256(_canonical_json({"ONUSDT": "2026-01-01"}).encode("utf-8")).hexdigest()
    result = run_source_v6_analysis(surface, selected, AlgorithmConfig.defaults(), algorithm_version="0.7-canonical-phase1", listing_dates=snapshot, listing_dates_sha256=snapshot_hash)
    connection = duckdb.connect(str(surface), read_only=True)
    try:
        columns = {str(row[1]): int(row[5]) for row in connection.execute("pragma table_info('analysis_runs')").fetchall()}
        assert columns["run_id"] == 1
    finally:
        connection.close()
    connection = duckdb.connect(str(surface))
    try:
        with pytest.raises(duckdb.ConstraintException):
            connection.execute("insert into analysis_runs(run_id, created_at_utc, result_json) values (?, ?, ?)", [result["analysis_run_id"], "now", "{}"])
    finally:
        connection.close()


def test_task2_v6_analysis_revalidates_surface_from_fresh_snapshot_before_commit(tmp_path, monkeypatch) -> None:
    import duckdb
    from datetime import date
    from hashlib import sha256
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import _canonical_json
    import mrs3.pipeline as pipeline_module
    from mrs3.source_v6_surface import load_source_v6_pipeline_input, publish_surface_db, run_source_v6_analysis

    _json_surface, (start, end), fragments = _canonical_v6_surface_fixture(tmp_path / "surface")
    surface = publish_surface_db(tmp_path / "surface-db", fragments, intervals={fragment.point.canonical_key: (start, end) for fragment in fragments})
    selected = load_source_v6_pipeline_input(surface, scope="ONUSDT|LONG|1h", start=start, end=end)
    snapshot = {"ONUSDT": date(2026, 1, 1)}
    snapshot_hash = sha256(_canonical_json({"ONUSDT": "2026-01-01"}).encode("utf-8")).hexdigest()
    original = pipeline_module._analyze_points

    def mutate_then_analyze(points, config):
        with duckdb.connect(str(surface)) as writer:
            writer.execute("update manifest set value='tampered' where key='surface_id'")
        return original(points, config)

    monkeypatch.setattr(pipeline_module, "_analyze_points", mutate_then_analyze)
    with pytest.raises(Exception, match="SURFACE_CHANGED"):
        run_source_v6_analysis(surface, selected, AlgorithmConfig.defaults(), algorithm_version="0.7-canonical-phase1", listing_dates=snapshot, listing_dates_sha256=snapshot_hash)


def test_an_empty_outgoing_fragment_does_not_exclude_incoming_facts() -> None:
    """Z5: seam exclusion de-duplicates; there is nothing to de-duplicate here.

    ADR-0013 excludes an incoming overlap cycle because the outgoing report
    already carries the same fact — "both report windows cannot contribute the
    same overlap facts". A zero-activity outgoing carries no fact at all, so
    excluding the incoming's cycle deletes evidence that nothing replaces, and
    the batch would report `COMMITTED` while doing it.
    """
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_batch, resolve_ownership

    empty = normalize_source_v6(
        (_FIXTURES / "source_v6_zero_activity.html").read_bytes(), source_name="zero.html"
    )
    incoming = normalize_source_v6(
        (_FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes(), source_name="b.html"
    )
    # The premise of the test: same contract, real overlap, and the incoming
    # cycle opens inside it — the exact shape that triggers seam exclusion.
    assert empty.point == incoming.point
    assert empty.settings_fingerprint == incoming.settings_fingerprint
    assert (empty.actions, empty.cycles, empty.events) == ((), (), ())
    assert any(cycle.open_timestamp_ms < empty.report_end_ms for cycle in incoming.cycles)

    decision = resolve_ownership(empty, incoming)
    assert decision.status == "RESOLVED"
    assert decision.reason == "EMPTY_OUTGOING_NOTHING_TO_EXCLUDE"
    assert decision.boundary_ms is None

    resolution = resolve_batch((empty, incoming))
    assert resolution.status == "COMMITTED"
    assert {fragment.source_name for fragment in resolution.active_fragments} == {
        "zero.html",
        "b.html",
    }


def test_an_empty_fragment_never_wins_an_identical_window_by_hash_order() -> None:
    """Z5: a tie-break must not hand a tested window to the fragment with no facts.

    `resolve_batch` seeds `active` with the first fragment by
    `(report_start_ms, fragment_id)`. With identical windows that is decided by
    hash order, so an empty fragment could take the window and the fragment
    carrying real actions could be the one reported `AMBIGUOUS_INCOMING`.
    """
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_batch

    empty = normalize_source_v6(
        (_FIXTURES / "source_v6_zero_activity.html").read_bytes(), source_name="zero.html"
    )
    real = normalize_source_v6(
        (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(), source_name="a.html"
    )
    assert (empty.report_start_ms, empty.report_end_ms) == (real.report_start_ms, real.report_end_ms)
    assert real.actions and not empty.actions

    for order in ((empty, real), (real, empty)):
        resolution = resolve_batch(order)
        # The contradiction is still surfaced rather than resolved silently.
        assert resolution.status == "PARTIAL"
        # But the window belongs to the fragment that observed something.
        assert [fragment.source_name for fragment in resolution.active_fragments] == ["a.html"]


def test_an_empty_incoming_fragment_does_not_delete_the_outgoing_open_tail() -> None:
    """Z5, mirrored. The first guard only looked at the outgoing side.

    Seam exclusion drops the outgoing's open tail because the incoming report is
    assumed to carry its continuation. An empty incoming carries nothing, so the
    tail is deleted with nothing replacing it — one cycle, two actions and two
    events, under a batch reported `COMMITTED`. That is contradictory evidence,
    not de-duplication: fail closed instead.
    """
    from dataclasses import replace

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_stitch import resolve_batch, resolve_ownership

    outgoing = normalize_source_v6(
        (_FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes(), source_name="a.html"
    )
    empty = normalize_source_v6(
        (_FIXTURES / "source_v6_zero_activity.html").read_bytes(), source_name="zero.html"
    )
    day = 86_400_000
    incoming = replace(
        empty,
        report_start_ms=empty.report_start_ms + 3 * day,
        report_end_ms=empty.report_end_ms + 3 * day,
    )
    assert any(not cycle.closed for cycle in outgoing.cycles), "premise: the outgoing has a tail"

    decision = resolve_ownership(outgoing, incoming)
    assert decision.status == "UNRESOLVED"
    assert decision.reason == "BRIDGE_NOT_COVERED"

    resolution = resolve_batch((outgoing, incoming))
    assert resolution.status == "PARTIAL"
    assert [item.source_name for item in resolution.active_fragments] == ["a.html"]
    # `persist_batch_resolution` writes fact rows only for RESOLVED and
    # USE_OLD_WITH_SEAM_EXCLUSION, so an UNRESOLVED decision deactivates
    # nothing and the outgoing's tail survives intact.
    assert [item.status for item in resolution.decisions] == ["UNRESOLVED"]


def test_batch_ordering_and_persisted_pairing_use_the_same_key() -> None:
    """The tie-break must not desynchronise `resolve_batch` from `persist_batch_resolution`.

    `resolve_batch` orders by `(start, empty, fragment_id)`; the persist step
    re-derives the outgoing side by bisecting its own ordering. If the two keys
    disagree, a decision can be persisted against a different outgoing fragment
    than the one it was computed from.
    """
    import inspect

    from mrs3 import source_v6_stitch

    source = inspect.getsource(source_v6_stitch)
    assert source.count("_batch_order_key") >= 3, "both orderings must share one key function"
