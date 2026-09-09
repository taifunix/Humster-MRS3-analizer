from __future__ import annotations

from pathlib import Path

import pytest

from mrs3.portfolio.live_reconcile import ALL_CHANNELS, INCONSISTENT, HEALTHY, PARTIAL, UNKNOWN, LiveReconciler, ReconcileApplication, ReconcileResult, RestSnapshotResult, _snapshot_digest, apply_reconcile, complete_rest_snapshot
from mrs3.portfolio.live_store import LiveStore, WriteOutcome, canonical_digest


def manifest() -> dict[str, object]:
    return {
        "manifest_schema_version": 1, "deployment_id": "dep-1", "manifest_id": "man-1", "version": 1,
        "account_alias": "fixture", "account_id": "acct-1", "portfolio_set": "set-1", "evaluation": "eval-1",
        "run_id": "run-1", "attempt_id": "attempt-1", "semantic_digest": "0" * 64,
        "series_version": "series-1", "metrics_version": "metrics-1", "snapshot_read_deadline_seconds": "10",
        "member_composition": [{"strategy_id": "s1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m", "priority": 1, "counted": True}],
        "source": "fixture", "limiter_settings": {"settings_version": "lim-1", "limit": 1, "grace_seconds": 10},
        "watchdog_settings": {"settings_version": "wd-1", "limit": 1, "grace_seconds": 10},
        "read_only_permissions": ["wallet.read", "positions.read", "orders.read", "executions.read"],
    }


def _digest(records: list[dict[str, object]]) -> str:
    return canonical_digest(records)


def _page(channel: str, records: list[dict[str, object]], *, cursor: str | None = None, next_cursor: str | None = None, terminal: bool = True, terminal_cursor: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"section": channel, "items": records, "cursor": cursor, "next_cursor": next_cursor, "terminal": terminal}
    if terminal_cursor is not None:
        result["terminal_cursor"] = terminal_cursor
    return result


def _pages(*, duration: object = "5.000", order_duration: object = "5.000", positions: list[dict[str, object]] | None = None) -> dict[str, object]:
    wallet = [{"record_id": "wallet-1", "equity": "100", "currency": "USDT"}]
    positions = positions or [{"symbol": "BTCUSDT", "side": "LONG", "size": "1"}]
    executions = [{"execution_id": "fill-1", "qty": "1", "price": "100"}]
    orders = [{"order_id": "order-1", "revision": 1, "qty": "1", "price": "99"}]
    records = {"wallet": wallet, "positions": positions, "executions": executions, "orders": orders, "cashflows": []}
    result: dict[str, object] = {
        "deployment_id": "dep-1", "account_id": "acct-1", "snapshot_id": "snap-1",
        "snapshot_start_observed_at": "2026-09-08T00:00:00Z", "snapshot_end_observed_at": "2026-09-08T00:00:05Z",
        "duration_seconds": duration, "open_orders_duration_seconds": order_duration,
        "watermarks": {"wallet": 0, "positions": 0, "executions": 0, "orders": 0},
        "declared_count": {channel: len(items) for channel, items in records.items()},
        "canonical_digest": {channel: _digest(items) for channel, items in records.items()},
    }
    result["snapshot_declared_count"] = sum(len(items) for items in records.values())
    result["snapshot_canonical_digest"] = _snapshot_digest({channel: records.get(channel, ()) for channel in ALL_CHANNELS})
    for channel, items in records.items():
        result[channel] = [_page(channel, items, terminal_cursor=f"{channel}-end")]
    return result


def test_complete_multipage_snapshot_is_canonical_and_cashflows_are_optional() -> None:
    source = _pages(positions=[
        {"symbol": "BTCUSDT", "side": "LONG", "size": "1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "size": "2"},
    ])
    source["positions"] = [
        _page("positions", [source["positions"][0]["items"][0]], next_cursor="p1", terminal=False),
        _page("positions", [source["positions"][0]["items"][1]], cursor="p1", terminal_cursor="positions-end"),
    ]
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == HEALTHY
    assert result.authoritative
    assert result.checkpoint["terminal_cursor"]["positions"] == "positions-end"
    assert result.checkpoint["record_count"] == 5
    assert result.provenance["rest"]["channels"]["positions"]["record_count"] == 2
    with pytest.raises(TypeError):
        result.snapshot["positions"] = ()  # type: ignore[index]


def test_page_level_scalar_declarations_are_accepted_and_result_types_are_stable() -> None:
    source = _pages()
    source.pop("declared_count")
    source.pop("canonical_digest")
    for channel in ("wallet", "positions", "executions", "orders"):
        page = source[channel][0]
        page["declared_count"] = len(page["items"])
        page["canonical_digest"] = _digest(page["items"])
    rest = complete_rest_snapshot(manifest(), source)
    assert isinstance(rest, RestSnapshotResult)
    result = LiveReconciler(manifest()).reconcile(source)
    assert isinstance(result, ReconcileResult)
    assert not hasattr(result, "outcomes")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda source: source["orders"].__setitem__(0, {"section": "orders", "items": [], "terminal": False, "next_cursor": "x"}), "ORDERS_REST_TERMINAL_MARKER_MISSING"),
        (lambda source: source["executions"].__setitem__(0, {**source["executions"][0], "cursor": "wrong"}), "EXECUTIONS_REST_CURSOR_START_INVALID"),
        (lambda source: source["positions"].__setitem__(0, {**source["positions"][0], "truncated": True}), "POSITIONS_REST_TRUNCATED"),
    ],
)
def test_missing_terminal_cursor_discontinuity_and_truncation_are_partial(mutate, reason: str) -> None:
    source = _pages()
    mutate(source)
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == PARTIAL
    assert reason in result.reasons
    assert not result.authoritative


def test_cursor_discontinuity_and_missing_explicit_terminal_cursor_are_partial() -> None:
    source = _pages(positions=[
        {"symbol": "BTCUSDT", "side": "LONG", "size": "1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "size": "2"},
    ])
    source["positions"] = [
        _page("positions", [source["positions"][0]["items"][0]], next_cursor="p1", terminal=False),
        _page("positions", [source["positions"][0]["items"][1]], cursor="wrong", terminal_cursor="positions-end"),
    ]
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == PARTIAL
    assert "POSITIONS_REST_CURSOR_DISCONTINUITY" in result.reasons
    source = _pages()
    source["orders"][0].pop("terminal_cursor")
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == PARTIAL
    assert "ORDERS_REST_TERMINAL_CURSOR_MISSING" in result.reasons


def test_overlap_duplicate_identity_and_declared_count_or_digest_mismatch_fail() -> None:
    source = _pages()
    source["positions"] = [
        _page("positions", source["positions"][0]["items"], next_cursor="p1", terminal=False),
        _page("positions", source["positions"][0]["items"], cursor="p1", terminal_cursor="positions-end"),
    ]
    assert "POSITIONS_REST_PAGE_OVERLAP" in complete_rest_snapshot(manifest(), source).reasons
    source = _pages()
    source["declared_count"]["orders"] = 2
    result = complete_rest_snapshot(manifest(), source)
    assert "ORDERS_DECLARED_COUNT_MISMATCH" in result.reasons
    source = _pages()
    source["canonical_digest"]["orders"] = "0" * 64
    assert "ORDERS_DIGEST_MISMATCH" in complete_rest_snapshot(manifest(), source).reasons


@pytest.mark.parametrize("duration, expected", [("10.000", HEALTHY), ("10.001", PARTIAL)])
def test_snapshot_deadline_exact_boundary_is_allowed(duration: str, expected: str) -> None:
    assert complete_rest_snapshot(manifest(), _pages(duration=duration)).status == expected


@pytest.mark.parametrize("duration, expected", [("5.000", HEALTHY), ("5.001", PARTIAL)])
def test_open_orders_five_second_boundary(duration: str, expected: str) -> None:
    assert complete_rest_snapshot(manifest(), _pages(order_duration=duration)).status == expected


def test_ws_out_of_order_contiguous_events_are_sorted_and_duplicates_are_idempotent() -> None:
    reconciler = LiveReconciler(manifest())
    events = [
        {"channel": "wallet", "sequence": 2, "event_id": "w2", "balance": "101"},
        {"channel": "wallet", "sequence": 1, "event_id": "w1", "balance": "100"},
        {"channel": "wallet", "sequence": 1, "event_id": "w1", "balance": "100"},
    ]
    result = reconciler.reconcile(_pages(), events)
    assert result.status == HEALTHY
    assert [event["sequence"] for event in result.applied_ws] == [1, 2]
    assert result.checkpoint_candidates["wallet"]["sequence"] == 2
    assert result.provenance["ws"]["wallet"]["applied_count"] == 2


def test_ws_conflicting_duplicate_and_gap_do_not_advance_candidate() -> None:
    reconciler = LiveReconciler(manifest())
    conflict = reconciler.reconcile(_pages(), [
        {"channel": "wallet", "sequence": 1, "event_id": "w1", "balance": "100"},
        {"channel": "wallet", "sequence": 1, "event_id": "w1", "balance": "999"},
    ])
    assert conflict.status == INCONSISTENT
    assert "WS_CONFLICTING_DUPLICATE" in conflict.reasons
    assert conflict.checkpoint_candidates["wallet"]["sequence"] == 0
    gap = reconciler.reconcile(_pages(), [{"channel": "wallet", "sequence": 2, "event_id": "w2"}])
    assert gap.status == INCONSISTENT
    assert "WS_SEQUENCE_GAP" in gap.reasons
    assert not gap.applied_ws


def test_float_and_secret_path_material_are_rejected_recursively() -> None:
    source = _pages()
    source["orders"][0]["items"][0]["nested"] = {"amount": 1.0}
    with pytest.raises(TypeError):
        complete_rest_snapshot(manifest(), source)
    source = _pages()
    source["orders"][0]["items"][0]["nested"] = {"api_key": "fixture-secret"}
    with pytest.raises(ValueError, match="secret-like"):
        complete_rest_snapshot(manifest(), source)


def test_store_argument_is_ignored_and_no_persistence_is_attempted(tmp_path: Path) -> None:
    class ExplodingStore:
        def __getattr__(self, name: str):
            raise AssertionError(f"store touched: {name}")

    result = LiveReconciler(manifest(), ExplodingStore()).reconcile(_pages())
    assert result.status == HEALTHY
    assert not list(tmp_path.iterdir())


def test_missing_open_order_duration_is_partial_and_non_authoritative() -> None:
    source = _pages()
    source.pop("open_orders_duration_seconds")
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == PARTIAL
    assert "OPEN_ORDER_SNAPSHOT_DURATION_MISSING" in result.reasons
    reconciled = LiveReconciler(manifest()).reconcile(source)
    assert reconciled.status == PARTIAL
    assert not reconciled.authoritative


def test_unpinned_snapshot_deadline_is_partial() -> None:
    source_manifest = manifest()
    source_manifest.pop("snapshot_read_deadline_seconds")
    result = complete_rest_snapshot(source_manifest, _pages())
    assert result.status == PARTIAL
    assert "SNAPSHOT_DEADLINE_UNPINNED" in result.reasons


@pytest.mark.parametrize("next_value", ["omitted", None, ""])
def test_nonterminal_page_requires_nonempty_next_cursor(next_value: object) -> None:
    source = _pages()
    page = source["orders"][0]
    page["terminal"] = False
    if next_value == "omitted":
        page.pop("next_cursor")
    else:
        page["next_cursor"] = next_value
    result = complete_rest_snapshot(manifest(), source)
    assert "ORDERS_REST_CURSOR_NEXT_MISSING" in result.reasons


@pytest.mark.parametrize("cursor_field, value", [("cursor", None), ("cursor", ""), ("previous_cursor", None)])
def test_subsequent_page_requires_nonempty_link_cursor(cursor_field: str, value: object) -> None:
    source = _pages(positions=[
        {"symbol": "BTCUSDT", "side": "LONG", "size": "1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "size": "2"},
    ])
    first, second = source["positions"][0]["items"]
    source["positions"] = [_page("positions", [first], next_cursor="p1", terminal=False), _page("positions", [second], cursor="p1", terminal_cursor="positions-end")]
    second_page = source["positions"][1]
    second_page.pop("cursor", None)
    second_page[cursor_field] = value
    result = complete_rest_snapshot(manifest(), source)
    assert "POSITIONS_REST_CURSOR_DISCONTINUITY" in result.reasons


def test_page_level_declarations_are_per_page_for_multipage_channel() -> None:
    source = _pages(positions=[
        {"symbol": "BTCUSDT", "side": "LONG", "size": "1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "size": "2"},
    ])
    first, second = source["positions"][0]["items"]
    source["positions"] = [_page("positions", [first], next_cursor="p1", terminal=False), _page("positions", [second], cursor="p1", terminal_cursor="positions-end")]
    source["declared_count"].pop("positions")
    source["canonical_digest"].pop("positions")
    for page in source["positions"]:
        page["declared_count"] = 1
        page["canonical_digest"] = _digest(page["items"])
    assert complete_rest_snapshot(manifest(), source).status == HEALTHY
    source["positions"][1]["declared_count"] = 9
    result = complete_rest_snapshot(manifest(), source)
    assert "POSITIONS_REST_PAGE_DECLARED_COUNT_MISMATCH" in result.reasons


def test_candidate_contains_snapshot_record_count() -> None:
    result = LiveReconciler(manifest()).reconcile(_pages())
    assert result.checkpoint_candidates["wallet"]["record_count"] == 4


@pytest.mark.parametrize("field, reason", [("snapshot_declared_count", "SNAPSHOT_DECLARED_COUNT_MISSING"), ("snapshot_canonical_digest", "SNAPSHOT_DIGEST_MISSING")])
def test_whole_snapshot_declarations_are_required(field: str, reason: str) -> None:
    source = _pages()
    source.pop(field)
    result = complete_rest_snapshot(manifest(), source)
    assert reason in result.reasons


@pytest.mark.parametrize("field, reason", [("snapshot_declared_count", "SNAPSHOT_DECLARED_COUNT_MISMATCH"), ("snapshot_canonical_digest", "SNAPSHOT_DIGEST_MISMATCH")])
def test_whole_snapshot_declarations_are_checked(field: str, reason: str) -> None:
    source = _pages()
    source[field] = 0 if field == "snapshot_declared_count" else "0" * 64
    result = complete_rest_snapshot(manifest(), source)
    assert reason in result.reasons


@pytest.mark.parametrize(
    "start, end, reason",
    [
        ("2026-09-08T00:00:00", "2026-09-08T00:00:05Z", "SNAPSHOT_START_OBSERVED_AT_INVALID"),
        ("not-a-time", "2026-09-08T00:00:05Z", "SNAPSHOT_START_OBSERVED_AT_INVALID"),
        ("2026-09-08T00:00:05Z", "2026-09-08T00:00:00Z", "SNAPSHOT_OBSERVATION_WINDOW_INVALID"),
    ],
)
def test_snapshot_times_are_utc_and_ordered(start: str, end: str, reason: str) -> None:
    source = _pages()
    source["snapshot_start_observed_at"] = start
    source["snapshot_end_observed_at"] = end
    result = complete_rest_snapshot(manifest(), source)
    assert reason in result.reasons
    assert result.status == PARTIAL


def test_offset_snapshot_times_are_normalized_to_utc() -> None:
    source = _pages()
    source["snapshot_start_observed_at"] = "2026-09-08T02:00:00+02:00"
    source["snapshot_end_observed_at"] = "2026-09-08T02:00:05+02:00"
    result = complete_rest_snapshot(manifest(), source)
    assert result.status == HEALTHY
    assert result.snapshot["snapshot_start_observed_at"] == "2026-09-08T00:00:00Z"


def test_non_string_snapshot_time_is_invalid() -> None:
    source = _pages()
    source["snapshot_start_observed_at"] = 123
    result = complete_rest_snapshot(manifest(), source)
    assert "SNAPSHOT_START_OBSERVED_AT_INVALID" in result.reasons


def test_snapshot_window_cannot_exceed_declared_duration() -> None:
    source = _pages(duration="2.000")
    source["snapshot_end_observed_at"] = "2026-09-08T00:00:05Z"
    result = complete_rest_snapshot(manifest(), source)
    assert "SNAPSHOT_WINDOW_EXCEEDS_DURATION" in result.reasons


def test_rest_conflict_is_inconsistent_and_partial_rest_survives_reconcile() -> None:
    source = _pages(positions=[{"symbol": "BTCUSDT", "side": "LONG", "size": "1"}])
    source["positions"] = [
        _page("positions", source["positions"][0]["items"], next_cursor="p1", terminal=False),
        _page("positions", [{"symbol": "BTCUSDT", "side": "LONG", "size": "9"}], cursor="p1", terminal_cursor="positions-end"),
    ]
    rest = complete_rest_snapshot(manifest(), source)
    assert rest.status == INCONSISTENT
    assert "POSITIONS_REST_PAGE_CONFLICT" in rest.reasons
    assert LiveReconciler(manifest()).reconcile(source).status == INCONSISTENT
    missing = _pages()
    missing.pop("orders")
    assert LiveReconciler(manifest()).reconcile(missing).status == PARTIAL


def test_ws_channel_without_rest_watermark_is_not_applied_or_defaulted() -> None:
    source = _pages()
    source["cashflows"] = [_page("cashflows", [{"cashflow_id": "cf-1", "amount": "1"}], terminal_cursor="cashflows-end")]
    source["declared_count"]["cashflows"] = 1
    source["canonical_digest"]["cashflows"] = _digest(source["cashflows"][0]["items"])
    source["snapshot_declared_count"] += 1
    source["snapshot_canonical_digest"] = _snapshot_digest({channel: (source[channel][0]["items"] if channel in source else ()) for channel in ALL_CHANNELS})
    result = LiveReconciler(manifest()).reconcile(source, [{"channel": "cashflows", "sequence": 1, "event_id": "cf-event"}])
    assert "CASHFLOWS_WATERMARK_MISSING" in result.reasons
    assert not result.applied_ws
    assert "cashflows" not in result.checkpoint_candidates


@pytest.mark.parametrize("mode", ["startup", "reconnect", "periodic"])
def test_supported_reconcile_modes_remain_healthy(mode: str) -> None:
    assert complete_rest_snapshot(manifest(), _pages(), mode=mode).status == HEALTHY


def test_invalid_mode_is_partial_and_page_after_terminal_is_diagnostic() -> None:
    invalid = complete_rest_snapshot(manifest(), _pages(), mode="invalid")
    assert invalid.status == PARTIAL
    assert "RECONCILE_MODE_INVALID" in invalid.reasons
    source = _pages(positions=[
        {"symbol": "BTCUSDT", "side": "LONG", "size": "1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "size": "2"},
    ])
    first, second = source["positions"][0]["items"]
    source["positions"] = [_page("positions", [first], next_cursor="p1", terminal=True, terminal_cursor="positions-end"), _page("positions", [second], cursor="p1", terminal=True, terminal_cursor="positions-end")]
    assert "POSITIONS_REST_PAGE_AFTER_TERMINAL" in complete_rest_snapshot(manifest(), source).reasons


@pytest.mark.parametrize("channel", ["wallet", "positions", "executions", "orders"])
def test_missing_required_channel_is_attributed(channel: str) -> None:
    source = _pages()
    source.pop(channel)
    assert f"{channel.upper()}_REST_CHANNEL_MISSING" in complete_rest_snapshot(manifest(), source).reasons


def test_missing_and_invalid_watermarks_are_attributed() -> None:
    source = _pages()
    source["watermarks"].pop("positions")
    assert "POSITIONS_WATERMARK_MISSING" in complete_rest_snapshot(manifest(), source).reasons
    source = _pages()
    source["watermarks"]["positions"] = "bad"
    assert "POSITIONS_WATERMARK_INVALID" in complete_rest_snapshot(manifest(), source).reasons


def test_top_level_pages_and_channel_pages_are_ambiguous() -> None:
    source = _pages()
    source["pages"] = [source["wallet"][0]]
    assert "REST_PAGES_AMBIGUOUS" in complete_rest_snapshot(manifest(), source).reasons


@pytest.mark.parametrize("field, reason", [("deployment_id", "DEPLOYMENT_ID_MISMATCH"), ("account_id", "ACCOUNT_ID_MISMATCH"), ("snapshot_id", "SNAPSHOT_ID_INVALID")])
def test_snapshot_identity_is_validated(field: str, reason: str) -> None:
    source = _pages()
    source[field] = None if field == "snapshot_id" else "wrong"
    assert reason in complete_rest_snapshot(manifest(), source).reasons


def _store(tmp_path: Path) -> LiveStore:
    store = LiveStore(tmp_path / "live.sqlite3")
    assert store.append_manifest(manifest()) is WriteOutcome.INSERTED
    return store


def _row_counts(store: LiveStore) -> dict[str, int]:
    return {table: len(store.rows(table, deployment_id="dep-1")) for table in (
        "account_snapshots", "position_snapshots", "order_snapshots", "execution_events",
        "stream_events", "cashflow_events", "reconciliations", "stream_checkpoints",
    )}


def test_apply_uses_public_bundle_without_transaction_or_private_append_hooks() -> None:
    source = (Path(__file__).parents[1] / "src" / "mrs3" / "portfolio" / "live_reconcile.py").read_text(encoding="utf-8")
    assert "store.transaction =" not in source
    assert "._append(" not in source


@pytest.mark.parametrize("source_factory", [
    lambda: {**_pages(), "open_orders_duration_seconds": None},
])
def test_apply_non_authoritative_reducer_result_is_a_zero_write_disposition(tmp_path: Path, source_factory) -> None:
    source = source_factory()
    if source.get("open_orders_duration_seconds") is None:
        source.pop("open_orders_duration_seconds")
    result = LiveReconciler(manifest()).reconcile(source)
    assert result.status != HEALTHY
    store = _store(tmp_path)
    before = _row_counts(store)
    disposition = apply_reconcile(store, result)
    assert isinstance(disposition, ReconcileApplication)
    assert disposition.status == result.status
    assert not disposition.outcomes
    assert not disposition.checkpoint_advanced
    assert "NOT_APPLIED_NON_AUTHORITATIVE" in disposition.reasons
    assert _row_counts(store) == before
    store.close()


def test_apply_ws_gap_is_a_zero_write_disposition(tmp_path: Path) -> None:
    result = LiveReconciler(manifest()).reconcile(_pages(), [{"channel": "wallet", "sequence": 2, "event_id": "w2"}])
    assert result.status == INCONSISTENT
    store = _store(tmp_path)
    before = _row_counts(store)
    disposition = apply_reconcile(store, result)
    assert disposition.status == INCONSISTENT
    assert not disposition.outcomes
    assert _row_counts(store) == before
    store.close()


def test_apply_healthy_bundle_is_atomic_and_exact_replay_is_idempotent(tmp_path: Path) -> None:
    result = LiveReconciler(manifest()).reconcile(_pages(), mode="startup")
    store = _store(tmp_path)
    first = apply_reconcile(store, result)
    assert isinstance(first, ReconcileApplication)
    assert first.status == HEALTHY
    assert first.checkpoint_advanced
    assert first.reconcile_id
    counts = _row_counts(store)
    second = apply_reconcile(store, result)
    assert second.status == HEALTHY
    assert second.checkpoint_advanced
    assert all(outcome is WriteOutcome.DUPLICATE for outcome in second.outcomes)
    assert _row_counts(store) == counts
    for channel in ("wallet", "positions", "executions", "orders"):
        assert store.latest_checkpoint("dep-1", channel)["sequence"] == 0
    store.close()


def test_apply_replay_after_sqlite_restart_is_idempotent(tmp_path: Path) -> None:
    result = LiveReconciler(manifest()).reconcile(_pages())
    store = _store(tmp_path)
    assert apply_reconcile(store, result).status == HEALTHY
    store.close()
    reopened = LiveStore(tmp_path / "live.sqlite3")
    disposition = apply_reconcile(reopened, result)
    assert disposition.status == HEALTHY
    assert disposition.checkpoint_advanced
    assert all(outcome is WriteOutcome.DUPLICATE for outcome in disposition.outcomes)
    reopened.close()


def test_apply_persists_snapshot_cashflows_in_same_bundle(tmp_path: Path) -> None:
    source = _pages()
    source["cashflows"] = [_page("cashflows", [{"cashflow_id": "cf-1", "amount": "10"}], terminal_cursor="cashflows-end")]
    source["declared_count"]["cashflows"] = 1
    source["canonical_digest"]["cashflows"] = _digest(source["cashflows"][0]["items"])
    source["snapshot_declared_count"] += 1
    source["snapshot_canonical_digest"] = _snapshot_digest({channel: (source[channel][0]["items"] if channel in source else ()) for channel in ALL_CHANNELS})
    result = LiveReconciler(manifest()).reconcile(source)
    assert result.status == HEALTHY
    store = _store(tmp_path)
    disposition = apply_reconcile(store, result)
    assert disposition.status == HEALTHY
    assert len(store.rows("cashflow_events", deployment_id="dep-1")) == 1
    store.close()


def test_apply_store_conflict_rolls_back_all_bundle_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = LiveReconciler(manifest()).reconcile(_pages())
    assert apply_reconcile(store, first).status == HEALTHY
    source = _pages()
    source["orders"][0]["items"][0]["qty"] = "2"
    source["canonical_digest"]["orders"] = _digest(source["orders"][0]["items"])
    source["snapshot_canonical_digest"] = _snapshot_digest({channel: (source[channel][0]["items"] if channel in source else ()) for channel in ALL_CHANNELS})
    conflicting = LiveReconciler(manifest()).reconcile(source)
    assert conflicting.status == HEALTHY
    before = _row_counts(store)
    disposition = apply_reconcile(store, conflicting)
    assert disposition.status == INCONSISTENT
    assert "STORE_CONFLICT" in disposition.reasons
    assert not disposition.checkpoint_advanced
    assert _row_counts(store) == before
    store.close()


def test_apply_exception_rolls_back_every_child_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    result = LiveReconciler(manifest()).reconcile(_pages(), [{"channel": "wallet", "sequence": 1, "event_id": "w1"}])
    original_append = store._append

    def explode(table, *args, **kwargs):
        if table == "stream_events":
            raise RuntimeError("fixture abort")
        return original_append(table, *args, **kwargs)

    monkeypatch.setattr(store, "_append", explode)
    before = _row_counts(store)
    with pytest.raises(RuntimeError, match="fixture abort"):
        apply_reconcile(store, result)
    assert _row_counts(store) == before
    store.close()


def test_missing_cashflow_channel_is_non_authoritative_without_unsupported_proof() -> None:
    source = _pages()
    source.pop("cashflows")
    source["snapshot_declared_count"] -= 0
    result = complete_rest_snapshot(manifest(), source)
    assert result.status != HEALTHY
    assert "CASHFLOWS_REST_CHANNEL_MISSING" in result.reasons


def test_cashflow_account_must_match_snapshot_at_atomic_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _pages()
    source["observed_at"] = "2026-09-08T00:00:05Z"
    source["cashflows"] = [{"cashflow_id": "cf-1", "account_id": "other", "amount": "1"}]
    with pytest.raises(ValueError, match="cashflow account"):
        store.append_reconcile_bundle(source)
    store.close()


@pytest.mark.parametrize("mode", ["startup", "reconnect", "periodic"])
def test_apply_supported_modes_use_the_same_atomic_seam(tmp_path: Path, mode: str) -> None:
    result = LiveReconciler(manifest()).reconcile(_pages(), mode=mode)
    store = _store(tmp_path / mode)
    disposition = apply_reconcile(store, result)
    assert disposition.status == HEALTHY
    assert disposition.checkpoint_advanced
    store.close()
