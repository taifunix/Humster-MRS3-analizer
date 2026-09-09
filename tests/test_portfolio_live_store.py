from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from decimal import Decimal
from hashlib import sha256

from mrs3.portfolio.live_store import (
    DeploymentManifest,
    LiveStore,
    LiveStoreConflict,
    LiveStoreError,
    WriteOutcome,
    canonical_bytes,
    canonical_digest,
    canonical_json,
    canonical_source_order,
)


def manifest() -> dict[str, object]:
    return {
        "manifest_schema_version": 1,
        "deployment_id": "dep-1",
        "manifest_id": "man-1",
        "version": 1,
        "account_alias": "paper",
        "account_id": "acct-1",
        "portfolio_set": "set-1",
        "evaluation": "eval-1",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "semantic_digest": "0" * 64,
        "series_version": "m6-series-v1",
        "metrics_version": "m6-metrics-v1",
        "member_composition": [{"strategy_id": "s1", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1m", "priority": 1, "counted": True}],
        "source": "fixture",
        "limiter_settings": {"settings_version": "lim-1", "limit": 1, "grace_seconds": 10},
        "watchdog_settings": {"settings_version": "wd-1", "L": 1, "grace_seconds": 10},
        "read_only_permissions": ["wallet.read", "positions.read"],
    }


def test_manifest_is_canonical_immutable_and_secret_free() -> None:
    first = DeploymentManifest(manifest())
    second = DeploymentManifest({"account_alias": "paper", **{key: value for key, value in manifest().items() if key != "account_alias"}})
    assert first.digest == second.digest
    assert first.to_dict()["member_composition"] == manifest()["member_composition"]
    assert DeploymentManifest({**manifest(), "read_only_permissions": ["wallet.read", "positions.read", "orders.read", "executions.read", "account.read"]}).account_alias == "paper"
    with pytest.raises(TypeError):
        first.payload["deployment_id"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="source=fixture"):
        DeploymentManifest({**manifest(), "source": "exchange"})
    with pytest.raises(ValueError, match="semantic_digest"):
        DeploymentManifest({**manifest(), "semantic_digest": "bad"})
    with pytest.raises(ValueError, match="read-only"):
        DeploymentManifest({**manifest(), "read_only_permissions": ["wallet.read", "orders.write"]})
    with pytest.raises(ValueError, match="member"):
        DeploymentManifest({**manifest(), "member_composition": [{"strategy_id": "s1", "symbol": "BTCUSDT", "side": "LONG"}]})
    with pytest.raises(ValueError, match="secret-like"):
        DeploymentManifest({**manifest(), "nested": {"api_key": "secret"}})
    DeploymentManifest({**manifest(), "description": "secret word", "token_count": 2})


def test_manifest_getattr_is_safe_before_payload_exists() -> None:
    item = object.__new__(DeploymentManifest)
    with pytest.raises(AttributeError):
        item.missing


def test_live_store_uses_wal_foreign_keys_and_rejects_baseline_targets(tmp_path: Path) -> None:
    with pytest.raises(LiveStoreError):
        LiveStore(tmp_path / "portfolio.duckdb")
    with pytest.raises(LiveStoreError):
        LiveStore(tmp_path / "live.sqlite3", performance_db=tmp_path / "live.sqlite3")
    store = LiveStore(tmp_path / "live.sqlite3")
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.connection.execute("SELECT database_kind FROM schema_info").fetchone()[0] == "mrs3_portfolio_live"
    store.close()


@pytest.mark.parametrize(
    "target",
    [
        "data/performanceDB",
        "data/performance-v2/strategy_performance.duckdb",
        "portfolio.duckdb",
    ],
)
def test_canonical_baseline_paths_are_rejected_before_file_creation(tmp_path: Path, target: str) -> None:
    path = tmp_path / target
    with pytest.raises(LiveStoreError):
        LiveStore(path)
    assert not path.exists()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_existing_foreign_sqlite_is_probed_read_only_and_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE foreign_table (value TEXT)")
        connection.execute("INSERT INTO foreign_table VALUES ('keep')")
    before = target.read_bytes()
    with pytest.raises(LiveStoreError, match="schema marker"):
        LiveStore(target)
    assert target.read_bytes() == before
    assert not target.with_name(target.name + "-wal").exists()
    assert not target.with_name(target.name + "-shm").exists()


@pytest.mark.parametrize(
    "key",
    ["api_secret", "apiSecret", "passphrase", "api_passphrase", "private_key_pem", "auth_key", "authKey", "signature", "hmac", "secret_id"],
)
def test_recursive_credential_keys_are_rejected_but_free_text_survives(key: str) -> None:
    with pytest.raises(ValueError, match="secret-like"):
        DeploymentManifest({**manifest(), "nested": [{key: "x"}]})
    DeploymentManifest({**manifest(), "description": f"{key} is only free text", "token_count": 2})


def test_freeze_and_canonical_float_guards_are_recursive() -> None:
    item = DeploymentManifest({**manifest(), "nested": ({"token_count": 2},)})
    assert isinstance(item.payload["nested"], tuple)
    with pytest.raises(TypeError, match="floats"):
        canonical_json({"nested": [{"value": 1.0}]})


def test_append_only_duplicate_conflict_and_restart(tmp_path: Path) -> None:
    target = tmp_path / "live.sqlite3"
    store = LiveStore(target)
    assert store.append_manifest(manifest()) is WriteOutcome.INSERTED
    assert store.append_manifest(DeploymentManifest(manifest())) is WriteOutcome.DUPLICATE
    assert store.append_manifest({**manifest(), "semantic_digest": "1" * 64}) is WriteOutcome.CONFLICT
    execution = {"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "fill-1", "symbol": "BTCUSDT", "realized_pnl": "1.25"}
    assert store.append_execution(execution) is WriteOutcome.INSERTED
    assert store.append_execution(dict(reversed(tuple(execution.items())))) is WriteOutcome.DUPLICATE
    assert store.append_execution({**execution, "realized_pnl": "2.00"}) is WriteOutcome.INCONSISTENT
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("DELETE FROM execution_events")
    store.close()
    reopened = LiveStore(target)
    assert len(reopened.rows("execution_events")) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reopened.connection.execute("UPDATE execution_events SET payload = '{}' ")
    reopened.close()


def test_recursive_triggers_block_replace_for_facts(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    store.append_execution({"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "fill-1"})
    assert store.connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute(
            "INSERT OR REPLACE INTO execution_events "
            "SELECT * FROM execution_events WHERE execution_id = 'fill-1'"
        )
    store.close()


def test_fact_requires_manifest_and_bundle_validates_children_before_write(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    with pytest.raises(LiveStoreError, match="manifest"):
        store.append_execution({"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "fill-1"})
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [{"symbol": "BTCUSDT", "side": "LONG", "size": "1"}], "orders": [], "executions": []}
    with pytest.raises(ValueError, match="execution identity"):
        store.append_reconcile_bundle({**rest, "executions": [{"realized_pnl": "1"}]})
    assert not store.rows("account_snapshots")


def test_rest_ws_execution_normalization_dedupes_envelopes(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": [{"execution_id": "fill-1", "account_id": "acct-1", "symbol": "BTCUSDT", "side": "LONG", "qty": "1", "price": "100", "realized_pnl": "2.5"}]}
    outcomes = store.append_reconcile_bundle(rest, events=({"channel": "execution", "sequence": 1, "event_id": "evt", "kind": "execution", "execution_id": "fill-1", "account_id": "acct-1", "symbol": "BTCUSDT", "side": "LONG", "qty": "1", "price": "100", "pnl": "2.5"},))
    assert outcomes.count(WriteOutcome.DUPLICATE) == 1
    assert len(store.rows("execution_events")) == 1
    conflict = store.append_execution({"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "fill-1", "symbol": "BTCUSDT", "side": "LONG", "qty": "2", "price": "100", "realized_pnl": "5"})
    assert conflict is WriteOutcome.INCONSISTENT


def test_bundle_writes_snapshots_events_and_checkpoint(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [{"symbol": "BTCUSDT", "side": "LONG", "size": "1"}], "orders": [], "executions": []}
    outcomes = store.append_reconcile_bundle(rest, events=({"channel": "wallet", "sequence": 1, "event_id": "w1"},), reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1},))
    assert WriteOutcome.INSERTED in outcomes
    assert store.latest_checkpoint("dep-1", "wallet")["sequence"] == 1
    assert len(store.rows("position_snapshots")) == 1
    store.close()


def test_bundle_persists_cashflows_and_excludes_them_from_account_snapshot(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": [], "cashflows": [{"cashflow_id": "cf-1", "amount": "10"}]}
    outcomes = store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1},))
    assert WriteOutcome.INSERTED in outcomes
    assert len(store.rows("cashflow_events")) == 1
    assert "cashflows" not in store.rows("account_snapshots")[0]
    store.close()


@pytest.mark.parametrize("cashflows", [None, {"cashflow_id": "cf-1"}, [{"amount": "10"}], [{"cashflow_id": "cf-1", "amount": 1.5}]])
def test_bundle_validates_cashflows_before_opening_transaction(tmp_path: Path, cashflows: object) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {}, "positions": [], "orders": [], "executions": [], "cashflows": cashflows}
    with pytest.raises((TypeError, ValueError)):
        store.append_reconcile_bundle(rest)
    assert not store.rows("account_snapshots")
    assert not store.rows("cashflow_events")
    store.close()


def test_rollback_on_conflict_is_opt_in_and_carries_outcomes(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": []}
    store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1},))
    before = {table: len(store.rows(table)) for table in ("account_snapshots", "reconciliations", "stream_checkpoints")}
    with pytest.raises(LiveStoreConflict) as error:
        store.append_reconcile_bundle({**rest, "wallet": {"equity": "101"}}, reconciliation={"reconcile_id": "rec-2", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 2},), rollback_on_conflict=True)
    assert WriteOutcome.CONFLICT in error.value.outcomes
    assert {table: len(store.rows(table)) for table in before} == before
    store.close()


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_rollback_on_conflict_requires_a_boolean(tmp_path: Path, value: object) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {}, "positions": [], "orders": [], "executions": []}
    with pytest.raises(TypeError, match="rollback_on_conflict"):
        store.append_reconcile_bundle(rest, rollback_on_conflict=value)  # type: ignore[arg-type]
    store.close()


def test_rollback_on_reconciliation_conflict_includes_reconciliation_outcome(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {}, "positions": [], "orders": [], "executions": []}
    store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"})
    before = len(store.rows("reconciliations"))
    with pytest.raises(LiveStoreConflict) as error:
        store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-1", "status": "UNKNOWN"}, rollback_on_conflict=True)
    assert WriteOutcome.CONFLICT in error.value.outcomes
    assert len(store.rows("reconciliations")) == before
    store.close()


def test_checkpoint_conflict_marks_reconciliation_inconsistent(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": []}
    store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1, "state": "ok"},))
    outcomes = store.append_reconcile_bundle(rest, reconciliation={"reconcile_id": "rec-2", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1, "state": "changed"},))
    assert WriteOutcome.CONFLICT in outcomes
    latest = store.rows("reconciliations")[-1]
    assert latest["status"] == "INCONSISTENT"
    assert "CONFLICTING_LIVE_FACT" in latest["reasons"]
    store.close()


def test_changed_snapshot_blocks_checkpoint_and_marks_second_reconcile_inconsistent(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    first = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": []}
    store.append_reconcile_bundle(first, reconciliation={"reconcile_id": "rec-1", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 1},))
    changed = {**first, "wallet": {"equity": "101"}}
    store.append_reconcile_bundle(changed, reconciliation={"reconcile_id": "rec-2", "status": "HEALTHY"}, checkpoints=({"channel": "wallet", "sequence": 2},))
    assert store.latest_checkpoint("dep-1", "wallet")["sequence"] == 1
    assert len(store.rows("stream_checkpoints")) == 1
    latest = store.rows("reconciliations")[-1]
    assert latest["reconcile_id"] == "rec-2"
    assert latest["status"] == "INCONSISTENT"
    assert "CONFLICTING_LIVE_FACT" in latest["reasons"]
    store.close()


def test_stream_sequence_and_checkpoint_columns_are_authoritative(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    store.append_stream_event({"deployment_id": "dep-1", "channel": "wallet", "event_id": "w1", "sequence": 7})
    assert store.connection.execute("SELECT sequence FROM stream_events").fetchone()[0] == 7
    store._append("stream_checkpoints", ("dep-1", "wallet", 3), ("deployment_id", "channel", "sequence"), {"sequence": 99, "source_kind": "WS"})
    checkpoint = store.latest_checkpoint("dep-1", "wallet")
    assert checkpoint == {"deployment_id": "dep-1", "channel": "wallet", "sequence": 3, "source_kind": "WS"}
    store.close()


def test_bundle_without_explicit_healthy_reconciliation_skips_checkpoints(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    rest = {"deployment_id": "dep-1", "snapshot_id": "snap-1", "account_id": "acct-1", "observed_at": "2026-09-07T00:00:00Z", "wallet": {"equity": "100"}, "positions": [], "orders": [], "executions": []}
    store.append_reconcile_bundle(rest, checkpoints=({"channel": "wallet", "sequence": 1},))
    assert store.latest_checkpoint("dep-1", "wallet") is None
    store.close()


def test_canonical_decimal_and_recursive_local_path_guard() -> None:
    assert canonical_json({"amount": Decimal("1.2300"), "zero": Decimal("-0.00")}) == '{"amount":"1.23","zero":"0"}'
    assert canonical_bytes({"amount": Decimal("1.2300")}) == b'{"amount":"1.23"}'
    with pytest.raises(ValueError, match="source ordering"):
        canonical_source_order({})
    source = {"effective_at_utc": "2026-09-07T00:00:00Z", "observed_at_utc": "2026-09-07T00:00:01Z", "source_kind": "WS", "source_id": "w1", "source_sequence": 2}
    assert canonical_source_order(source) == (
        source["effective_at_utc"], source["observed_at_utc"], source["source_kind"], source["source_id"], source["source_sequence"], sha256(canonical_json(source).encode()).hexdigest()
    )
    with pytest.raises(ValueError, match="local path"):
        DeploymentManifest({**manifest(), "nested": {"fixture_path": "C:/private/fixture.json"}})
    with pytest.raises(ValueError, match="local path"):
        DeploymentManifest({**manifest(), "nested": [{"credentials_path": "/tmp/secret.json"}]})


def test_manifest_and_fact_float_guards_are_wrapped_and_atomic(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid deployment manifest.*float"):
        DeploymentManifest({**manifest(), "nested": 1.5})
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    with pytest.raises(TypeError, match="floats"):
        store.append_execution({"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "float-1", "value": {"amount": 1.5}})
    assert not store.rows("execution_events")
    store.close()


def test_rows_follow_full_canonical_source_order(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    facts = [
        {"deployment_id": "dep-1", "snapshot_id": "s3", "observed_at": "2026-09-07T00:00:03Z", "effective_at": "2026-09-07T00:00:03Z", "source_kind": "WS", "source_id": "z", "source_sequence": 3, "equity": "103"},
        {"deployment_id": "dep-1", "snapshot_id": "s1", "observed_at": "2026-09-07T00:00:01Z", "effective_at": "2026-09-07T00:00:01Z", "source_kind": "REST", "source_id": "a", "source_sequence": 1, "equity": "101"},
        {"deployment_id": "dep-1", "snapshot_id": "s4", "observed_at": "2026-09-07T00:00:04Z", "effective_at": "2026-09-07T00:00:04Z", "source_kind": "WS", "source_id": "b", "source_sequence": 4, "equity": "104"},
        {"deployment_id": "dep-1", "snapshot_id": "s2", "observed_at": "2026-09-07T00:00:02Z", "effective_at": "2026-09-07T00:00:02Z", "source_kind": "REST", "source_id": "c", "source_sequence": 2, "equity": "102"},
    ]
    for fact in facts:
        assert store.append_account_snapshot(fact) is WriteOutcome.INSERTED
    expected = sorted(
        (
            fact["effective_at"], fact["observed_at"], fact["source_kind"], fact["source_id"], fact["source_sequence"], canonical_digest(fact)
        )
        for fact in facts
    )
    actual = [
        (row["effective_at"], row["observed_at"], row["source_kind"], row["source_id"], row["source_sequence"], row["canonical_digest"])
        for row in store.rows("account_snapshots")
    ]
    assert actual == expected
    store.close()


def test_fact_rows_have_real_manifest_foreign_key_and_revision_provenance(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    foreign_keys = store.connection.execute("PRAGMA foreign_key_list(account_snapshots)").fetchall()
    assert any(row[2] == "deployment_manifests" for row in foreign_keys)
    assert store.append_account_snapshot({"deployment_id": "dep-1", "snapshot_id": "s1", "observed_at": "2026-09-07T00:00:00Z", "equity": Decimal("100.00")}) is WriteOutcome.INSERTED
    row = store.rows("account_snapshots")[0]
    assert row["manifest_id"] == "man-1"
    assert row["manifest_version"] == "1"
    assert row["schema_version"] == 1
    assert store.append_account_snapshot({"deployment_id": "dep-1", "snapshot_id": "s1", "observed_at": "2026-09-07T00:00:00Z", "equity": Decimal("101")}) is WriteOutcome.CONFLICT
    assert store.append_account_snapshot({"deployment_id": "dep-1", "snapshot_id": "s1-correction", "observed_at": "2026-09-07T00:00:00Z", "equity": Decimal("101"), "parent_snapshot_id": "s1", "correction_of": "s1"}) is WriteOutcome.INSERTED
    corrected = store.rows("account_snapshots")[-1]
    assert corrected["parent_snapshot_id"] == "s1"
    assert corrected["correction_of"] == "s1"
    store.close()


def test_fact_local_paths_are_rejected_recursively(tmp_path: Path) -> None:
    store = LiveStore(tmp_path / "live.sqlite3")
    store.append_manifest(manifest())
    with pytest.raises(ValueError, match="local path"):
        store.append_execution({"deployment_id": "dep-1", "account_id": "acct-1", "execution_id": "fill-1", "nested": {"fixture_path": "D:/reports/live.json"}})
    store.close()
