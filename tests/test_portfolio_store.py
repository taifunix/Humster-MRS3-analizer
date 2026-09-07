from __future__ import annotations

import json
from contextlib import suppress
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import time
from pathlib import Path
from threading import Barrier

import duckdb
import pytest
import mrs3.portfolio.store as store_module

from mrs3.portfolio.store import (
    LOCK_GATE_UNAVAILABLE,
    LOCK_OWNER_UNVERIFIABLE,
    LOCK_RELEASE_FAILED,
    PortfolioDBLease,
    PortfolioStore,
    PortfolioStoreError,
    manual_clear_lock,
)
from mrs3.portfolio.reports import ReportNormalizationError, normalize_report


def _m6_report(member: str, *, run_id: str = "run", attempt_id: str = "attempt", size: str = "1", bad: bool = False, actual_leverage: dict[str, str] | None = None, margin_fail: bool = False, missing_close_pnl: bool = False, portfolio: dict[str, str] | None = None):
    action = {"timestamp": "2026-01-01T00:00:00Z", "symbol": member, "action": "OPEN", "side": "LONG", "size": size}
    document = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": run_id, "attempt_id": attempt_id, "member": member},
        "action_count": 2, "actions": [action, {**action, "timestamp": "2026-01-01T00:00:01Z", "action": "CLOSE", "size": "1"}],
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "series": {"equity": [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "101"]]},
    }
    if actual_leverage is not None:
        document["actual_leverage"] = actual_leverage
    if margin_fail:
        document["series"]["notional"] = [["2026-01-01T00:00:00Z", "200"], ["2026-01-01T00:00:02Z", "200"]]
        document["series"]["margin_balance"] = [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "100"]]
    if missing_close_pnl:
        document["actions"][1].pop("pnl", None)
    if portfolio is not None:
        document["portfolio"] = portfolio
    if bad:
        document["action_count"] = 3
    return normalize_report(document)


def test_schema_has_logical_entities_and_numeric_child_series(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.initialize()

    with duckdb.connect(str(store.path), read_only=True) as db:
        tables = {row[0] for row in db.execute("SHOW TABLES").fetchall()}

    assert {"campaigns", "trading_runs", "evaluations", "portfolio_sets"} <= tables
    assert {"trading_run_series", "evaluation_series", "portfolio_set_series"} <= tables


def test_schema_markers_are_created_and_existing_mismatch_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    store = PortfolioStore(target)
    store.initialize()

    with duckdb.connect(str(target), read_only=True) as db:
        assert db.execute("SELECT schema_version, database_kind, database_instance_id FROM schema_info").fetchone()[0:2] == (
            1,
            "mrs3_portfolio",
        )
        assert db.execute("SELECT count(*) FROM schema_info").fetchone()[0] == 1
    with duckdb.connect(str(target)) as db:
        db.execute("UPDATE schema_info SET schema_version = 999")
    with pytest.raises(PortfolioStoreError, match="schema marker"):
        store.create_campaign("c1", "d1", "content")


def test_campaign_exact_duplicate_returns_original_and_collision_fails_closed(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    first = store.create_campaign("c1", "digest-1", {"x": 1})

    assert store.create_campaign("c2", "digest-1", {"x": 1}) == first
    with pytest.raises(PortfolioStoreError, match="collision|mismatch"):
        store.create_campaign("c3", "digest-1", {"x": 2})
    with pytest.raises(PortfolioStoreError, match="collision|mismatch"):
        store.create_campaign("c1", "digest-2", {"x": 1})


def test_duplicate_snapshot_and_run_are_idempotent_and_run_lineage_is_immutable(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "digest-1", "content")
    assert store.save_campaign_snapshot("s1", campaign, "snapshot-digest", "snapshot") == "s1"
    assert store.save_campaign_snapshot("s1", campaign, "snapshot-digest", "snapshot") == "s1"
    assert store.get_campaign_snapshot("s1")["content"] == "snapshot"
    assert store.create_trading_run("r1", campaign, {"payload": 1}) == "r1"
    assert store.create_trading_run("r1", campaign, {"payload": 1}) == "r1"

    with pytest.raises(PortfolioStoreError, match="immutable|mismatch"):
        store.create_trading_run("r1", "another-campaign", {"payload": 1})


def test_concurrent_exact_campaign_duplicate_returns_one_identity(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    barrier = Barrier(2)

    def publish(campaign_id: str) -> str:
        barrier.wait()
        return PortfolioStore(target).create_campaign(campaign_id, "same-digest", {"fact": "same"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(publish, ("c1", "c2")))
    assert result[0] == result[1]
    assert result[0] in {"c1", "c2"}
    with duckdb.connect(str(target), read_only=True) as db:
        assert db.execute("select count(*) from campaigns").fetchone() == (1,)
        assert db.execute("select campaign_id, canonical_digest, content from campaigns").fetchone() == (
            result[0],
            "same-digest",
            '{"fact":"same"}',
        )


def test_campaign_lock_contention_without_committed_duplicate_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    with PortfolioDBLease(target):
        with pytest.raises(PortfolioStoreError, match="busy"):
            PortfolioStore(target).create_campaign("c1", "d1", "content")


def test_store_does_not_retry_unverifiable_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    lease.lock_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_start_identity": "unknown",
                "host_identity": "foreign-host",
                "boot_identity": "foreign-boot",
                "target_path": str(target.resolve()),
            }
        ),
        encoding="utf-8",
    )

    def unexpected_database_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unverifiable lock must not retry through DuckDB")

    monkeypatch.setattr(store_module.duckdb, "connect", unexpected_database_open)
    with pytest.raises(PortfolioStoreError, match=LOCK_OWNER_UNVERIFIABLE):
        PortfolioStore(target).create_campaign("c1", "d1", "content")
    assert not target.exists()


def test_pid_namespace_mismatch_blocks_pid_inspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    owner = {**lease.identity, "pid": 999999, "process_start_identity": "dead", "pid_namespace_identity": "other"}
    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")

    def unexpected_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PID must not be inspected across namespaces")

    monkeypatch.setattr(store_module.psutil, "Process", unexpected_process)
    with pytest.raises(PortfolioStoreError) as raised:
        with PortfolioDBLease(target):
            pass
    assert raised.value.code == LOCK_OWNER_UNVERIFIABLE


def test_pid_namespace_identity_is_explicit_and_unknown_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = store_module._pid_namespace_identity("machine")
    if store_module.sys.platform.startswith("linux"):
        assert identity == str(store_module.os.stat("/proc/self/ns/pid").st_ino)

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(store_module.os, "stat", unreadable)
    if store_module.os.name != "nt" and store_module.sys.platform.startswith("linux"):
        assert store_module._pid_namespace_identity("machine") == "unknown"


def test_evaluation_keeps_execution_and_decision_campaigns_separate(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    execution = store.create_campaign("execution", "ed", "execution")
    decision = store.create_campaign("decision", "dd", "decision")
    store.create_trading_run("run", execution, "run-payload")
    store.create_evaluation("evaluation", "run", decision, "evaluation-payload")

    row = store.get_evaluation("evaluation")
    assert row["execution_campaign_id"] == execution
    assert row["decision_campaign_id"] == decision


def test_portfolio_set_supports_empty_members_order_and_idempotent_recreation(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")

    assert store.create_portfolio_set("empty", "empty-digest", {"kind": "empty"}) == "empty"
    assert store.create_portfolio_set("empty", "empty-digest", {"kind": "empty"}) == "empty"
    assert (
        store.create_portfolio_set(
            "set-1", "set-digest", {"kind": "ordered"}, ("member-a", "member-b")
        )
        == "set-1"
    )
    assert (
        store.create_portfolio_set(
            "set-2", "set-digest", {"kind": "ordered"}, ("member-a", "member-b")
        )
        == "set-1"
    )

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute(
            "SELECT member_ordinal, member_id FROM portfolio_set_members "
            "WHERE portfolio_set_id = 'set-1' ORDER BY member_ordinal"
        ).fetchall() == [(0, "member-a"), (1, "member-b")]
        assert db.execute(
            "SELECT count(*) FROM portfolio_set_members WHERE portfolio_set_id = 'empty'"
        ).fetchone() == (0,)


def test_portfolio_set_identity_content_member_mismatch_and_digest_collision_fail(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.create_portfolio_set("set-1", "set-digest", {"kind": "ordered"}, ("member-a",))

    with pytest.raises(PortfolioStoreError, match="identity mismatch"):
        store.create_portfolio_set("set-1", "other-digest", {"kind": "ordered"}, ("member-a",))
    with pytest.raises(PortfolioStoreError, match="identity mismatch"):
        store.create_portfolio_set("set-1", "set-digest", {"kind": "changed"}, ("member-a",))
    with pytest.raises(PortfolioStoreError, match="identity mismatch"):
        store.create_portfolio_set("set-1", "set-digest", {"kind": "ordered"}, ("member-b",))
    with pytest.raises(PortfolioStoreError, match="digest collision"):
        store.create_portfolio_set("set-2", "set-digest", {"kind": "changed"}, ("member-a",))


def test_evaluation_and_portfolio_set_series_store_exact_numeric_values(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "campaign")
    store.create_trading_run("r1", campaign, "run")
    evaluation = store.create_evaluation("e1", "r1", campaign, "evaluation")
    portfolio_set = store.create_portfolio_set("set-1", "set-digest", "set")
    points = [
        ("2026-01-01T00:00:00Z", Decimal("100.120000000001"), 7),
        ("2026-01-01T00:00:01Z", Decimal("99.5"), 8),
    ]

    assert store.insert_evaluation_series(evaluation, points, "equity") == 2
    assert store.insert_portfolio_set_series(portfolio_set, points, "equity") == 2

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute(
            "SELECT timestamp_utc, source_ordinal, numeric_value FROM evaluation_series "
            "WHERE evaluation_id = 'e1' ORDER BY source_ordinal"
        ).fetchall() == [
            ("2026-01-01T00:00:00Z", 7, Decimal("100.120000000001")),
            ("2026-01-01T00:00:01Z", 8, Decimal("99.500000000000")),
        ]
        assert db.execute(
            "SELECT timestamp_utc, source_ordinal, numeric_value FROM portfolio_set_series "
            "WHERE portfolio_set_id = 'set-1' ORDER BY source_ordinal"
        ).fetchall() == [
            ("2026-01-01T00:00:00Z", 7, Decimal("100.120000000001")),
            ("2026-01-01T00:00:01Z", 8, Decimal("99.500000000000")),
        ]


@pytest.mark.parametrize(
    ("insert_method", "table", "owner_id"),
    (
        ("insert_evaluation_series", "evaluation_series", "e1"),
        ("insert_portfolio_set_series", "portfolio_set_series", "set-1"),
    ),
)
def test_evaluation_and_portfolio_set_series_require_owner_and_rollback_on_bad_point(
    tmp_path: Path, insert_method: str, table: str, owner_id: str
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "campaign")
    store.create_trading_run("r1", campaign, "run")
    store.create_evaluation("e1", "r1", campaign, "evaluation")
    store.create_portfolio_set("set-1", "set-digest", "set")
    insert = getattr(store, insert_method)
    valid = ("2026-01-01T00:00:00Z", Decimal("1"))

    with pytest.raises(PortfolioStoreError, match="unknown"):
        insert("missing", [valid])
    with pytest.raises(PortfolioStoreError):
        insert(owner_id, [valid, ("bad", Decimal("2"))])

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_series_publication_rolls_back_all_points_on_failure(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "c")
    store.create_trading_run("r1", campaign, "run")

    with pytest.raises(PortfolioStoreError):
        store.insert_series("r1", [("2026-01-01T00:00:00Z", 100), ("bad", "not-a-number")])

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM trading_run_series").fetchone()[0] == 0


def test_series_uses_exact_decimal_storage_and_rejects_float_or_excess_scale(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "c")
    store.create_trading_run("r1", campaign, "run")
    store.insert_series("r1", [("2026-01-01T00:00:00Z", Decimal("1.230000000001"))])

    with duckdb.connect(str(store.path), read_only=True) as db:
        column = db.execute("DESCRIBE trading_run_series").fetchall()
        value_type = next(row[1] for row in column if row[0] == "numeric_value")
        assert value_type == "DECIMAL(38,12)"
        assert db.execute("SELECT numeric_value FROM trading_run_series").fetchone()[0] == Decimal("1.230000000001")

    with pytest.raises(PortfolioStoreError, match="decimal|float"):
        store.insert_series("r1", [("2026-01-01T00:00:01Z", 1.25)])
    with pytest.raises(PortfolioStoreError, match="scale"):
        store.insert_series("r1", [("2026-01-01T00:00:02Z", "1.0000000000001")])
    store.insert_series("r1", [("2026-01-01T00:00:03Z", Decimal("9" * 26), 1)])
    with pytest.raises(PortfolioStoreError, match="integer digits"):
        store.insert_series("r1", [("2026-01-01T00:00:04Z", Decimal("1" + "0" * 26), 2)])


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00.1Z",
        "2026-01-01T00:00:00.12345Z",
        "2026-02-30T00:00:00Z",
        "2026-01-01T00:00:00+02:00",
    ),
)
def test_series_rejects_noncanonical_or_non_utc_timestamps(tmp_path: Path, timestamp: str) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "c")
    store.create_trading_run("r1", campaign, "run")

    with pytest.raises(PortfolioStoreError, match="timestamp_utc"):
        store.insert_series("r1", [(timestamp, Decimal("1"))])


def test_series_accepts_declared_timestamp_precisions(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    campaign = store.create_campaign("c1", "d1", "c")
    store.create_trading_run("r1", campaign, "run")

    store.insert_series(
        "r1",
        [
            ("2026-01-01T00:00:00.123Z", Decimal("1")),
            ("2026-01-01T00:00:00.123456Z", Decimal("2")),
        ],
    )


def test_current_result_replacement_keeps_one_stable_result_id(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.replace_current_result("17", {"window": "W1"})
    store.replace_current_result("17", {"window": "W2"})

    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT count(*), payload FROM current_results WHERE result_id = '17' GROUP BY result_id, payload").fetchone() == (1, '{"window":"W2"}')


def test_lock_contention_and_canonical_alias(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    alias = target.parent / "sub" / ".." / target.name
    target.parent.joinpath("sub").mkdir()
    with PortfolioDBLease(target):
        with pytest.raises(PortfolioStoreError, match="busy"):
            with PortfolioDBLease(alias):
                pass
    assert PortfolioDBLease(target).lock_path == PortfolioDBLease(alias).lock_path


def test_lease_release_does_not_remove_replacement_token(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    lease.__enter__()
    try:
        replacement = json.loads(lease.lock_path.read_text(encoding="utf-8"))
        replacement["acquisition_token"] = "replacement-token"
        lease.lock_path.write_text(json.dumps(replacement), encoding="utf-8")
    finally:
        lease.__exit__(None, None, None)
    assert lease.lock_path.exists()
    lease.lock_path.unlink()


def test_lease_release_retries_gate_contention_and_removes_own_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = PortfolioDBLease(tmp_path / "portfolio.duckdb")
    lease.__enter__()
    calls = 0

    real_remove = store_module._remove_lock_if_unchanged

    def contend_once(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        if calls == 0:
            calls += 1
            raise PortfolioStoreError("contended", code="LOCK_BUSY")
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(store_module, "_remove_lock_if_unchanged", contend_once)
    lease.__exit__(None, None, None)
    assert calls == 1
    assert not lease.lock_path.exists()


def test_lease_release_surfaces_bounded_gate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = PortfolioDBLease(tmp_path / "portfolio.duckdb")
    lease.__enter__()

    def always_busy(*_args: object, **_kwargs: object) -> bool:
        raise PortfolioStoreError("busy", code="LOCK_BUSY")

    monkeypatch.setattr(store_module, "_remove_lock_if_unchanged", always_busy)
    with pytest.raises(PortfolioStoreError) as raised:
        lease.__exit__(None, None, None)
    assert raised.value.code == LOCK_RELEASE_FAILED
    assert lease.lock_path.exists()
    lease.lock_path.unlink()


def test_gate_open_failure_is_coded_and_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(PortfolioStoreError) as raised:
        with store_module._locked_gate(tmp_path / "missing" / "gate"):
            pass
    assert raised.value.code == LOCK_GATE_UNAVAILABLE


def test_vanished_lock_metadata_is_busy_and_retries_link_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "portfolio.duckdb"
    stale = PortfolioDBLease(target)
    stale.lock_path.write_text(json.dumps({**stale.identity, "pid": 999999}), encoding="utf-8")
    candidate = PortfolioDBLease(target)
    real_link = store_module.os.link
    vanished = False

    def vanish_then_link(source: object, destination: object) -> None:
        nonlocal vanished
        if Path(destination) == candidate.lock_path and not vanished:
            vanished = True
            candidate.lock_path.unlink()
            raise FileExistsError(destination)
        real_link(source, destination)

    monkeypatch.setattr(store_module.os, "link", vanish_then_link)
    with candidate:
        assert vanished
        assert candidate.lock_path.exists()


def test_foreign_owner_is_unverifiable_and_cannot_be_reclaimed(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    lease.lock_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_start_identity": "unknown",
                "host_identity": "foreign-host",
                "boot_identity": "foreign-boot",
                "target_path": str(target.resolve()),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PortfolioStoreError, match=LOCK_OWNER_UNVERIFIABLE):
        with PortfolioDBLease(target):
            pass


def test_unknown_owner_identity_is_unverifiable_even_when_pid_is_dead(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    lease.lock_path.write_text(
        json.dumps({**lease.identity, "pid": 999999, "process_start_identity": "unknown"}),
        encoding="utf-8",
    )
    with pytest.raises(PortfolioStoreError, match=LOCK_OWNER_UNVERIFIABLE):
        with PortfolioDBLease(target):
            pass


@pytest.mark.parametrize("container_identity", [None, "unknown", "other-container"])
def test_dead_owner_requires_equal_known_container_identity(
    tmp_path: Path, container_identity: str | None
) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    owner = {**lease.identity, "pid": 999999, "process_start_identity": "dead"}
    if container_identity is None:
        owner.pop("container_identity")
    else:
        owner["container_identity"] = container_identity
    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")

    with pytest.raises(PortfolioStoreError) as raised:
        with PortfolioDBLease(target):
            pass
    assert raised.value.code == LOCK_OWNER_UNVERIFIABLE


def test_same_host_boot_dead_owner_is_recovered_without_audit(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    identity = lease.identity
    lease.lock_path.write_text(
        json.dumps(
            {
                **identity,
                "pid": 999999,
                "process_start_identity": "dead-start",
                "target_path": str(target.resolve()),
            }
        ),
        encoding="utf-8",
    )
    with PortfolioDBLease(target):
        assert lease.lock_path.exists()
    assert not lease.lock_path.exists()
    assert not lease.audit_path.exists()


def test_manual_clear_durably_appends_audit_before_removing_lock(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    owner = {
        **lease.identity,
        "pid": 999999,
        "process_start_identity": "retired",
        "target_path": str(target.resolve()),
    }
    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")

    manual_clear_lock(target, operator_identity="operator@example", reason="retired host")

    assert not lease.lock_path.exists()
    records = sorted(lease.audit_path.glob("*.json"))
    assert len(records) == 1
    event = json.loads(records[0].read_text(encoding="utf-8"))
    assert event["reason"] == "LOCK_MANUAL_CLEAR"
    assert event["stale_owner"] == owner
    assert event["operator_identity"] == "operator@example"


def test_manual_clear_does_not_remove_replacement_after_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "portfolio.duckdb"
    stale = PortfolioDBLease(target)
    stale_owner = {**stale.identity, "pid": 999999, "process_start_identity": "retired"}
    stale.lock_path.write_text(json.dumps(stale_owner), encoding="utf-8")
    replacement = PortfolioDBLease(target)
    real_samefile = store_module.os.path.samefile
    triggered = False
    replacement_blocked = False

    def replace_after_first_identity_check(left: object, right: object) -> bool:
        nonlocal replacement_blocked, triggered
        result = real_samefile(left, right)
        if not triggered:
            triggered = True
            try:
                replacement.__enter__()
            except PortfolioStoreError as error:
                replacement_blocked = error.code == "LOCK_BUSY"
        return result

    monkeypatch.setattr(store_module.os.path, "samefile", replace_after_first_identity_check)
    try:
        manual_clear_lock(target, operator_identity="operator@example", reason="race")
        assert replacement_blocked
        with replacement:
            assert replacement.lock_path.exists()
    finally:
        replacement.__exit__(None, None, None)


def test_automatic_recovery_never_creates_manual_attestation(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    lease.lock_path.write_text(
        json.dumps({**lease.identity, "pid": 999999, "process_start_identity": "dead"}),
        encoding="utf-8",
    )
    with PortfolioDBLease(target):
        pass
    assert not lease.audit_path.exists()


def test_manual_clear_appends_second_record_and_publication_failure_keeps_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "portfolio.duckdb"
    lease = PortfolioDBLease(target)
    owner = {**lease.identity, "pid": 999999, "process_start_identity": "retired"}
    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")
    manual_clear_lock(target, operator_identity="operator@example", reason="first")
    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")
    manual_clear_lock(target, operator_identity="operator@example", reason="second")
    assert len(list(lease.audit_path.glob("*.json"))) == 2

    lease.lock_path.write_text(json.dumps(owner), encoding="utf-8")
    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected attestation failure")
    monkeypatch.setattr("mrs3.portfolio.store._publish_attestation", fail_publication)
    with pytest.raises(OSError, match="injected") as raised:
        manual_clear_lock(target, operator_identity="operator@example", reason="failed")
    assert not isinstance(raised.value, PortfolioStoreError)
    assert lease.lock_path.exists()


def test_attestation_collision_never_overwrites_existing_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "portfolio.duckdb.lock.audit.v1"
    audit.mkdir()
    existing = audit / "event-collision.json"
    tokens = iter(("collision", "fresh"))
    monkeypatch.setattr(store_module, "uuid4", lambda: type("Token", (), {"hex": next(tokens)})())
    real_link = store_module.os.link

    def collision_link(source: Path, destination: Path) -> None:
        if Path(destination) == existing:
            existing.write_bytes(b"old\n")
            raise FileExistsError(destination)
        real_link(source, destination)

    monkeypatch.setattr(store_module.os, "link", collision_link)
    published = store_module._publish_attestation(audit, {"event": "new"})

    assert published.name == "event-fresh.json"
    assert existing.read_bytes() == b"old\n"


def test_reclaim_then_new_owner_race_is_lock_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "portfolio.duckdb"
    stale = PortfolioDBLease(target)
    stale.lock_path.write_text(
        json.dumps({**stale.identity, "pid": 999999, "process_start_identity": "dead"}),
        encoding="utf-8",
    )
    replacement = PortfolioDBLease(target)
    real_remove = store_module._remove_lock_if_unchanged
    triggered = False

    def reclaim_then_acquire(lock_path: Path, expected: bytes, gate: object = None) -> bool:
        nonlocal triggered
        result = real_remove(lock_path, expected, gate)
        if result and not triggered:
            triggered = True
            replacement.lock_path.write_text(
                json.dumps({**replacement.identity, "acquisition_token": "replacement"}),
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(store_module, "_remove_lock_if_unchanged", reclaim_then_acquire)
    try:
        with pytest.raises(PortfolioStoreError) as raised:
            with PortfolioDBLease(target):
                pass
        assert raised.value.code == "LOCK_BUSY"
        assert replacement.lock_path.exists()
    finally:
        replacement.__exit__(None, None, None)
        with suppress(FileNotFoundError):
            replacement.lock_path.unlink()


def _spawn_holding_lease(target: Path, ready: Path, release: Path) -> subprocess.Popen[str]:
    script = """
import sys
import time
from pathlib import Path
from mrs3.portfolio.store import PortfolioDBLease

target, ready, release = map(Path, sys.argv[1:4])
lease = PortfolioDBLease(target)
lease.__enter__()
try:
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
finally:
    lease.__exit__(None, None, None)
"""
    return subprocess.Popen(
        [sys.executable, "-c", script, str(target), str(ready), str(release)],
        cwd=str(Path.cwd()),
    )


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.01)
    assert path.exists()


def test_cross_process_busy_reclaim_and_manual_clear_are_safe(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.duckdb"

    ready = tmp_path / "live.ready"
    release = tmp_path / "live.release"
    live = _spawn_holding_lease(target, ready, release)
    try:
        _wait_for_path(ready)
        with pytest.raises(PortfolioStoreError) as raised:
            with PortfolioDBLease(target):
                pass
        assert raised.value.code == "LOCK_BUSY"
        assert PortfolioDBLease(target).lock_path.exists()
    finally:
        release.write_text("release", encoding="utf-8")
        live.wait(timeout=10)

    ready = tmp_path / "dead.ready"
    release = tmp_path / "dead.release"
    dead = _spawn_holding_lease(target, ready, release)
    _wait_for_path(ready)
    dead.kill()
    dead.wait(timeout=10)
    with PortfolioDBLease(target):
        assert PortfolioDBLease(target).lock_path.exists()

    ready = tmp_path / "clear.ready"
    release = tmp_path / "clear.release"
    clear = _spawn_holding_lease(target, ready, release)
    try:
        _wait_for_path(ready)
        manual_clear_lock(target, operator_identity="operator@example", reason="cross-process")
        assert clear.poll() is None
        assert not PortfolioDBLease(target).lock_path.exists()
    finally:
        release.write_text("release", encoding="utf-8")
        clear.wait(timeout=10)


def test_m6_portfolio_publication_is_idempotent_and_readback_proved(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    report = _m6_report("A")
    first = store.publish_portfolio_run("run", (report,), executable_identity={"binary": "b"})
    second = store.publish_portfolio_run("run", (report,), executable_identity={"binary": "b"})
    assert first == second
    assert first.normalized_facts_complete
    facts = store.read_portfolio_run("run", "attempt")
    assert facts and facts["report_count"] == facts["action_count"] // 2 == 1
    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM portfolio_report_actions").fetchone() == (2,)


def test_m6_corrupt_member_fails_before_any_portfolio_write(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    _m6_report("A")
    with pytest.raises(ReportNormalizationError):
        _m6_report("B", bad=True)
    assert store.read_portfolio_run("run") is None


def test_m6_same_executable_semantic_mismatch_is_nondeterministic(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    first = _m6_report("A", attempt_id="first", size="1")
    second = _m6_report("A", attempt_id="second", size="2")
    store.publish_portfolio_run("run", (first,), executable_identity={"binary": "b"})
    proof = store.publish_portfolio_run("run", (second,), executable_identity={"binary": "b"})
    assert not proof.committed
    assert store.read_portfolio_run("run", "second")["status"] == "NONDETERMINISTIC_RESULT"


def test_m6_unavailable_or_changed_executable_identity_is_unverified(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    first = _m6_report("A", attempt_id="first")
    second = _m6_report("A", attempt_id="second", size="2")
    store.publish_portfolio_run("run", (first,), executable_identity={})
    unknown = store.publish_portfolio_run("run", (second,), executable_identity={})
    assert not unknown.committed
    assert store.read_portfolio_run("run", "second")["status"] == "UNKNOWN"

    other = _m6_report("A", attempt_id="other", size="3")
    store.publish_portfolio_run("run", (other,), executable_identity={"binary": "other"})
    assert store.compare_portfolio_runs("run", "first", "other") == "UNKNOWN"


def test_m6_leverage_mismatch_is_persisted_and_blocks_cleanup_proof(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    proof = store.publish_portfolio_run(
        "run", (_m6_report("A", actual_leverage={"A": "5"}),),
        executable_identity={"binary": "b"}, planned_leverage={"A": "3"},
    )
    assert proof.committed is False and proof.normalized_facts_complete is False
    facts = store.read_portfolio_run("run", "attempt")
    assert facts["status"] == "NEEDS_RETEST"
    assert facts["metrics"]["A"]["leverage"]["reason"] == "LEVERAGE_MISMATCH"


def test_m6_relational_decimal_facts_keep_eighteen_fractional_digits(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    report = _m6_report("A", size="1.123456789012345678")
    store.publish_portfolio_run("run", (report,), executable_identity={"binary": "b"})
    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT numeric_size FROM portfolio_report_actions WHERE source_ordinal = 0").fetchone() == ("1.123456789012345678",)


def test_m6_corrupt_metrics_payload_fails_durable_readback(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.publish_portfolio_run("run", (_m6_report("A"),), executable_identity={"binary": "b"})
    with duckdb.connect(str(store.path)) as db:
        db.execute("UPDATE portfolio_runs SET payload = ? WHERE run_id = ?", ['{"metrics":{}}', "run"])
    with pytest.raises(PortfolioStoreError):
        store.read_portfolio_run("run", "attempt")


def test_m6_post_commit_readback_failure_cannot_return_cleanup_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")

    def fail_readback(*_args, **_kwargs):
        raise PortfolioStoreError("forced durable readback failure")

    monkeypatch.setattr(store, "read_portfolio_run", fail_readback)
    with pytest.raises(PortfolioStoreError, match="forced durable"):
        store.publish_portfolio_run("run", (_m6_report("A"),), executable_identity={"binary": "b"})
    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM portfolio_runs").fetchone() == (1,)


@pytest.mark.parametrize("identity", [object(), 1.25])
def test_m6_identity_rejects_unsupported_or_float_values(tmp_path: Path, identity: object) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    with pytest.raises(PortfolioStoreError):
        store.publish_portfolio_run("run", (_m6_report("A"),), executable_identity={"binary": identity})


def test_m6_margin_failure_is_persisted_incomplete_and_cannot_prove_cleanup(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    proof = store.publish_portfolio_run("run", (_m6_report("A", margin_fail=True),), executable_identity={"binary": "b"})
    assert proof.committed is False and proof.normalized_facts_complete is False
    facts = store.read_portfolio_run("run", "attempt")
    assert facts["status"] == "INCOMPLETE"
    assert facts["metrics"]["A"]["margin_guard"]["status"] == "FAIL"
    assert facts["metrics"]["A"]["margin_guard"]["reason"] == "MARGIN_BOUND_FAILED"


def test_m6_unverified_financial_reconciliation_is_persisted_incomplete(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    report = _m6_report(
        "A", missing_close_pnl=True,
        portfolio={"realized_pnl": "0", "fees": "0"},
    )
    proof = store.publish_portfolio_run("run", (report,), executable_identity={"binary": "b"})
    assert proof.committed is False and proof.normalized_facts_complete is False
    facts = store.read_portfolio_run("run", "attempt")
    assert facts["status"] == "INCOMPLETE"
    assert "FINANCIAL_RECONCILIATION_UNVERIFIED" in facts["metrics"]["A"]["blocking_diagnostics"]


def test_m6_compare_rejects_missing_database_and_identical_attempt(tmp_path: Path) -> None:
    missing = PortfolioStore(tmp_path / "missing.duckdb")
    with pytest.raises(PortfolioStoreError, match="evidence is missing"):
        missing.compare_portfolio_runs("run", "one", "two")
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.initialize()
    with pytest.raises(PortfolioStoreError, match="distinct attempts"):
        store.compare_portfolio_runs("run", "one", "one")


def test_m6_mid_publish_failure_rolls_back_every_child_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    store.initialize()
    store.replace_current_result("old", {"value": "preserve"})
    first, second = _m6_report("A"), _m6_report("B")
    original = store_module._report_payload

    def fail_on_member(value):
        if getattr(value, "member", None) == "B":
            raise ValueError("corrupt sibling")
        return original(value)

    monkeypatch.setattr(store_module, "_report_payload", fail_on_member)
    with pytest.raises(PortfolioStoreError):
        store.publish_portfolio_run("run", (first, second), executable_identity={"binary": "b"})
    with duckdb.connect(str(store.path), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM current_results WHERE result_id = 'old'").fetchone() == (1,)
        for table in ("portfolio_runs", "portfolio_reports", "portfolio_report_actions", "portfolio_report_series", "portfolio_position_cycles"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
