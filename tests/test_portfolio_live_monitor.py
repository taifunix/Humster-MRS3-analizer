from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from mrs3.portfolio.live_monitor import (
    ATTRIBUTED,
    AVAILABLE,
    HEALTHY,
    INCONSISTENT,
    LIMITER_BREACH,
    LIMITER_OVERFLOW,
    LIMITER_RECOVERED,
    UNKNOWN,
    UNATTRIBUTED,
    LiveMonitor,
    attribute_execution,
)
from mrs3.portfolio.live_store import LiveStore, LiveStoreError
from mrs3.portfolio import live_reconcile


def manifest(*, ambiguous: bool = False) -> dict[str, object]:
    members = [
        {"strategy_id": "s1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m", "priority": 1, "counted": True},
        {"strategy_id": "s2", "symbol": "ETHUSDT", "side": "SHORT", "timeframe": "5m", "priority": 1, "counted": True},
    ]
    if ambiguous:
        members.append({"strategy_id": "s3", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "15m", "priority": 1, "counted": True})
    return {"manifest_schema_version": 1, "deployment_id": "dep-1", "manifest_id": "man-1", "version": 1, "account_alias": "paper", "account_id": "acct-1", "portfolio_set": "set-1", "evaluation": "eval-1", "run_id": "run-1", "attempt_id": "attempt-1", "semantic_digest": "0" * 64, "series_version": "series-1", "metrics_version": "metrics-1", "source": "fixture", "cashflows_supported": False, "member_composition": members, "limiter_settings": {"settings_version": "lim-1", "L": 1, "grace_seconds": 10}, "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10, "freshness_seconds": 60}, "read_only_permissions": ["wallet.read"]}


def rest() -> dict[str, object]:
    return {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100.00", "available_margin": "80.00"}, "positions": [{"symbol": "BTCUSDT", "side": "LONG", "size": "1", "priority": 1}], "orders": [], "executions": [], "watermarks": {"wallet": 0}}


def test_reconcile_is_complete_and_read_models_keep_decimal_strings(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    result = monitor.reconcile(rest())
    assert result.status == HEALTHY
    assert monitor.account_dashboard()["equity"] == "100.00"
    assert monitor.symbol_dashboard()[0]["size"] == "1"
    assert monitor.account_dashboard()["provenance"]["manifest_id"] == "man-1"
    store.close()


def test_missing_supported_cashflow_channel_keeps_adjusted_metrics_unknown() -> None:
    configured = {**manifest(), "cashflows_supported": True}
    monitor = LiveMonitor(configured, clock=lambda: "2026-09-07T00:00:01Z")
    result = monitor.reconcile(rest())
    assert result.status != HEALTHY
    assert monitor.account_metrics()["availability"] == UNKNOWN


def test_fact_order_normalizes_fractional_z_and_offset_timestamps() -> None:
    from mrs3.portfolio.live_monitor import _fact_order

    earlier = _fact_order({"effective_at_utc": "2026-09-07T00:00:01.100Z", "observed_at_utc": "2026-09-07T00:00:01.100Z", "source_id": "z"}, "z")
    later = _fact_order({"effective_at_utc": "2026-09-07T02:00:01.100001+02:00", "observed_at_utc": "2026-09-07T02:00:01.100001+02:00", "source_id": "offset"}, "offset")
    assert earlier is not None and later is not None
    assert earlier < later


def test_account_metrics_use_normalized_utc_cashflow_order_for_intermediate_equity() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:03Z")
    first = rest()
    first["wallet"] = {"equity": "100", "currency": "USDT"}
    assert monitor.reconcile(first).status == HEALTHY
    early = {"cashflow_id": "early", "timestamp_utc": "2026-09-07T02:00:01.400+02:00", "observed_at_utc": "2026-09-07T02:00:01.400+02:00", "amount": "4", "direction": "DEPOSIT", "currency": "USDT"}
    late = {"cashflow_id": "late", "timestamp_utc": "2026-09-07T00:00:01.500Z", "observed_at_utc": "2026-09-07T00:00:01.500Z", "amount": "3", "direction": "DEPOSIT", "currency": "USDT"}
    second = {**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:01.450Z", "wallet": {"equity": "110", "currency": "USDT"}, "cashflows": [late, early]}
    assert monitor.reconcile(second).status == HEALTHY
    path = monitor.account_metrics()["adjusted_equity_path"]
    assert path[-1]["value"] == "106"


def test_first_snapshot_malformed_cashflow_order_makes_metrics_unknown() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:03Z")
    malformed = {
        "cashflow_id": "bad-order",
        "effective_at_utc": "not-a-timestamp",
        "timestamp_utc": "2026-09-07T00:00:01Z",
        "observed_at_utc": "2026-09-07T00:00:01Z",
        "source_sequence": "not-an-integer",
        "amount": "5",
        "direction": "DEPOSIT",
        "currency": "USDT",
    }
    assert monitor.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}, "cashflows": [malformed]}).status == HEALTHY
    metrics = monitor.account_metrics()
    assert metrics["availability"] == UNKNOWN and metrics["net_trading_pnl"] is None


def test_mixed_malformed_reconcile_boundaries_block_restart(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    store.append_reconciliation("dep-1", "recon-valid-boundary", INCONSISTENT, {"observed_at": "2026-09-07T00:00:02Z", "snapshot_id": "snap-valid"})
    store.append_reconciliation("dep-1", "recon-malformed-boundary", INCONSISTENT, {"observed_at": "not-a-timestamp", "snapshot_id": "snap-bad"})
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_metrics()["availability"] == UNKNOWN
    store.close()


def test_partial_stale_disconnect_and_gap_are_fail_closed(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T01:00:00Z")
    assert monitor.reconcile({"deployment_id": "dep-1"}).status == UNKNOWN
    assert monitor.reconcile(rest()).status == UNKNOWN
    fresh = {**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:59:59Z", "watermarks": {"wallet": 0}}
    assert monitor.reconcile(fresh, ({"channel": "wallet", "sequence": 2, "event_id": "w2"},)).status == INCONSISTENT
    store.close()


def test_malformed_none_sections_are_unknown_without_crashing(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    result = monitor.reconcile({**rest(), "positions": None})
    assert result.status == UNKNOWN and "POSITIONS_INCOMPLETE" in result.reasons
    assert store.latest_checkpoint("dep-1", "wallet") is None
    store.close()


def test_nonhealthy_or_conflicting_reconcile_does_not_advance_checkpoint(tmp_path: Path) -> None:
    current = ["2026-09-07T00:00:01Z"]
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: current[0])
    first = monitor.reconcile(rest(), ({"channel": "wallet", "sequence": 1, "event_id": "w1", "balance": "100"},))
    assert first.status == HEALTHY and store.latest_checkpoint("dep-1", "wallet")["sequence"] == 1
    current[0] = "2026-09-07T01:00:00Z"
    stale = monitor.reconcile({**rest(), "snapshot_id": "snap-stale", "watermarks": {"wallet": 1}}, ({"channel": "wallet", "sequence": 2, "event_id": "w2", "balance": "101"},))
    assert stale.status == UNKNOWN and store.latest_checkpoint("dep-1", "wallet")["sequence"] == 1
    current[0] = "2026-09-07T00:00:02Z"
    conflict = monitor.reconcile({**rest(), "snapshot_id": "snap-conflict", "watermarks": {"wallet": 1}}, ({"channel": "wallet", "sequence": 2, "event_id": "w1", "balance": "999"},))
    assert conflict.status == INCONSISTENT and store.latest_checkpoint("dep-1", "wallet")["sequence"] == 1


def test_execution_is_idempotent_and_forced_close_requires_evidence(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    first = monitor.reconcile(rest(), ({"channel": "execution", "sequence": 1, "event_id": "fill-1", "execution_id": "fill-1", "account_id": "acct-1", "symbol": "BTCUSDT", "side": "LONG", "realized_pnl": "2.5", "close_reason": "forced_close"},))
    assert first.status == HEALTHY and first.realised_pnl == "2.5"
    second = monitor.reconcile({**rest(), "snapshot_id": "snap-2", "watermarks": {"execution": 1}}, ())
    assert second.executions_applied == 0
    assert second.forced_close_evidence == "UNKNOWN"
    store.close()


def test_replayed_ws_identity_with_new_sequence_is_applied_once() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    event = {"channel": "execution", "sequence": 1, "event_id": "fill-dup", "execution_id": "fill-dup", "account_id": "acct-1", "symbol": "BTCUSDT", "side": "LONG", "realized_pnl": "2.5"}
    replay = {**event, "sequence": 2}
    result = monitor.reconcile(rest(), (event, replay))
    assert result.executions_applied == 1


def test_rest_total_pnl_is_authoritative_and_no_store_read_model_fails_closed() -> None:
    snapshot = {**rest(), "wallet": {"equity": "100", "realized_pnl": "100"}}
    monitor = LiveMonitor(manifest())
    result = monitor.reconcile(snapshot, ({"channel": "execution", "sequence": 1, "event_id": "fill-1", "execution_id": "fill-1", "account_id": "acct-1", "realized_pnl": "2.5"},))
    assert result.realised_pnl == "100"
    assert monitor.account_dashboard()["availability"] == UNKNOWN


def test_naive_observed_or_injected_clock_is_invalid() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01")
    with pytest.raises(ValueError, match="clock"):
        monitor.reconcile(rest())
    monitor = LiveMonitor(manifest(), clock=lambda: "not-a-time")
    with pytest.raises(ValueError, match="clock"):
        monitor.reconcile(rest())
    assert LiveMonitor(manifest()).reconcile({**rest(), "observed_at": "2026-09-07T00:00:00"}).status == UNKNOWN


def test_stable_and_ambiguous_attribution() -> None:
    assert attribute_execution({"symbol": "BTCUSDT", "side": "LONG"}, manifest()).strategy_id == "s1"
    ambiguous = attribute_execution({"symbol": "BTCUSDT", "side": "LONG"}, manifest(ambiguous=True))
    assert ambiguous.status == UNATTRIBUTED
    assert attribute_execution({"strategy_id": "s2"}, manifest()).strategy_id == "s2"


def test_watchdog_overflow_recovery_breach_and_exemption() -> None:
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    monitor = LiveMonitor(manifest())
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "priority": 1, "qty": "1"}, {"symbol": "XRPUSDT", "side": "LONG", "priority": 1, "qty": "1"}, {"symbol": "ETHUSDT", "side": "SHORT", "priority": 0, "qty": "1"}]
    overflow = monitor.watchdog(positions, now=now)
    assert overflow.state == LIMITER_OVERFLOW and overflow.counted_positions == 2
    recovered = monitor.watchdog([], now=now.replace(second=5), previous=overflow)
    assert recovered.state == LIMITER_RECOVERED
    breach = monitor.watchdog(positions, now=now.replace(second=10), previous=overflow)
    assert breach.state == LIMITER_BREACH
    disabled = LiveMonitor({**manifest(), "watchdog_settings": {"settings_version": "wd-0", "L": 0, "grace_seconds": 1}}).watchdog(positions, now=now)
    assert disabled.state == "DISABLED"


def test_watchdog_requires_explicit_state_and_dedupes_pair_slots() -> None:
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    monitor = LiveMonitor(manifest())
    duplicate_pair = [{"symbol": "BTCUSDT", "side": "LONG", "priority": 1, "qty": "1"}, {"symbol": "BTCUSDT", "side": "LONG", "priority": 1, "qty": "3"}]
    assert monitor.watchdog(duplicate_pair, now=now).counted_positions == 1
    assert monitor.watchdog([{"symbol": "BTCUSDT", "side": "LONG", "priority": 1}], now=now).state == UNKNOWN
    assert monitor.watchdog([{"symbol": "BTCUSDT", "side": "LONG", "priority": True, "qty": "1"}], now=now).state == UNKNOWN


def test_watchdog_persists_only_transitions(tmp_path: Path) -> None:
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store)
    positions = [{"pair_slot": "btc-slot", "symbol": "BTCUSDT", "side": "LONG", "priority": 1, "qty": "1"}, {"pair_slot": "eth-slot", "symbol": "ETHUSDT", "side": "SHORT", "priority": 1, "qty": "1"}]
    first = monitor.watchdog(positions, now=now)
    second = monitor.watchdog([{**positions[0], "qty": "2"}, positions[1]], now=now.replace(second=1))
    recovered = monitor.watchdog([], now=now.replace(second=2), previous=second)
    monitor.watchdog([], now=now.replace(second=3), previous=recovered)
    rows = store.rows("watchdog_findings")
    assert first.state == second.state == "LIMITER_OVERFLOW" and recovered.state == "LIMITER_RECOVERED"
    assert [row["state"] for row in rows] == ["LIMITER_OVERFLOW", "LIMITER_RECOVERED"]
    store.close()


def test_resize_requires_compatible_typed_capacities_and_drift_is_versioned() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:10Z")
    capacity = lambda value: {"value": value, "unit": "USDT", "provenance": "fixture", "observed_at": "2026-09-07T00:00:00Z", "settings_version": "wd-1"}
    recommendation = monitor.recommend_resize(margin_capacity=capacity("5"), liquidity_capacity=capacity("3"))
    assert recommendation["status"] == "AVAILABLE" and recommendation["recommendation"] == "3"
    assert monitor.recommend_resize(margin_capacity=capacity("5"), liquidity_capacity={**capacity("3"), "unit": "BTC"})["status"] == UNKNOWN
    findings = monitor.evaluate_drift({"symbols": ["BTCUSDT"], "leverage": {"BTCUSDT": "2"}, "order_size": {"BTCUSDT": "1"}, "config_version": "a"}, {"symbols": ["ETHUSDT"], "leverage": {"BTCUSDT": "3"}, "order_size": {"BTCUSDT": "2"}, "config_version": "b"})
    assert {item["finding"] for item in findings} == {"UNEXPECTED_SYMBOL", "LEVERAGE_DRIFT", "ORDER_SIZE_DRIFT", "CONFIG_DRIFT"}
    assert monitor.recommend_resize(margin_capacity={**capacity("5"), "value": "-1"}, liquidity_capacity=capacity("3"))["status"] == UNKNOWN
    assert monitor.recommend_resize(margin_capacity={**capacity("5"), "observed_at": "2026-09-07T00:00:20Z"}, liquidity_capacity=capacity("3"))["status"] == UNKNOWN


def test_hostile_exchange_fake_is_never_called() -> None:
    class Hostile:
        def __getattr__(self, name: str):
            if name in {"create_order", "cancel_order", "set_leverage", "mutate_config", "notify"}:
                raise AssertionError(name)
            raise AttributeError(name)

    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    monitor.exchange = Hostile()  # type: ignore[attr-defined]
    assert monitor.reconcile(rest()).status == HEALTHY


def test_monitor_fails_closed_on_conflicting_stored_manifest(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    with pytest.raises(LiveStoreError, match="manifest"):
        LiveMonitor({**manifest(), "semantic_digest": "1" * 64}, store)
    store.close()


def test_missing_or_invalid_freshness_policy_is_unknown() -> None:
    without_policy = {**manifest(), "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10}}
    assert LiveMonitor(without_policy, clock=lambda: "2026-09-07T00:00:01Z").reconcile(rest()).status == UNKNOWN
    invalid = {**manifest(), "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10, "freshness_seconds": "60"}}
    assert LiveMonitor(invalid, clock=lambda: "2026-09-07T00:00:01Z").reconcile(rest()).status == UNKNOWN


def test_account_mismatch_is_reconciliation_only_and_local_pnl_is_scoped(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    mismatched = {**rest(), "account_id": "acct-2", "executions": [{"execution_id": "foreign", "account_id": "acct-2", "realized_pnl": "9"}]}
    result = monitor.reconcile(mismatched)
    assert result.status == INCONSISTENT and result.realised_pnl is None
    assert not store.rows("account_snapshots") and not store.rows("execution_events")
    assert len(store.rows("reconciliations")) == 1
    store.close()


def test_local_execution_pnl_does_not_survive_gap_or_unknown(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    first = monitor.reconcile(rest(), ({"channel": "execution", "sequence": 1, "event_id": "fill-1", "execution_id": "fill-1", "account_id": "acct-1", "realized_pnl": "2.5"},))
    assert first.realised_pnl == "2.5"
    gap = monitor.reconcile({**rest(), "snapshot_id": "snap-gap", "watermarks": {"execution": 0}}, ({"channel": "execution", "sequence": 2, "event_id": "fill-2", "execution_id": "fill-2", "account_id": "acct-1", "realized_pnl": "7"},))
    assert gap.status == INCONSISTENT and gap.realised_pnl is None
    store.close()


def test_malformed_reconcile_identities_return_unknown_and_reconciliation_only(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    result = monitor.reconcile({**rest(), "deployment_id": 7, "snapshot_id": {}})
    assert result.status in {UNKNOWN, INCONSISTENT}
    assert not store.rows("account_snapshots")
    assert len(store.rows("reconciliations")) == 1
    store.close()


def test_duplicate_count_only_counts_duplicate_execution_events() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    wallet = {"channel": "wallet", "sequence": 1, "event_id": "wallet-1"}
    execution = {"channel": "execution", "sequence": 1, "event_id": "fill-1", "execution_id": "fill-1", "account_id": "acct-1", "realized_pnl": "2.5"}
    result = monitor.reconcile(rest(), (wallet, wallet, execution, execution))
    assert result.executions_duplicate == 1


def test_raw_mapping_without_known_members_cannot_attribute() -> None:
    result = attribute_execution({"strategy_id": "s1"}, {"strategies": {}})
    assert result.status != "ATTRIBUTED"


def test_monitor_delegates_reduction_to_live_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    original = live_reconcile.LiveReconciler.reconcile
    calls = []

    def wrapped(self, pages, ws_events=(), **kwargs):
        calls.append((pages, tuple(ws_events)))
        return original(self, pages, ws_events, **kwargs)

    monkeypatch.setattr(live_reconcile.LiveReconciler, "reconcile", wrapped)
    assert LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z").reconcile(rest()).status == HEALTHY
    assert calls


def test_order_link_attribution_is_explicit_and_mapping_drift_is_unknown() -> None:
    configured = manifest()
    configured["member_composition"][0]["orderLinkId"] = "link-btc"  # type: ignore[index]
    assert attribute_execution({"orderLinkId": "link-btc", "symbol": "BTCUSDT", "side": "LONG"}, configured).status == ATTRIBUTED
    assert attribute_execution({"orderLinkId": "link-moved", "symbol": "BTCUSDT", "side": "LONG"}, configured).status == UNKNOWN


def test_account_metrics_remove_cashflows_and_preserve_negative_high_water() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    first = rest()
    first["wallet"] = {"equity": "100", "currency": "USDT"}
    first["cashflows"] = [{"cashflow_id": "cf-start", "timestamp_utc": "2026-09-07T00:00:00Z", "amount": "50", "direction": "DEPOSIT", "currency": "USDT"}]
    monitor.reconcile(first)
    second = {**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "160", "currency": "USDT"}, "cashflows": [
        {"cashflow_id": "cf-in", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "50", "direction": "DEPOSIT", "currency": "USDT"},
        {"cashflow_id": "cf-out", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "-10", "direction": "WITHDRAWAL", "currency": "USDT"},
        {"cashflow_id": "cf-transfer", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "25", "classification": "INTERNAL_TRANSFER", "internal": True, "scope": "INTERNAL", "boundary": False, "currency": "USDT"},
    ]}
    monitor.reconcile(second)
    dashboard = monitor.account_dashboard()
    assert dashboard["adjusted_equity_path"][-1]["value"] == "120"
    assert dashboard["net_trading_pnl"] == "20"
    assert dashboard["max_drawdown"] == "0"
    negative = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    negative.reconcile({**rest(), "wallet": {"equity": "-100", "currency": "USDT"}})
    negative.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "-120", "currency": "USDT"}})
    negative_dashboard = negative.account_dashboard()
    assert negative_dashboard["max_drawdown"] == "20"
    assert negative_dashboard["max_drawdown_pct"] is None


def test_account_metrics_currency_mismatch_and_unknown_classification_fail_closed() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    monitor.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}})
    monitor.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "120", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-unknown", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "5", "direction": "OTHER", "currency": "USDT"}]})
    dashboard = monitor.account_dashboard()
    assert dashboard["net_trading_pnl"] is None
    mismatch = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    mismatch.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}})
    mismatch.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "120", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-eur", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "5", "direction": "DEPOSIT", "currency": "EUR"}]})
    assert mismatch.account_dashboard()["adjusted_equity"] is None


def test_bare_transfer_is_unknown_but_proven_internal_nonboundary_is_zero() -> None:
    bare = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    bare.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}})
    bare.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "120", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-bare-transfer", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "5", "classification": "TRANSFER", "currency": "USDT"}]})
    assert bare.account_metrics()["availability"] == UNKNOWN
    proven = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    proven.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}})
    proven.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "120", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-proven-transfer", "timestamp_utc": "2026-09-07T00:00:10Z", "amount": "5", "classification": "TRANSFER", "internal": True, "scope": "INTERNAL", "boundary": False, "currency": "USDT"}]})
    assert proven.account_metrics()["availability"] == AVAILABLE and proven.account_metrics()["net_trading_pnl"] == "20"


def test_cashflow_merge_uses_accepted_effective_timestamp() -> None:
    from mrs3.portfolio.live_monitor import _fact_order, _fact_time

    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:20Z")
    monitor.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}})
    flow = {"cashflow_id": "cf-effective", "effective_at_utc": "2026-09-07T00:00:10Z", "timestamp_utc": "2026-09-07T00:00:20Z", "observed_at_utc": "2026-09-07T00:00:10Z", "amount": "10", "direction": "DEPOSIT", "currency": "USDT"}
    monitor.reconcile({**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:10Z", "wallet": {"equity": "110", "currency": "USDT"}, "cashflows": [flow]})
    accepted = next(item for item in monitor._cashflow_points if item.get("cashflow_id") == "cf-effective")
    assert _fact_order(accepted, "cf-effective")[0] == _fact_time(accepted)
    assert monitor.account_metrics()["adjusted_equity_path"][-1]["value"] == "100"


def test_margin_timeline_recomputes_on_numerator_or_denominator_and_marks_stale() -> None:
    monitor = LiveMonitor({**manifest(), "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10, "freshness_seconds": 5}}, clock=lambda: "2026-09-07T00:00:04Z")
    events = (
        {"event_id": "margin-1", "timestamp_utc": "2026-09-07T00:00:00Z", "initial_margin": "10", "maintenance_margin": "5", "margin_balance": "100", "currency": "USDT"},
        {"event_id": "margin-2", "timestamp_utc": "2026-09-07T00:00:03Z", "margin_balance": "200", "currency": "USDT"},
    )
    timeline = monitor.margin_timeline(events)
    assert len(timeline) == 2
    assert timeline[0]["im_load"] == "10" and timeline[1]["im_load"] == "5"
    assert monitor.margin_timeline(events, now="2026-09-07T00:00:20Z")[-1]["im_load"] == "5"
    assert monitor.margin_dashboard(events, now="2026-09-07T00:00:20Z")["im_load"] is None


def test_margin_history_uses_event_time_for_carried_fact_freshness() -> None:
    monitor = LiveMonitor({**manifest(), "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10, "freshness_seconds": 5}}, clock=lambda: "2026-09-07T00:00:30Z")
    events = (
        {"event_id": "margin-1", "timestamp_utc": "2026-09-07T00:00:00Z", "initial_margin": "10", "maintenance_margin": "5", "margin_balance": "100", "currency": "USDT"},
        {"event_id": "margin-2", "timestamp_utc": "2026-09-07T00:00:04Z", "margin_balance": "200", "currency": "USDT"},
        {"event_id": "margin-3", "timestamp_utc": "2026-09-07T00:00:07Z", "margin_balance": "300", "currency": "USDT"},
    )
    timeline = monitor.margin_timeline(events, now="2026-09-07T00:01:00Z")
    assert timeline[1]["status"] == AVAILABLE and timeline[1]["im_load"] == "5"
    assert timeline[2]["status"] == UNKNOWN and timeline[2]["im_load"] is None


def test_equal_timestamp_margin_conflict_invalidates_only_component_until_reestablished() -> None:
    monitor = LiveMonitor({**manifest(), "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10, "freshness_seconds": 60}})
    events = (
        {"event_id": "a", "timestamp_utc": "2026-09-07T00:00:00Z", "initial_margin": "10", "maintenance_margin": "5", "margin_balance": "100", "currency": "USDT"},
        {"event_id": "b", "timestamp_utc": "2026-09-07T00:00:00Z", "initial_margin": "12", "currency": "USDT"},
        {"event_id": "c", "timestamp_utc": "2026-09-07T00:00:01Z", "margin_balance": "200", "currency": "USDT"},
        {"event_id": "d", "timestamp_utc": "2026-09-07T00:00:02Z", "initial_margin": "14", "currency": "USDT"},
    )
    timeline = monitor.margin_timeline(events)
    assert timeline[0]["status"] == INCONSISTENT and timeline[0]["initial_margin"] is None and timeline[0]["denominator"] == "100"
    assert timeline[1]["status"] == INCONSISTENT and timeline[1]["initial_margin"] is None and timeline[1]["denominator"] == "200"
    assert timeline[2]["status"] == AVAILABLE and timeline[2]["im_load"] == "7"


def test_zero_margin_components_are_available_when_denominator_is_positive() -> None:
    monitor = LiveMonitor(manifest())
    timeline = monitor.margin_timeline((
        {"event_id": "zero", "timestamp_utc": "2026-09-07T00:00:00Z", "initial_margin": "0", "maintenance_margin": "0", "margin_balance": "100", "currency": "USDT"},
    ))
    assert timeline[0]["status"] == AVAILABLE and timeline[0]["im_load"] == "0" and timeline[0]["mm_load"] == "0"


def test_rejected_ws_facts_cannot_drive_forced_close_or_margin_models() -> None:
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    forced_close = {"channel": "execution", "sequence": 1, "event_id": "close-1", "execution_id": "close-1", "account_id": "acct-1", "close_reason": "forced_close"}
    margin = {"channel": "wallet", "sequence": 1, "event_id": "wallet-1", "timestamp_utc": "2026-09-07T00:00:01Z", "initial_margin": "10", "maintenance_margin": "5", "margin_balance": "100", "currency": "USDT"}
    watermarked = monitor.reconcile({**rest(), "watermarks": {"wallet": 1, "positions": 0, "orders": 0, "executions": 1}}, (forced_close, margin))
    assert watermarked.status == HEALTHY and watermarked.forced_close_evidence == UNKNOWN
    assert monitor.account_dashboard()["forced_close_evidence"] == UNKNOWN
    assert monitor.margin_dashboard()["status"] == UNKNOWN
    gapped = monitor.reconcile({**rest(), "snapshot_id": "snap-2", "watermarks": {"wallet": 0, "positions": 0, "orders": 0, "executions": 0}}, ({**forced_close, "sequence": 2, "event_id": "close-2", "execution_id": "close-2"}, {**margin, "sequence": 2, "event_id": "wallet-2"}))
    assert gapped.status == INCONSISTENT and gapped.forced_close_evidence == UNKNOWN
    assert monitor.margin_dashboard()["status"] == UNKNOWN


def test_restart_fails_closed_across_bad_reconcile_until_new_healthy_baseline(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-gap"}, ({"channel": "wallet", "sequence": 2, "event_id": "wallet-gap"},)).status == INCONSISTENT
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_dashboard()["availability"] == UNKNOWN
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-new", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "120"}, "watermarks": {"wallet": 1, "positions": 1, "orders": 1, "executions": 1}}).status == HEALTHY
    recovered = LiveMonitor(manifest(), store)
    dashboard = recovered.account_dashboard()
    assert dashboard["equity"] == "120" and len(dashboard["adjusted_equity_path"]) == 1
    store.close()


def test_restart_rejects_pre_boundary_flow_inside_retained_effective_window(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    pre_boundary = {"cashflow_id": "cf-pre", "timestamp_utc": "2026-09-07T00:00:03Z", "observed_at_utc": "2026-09-07T00:00:00Z", "observed_at": "2026-09-07T00:00:04Z", "amount": "50", "direction": "DEPOSIT", "currency": "USDT"}
    post_baseline = {"cashflow_id": "cf-post", "timestamp_utc": "2026-09-07T00:00:03Z", "observed_at_utc": "2026-09-07T00:00:04Z", "amount": "10", "direction": "DEPOSIT", "currency": "USDT"}
    assert monitor.reconcile({**rest(), "wallet": {"equity": "100", "currency": "USDT"}, "cashflows": [pre_boundary]}).status == HEALTHY
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-gap", "observed_at": "2026-09-07T00:00:01Z"}, ({"channel": "wallet", "sequence": 2, "event_id": "wallet-gap"},)).status == INCONSISTENT
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-baseline", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "100", "currency": "USDT"}, "watermarks": {"wallet": 1, "positions": 1, "orders": 1, "executions": 1}}).status == HEALTHY
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-post", "observed_at": "2026-09-07T00:00:04Z", "wallet": {"equity": "110", "currency": "USDT"}, "cashflows": [post_baseline], "watermarks": {"wallet": 2, "positions": 2, "orders": 2, "executions": 2}}).status == HEALTHY
    restarted = LiveMonitor(manifest(), store)
    dashboard = restarted.account_dashboard()
    assert restarted.account_metrics()["availability"] == UNKNOWN and dashboard["net_trading_pnl"] is None
    store.close()


def test_restart_keeps_post_boundary_flow_inside_retained_effective_window(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-gap"}, ({"channel": "wallet", "sequence": 2, "event_id": "wallet-gap"},)).status == INCONSISTENT
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-baseline", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "100", "currency": "USDT"}, "watermarks": {"wallet": 1, "positions": 1, "orders": 1, "executions": 1}}).status == HEALTHY
    post_boundary = {"cashflow_id": "cf-post-positive", "timestamp_utc": "2026-09-07T00:00:03Z", "observed_at_utc": "2026-09-07T00:00:03Z", "amount": "10", "direction": "DEPOSIT", "currency": "USDT"}
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-post", "observed_at": "2026-09-07T00:00:04Z", "wallet": {"equity": "110", "currency": "USDT"}, "cashflows": [post_boundary], "watermarks": {"wallet": 2, "positions": 2, "orders": 2, "executions": 2}}).status == HEALTHY
    restarted = LiveMonitor(manifest(), store)
    metrics = restarted.account_metrics()
    assert metrics["availability"] == AVAILABLE and metrics["adjusted_equity"] == "100" and metrics["net_trading_pnl"] == "0"
    store.close()


def test_restart_clean_and_post_boundary_reject_nonfinite_equity_rows(tmp_path: Path) -> None:
    clean_store = LiveStore(tmp_path / "clean.sqlite3")
    clean_monitor = LiveMonitor(manifest(), clean_store, clock=lambda: "2026-09-07T00:00:01Z")
    assert clean_monitor.reconcile(rest()).status == HEALTHY
    clean_store.append_account_snapshot({"deployment_id": "dep-1", "snapshot_id": "snap-bad-equity", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "NaN", "currency": "USDT"}})
    clean_restart = LiveMonitor(manifest(), clean_store)
    assert clean_restart.account_metrics()["availability"] == UNKNOWN
    clean_store.close()

    post_store = LiveStore(tmp_path / "post.sqlite3")
    post_monitor = LiveMonitor(manifest(), post_store, clock=lambda: "2026-09-07T00:00:01Z")
    assert post_monitor.reconcile(rest()).status == HEALTHY
    assert post_monitor.reconcile({**rest(), "snapshot_id": "snap-gap"}, ({"channel": "wallet", "sequence": 2, "event_id": "wallet-gap"},)).status == INCONSISTENT
    assert post_monitor.reconcile({**rest(), "snapshot_id": "snap-baseline", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "100", "currency": "USDT"}, "watermarks": {"wallet": 1, "positions": 1, "orders": 1, "executions": 1}}).status == HEALTHY
    post_store.append_account_snapshot({"deployment_id": "dep-1", "snapshot_id": "snap-bad-equity", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:03Z", "wallet": {"equity": "Infinity", "currency": "USDT"}})
    post_store.append_reconciliation("dep-1", "recon-bad-equity", HEALTHY, {"snapshot_id": "snap-bad-equity", "observed_at": "2026-09-07T00:00:03Z"})
    post_restart = LiveMonitor(manifest(), post_store)
    assert post_restart.account_metrics()["availability"] == UNKNOWN
    post_store.close()


def test_restart_rejects_missing_and_conflicting_account_snapshot_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    original_rows = store.rows
    def missing_rows(table: str, *, deployment_id: str | None = None):
        rows = original_rows(table, deployment_id=deployment_id)
        if table == "account_snapshots":
            return tuple({**row, "snapshot_id": ""} for row in rows)
        return rows
    monkeypatch.setattr(store, "rows", missing_rows)
    assert LiveMonitor(manifest(), store).account_metrics()["availability"] == UNKNOWN
    monkeypatch.undo()

    original_rows = store.rows
    def conflicting_rows(table: str, *, deployment_id: str | None = None):
        rows = original_rows(table, deployment_id=deployment_id)
        if table == "account_snapshots" and rows:
            return (*rows, {**rows[0], "wallet": {"equity": "101", "currency": "USDT"}})
        return rows
    monkeypatch.setattr(store, "rows", conflicting_rows)
    assert LiveMonitor(manifest(), store).account_metrics()["availability"] == UNKNOWN
    store.close()


def test_live_and_restart_equity_observation_resolver_match(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), clock=lambda: "2026-09-07T00:00:01Z")
    LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    first = {**rest(), "observed_at": "2026-09-07T00:00:00Z", "snapshot_end_observed_at": "2026-09-07T00:00:01Z", "wallet": {"equity": "100", "currency": "USDT"}}
    second = {**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:02Z", "snapshot_end_observed_at": "2026-09-07T00:00:03Z", "wallet": {"equity": "110", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-between-equity", "timestamp_utc": "2026-09-07T00:00:02Z", "observed_at_utc": "2026-09-07T00:00:02Z", "amount": "10", "direction": "DEPOSIT", "currency": "USDT"}]}
    monitor._record_metric_facts(first, ())
    monitor._record_metric_facts(second, ())
    store.append_account_snapshot(first)
    store.append_account_snapshot(second)
    store.append_cashflow({**second["cashflows"][0], "deployment_id": "dep-1", "account_id": "acct-1"})  # type: ignore[index]
    store.append_reconciliation("dep-1", "recon-first", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:01Z"})
    store.append_reconciliation("dep-1", "recon-second", HEALTHY, {"snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:03Z"})
    live_metrics = monitor.account_metrics()
    restarted_metrics = LiveMonitor(manifest(), store).account_metrics()
    assert restarted_metrics["availability"] == AVAILABLE and restarted_metrics["adjusted_equity_path"] == live_metrics["adjusted_equity_path"] and restarted_metrics["net_trading_pnl"] == live_metrics["net_trading_pnl"]
    store.close()


def test_restart_uses_snapshot_end_observation_for_legacy_account_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = {**rest(), "observed_at": None, "snapshot_end_observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}}
    second = {**rest(), "snapshot_id": "snap-2", "observed_at": None, "snapshot_end_observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "110", "currency": "USDT"}}
    live = LiveMonitor(manifest())
    live._record_metric_facts(first, ())
    live._record_metric_facts(second, ())
    live_metrics = live.account_metrics()

    store = LiveStore(tmp_path / "snapshot-end-observed.sqlite3")
    LiveMonitor(manifest(), store)
    for snapshot in (first, second):
        store.append_account_snapshot({**snapshot, "observed_at": snapshot["snapshot_end_observed_at"]})
    store.append_reconciliation("dep-1", "recon-first", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:00Z"})
    store.append_reconciliation("dep-1", "recon-second", HEALTHY, {"snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:02Z"})
    original_rows = store.rows

    def legacy_rows(table: str, *, deployment_id: str | None = None):
        rows = original_rows(table, deployment_id=deployment_id)
        if table != "account_snapshots":
            return rows
        return tuple({
            **row,
            "observed_at": None,
            "observed_at_utc": None,
            "effective_at": None,
            "effective_at_utc": None,
            "snapshot_end_observed_at": row.get("snapshot_end_observed_at"),
        } for row in rows)

    monkeypatch.setattr(store, "rows", legacy_rows)
    restarted_metrics = LiveMonitor(manifest(), store).account_metrics()
    assert restarted_metrics["availability"] == live_metrics["availability"] == AVAILABLE
    assert restarted_metrics["currency"] == live_metrics["currency"] == "USDT"
    assert restarted_metrics["adjusted_equity_path"] == live_metrics["adjusted_equity_path"]
    assert restarted_metrics["net_trading_pnl"] == live_metrics["net_trading_pnl"]
    store.close()


def test_duplicate_live_snapshot_currency_conflict_is_unknown() -> None:
    monitor = LiveMonitor(manifest())
    snapshot = {"snapshot_id": "snap-currency", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}}
    monitor._record_metric_facts(snapshot, ())
    monitor._record_metric_facts({**snapshot, "wallet": {"equity": "100", "currency": "USD"}}, ())
    assert monitor.account_metrics()["availability"] == UNKNOWN


def test_missing_cashflow_currency_is_unknown_live_and_after_restart(tmp_path: Path) -> None:
    live = LiveMonitor(manifest())
    live._record_metric_facts({"snapshot_id": "snap-missing-currency", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-missing-currency", "timestamp_utc": "2026-09-07T00:00:01Z", "amount": "5", "direction": "DEPOSIT"}]}, ())
    assert live.account_metrics()["availability"] == UNKNOWN
    store = LiveStore(tmp_path / "missing-currency.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    store.append_cashflow({"deployment_id": "dep-1", "account_id": "acct-1", "cashflow_id": "cf-missing-currency", "timestamp_utc": "2026-09-07T00:00:01Z", "amount": "5", "direction": "DEPOSIT"})
    assert LiveMonitor(manifest(), store).account_metrics()["availability"] == UNKNOWN
    store.close()


def test_cashflow_integrity_failure_stays_unknown_after_clean_snapshot_and_restart(tmp_path: Path) -> None:
    bad = {
        **rest(),
        "wallet": {"equity": "100", "currency": "USDT"},
        "cashflows": [{
            "cashflow_id": "cf-invalid-sticky",
            "timestamp_utc": "2026-09-07T00:00:01Z",
            "amount": "5",
            "direction": "DEPOSIT",
            # Missing currency is malformed evidence, not a recoverable gap.
        }],
    }
    clean = {
        **rest(),
        "snapshot_id": "snap-clean-after-flow-invalid",
        "observed_at": "2026-09-07T00:00:02Z",
        "wallet": {"equity": "105", "currency": "USDT"},
    }

    live = LiveMonitor(manifest())
    live._record_metric_facts(bad, ())
    live._record_metric_facts(clean, ())

    store = LiveStore(tmp_path / "flow-integrity.sqlite3")
    LiveMonitor(manifest(), store)
    store.append_account_snapshot(bad)
    store.append_account_snapshot(clean)
    store.append_cashflow({**bad["cashflows"][0], "deployment_id": "dep-1", "account_id": "acct-1"})  # type: ignore[index]
    store.append_reconciliation("dep-1", "recon-bad-flow", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:00Z"})
    store.append_reconciliation("dep-1", "recon-clean-after-flow-invalid", HEALTHY, {"snapshot_id": "snap-clean-after-flow-invalid", "observed_at": "2026-09-07T00:00:02Z"})
    restarted = LiveMonitor(manifest(), store)

    live_metrics = live.account_metrics()
    restart_metrics = restarted.account_metrics()
    assert live_metrics["availability"] == UNKNOWN
    assert restart_metrics["availability"] == UNKNOWN
    assert live_metrics["adjusted_equity_path"] == restart_metrics["adjusted_equity_path"]
    restarted.reconcile(clean)
    after_clean = restarted.account_metrics()
    assert after_clean["availability"] == UNKNOWN
    assert after_clean["adjusted_equity_path"] == live_metrics["adjusted_equity_path"]
    store.close()


def test_integrity_invalid_is_sticky_across_clean_snapshot_and_matches_restart(tmp_path: Path) -> None:
    bad = {**rest(), "wallet": {"equity": "NaN", "currency": "USDT"}}
    clean = {**rest(), "snapshot_id": "snap-clean-after-invalid", "observed_at": "2026-09-07T00:00:01Z", "wallet": {"equity": "100", "currency": "USDT"}}
    live = LiveMonitor(manifest())
    live._record_metric_facts(bad, ())
    live._record_metric_facts(clean, ())
    store = LiveStore(tmp_path / "sticky-integrity.sqlite3")
    LiveMonitor(manifest(), store)
    store.append_account_snapshot(bad)
    store.append_account_snapshot(clean)
    store.append_reconciliation("dep-1", "recon-bad", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:00Z"})
    store.append_reconciliation("dep-1", "recon-clean", HEALTHY, {"snapshot_id": "snap-clean-after-invalid", "observed_at": "2026-09-07T00:00:01Z"})
    restarted = LiveMonitor(manifest(), store)
    live_metrics = live.account_metrics()
    restart_metrics = restarted.account_metrics()
    assert live_metrics["availability"] == UNKNOWN and restart_metrics["availability"] == UNKNOWN
    assert live_metrics["adjusted_equity_path"] == restart_metrics["adjusted_equity_path"]
    store.close()


def test_missing_cashflow_currency_before_and_after_equity_range_is_unknown_live_and_restart(tmp_path: Path) -> None:
    first = {**rest(), "wallet": {"equity": "100"}}
    second = {**rest(), "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:02Z", "wallet": {"equity": "110"}, "cashflows": [
        {"cashflow_id": "cf-before", "timestamp_utc": "2026-09-06T23:59:00Z", "amount": "5", "direction": "DEPOSIT"},
        {"cashflow_id": "cf-after", "timestamp_utc": "2026-09-07T00:00:03Z", "amount": "5", "direction": "DEPOSIT"},
    ]}
    live = LiveMonitor(manifest())
    live._record_metric_facts(first, ())
    live._record_metric_facts(second, ())
    assert live.account_metrics()["availability"] == UNKNOWN and live.account_metrics()["currency"] is None
    store = LiveStore(tmp_path / "missing-currency-range.sqlite3")
    LiveMonitor(manifest(), store)
    store.append_account_snapshot(first)
    store.append_account_snapshot(second)
    store.append_cashflow({**second["cashflows"][0], "deployment_id": "dep-1", "account_id": "acct-1"})  # type: ignore[index]
    store.append_cashflow({**second["cashflows"][1], "deployment_id": "dep-1", "account_id": "acct-1"})  # type: ignore[index]
    store.append_reconciliation("dep-1", "recon-first", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:00Z"})
    store.append_reconciliation("dep-1", "recon-second", HEALTHY, {"snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:02Z"})
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_metrics()["availability"] == UNKNOWN and restarted.account_metrics()["currency"] is None
    store.close()


def test_equityless_snapshot_then_clean_snapshot_is_sticky_and_matches_restart(tmp_path: Path) -> None:
    equityless = {**rest(), "wallet": {"currency": "USDT"}}
    clean = {**rest(), "snapshot_id": "snap-clean-after-equityless", "observed_at": "2026-09-07T00:00:01Z", "wallet": {"equity": "100", "currency": "USDT"}}
    live = LiveMonitor(manifest())
    live._record_metric_facts(equityless, ())
    live._record_metric_facts(clean, ())
    store = LiveStore(tmp_path / "equityless.sqlite3")
    LiveMonitor(manifest(), store)
    store.append_account_snapshot(equityless)
    store.append_account_snapshot(clean)
    store.append_reconciliation("dep-1", "recon-equityless", HEALTHY, {"snapshot_id": "snap-1", "observed_at": "2026-09-07T00:00:00Z"})
    store.append_reconciliation("dep-1", "recon-clean", HEALTHY, {"snapshot_id": "snap-clean-after-equityless", "observed_at": "2026-09-07T00:00:01Z"})
    restarted = LiveMonitor(manifest(), store)
    live_metrics = live.account_metrics()
    restart_metrics = restarted.account_metrics()
    assert live_metrics["availability"] == UNKNOWN and restart_metrics["availability"] == UNKNOWN
    assert live_metrics["adjusted_equity_path"] == restart_metrics["adjusted_equity_path"]
    store.close()


def test_multi_currency_equity_output_is_unknown_with_no_currency() -> None:
    monitor = LiveMonitor(manifest())
    monitor._record_metric_facts({"snapshot_id": "snap-usdt", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}}, ())
    monitor._record_metric_facts({"snapshot_id": "snap-usd", "observed_at": "2026-09-07T00:00:01Z", "wallet": {"equity": "101", "currency": "USD"}}, ())
    metrics = monitor.account_metrics()
    assert metrics["availability"] == UNKNOWN and metrics["currency"] is None
def test_restart_clean_branch_rejects_cross_account_cashflow(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    store.append_cashflow({
        "deployment_id": "dep-1",
        "account_id": "acct-other",
        "cashflow_id": "cf-foreign",
        "timestamp_utc": "2026-09-07T00:00:00Z",
        "observed_at_utc": "2026-09-07T00:00:00Z",
        "amount": "5",
        "direction": "DEPOSIT",
        "currency": "USDT",
    })
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_dashboard()["availability"] == UNKNOWN
    store.close()


def test_restart_post_boundary_branch_rejects_cross_account_cashflow(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-gap"}, ({"channel": "wallet", "sequence": 2, "event_id": "wallet-gap"},)).status == INCONSISTENT
    assert monitor.reconcile({**rest(), "snapshot_id": "snap-new", "observed_at": "2026-09-07T00:00:02Z", "watermarks": {"wallet": 1, "positions": 1, "orders": 1, "executions": 1}}).status == HEALTHY
    store.append_cashflow({
        "deployment_id": "dep-1",
        "account_id": "acct-other",
        "cashflow_id": "cf-foreign-post-boundary",
        "timestamp_utc": "2026-09-07T00:00:03Z",
        "observed_at_utc": "2026-09-07T00:00:03Z",
        "amount": "5",
        "direction": "DEPOSIT",
        "currency": "USDT",
    })
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_dashboard()["availability"] == UNKNOWN
    store.close()


def test_restart_clean_branch_rejects_malformed_cashflow_timestamp(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    store.append_cashflow({
        "deployment_id": "dep-1",
        "account_id": "acct-1",
        "cashflow_id": "cf-malformed-time",
        "effective_at_utc": "not-a-timestamp",
        "observed_at_utc": "2026-09-07T00:00:00Z",
        "amount": "5",
        "direction": "DEPOSIT",
        "currency": "USDT",
    })
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_dashboard()["availability"] == UNKNOWN
    store.close()


def test_restart_clean_branch_rejects_malformed_account_timestamp(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    monitor = LiveMonitor(manifest(), store, clock=lambda: "2026-09-07T00:00:01Z")
    assert monitor.reconcile(rest()).status == HEALTHY
    store.append_account_snapshot({
        "deployment_id": "dep-1",
        "snapshot_id": "snap-malformed-account-time",
        "account_id": "acct-1",
        "observed_at": "not-a-timestamp",
        "wallet": {"equity": "110", "currency": "USDT"},
    })
    restarted = LiveMonitor(manifest(), store)
    assert restarted.account_metrics()["availability"] == UNKNOWN
    store.close()


def test_metric_snapshot_identity_is_required_and_conflicts_are_unknown() -> None:
    monitor = LiveMonitor(manifest())
    monitor._record_metric_facts({"observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}}, ())
    monitor._record_metric_facts({"observed_at": "2026-09-07T00:00:01Z", "wallet": {"equity": "101", "currency": "USDT"}}, ())
    assert monitor.account_metrics()["availability"] == UNKNOWN
    exact = LiveMonitor(manifest())
    snapshot = {"snapshot_id": "snap-exact", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}}
    exact._record_metric_facts(snapshot, ())
    exact._record_metric_facts(dict(snapshot), ())
    assert exact.account_metrics()["availability"] == AVAILABLE
    exact._record_metric_facts({**snapshot, "wallet": {"equity": "101", "currency": "USDT"}}, ())
    assert exact.account_metrics()["availability"] == UNKNOWN


def test_cashflow_sign_and_order_identity_conflicts_fail_closed() -> None:
    monitor = LiveMonitor(manifest())
    monitor._record_metric_facts({"snapshot_id": "snap-sign", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-negative-deposit", "timestamp_utc": "2026-09-07T00:00:01Z", "amount": "-5", "direction": "DEPOSIT", "currency": "USDT"}, {"cashflow_id": "cf-none-source", "timestamp_utc": "2026-09-07T00:00:01Z", "amount": "5", "direction": "DEPOSIT", "source_id": None, "currency": "USDT"}]}, ())
    assert monitor.account_metrics()["availability"] == UNKNOWN


def test_nonfinite_equity_and_cashflow_fail_closed_without_exception() -> None:
    monitor = LiveMonitor(manifest())
    monitor._record_metric_facts({"snapshot_id": "snap-nan", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": Decimal("NaN"), "currency": "USDT"}}, ())
    assert monitor.account_metrics()["availability"] == UNKNOWN
    monitor = LiveMonitor(manifest())
    monitor._record_metric_facts({"snapshot_id": "snap-inf", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100", "currency": "USDT"}, "cashflows": [{"cashflow_id": "cf-inf", "timestamp_utc": "2026-09-07T00:00:01Z", "amount": Decimal("Infinity"), "direction": "DEPOSIT", "currency": "USDT"}]}, ())
    assert monitor.account_metrics()["availability"] == UNKNOWN


def _entry_manifest(*, role: bool = True) -> dict[str, object]:
    value = manifest()
    if role:
        value["member_composition"][0]["role"] = "ENTRY"  # type: ignore[index]
    return value


def _entry_rest(*orders: dict[str, object], observed_at: str = "2026-09-07T00:00:00Z", positions: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        **rest(),
        "observed_at": observed_at,
        "positions": positions or [],
        "orders": list(orders),
    }


def test_entry_projection_uses_explicit_role_and_exact_decimal_aggregation() -> None:
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
    result = monitor.reconcile(_entry_rest(
        {"order_id": "b", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "0.25", "price": "101.10"},
        {"order_id": "a", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1.5", "price": "100"},
    ))
    assert result.status == HEALTHY
    projection = monitor.entry_order_projection(("BTCUSDT", "LONG"))
    assert projection["state"] == "PENDING_ENTRY"
    assert projection["active_entry_quantity"] == "1.75"
    assert projection["active_entry_notional"] == "175.275"
    assert projection["order_ids"] == ["a", "b"] and projection["order_revisions"] == [1, 1]
    assert monitor.order_reconcile_policy(in_flight=True, pending_triggers=3)["coalesced_triggers"] == 2


def test_entry_projection_does_not_infer_role_and_reports_position_state() -> None:
    monitor = LiveMonitor(_entry_manifest(role=False), clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "qty": "2", "price": "100"}))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["status"] == UNKNOWN and projection["reason"] == "ORDER_ROLE_UNKNOWN"
    shared = manifest(ambiguous=True)
    for member in shared["member_composition"]:  # type: ignore[union-attr]
        if member["symbol"] == "BTCUSDT":
            member["role"] = "ENTRY"
    monitor = LiveMonitor(shared, clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "o-shared", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "qty": "2", "price": "100"}))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["status"] == UNKNOWN and projection["reason"] == "ORDER_ROLE_UNKNOWN"
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100"}, positions=[{"symbol": "BTCUSDT", "side": "LONG", "size": "1"}]))
    assert monitor.entry_order_projection("BTCUSDT", "LONG")["state"] == "IN_POSITION"


def test_entry_projection_fake_clock_boundary_and_stale_hiding() -> None:
    now = ["2026-09-07T00:00:00Z"]
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: now[0])
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100"}))
    for value, expected in (("19.999", "CURRENT"), ("20.000", "CURRENT"), ("20.001", "STALE")):
        now[0] = f"2026-09-07T00:00:{value.zfill(6)}Z" if "." not in value else "2026-09-07T00:00:20.001Z"
        if value == "19.999":
            now[0] = "2026-09-07T00:00:19.999Z"
        elif value == "20.000":
            now[0] = "2026-09-07T00:00:20Z"
        projection = monitor.entry_order_projection("BTCUSDT", "LONG")
        assert projection["status"] == expected
        if expected == "STALE":
            assert projection["active_entry_quantity"] is None and projection["state"] is None and projection["order_ids"] == []


def test_entry_projection_ws_resize_wallet_nonconfirmation_and_rest_recovery() -> None:
    now = ["2026-09-07T00:00:10Z"]
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: now[0])
    baseline = _entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"})
    monitor.reconcile(baseline)
    first = monitor.entry_order_projection("BTCUSDT", "LONG")
    now[0] = "2026-09-07T00:00:11Z"
    monitor.reconcile({**baseline, "snapshot_id": "snap-2", "observed_at": "2026-09-07T00:00:01Z"}, ({"channel": "wallet", "sequence": 1, "event_id": "wallet-1", "balance": "101"},))
    wallet = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert wallet["confirmation_at"] == first["confirmation_at"]
    now[0] = "2026-09-07T00:00:12Z"
    monitor.reconcile({**baseline, "snapshot_id": "snap-3", "observed_at": "2026-09-07T00:00:02Z"}, ({"channel": "orders", "sequence": 1, "event_id": "order-1", "order_id": "o1", "revision": 2, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100", "observed_at": "2026-09-07T00:00:05Z"},))
    resized = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert resized["source"] == "WS" and resized["active_entry_quantity"] == "2"
    gap = {**baseline, "snapshot_id": "snap-4", "observed_at": "2026-09-07T00:00:06Z"}
    monitor.reconcile(gap, ({"channel": "orders", "sequence": 2, "event_id": "order-2", "order_id": "o1", "revision": 4, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "4", "price": "100"},))
    assert monitor.entry_order_projection("BTCUSDT", "LONG")["status"] == UNKNOWN
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 4, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "4", "price": "100"}, observed_at="2026-09-07T00:00:07Z"))
    recovered = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert recovered["source"] == "REST_RECONCILED" and recovered["active_entry_quantity"] == "4"


def test_entry_projection_missing_values_and_revision_conflict_fail_closed() -> None:
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1"}))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["active_entry_quantity"] is None and projection["active_entry_notional"] is None and projection["availability"] == UNKNOWN
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"}, observed_at="2026-09-07T00:00:01Z"), ({"channel": "orders", "sequence": 1, "event_id": "conflict", "order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100"},))
    assert monitor.entry_order_projection("BTCUSDT", "LONG")["status"] == UNKNOWN


def test_entry_projection_requires_explicit_revision_and_hides_unknown_aggregate() -> None:
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "missing-revision", "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"}))
    assert monitor.entry_order_projection("BTCUSDT", "LONG")["status"] == UNKNOWN
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
    monitor.reconcile(_entry_rest({"order_id": "missing-qty", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "price": "100"}))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["status"] == UNKNOWN and projection["state"] is None
    assert projection["active_entry_quantity"] is None and projection["active_entry_notional"] is None


def test_entry_projection_uses_latest_order_event_time_independent_of_input_order() -> None:
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:12Z")
    baseline = _entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"})
    monitor.reconcile(baseline, (
        {"channel": "orders", "sequence": 2, "event_id": "o2", "order_id": "o1", "revision": 3, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "3", "price": "100", "observed_at": "2026-09-07T00:00:10Z"},
        {"channel": "orders", "sequence": 1, "event_id": "o1", "order_id": "o1", "revision": 2, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100", "observed_at": "2026-09-07T00:00:11Z"},
    ))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["source"] == "WS" and projection["observation_utc"] == "2026-09-07T00:00:11Z"
    assert projection["confirmation_utc"] == "2026-09-07T00:00:11Z"


def test_entry_reconcile_policy_enforces_interval_budgets_and_coalescing() -> None:
    monitor = LiveMonitor(_entry_manifest())
    valid = monitor.order_reconcile_policy(rest_interval_seconds="10", completion_seconds="2", validation_seconds="2", append_seconds="1", chart_get_seconds="5", in_flight=True, pending_triggers=2)
    assert valid["status"] == AVAILABLE and valid["rest_due"] is False and valid["coalesced_triggers"] == 1
    assert valid["completion_validation_append_seconds"] == "5" and valid["chart_get_budget_seconds"] == "5"
    for kwargs in (
        {"rest_interval_seconds": "10.001"},
        {"completion_seconds": "2", "validation_seconds": "2", "append_seconds": "1.001"},
        {"chart_get_seconds": "5.001"},
        {"completion_seconds": -1},
        {"append_seconds": True},
    ):
        assert monitor.order_reconcile_policy(**kwargs)["status"] == UNKNOWN
    assert monitor.order_reconcile_policy(now="2026-09-07T00:00:09.999Z", last_rest_observed_at="2026-09-07T00:00:00Z")["rest_due"] is False
    assert monitor.order_reconcile_policy(now="2026-09-07T00:00:10Z", last_rest_observed_at="2026-09-07T00:00:00Z")["rest_due"] is True
    assert monitor.order_reconcile_policy(now="2026-09-07T00:00:10.001Z", last_rest_observed_at="2026-09-07T00:00:00Z")["rest_due"] is True
    assert monitor.order_reconcile_policy(now="not-a-time", last_rest_observed_at="2026-09-07T00:00:00Z")["status"] == UNKNOWN


def test_entry_projection_ws_first_same_reconcile_and_cancel_replace_fail_closed() -> None:
    monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:06Z")
    baseline = _entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"})
    monitor.reconcile(baseline, ({"channel": "orders", "sequence": 1, "event_id": "resize", "order_id": "o1", "revision": 2, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100", "observed_at": "2026-09-07T00:00:05Z"},))
    projection = monitor.entry_order_projection("BTCUSDT", "LONG")
    assert projection["source"] == "WS" and projection["active_entry_quantity"] == "2"
    monitor.reconcile(_entry_rest({"order_id": "o1", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"}, observed_at="2026-09-07T00:00:06Z"), ({"channel": "orders", "sequence": 2, "event_id": "replace", "order_id": "o1", "revision": 2, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100", "cancel_replace": True},))
    assert monitor.entry_order_projection("BTCUSDT", "LONG")["status"] == UNKNOWN


def test_entry_projection_duplicate_rest_identity_fails_closed_regardless_of_order() -> None:
    orders = (
        {"order_id": "dup", "revision": 1, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "1", "price": "100"},
        {"order_id": "dup", "revision": 2, "symbol": "BTCUSDT", "side": "LONG", "role": "ENTRY", "qty": "2", "price": "100"},
    )
    for ordered in (orders, tuple(reversed(orders))):
        monitor = LiveMonitor(_entry_manifest(), clock=lambda: "2026-09-07T00:00:00Z")
        monitor.reconcile(_entry_rest(*ordered))
        assert monitor.entry_order_projection("BTCUSDT", "LONG")["status"] == UNKNOWN
