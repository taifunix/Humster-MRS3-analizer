from __future__ import annotations

from hashlib import sha256
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
import mrs3.performance_v2_import as import_module

from mrs3.performance_v2_import import (
    PerformanceV2ImportError,
    PerformanceV2ImportRequest,
    PerformanceV2LockedError,
    import_performance_v2,
)
from mrs3.performance_v2_store import (
    PerformanceV2Config,
    initialize_performance_v2,
    performance_v2_database_path,
)


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "report_current_v2.html"


def _strategy(name: str, *, orders: int = 1, close_ma: int = 3) -> dict[str, object]:
    return {
        "name": name,
        "exchange": {"name": "Bybit", "use_upnl": True},
        "basic": {
            "strategy": "mrs3",
            "symbol": "ONUSDT",
            "time_frame": "1h",
            "use_long": True,
            "use_short": False,
        },
        "mrs3": {
            "ma_long": [
                {"id": i, "len": 6 + i, "multiplier": 1 - i / 1000, "lot_x": 1 / orders}
                for i in range(1, orders + 1)
            ],
            "ma_short": [],
            "ma_close_long": {"len": close_ma},
            "ma_close_short": {"len": close_ma},
        },
    }


def _canonical_strategy_hash(strategy: dict[str, object]) -> str:
    return sha256(
        json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _inbox(tmp_path: Path, names: tuple[str, ...] = ("alpha",), *, orders: int = 1) -> tuple[Path, Path, bytes]:
    inbox = tmp_path / "inbox"
    strategy_root = inbox / "strategies"
    report_root = tmp_path / "reports"
    strategy_root.mkdir(parents=True)
    report_root.mkdir()
    report = FIXTURE.read_bytes()
    diagnostics: dict[str, object] = {}
    entries: list[dict[str, object]] = []
    for index, name in enumerate(names, start=1):
        strategy = _strategy(name, orders=orders)
        strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
        strategy_path = strategy_root / f"{name}.json"
        strategy_path.write_bytes(strategy_bytes)
        report_path = report_root / f"{name}.html"
        report_path.write_bytes(report)
        diagnostics[f"candidate-{index}"] = {
            "order_count": orders,
            "orders": [
                {
                    "order_id": order_id,
                    "plateau_id": f"P{order_id}",
                    "plateau_point_count": 4,
                    "base_point_trades": 20,
                    "plateau_total_trades": 80,
                }
                for order_id in range(1, orders + 1)
            ],
        }
        entries.append(
            {
                "manifest_entry_id": f"{index:032x}",
                "strategy_name": name,
                "strategy_version_id": _canonical_strategy_hash(strategy),
                "strategy_path": str(strategy_path),
                "report_path": str(report_path),
                "wizard_run_id": "run-1",
                "exchange_name": "Bybit",
                "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
                "source_report_sha256": sha256(report).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "batch_id": "v2-test",
        "expected_strategy_names": list(names),
        "tester_config_sha256": "t" * 64,
        "commission_contract": {
            "MakerFee": "0.0002",
            "TakerFee": "0.0004",
            "SlippagePercent": "0.01",
            "FundingRate": "0.0001",
            "FundingIntervalHours": "8",
        },
        "commission_contract_id": "c" * 64,
        "run_mode": "FAST",
        "entries": entries,
        "v6_provenance": {
            "analysis_run_id": "a" * 64,
            "generation_manifest_sha256": "g" * 64,
            "strategy_json_sha256": {f"{name}.json": entries[i]["strategy_version_id"] for i, name in enumerate(names)},
            "candidate_identity_to_strategy_names": {
                f"candidate-{i}": [name] for i, name in enumerate(names, start=1)
            },
            "candidate_diagnostics": diagnostics,
        },
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = sha256(
        b"".join(
            path.relative_to(inbox).as_posix().encode() + b"\0" + path.read_bytes()
            for path in sorted(inbox.rglob("*"))
            if path.is_file()
        )
    ).digest()
    return inbox, report_root, snapshot


def _request(
    tmp_path: Path,
    *,
    names: tuple[str, ...] = ("alpha",),
    orders: int = 1,
    mode: str = "ADD",
    initialize_db: bool = True,
) -> tuple[PerformanceV2ImportRequest, bytes]:
    inbox, report_root, snapshot = _inbox(tmp_path, names, orders=orders)
    config = PerformanceV2Config(tmp_path / "v2", workers=4)
    request = PerformanceV2ImportRequest(inbox, report_root, config, mode=mode)
    if initialize_db:
        _db(request)
    return request, snapshot


def _db(request: PerformanceV2ImportRequest) -> Path:
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(target)) as connection:
        initialize_performance_v2(connection)
    return target


def _rewrite_report(request: PerformanceV2ImportRequest, replacement: bytes) -> None:
    report_path = request.report_root / "alpha.html"
    report_path.write_bytes(replacement)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["source_report_sha256"] = sha256(replacement).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def test_append_rows_uses_duckdb_native_dataframe_append() -> None:
    class AppendOnlyConnection:
        def __init__(self) -> None:
            self.connection = duckdb.connect(":memory:")
            self.connection.execute("create table rows_to_append (id integer, amount decimal(38, 12))")

        def append(self, table: str, frame) -> None:
            self.connection.append(table, frame)

        def executemany(self, *_args: object) -> None:
            raise AssertionError("large report rows must not use executemany")

    connection = AppendOnlyConnection()
    import_module._append_rows(
        connection, "rows_to_append", ("id", "amount"), [(1, Decimal("1.25")), (2, Decimal("2.50"))]
    )

    assert connection.connection.execute("select * from rows_to_append order by id").fetchall() == [
        (1, Decimal("1.250000000000")),
        (2, Decimal("2.500000000000")),
    ]


def test_append_rows_rounds_long_decimal_before_dataframe_type_inference() -> None:
    class CaptureConnection:
        frame = None

        def append(self, _table: str, frame) -> None:
            self.frame = frame

    connection = CaptureConnection()

    import_module._append_rows(
        connection,
        "rows_to_append",
        ("id", "amount"),
        [(1, Decimal("-29.769149208741522230595327812"))],
    )

    assert connection.frame.iloc[0]["amount"] == "-29.769149208742"

    target = duckdb.connect(":memory:")
    target.execute("create table rows_to_append (id integer, amount decimal(38, 12))")
    import_module._append_rows(
        target,
        "rows_to_append",
        ("id", "amount"),
        [(1, Decimal("-29.769149208741522230595327812"))],
    )
    assert target.execute("select amount from rows_to_append").fetchone() == (
        Decimal("-29.769149208742"),
    )


def test_add_publishes_multiple_strategies_and_one_current_result_each(tmp_path: Path) -> None:
    request, snapshot = _request(tmp_path, names=("alpha", "beta"), orders=2)
    inbox_bytes = (request.inbox / "inbox_manifest.json").read_bytes()

    result = import_performance_v2(request)

    assert result.imported_count == 2
    assert result.skipped_count == 0
    assert result.rejected_count == 0
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (2,)
        assert connection.execute("select count(*) from strategy_orders").fetchone() == (4,)
        assert connection.execute("select count(*) from strategy_results").fetchone() == (2,)
        assert connection.execute("select count(*) from strategies where current_result_id is not null").fetchone() == (2,)
    assert (request.inbox / "inbox_manifest.json").read_bytes() == inbox_bytes
    assert snapshot


def test_import_reports_parse_progress_for_each_completed_report(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    events: list[tuple[str, int, int]] = []

    import_performance_v2(
        request,
        progress=lambda stage, completed, total: events.append((stage, completed, total)),
    )

    assert events[0] == ("PARSING", 0, 2)
    assert [completed for stage, completed, total in events if stage == "PARSING"] == [0, 1, 2]
    assert events[-1] == ("PUBLISHING", 2, 2)


def test_add_accepts_tester_report_order_ids_outside_mrs3_order_slots(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    report = FIXTURE.read_bytes().replace(b"<td>1</td><td>opened</td>", b"<td>2</td><td>opened</td>", 1)
    report = report.replace(b"<td>1</td><td>closed</td>", b"<td>2</td><td>closed</td>", 1)
    _rewrite_report(request, report)

    assert import_performance_v2(request).imported_count == 1


def test_identical_current_payload_is_skipped_and_changed_add_is_rejected(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    first = import_performance_v2(request)
    assert first.imported_count == 1

    second = import_performance_v2(request)
    assert second.imported_count == 0
    assert second.skipped_count == 1

    changed_strategy = json.loads((request.inbox / "strategies" / "alpha.json").read_text())
    changed_strategy["mrs3"]["ma_long"][0]["len"] = 99  # type: ignore[index]
    strategy_path = request.inbox / "strategies" / "alpha.json"
    strategy_bytes = json.dumps(changed_strategy, separators=(",", ":")).encode()
    strategy_path.write_bytes(strategy_bytes)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["source_strategy_sha256"] = sha256(strategy_bytes).hexdigest()
    manifest["entries"][0]["strategy_version_id"] = _canonical_strategy_hash(changed_strategy)
    manifest["v6_provenance"]["strategy_json_sha256"]["alpha.json"] = manifest["entries"][0]["strategy_version_id"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(PerformanceV2ImportError, match="existing"):
        import_performance_v2(request)


def test_replace_requires_mapping_and_rolls_back_on_typed_mismatch(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id = connection.execute("select strategy_id from strategies where strategy_name = 'alpha'").fetchone()[0]
        old_result = connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone()[0]

    with pytest.raises(PerformanceV2ImportError, match="mapping"):
        import_performance_v2(PerformanceV2ImportRequest(request.inbox, request.report_root, request.config, mode="REPLACE"))

    strategy_path = request.inbox / "strategies" / "alpha.json"
    changed = json.loads(strategy_path.read_text())
    changed["mrs3"]["ma_close_long"]["len"] = 999  # type: ignore[index]
    changed_bytes = json.dumps(changed, separators=(",", ":")).encode()
    strategy_path.write_bytes(changed_bytes)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["source_strategy_sha256"] = sha256(changed_bytes).hexdigest()
    manifest["entries"][0]["strategy_version_id"] = _canonical_strategy_hash(changed)
    manifest["v6_provenance"]["strategy_json_sha256"]["alpha.json"] = manifest["entries"][0]["strategy_version_id"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(PerformanceV2ImportError, match="typed"):
        import_performance_v2(
            PerformanceV2ImportRequest(request.inbox, request.report_root, request.config, mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id})
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (old_result,)


def test_replace_switches_current_result_and_keeps_one_result(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id = connection.execute("select strategy_id from strategies where strategy_name = 'alpha'").fetchone()[0]
        old_result = connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone()[0]
    changed = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    _rewrite_report(request, changed)

    result = import_performance_v2(
        PerformanceV2ImportRequest(request.inbox, request.report_root, request.config, mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id})
    )

    assert result.imported_count == 1
    with duckdb.connect(str(target), read_only=True) as connection:
        new_result = connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone()[0]
        assert new_result != old_result
        assert connection.execute("select count(*) from strategy_results where strategy_id = ?", [strategy_id]).fetchone() == (1,)
        assert connection.execute("select final_balance from strategy_results where strategy_id = ?", [strategy_id]).fetchone() == (Decimal("1019.9"),)
    assert (request.inbox / "strategies" / "alpha.json").read_bytes()
    assert (request.inbox / "inbox_manifest.json").read_bytes()


def test_replace_upgrades_pre_task5_window_schema_before_rebuilding_children(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id = connection.execute(
            "select strategy_id from strategies where strategy_name = 'alpha'"
        ).fetchone()[0]
        connection.execute("alter table window_metrics drop column holding_seconds")
        connection.execute("alter table window_metrics drop column time_in_market_pct")
    _rewrite_report(request, FIXTURE.read_bytes().replace(b"1009.9", b"1019.9"))

    result = import_performance_v2(
        PerformanceV2ImportRequest(
            request.inbox,
            request.report_root,
            request.config,
            mode="REPLACE",
            replacement_strategy_ids={"alpha": strategy_id},
        )
    )

    assert result.imported_count == 1
    with duckdb.connect(str(target), read_only=True) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "select column_name from information_schema.columns where table_name = 'window_metrics'"
            ).fetchall()
        }
        assert {"holding_seconds", "time_in_market_pct"} <= columns


def test_replace_rollback_restores_old_result_after_delete_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id, old_result = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
        old_actions = connection.execute("select count(*) from strategy_actions where result_id = ?", [old_result]).fetchone()[0]
    _rewrite_report(request, FIXTURE.read_bytes().replace(b"1009.9", b"1019.9"))
    original_inbox = {path.relative_to(request.inbox): path.read_bytes() for path in request.inbox.rglob("*") if path.is_file()}
    import mrs3.performance_v2_import as import_module
    monkeypatch.setattr(import_module, "_after_delete_before_insert", lambda *args: (_ for _ in ()).throw(RuntimeError("injected delete failure")))

    with pytest.raises(PerformanceV2ImportError, match="transaction failed|injected"):
        import_performance_v2(
            PerformanceV2ImportRequest(request.inbox, request.report_root, request.config, mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id})
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (old_result,)
        assert connection.execute("select count(*) from strategy_actions where result_id = ?", [old_result]).fetchone() == (old_actions,)
    assert {path.relative_to(request.inbox): path.read_bytes() for path in request.inbox.rglob("*") if path.is_file()} == original_inbox


def test_lock_conflict_does_not_read_inbox_or_create_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = _db(request)
    held = duckdb.connect(str(target))
    import mrs3.performance_v2_import as import_module
    monkeypatch.setattr(import_module.duckdb, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(duckdb.IOException("lock")))
    monkeypatch.setattr(import_module, "read_performance_v2_inbox", lambda *args, **kwargs: pytest.fail("inbox was read while locked"))
    try:
        with pytest.raises(PerformanceV2LockedError):
            import_performance_v2(request)
    finally:
        held.close()
    assert not (request.config.database_root / ".staging").exists()


def test_missing_target_fails_before_connect_and_does_not_create_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    import mrs3.performance_v2_import as import_module
    monkeypatch.setattr(import_module.duckdb, "connect", lambda *args, **kwargs: pytest.fail("connect called"))

    with pytest.raises(PerformanceV2ImportError, match="target does not exist"):
        import_performance_v2(request)

    target = performance_v2_database_path(request.config)
    assert not target.exists()
    assert not request.config.database_root.exists()
    assert not (request.config.database_root / "import_audit.v2.json").exists()


def test_empty_target_fails_schema_gate_without_staging_or_audit(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True)
    target.touch()

    with pytest.raises(PerformanceV2ImportError, match="schema version 2"):
        import_performance_v2(request)

    assert target.exists()
    assert not (request.config.database_root / ".staging").exists()
    assert not (request.config.database_root / "import_audit.v2.json").exists()


def test_invalid_schema_fails_before_staging_or_audit(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True)
    with duckdb.connect(str(target)) as connection:
        connection.execute("create table not_v2 (value integer)")

    with pytest.raises(PerformanceV2ImportError, match="schema version 2"):
        import_performance_v2(request)

    assert not (request.config.database_root / ".staging").exists()
    assert not (request.config.database_root / "import_audit.v2.json").exists()


def test_absent_target_is_not_reported_as_lock_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    import mrs3.performance_v2_import as import_module
    monkeypatch.setattr(
        import_module.duckdb,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(duckdb.IOException("lock")),
    )

    with pytest.raises(PerformanceV2ImportError, match="target does not exist") as error:
        import_performance_v2(request)

    assert not isinstance(error.value, PerformanceV2LockedError)
    assert not request.config.database_root.exists()
