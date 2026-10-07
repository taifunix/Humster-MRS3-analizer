from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import csv
import json
from http.client import HTTPConnection
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import threading

import duckdb
import pytest
from openpyxl import Workbook, load_workbook
import mrs3.performance_v2_import as import_module

from mrs3.performance_v2_import import (
    PerformanceV2ImportError,
    PerformanceV2ImportRequest,
    PerformanceV2LockedError,
    import_performance_v2,
)
from mrs3.panel import PanelController, create_panel_server
from mrs3.performance_v2_html import parse_current_performance_v2_html
from mrs3.performance_v2_input import PerformanceV2InputError, read_performance_v2_inbox
from mrs3.performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    PerformanceV2WriterLock,
    _SCHEMA,
    _SELECTION_SCHEMA,
    decode_optimizer_source_metadata,
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
        strategy = _strategy(name, orders=orders, close_ma=3 + index - 1)
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
    commission_contract = {
        "MakerFee": "0.0002",
        "TakerFee": "0.0004",
        "SlippagePercent": "0.01",
        "FundingRate": "0.0001",
        "FundingIntervalHours": "8",
    }
    manifest = {
        "schema_version": 1,
        "batch_id": "v2-test",
        "expected_strategy_names": list(names),
        "tester_config_sha256": "t" * 64,
        "commission_contract": commission_contract,
        "commission_contract_id": sha256(json.dumps(commission_contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
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
    dates_path = tmp_path / "Input" / "dates.xlsx"
    dates_path.parent.mkdir()
    workbook = Workbook()
    workbook.active.append(["ONUSDT", datetime(2025, 12, 25)])
    workbook.save(dates_path)
    request = PerformanceV2ImportRequest(
        inbox, report_root, config, mode=mode, listing_dates_path=Path("Input/dates.xlsx")
    )
    if initialize_db:
        _db(request)
    return request, snapshot


def _db(request: PerformanceV2ImportRequest) -> Path:
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True, exist_ok=True)
    repository_root = Path(__file__).resolve().parents[1]
    known_production_targets = {
        (repository_root / "data" / "performanceDB" / "strategy_performance.duckdb").resolve(),
        (repository_root / "data" / "performance-v2" / "strategy_performance.duckdb").resolve(),
    }
    assert target.is_relative_to(request.config.database_root.resolve()), "writable importer target must be beneath its fixture root"
    assert target.resolve() not in known_production_targets, "known production PerformanceDB paths are forbidden"
    assert not target.resolve().is_relative_to(repository_root), "repository databases are read-only"
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


def _single_mode_manifest(request: PerformanceV2ImportRequest, *, omit_commission: bool) -> None:
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_mode"] = "SINGLE_MODE"
    manifest["test_start"] = "2026-01-01"
    manifest["test_end"] = "2026-01-09"
    if omit_commission:
        manifest.pop("commission_contract")
        manifest.pop("commission_contract_id")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_single_mode_add_and_replace_store_unknown_commission_as_sql_null(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    _single_mode_manifest(request, omit_commission=True)

    added = import_performance_v2(request)
    assert added.imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id, result_id, rate, fees, pnl = connection.execute(
            """select s.strategy_id, r.result_id, r.commission_rate, r.total_fees, r.total_pnl
               from strategies s join strategy_results r on r.strategy_id = s.strategy_id"""
        ).fetchone()
        assert rate is None
        assert fees == Decimal("0.1")
        assert pnl == Decimal("9.9")
        before_actions = connection.execute(
            "select action_index, fee from strategy_actions where result_id = ? order by action_index", [result_id]
        ).fetchall()
    changed = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    _rewrite_report(request, changed)
    replaced = import_performance_v2(
        PerformanceV2ImportRequest(
            request.inbox, request.report_root, request.config,
            mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
            listing_dates_path=request.listing_dates_path,
        )
    )
    assert replaced.imported_count == 1
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select commission_rate from strategy_results where result_id = ?", [result_id]
        ).fetchone() == (None,)
        assert connection.execute(
            "select action_index, fee from strategy_actions where result_id = ? order by action_index", [result_id]
        ).fetchall() == before_actions


@pytest.mark.parametrize("mode", ["ADD", "REPLACE"])
def test_optional_commission_preserves_persisted_financial_facts(tmp_path: Path, mode: str) -> None:
    snapshots = []
    for label, omit_commission in (("known", False), ("unknown", True)):
        request, _ = _request(tmp_path / label)
        _single_mode_manifest(request, omit_commission=False)
        if mode == "REPLACE":
            assert import_performance_v2(request).imported_count == 1
            with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
                strategy_id = connection.execute("select strategy_id from strategies where strategy_name = 'alpha'").fetchone()[0]
            _rewrite_report(request, FIXTURE.read_bytes().replace(b"1009.9", b"1019.9"))
        if omit_commission:
            _single_mode_manifest(request, omit_commission=True)
        if mode == "REPLACE":
            request = replace(request, mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id})
        assert import_performance_v2(request).imported_count == 1
        with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
            rate, *financial = connection.execute(
                """select commission_rate, initial_balance, final_balance, total_pnl,
                          total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees,
                          total_trades from strategy_results"""
            ).fetchone()
            actions = connection.execute(
                """select action_index, timestamp_utc, symbol, order_id, action, size,
                          post_size, post_side, pnl, fee, balance
                     from strategy_actions order by action_index"""
            ).fetchall()
            equity = connection.execute(
                "select sample_index, timestamp_utc, wallet, equity from strategy_equity order by sample_index"
            ).fetchall()
        snapshots.append((rate, financial, actions, equity))
    known, unknown = snapshots
    assert known[0] == Decimal("0.0004")
    assert unknown[0] is None
    assert known[1:] == unknown[1:]


def test_import_phase_evidence_is_bounded_and_keeps_audit_schema(tmp_path: Path, monkeypatch) -> None:
    request, _ = _request(tmp_path / "timed")
    clock_reads = 0

    def clock() -> float:
        nonlocal clock_reads
        clock_reads += 1
        return clock_reads / 1000

    monkeypatch.setattr(import_module, "perf_counter", clock)

    result = import_performance_v2(request)

    assert result.phases
    assert clock_reads <= 30
    assert len(result.phases) <= 15
    assert all(value == round(value, 6) for value in result.phases.values())
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["schema_version"] == 2
    assert audit["imported_count"] == result.imported_count
    assert "phases" in audit
    assert "AUDIT_WRITE" not in audit["phases"]
    assert "WRITER_LOCK_RELEASE" not in audit["phases"]
    monkeypatch.setattr(import_module, "perf_counter", lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")))
    untimed_request, _ = _request(tmp_path / "untimed")
    untimed = import_performance_v2(untimed_request)
    old_audit = json.loads(untimed.audit_path.read_text(encoding="utf-8"))
    assert set(audit) == set(old_audit) | {"phases"}
    assert {key: type(value) for key, value in audit.items() if key != "phases"} == {
        key: type(value) for key, value in old_audit.items()
    }


def test_import_timing_clock_failure_keeps_publication_behavior(tmp_path: Path, monkeypatch) -> None:
    request, _ = _request(tmp_path)

    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(import_module, "perf_counter", broken_clock)
    result = import_performance_v2(request)

    assert result.status == "COMMITTED"
    assert result.imported_count == 1
    assert result.phases == {}
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert "phases" not in audit


def test_import_add_does_not_create_optimizer_prepared_inputs(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)

    assert import_performance_v2(request).imported_count == 1

    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (0,)


def test_import_rejects_manifest_mutation_before_reader_without_db_mutation(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    manifest_path = request.inbox / "inbox_manifest.json"
    expected_digest = sha256(manifest_path.read_bytes()).hexdigest()
    request = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode=request.mode,
        listing_dates_path=request.listing_dates_path,
        expected_inbox_manifest_sha256=expected_digest,
    )
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises(PerformanceV2ImportError, match="manifest digest"):
        import_performance_v2(request)

    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (0,)


def _swap_current_action_rows(source: bytes) -> bytes:
    opened = (
        b"<tr><td>2026-01-01T01:00:00Z</td><td>ONUSDT</td><td>1</td>"
        b"<td>opened</td><td>0.05</td><td>0</td><td>999.95</td>"
        b"<td>1</td><td>1</td><td>long</td></tr>"
    )
    closed = (
        b"<tr><td>2026-01-03T01:00:00+00:00</td><td>ONUSDT</td><td>1</td>"
        b"<td>closed</td><td>0.05</td><td>9.9</td><td>1009.9</td>"
        b"<td>1</td><td>0</td><td></td></tr>"
    )
    marker = b"<tr><td>__SWAPPED_ACTION_ROW__</td></tr>"
    assert source.count(opened) == source.count(closed) == 1
    return source.replace(opened, marker, 1).replace(closed, opened, 1).replace(marker, closed, 1)


def _report_with_source_metadata() -> bytes:
    source = FIXTURE.read_bytes()
    source = source.replace(
        b'"use_upnl":true}',
        b'"use_upnl":true,"use_frozen_balance":true,"account":"must-not-save"}',
        1,
    ).replace(
        b'"use_short":false}',
        b'"use_short":false,"max_balance":200,"risk_long":1.5}',
        1,
    )
    for old, new in (
        (b"<th>Action</th><th>Fee</th>", b"<th>Action</th><th>Price</th><th>Cost</th><th>Fee</th>"),
        (b"<td>opened</td><td>0.05</td>", b"<td>opened</td><td>1.2300</td><td>4.5600</td><td>0.05</td>"),
        (b"<td>closed</td><td>0.05</td>", b"<td>closed</td><td>1.2300</td><td>4.5600</td><td>0.05</td>"),
    ):
        source = source.replace(old, new, 1)
    return source


def test_append_rows_uses_duckdb_native_dataframe_append() -> None:
    class AppendOnlyConnection:
        def __init__(self) -> None:
            self.connection = duckdb.connect(":memory:")
            self.connection.execute("create table rows_to_append (id integer, amount decimal(38, 12))")

        def append(self, table: str, frame) -> None:
            self.connection.append(table, frame)

        def execute(self, *args: object):
            return self.connection.execute(*args)

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
        def __init__(self) -> None:
            self.frame = None
            self.connection = duckdb.connect(":memory:")
            self.connection.execute("create table rows_to_append (id integer, amount decimal(38, 12))")

        def append(self, _table: str, frame) -> None:
            self.frame = frame

        def execute(self, *args: object):
            return self.connection.execute(*args)

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


def test_append_rows_uses_main_catalog_when_attached_catalog_repeats_table_name() -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("create table rows_to_append (id integer, amount decimal(38, 12))")
        connection.execute("attach ':memory:' as attached")
        connection.execute("create table attached.rows_to_append (id integer, unrelated integer)")

        import_module._append_rows(
            connection, "rows_to_append", ("id", "amount"), [(1, Decimal("1.25"))]
        )

        assert connection.execute("select * from rows_to_append").fetchall() == [
            (1, Decimal("1.250000000000")),
        ]
    finally:
        connection.close()


def test_append_rows_caches_validated_schema_only_within_supplied_cache() -> None:
    raw = duckdb.connect(":memory:")
    raw.execute("create table rows_to_append (id integer, amount decimal(38, 12))")
    metadata_calls: list[str] = []

    class CountingConnection:
        def execute(self, sql: str, parameters=None):
            if "information_schema.columns" in sql:
                metadata_calls.append(sql)
            return raw.execute(sql, parameters) if parameters is not None else raw.execute(sql)

        def append(self, table: str, frame) -> None:
            raw.append(table, frame)

    connection = CountingConnection()
    cache: dict[str, tuple[str, ...]] = {}
    rows = [(1, Decimal("1.25"))]
    import_module._append_rows(
        connection, "rows_to_append", ("id", "amount"), rows, schema_cache=cache
    )
    import_module._append_rows(
        connection, "rows_to_append", ("id", "amount"), [(2, Decimal("2.50"))], schema_cache=cache
    )
    assert len(metadata_calls) == 1
    assert cache == {"rows_to_append": ("id", "amount")}

    import_module._append_rows(
        connection, "rows_to_append", ("id", "amount"), [(3, Decimal("3.75"))], schema_cache={}
    )
    import_module._append_rows(connection, "rows_to_append", ("id", "amount"), [])
    import_module._append_rows(
        connection, "rows_to_append", ("id", "amount"), [(4, Decimal("4.00"))]
    )
    assert len(metadata_calls) == 3
    import_module._append_rows(connection, "rows_to_append", ("id", "amount"), [])
    assert len(metadata_calls) == 3

    with pytest.raises(PerformanceV2ImportError, match="append columns do not match"):
        import_module._append_rows(
            connection, "rows_to_append", ("wrong",), [(4,)], schema_cache={}
        )

    cache = {"rows_to_append": ("wrong", "amount")}
    with pytest.raises(PerformanceV2ImportError, match="append columns do not match"):
        import_module._append_rows(
            connection, "rows_to_append", ("id", "amount"), [(4, Decimal("4.00"))], schema_cache=cache
        )
    assert cache == {"rows_to_append": ("wrong", "amount")}

    raw.execute("create table invalid_rows (id integer, extra integer)")
    invalid_cache: dict[str, tuple[str, ...]] = {}
    with pytest.raises(PerformanceV2ImportError, match="append columns do not match"):
        import_module._append_rows(
            connection, "invalid_rows", ("id", "amount"), [(4, Decimal("4.00"))],
            schema_cache=invalid_cache,
        )
    assert invalid_cache == {}
    raw.close()


def test_replace_timestamp_updates_use_scalar_and_bounded_batch_shapes() -> None:
    raw = duckdb.connect(":memory:")
    raw.execute("create table strategies (strategy_id bigint, updated_at_utc timestamptz)")
    raw.executemany("insert into strategies values (?, null)", [(strategy_id,) for strategy_id in range(1, 2049)])
    statements: list[tuple[str, object]] = []

    class RecordingConnection:
        def execute(self, sql: str, parameters=None):
            if "update strategies set updated_at_utc" in sql.casefold():
                statements.append((sql, parameters))
            return raw.execute(sql, parameters) if parameters is not None else raw.execute(sql)

    connection = RecordingConnection()
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    import_module._update_replacement_timestamps(connection, [], now)
    assert statements == []

    import_module._update_replacement_timestamps(connection, [7], now)
    assert statements[0] == (
        "update strategies set updated_at_utc = ? where strategy_id = ?",
        [now, 7],
    )

    exact_ids = list(range(1, 1025))
    import_module._update_replacement_timestamps(connection, exact_ids, now)
    assert len(statements) == 2
    assert "?::bigint[]" in statements[1][0].casefold()
    assert "unnest" in statements[1][0].casefold()
    assert statements[1][1] == [now, exact_ids]

    ids = [1000, *range(1, 1026), 1000]
    import_module._update_replacement_timestamps(connection, ids, now)
    batch = statements[2:]
    assert len(batch) == 2
    assert all("?::bigint[]" in sql.casefold() and "unnest" in sql.casefold() for sql, _parameters in batch)
    assert [parameters[1] for _sql, parameters in batch] == [
        list(dict.fromkeys(ids))[:1024],
        [1025],
    ]
    assert raw.execute("select count(*) from strategies where updated_at_utc = ?", [now]).fetchone() == (1025,)
    raw.close()


def test_child_readback_uses_two_grouped_queries_and_zero_fills_missing_groups() -> None:
    raw = duckdb.connect(":memory:")
    try:
        raw.execute("create table strategy_actions (result_id integer)")
        raw.execute("create table strategy_equity (result_id integer)")
        raw.execute("insert into strategy_actions values (10)")
        raw.execute("insert into strategy_equity values (10)")
        grouped: list[str] = []

        class CountingConnection:
            def execute(self, sql: str, parameters=None):
                if "group by result_id" in sql.casefold():
                    grouped.append(sql)
                return raw.execute(sql, parameters) if parameters is not None else raw.execute(sql)

        with pytest.raises(PerformanceV2ImportError, match=r"20: expected actions=2 actual=0") as error:
            import_module._verify_child_counts(
                CountingConnection(), ((10, 1, 1), (20, 2, 1))
            )
        assert "20: expected actions=2 actual=0 equity=1 actual=0" in str(error.value)
        assert len(grouped) == 2
    finally:
        raw.close()


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


def test_equity_batch_flush_uses_schema_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path)
    monkeypatch.setattr(import_module, "_APPEND_BATCH_ROWS", 1)

    result = import_performance_v2(request)

    assert result.imported_count == 1
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        result_id = connection.execute("select current_result_id from strategies").fetchone()[0]
        action_count, equity_count = connection.execute(
            """select
                   (select count(*) from strategy_actions where result_id = ?),
                   (select count(*) from strategy_equity where result_id = ?)""",
            [result_id, result_id],
        ).fetchone()
        assert action_count > 0
        assert equity_count > 0


@pytest.mark.parametrize(
    ("batch_size", "expected_actions", "expected_equity"),
    [
        (1, [1, 1], [1, 1, 1]),
        (2, [2], [2, 1]),
        (3, [2], [3]),
        (4, [2], [3]),
    ],
)
def test_writer_batches_flush_at_cap_without_empty_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_size: int,
    expected_actions: list[int],
    expected_equity: list[int],
) -> None:
    request, _ = _request(tmp_path)
    monkeypatch.setattr(import_module, "_APPEND_BATCH_ROWS", batch_size)
    calls: list[tuple[str, int]] = []
    append_rows = import_module._append_rows

    def append_spy(*args: object, **kwargs: object) -> None:
        calls.append((str(args[1]), len(args[3])))
        append_rows(*args, **kwargs)

    monkeypatch.setattr(import_module, "_append_rows", append_spy)
    assert import_performance_v2(request).imported_count == 1
    assert calls
    assert all(0 < size <= batch_size for _table, size in calls)
    assert [size for table, size in calls if table == "strategy_actions"] == expected_actions
    assert [size for table, size in calls if table == "strategy_equity"] == expected_equity


def test_publish_requeries_action_and_equity_schema_for_each_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path)
    monkeypatch.setattr(import_module, "_APPEND_BATCH_ROWS", 1)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        listing_date_utc=datetime(2025, 12, 25, tzinfo=timezone.utc),
        reported_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reported_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        effective_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        warmup_hours=120,
    )
    target = performance_v2_database_path(request.config)
    raw = duckdb.connect(str(target))
    metadata_tables: list[str] = []

    class RecordingConnection:
        def execute(self, sql, parameters=None):
            if "table_catalog = current_database()" in str(sql).casefold():
                metadata_tables.append(str(parameters[0]))
            return raw.execute(sql, parameters) if parameters is not None else raw.execute(sql)

        def executemany(self, sql, parameters):
            return raw.executemany(sql, parameters)

        def append(self, table, frame):
            return raw.append(table, frame)

    recording = RecordingConnection()
    try:
        assert import_module._publish(recording, request, prepared, (parsed,), "first-publication") == (1, 0, 0)
        strategy_id = raw.execute(
            "select strategy_id from strategies where strategy_name = 'alpha'"
        ).fetchone()[0]
        replacement = replace(
            request,
            mode="REPLACE",
            replacement_strategy_ids={"alpha": strategy_id},
        )
        assert import_module._publish(
            recording, replacement, prepared, (parsed,), "second-publication"
        ) == (1, 0, 0)
    finally:
        raw.close()

    assert metadata_tables == [
        "strategy_actions", "strategy_equity", "strategy_actions", "strategy_equity"
    ]


def test_split_writer_frames_preserve_full_database_rows_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module, "_utc_now", lambda: fixed_now)
    snapshots = []
    for cap in (20_000, 2, 1):
        request, _ = _request(tmp_path / f"cap-{cap}")
        prepared = read_performance_v2_inbox(request.inbox, request.report_root)
        parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
        parsed = replace(
            parsed,
            listing_date_utc=datetime(2025, 12, 25, tzinfo=timezone.utc),
            reported_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reported_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
            effective_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
            warmup_hours=120,
            actions=(
                parsed.actions[0],
                replace(
                    parsed.actions[1],
                    price=Decimal("-29.769149208742"),
                    cost=Decimal("123456789.123456789012"),
                ),
            ),
        )
        monkeypatch.setattr(import_module, "_APPEND_BATCH_ROWS", cap)
        with duckdb.connect(str(performance_v2_database_path(request.config))) as connection:
            assert import_module._publish(connection, request, prepared, (parsed,), "parity") == (1, 0, 0)
            snapshots.append(tuple(
                connection.execute(query).fetchall()
                for query in (
                    "select * from strategy_actions order by result_id, action_index",
                    "select * from strategy_equity order by result_id, sample_index",
                    "select source_digest, prepared_json from optimizer_prepared_inputs order by result_id",
                    "select source_filename, source_html_sha256, source_size_bytes, action_count, equity_sample_count, status from import_files order by source_filename",
                )
            ))
    assert snapshots[0] == snapshots[1] == snapshots[2]
    actions, equity, prepared_rows, files = snapshots[0]
    assert len(actions) == 2 and len(equity) == 3 and not prepared_rows and len(files) == 1
    assert actions[0][12:14] == (None, None)
    assert actions[1][12:14] == (
        Decimal("-29.769149208742"), Decimal("123456789.123456789012")
    )


def test_import_persists_allowlisted_source_metadata_and_existing_action_slot(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    _rewrite_report(request, _report_with_source_metadata())

    assert import_performance_v2(request).imported_count == 1

    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        result_id, imported_at, metadata = connection.execute(
            "select result_id, imported_at_utc, optimizer_source_metadata_json from strategy_results"
        ).fetchone()
        typed_price, typed_cost, typed_upnl, typed_frozen, typed_risk, typed_max = connection.execute(
            """select a.price, a.cost, r.sizing_use_upnl, r.sizing_use_frozen_balance,
                      r.sizing_risk_long, r.sizing_max_balance
                 from strategy_actions a join strategy_results r on r.result_id = a.result_id
                where a.result_id = ? order by a.action_index limit 1""",
            [result_id],
        ).fetchone()
        columns = [row[0] for row in connection.execute(
            "select column_name from information_schema.columns where table_name = 'strategy_actions' order by ordinal_position"
        ).fetchall()]
        action_payload = connection.execute(
            "select raw_action_json from strategy_actions where result_id = ? order by action_index limit 1", [result_id]
        ).fetchone()[0]

    assert columns == [
        "result_id", "action_index", "timestamp_utc", "symbol", "order_id", "action", "size",
        "post_size", "post_side", "pnl", "fee", "balance", "price", "cost", "raw_action_json",
    ]
    assert json.loads(action_payload) == {
        "cost": "4.5600", "price": "1.2300", "price_cost_semantics": "actual_fill_not_planned_position",
        "schema_version": 1,
    }
    assert (typed_price, typed_cost) == (Decimal("1.2300"), Decimal("4.5600"))
    assert (typed_upnl, typed_frozen, typed_risk, typed_max) == (
        True, True, Decimal("1.500000000000"), Decimal("200.000000000000")
    )
    decoded = decode_optimizer_source_metadata(metadata, imported_at)
    assert decoded is not None
    assert decoded["settings"] == {
        "basic": {"max_balance": "200", "risk_long": "1.5"},
        "exchange": {"use_frozen_balance": True, "use_upnl": True},
    }
    assert "must-not-save" not in metadata


def test_import_persists_typed_facts_without_preparing_optimizer_input(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)

    assert import_performance_v2(request).imported_count == 1
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        result_id, action_price, action_cost, sizing = connection.execute(
            """select r.result_id, a.price, a.cost, r.sizing_use_upnl
               from strategy_results r join strategy_actions a on a.result_id = r.result_id
               order by a.action_index limit 1"""
        ).fetchone()
        prepared_count = connection.execute(
            "select count(*) from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]

    assert action_price is None and action_cost is None
    assert sizing is True
    assert prepared_count == 0


def test_over_scale_price_keeps_exact_raw_provenance_when_typed_value_is_null(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    report = _report_with_source_metadata()
    report = report.replace(b"1.2300", b"1.2345678901234", 1).replace(b"4.5600", b"4.5678901234567", 1)
    _rewrite_report(request, report)

    assert import_performance_v2(request).imported_count == 1
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        result_id, typed_price, typed_cost, raw_action = connection.execute(
            """select result_id, price, cost, raw_action_json
                 from strategy_actions order by action_index limit 1"""
        ).fetchone()
        prepared_count = connection.execute(
            "select count(*) from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]

    assert typed_price is None
    assert typed_cost is None
    assert json.loads(raw_action)["price"] == "1.2345678901234"
    assert json.loads(raw_action)["cost"] == "4.5678901234567"
    assert prepared_count == 0


def test_source_metadata_keeps_invalid_field_evidence_with_legacy_exchange_setting() -> None:
    metadata = import_module._optimizer_source_metadata_json(
        {"exchange": {"use_upnl": True}, "basic": {"use_fix": "invalid"}},
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "a" * 64,
    )

    assert metadata is not None
    assert json.loads(metadata)["invalid_fields"] == ["basic.use_fix"]


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


def test_import_rejects_swapped_html_actions_and_admits_valid_sibling(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    swapped = _swap_current_action_rows(FIXTURE.read_bytes()).replace(
        b'[1767402000000,"1009.9"]', b'[1767312000000,"999.95"]'
    )
    alpha_path = request.report_root / "alpha.html"
    alpha_path.write_bytes(swapped)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["source_report_sha256"] = sha256(swapped).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    result = import_performance_v2(request)

    assert result.status == "COMMITTED"
    assert result.imported_count == 1
    assert result.rejected_count == 1
    assert any(
        failure["strategy_name"] == "alpha" and failure["reason"] == "ACTIONS_OUT_OF_ORDER"
        for failure in result.failures
    )
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select strategy_name from strategies order by strategy_name").fetchall() == [("beta",)]
        assert connection.execute("select count(*) from strategy_actions").fetchone() == (2,)


def test_replace_preserves_user_finalist_status_rank_and_comment(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    _rewrite_report(request, _report_with_source_metadata())
    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        connection.execute(
            """insert into selection_runs values
               ('selection-1', '00000000-0000-0000-0000-000000000001', 'ONUSDT', 'LONG', 'test',
                '{}', 'a', '{}', 'b', 1, 1, 1, 1, 'c', ?)""",
            [now],
        )
        connection.execute(
            "insert into selection_review_imports values ('review-1', 'selection-1', 'd', ?, 1)", [now]
        )
        connection.execute(
            "insert into selection_review_rows values ('review-1', ?, 'FINALIST', 1, null, 'keep this')",
            [strategy_id],
        )
    changed = _report_with_source_metadata().replace(b"1009.9", b"1019.9")
    _rewrite_report(request, changed)
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids={"alpha": strategy_id}, expected_current_result_ids={"alpha": result_id},
        listing_dates_path=request.listing_dates_path,
    )

    assert import_performance_v2(replacement).imported_count == 1

    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select user_status, user_rank, comment from selection_review_rows where strategy_id = ?", [strategy_id]
        ).fetchone() == ("FINALIST", 1, "keep this")
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_id]
        ).fetchone() == (result_id,)

        first_result = result_id
        first_actions = connection.execute(
            "select count(*) from strategy_actions where result_id = ? and raw_action_json is not null", [result_id]
        ).fetchone()[0]
        assert first_actions > 0
        assert connection.execute(
            "select optimizer_source_metadata_json is not null from strategy_results where result_id = ?", [result_id]
        ).fetchone() == (True,)

    _rewrite_report(request, FIXTURE.read_bytes())
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids={"alpha": strategy_id}, expected_current_result_ids={"alpha": first_result},
        listing_dates_path=request.listing_dates_path,
    )
    assert import_performance_v2(replacement).imported_count == 1

    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_id]
        ).fetchone() == (first_result,)
        assert connection.execute(
            "select user_status, user_rank, comment from selection_review_rows where strategy_id = ?", [strategy_id]
        ).fetchone() == ("FINALIST", 1, "keep this")
        assert connection.execute(
            "select raw_action_json from strategy_actions where result_id = ? order by action_index", [first_result]
        ).fetchall() == [(None,), (None,)]
        assert connection.execute(
            "select optimizer_source_metadata_json from strategy_results where result_id = ?", [first_result]
        ).fetchone() == (None,)


def _change_all_inbox_reports(request: PerformanceV2ImportRequest) -> None:
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    assert changed != FIXTURE.read_bytes()
    for entry in manifest["entries"]:
        report_path = Path(entry["report_path"])
        report_path.write_bytes(changed)
        entry["source_report_sha256"] = sha256(changed).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _current_strategy_map(request: PerformanceV2ImportRequest) -> tuple[dict[str, int], dict[str, int]]:
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        rows = connection.execute(
            "select strategy_name, strategy_id, current_result_id from strategies"
        ).fetchall()
    return (
        {str(name): int(strategy_id) for name, strategy_id, _result_id in rows},
        {str(name): int(result_id) for name, _strategy_id, result_id in rows},
    )


def _current_final_balances(request: PerformanceV2ImportRequest) -> dict[str, float]:
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        rows = connection.execute(
            "select s.strategy_name, r.final_balance from strategies s "
            "join strategy_results r on r.result_id = s.current_result_id"
        ).fetchall()
    return {str(name): float(balance) for name, balance in rows}


def test_replace_rejects_parsed_entry_missing_expected_result_id(tmp_path: Path) -> None:
    initial, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(initial).imported_count == 2
    strategy_ids, expected = _current_strategy_map(initial)
    before_balances = _current_final_balances(initial)
    replacement = PerformanceV2ImportRequest(
        initial.inbox, initial.report_root, initial.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids, expected_current_result_ids=expected,
        listing_dates_path=initial.listing_dates_path,
    )
    # The request constructor enforces complete coverage.  Simulate corrupt
    # persisted/internal state to verify the importer itself also fails closed.
    object.__setattr__(replacement, "expected_current_result_ids", {"beta": expected["beta"]})
    _change_all_inbox_reports(replacement)

    result = import_performance_v2(replacement)

    assert result.imported_count == 1
    assert result.rejected_count == 1
    assert any(
        failure["strategy_id"] == strategy_ids["alpha"]
        and failure["strategy_name"] == "alpha"
        and failure["reason"] == "MISSING_EXPECTED_RESULT"
        for failure in result.failures
    )
    target = performance_v2_database_path(replacement.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_ids["alpha"]]
        ).fetchone() == (expected["alpha"],)
        assert connection.execute(
            "select count(*) from strategy_results where strategy_id = ?", [strategy_ids["alpha"]]
        ).fetchone() == (1,)
        after_balances = dict(connection.execute(
            "select s.strategy_name, r.final_balance from strategies s "
            "join strategy_results r on r.result_id = s.current_result_id"
        ).fetchall())
        assert float(after_balances["alpha"]) == before_balances["alpha"]
        assert float(after_balances["beta"]) != before_balances["beta"]


def test_replace_stale_guard_resolves_current_result_by_frozen_strategy_id(tmp_path: Path) -> None:
    initial, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(initial).imported_count == 2
    strategy_ids, expected = _current_strategy_map(initial)
    before_balances = _current_final_balances(initial)
    # A stale name-to-ID mapping must not borrow alpha's current Result ID
    # from a name lookup.  The wrong frozen ID points at beta, so only alpha
    # is isolated as stale and beta remains eligible to replace.
    replacement_ids = {"alpha": strategy_ids["beta"], "beta": strategy_ids["beta"]}
    replacement = PerformanceV2ImportRequest(
        initial.inbox, initial.report_root, initial.config, mode="REPLACE",
        replacement_strategy_ids=replacement_ids, expected_current_result_ids=expected,
        listing_dates_path=initial.listing_dates_path,
    )
    _change_all_inbox_reports(replacement)

    result = import_performance_v2(replacement)

    assert result.imported_count == 1
    assert result.rejected_count == 1
    assert any(
        failure["strategy_id"] == replacement_ids["alpha"]
        and failure["strategy_name"] == "alpha"
        and failure["reason"] == "STALE_RESULT"
        for failure in result.failures
    )
    target = performance_v2_database_path(replacement.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_ids["alpha"]]
        ).fetchone() == (expected["alpha"],)
        after_balances = dict(connection.execute(
            "select s.strategy_name, r.final_balance from strategies s "
            "join strategy_results r on r.result_id = s.current_result_id"
        ).fetchall())
        assert float(after_balances["alpha"]) == before_balances["alpha"]
        assert float(after_balances["beta"]) != before_balances["beta"]


def test_replace_missing_current_guard_row_fails_only_that_strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initial, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(initial).imported_count == 2
    strategy_ids, expected = _current_strategy_map(initial)
    before_balances = _current_final_balances(initial)
    replacement = PerformanceV2ImportRequest(
        initial.inbox, initial.report_root, initial.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids, expected_current_result_ids=expected,
        listing_dates_path=initial.listing_dates_path,
    )
    _change_all_inbox_reports(replacement)
    target = performance_v2_database_path(replacement.config)
    real_connect = import_module.duckdb.connect
    guard_queries = 0

    class ResultRows:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self.rows[0] if self.rows else None

    class GuardConnection:
        def __init__(self, raw) -> None:
            self.raw = raw

        def execute(self, sql, parameters=None):
            nonlocal guard_queries
            compact = " ".join(str(sql).casefold().split())
            if guard_queries == 0 and "select strategy_id" in compact and "unnest" in compact and "from strategies" in compact:
                guard_queries += 1
                rows = self.raw.execute(sql, parameters).fetchall()
                return ResultRows([row for row in rows if int(row[0]) != strategy_ids["alpha"]])
            return self.raw.execute(sql, parameters) if parameters is not None else self.raw.execute(sql)

        def executemany(self, sql, parameters):
            return self.raw.executemany(sql, parameters)

        def append(self, table, frame):
            return self.raw.append(table, frame)

        def close(self) -> None:
            self.raw.close()

        def __getattr__(self, name: str):
            return getattr(self.raw, name)

    monkeypatch.setattr(
        import_module.duckdb, "connect",
        lambda *args, **kwargs: GuardConnection(real_connect(*args, **kwargs)),
    )

    result = import_performance_v2(replacement)

    assert guard_queries == 1
    assert (result.imported_count, result.rejected_count) == (1, 1)
    assert result.imported_count + result.rejected_count == len(strategy_ids)
    assert result.successful_replacements == ({
        "strategy_id": strategy_ids["beta"],
        "old_result_id": expected["beta"],
        "new_result_id": expected["beta"],
    },)
    assert result.failures == ({
        "strategy_id": strategy_ids["alpha"],
        "strategy_name": "alpha",
        "symbol": "ONUSDT",
        "reason": "MISSING_CURRENT_RESULT",
    },)
    with real_connect(str(target), read_only=True) as connection:
        after_balances = dict(connection.execute(
            "select s.strategy_name, r.final_balance from strategies s "
            "join strategy_results r on r.result_id = s.current_result_id"
        ).fetchall())
    assert float(after_balances["alpha"]) == before_balances["alpha"]
    assert float(after_balances["beta"]) != before_balances["beta"]


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
            PerformanceV2ImportRequest(
                request.inbox, request.report_root, request.config,
                mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
                listing_dates_path=request.listing_dates_path,
            )
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (old_result,)


def test_replace_switches_current_result_and_replaces_only_scoped_children(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies where strategy_name = 'alpha'").fetchone()[0]
        old_result = connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone()[0]
        old_actions = connection.execute(
            "select * from strategy_actions where result_id = ? order by action_index", [old_result]
        ).fetchall()
        old_equity = connection.execute(
            "select * from strategy_equity where result_id = ? order by sample_index", [old_result]
        ).fetchall()
        window_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        window_end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        connection.execute(
            """insert into window_metrics (
                result_id, requested_start_utc, requested_end_utc, metrics_version,
                effective_start_utc, effective_end_utc, availability_status, unavailable_reason,
                growth_factor, return_pct, daily_log_return, daily_growth_pct, max_drawdown_pct,
                return_dd_ratio, fees_pct, profit_factor, trade_count, win_rate_pct,
                holding_seconds, time_in_market_pct, calculated_at_utc
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                old_result, window_start, window_end, "test", window_start, window_end, "AVAILABLE", None,
                Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                Decimal("0"), Decimal("1"), 1, Decimal("100"), Decimal("0"), Decimal("0"), window_end,
            ],
        )
        connection.execute(
            "insert into equity_quality_metrics values (?, 'old-source', 'equity-quality-r7.3-v1', '{}', 'old-digest', now())",
            [old_result],
        )
    changed = FIXTURE.read_bytes().replace(
        b"2026-01-01 - 2026-01-09", b"2026-01-01 - 2026-01-10"
    ).replace(b"1009.9", b"1019.9")
    _rewrite_report(request, changed)

    result = import_performance_v2(
        PerformanceV2ImportRequest(
            request.inbox, request.report_root, request.config,
            mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
            listing_dates_path=request.listing_dates_path,
        )
    )

    assert result.imported_count == 1
    with duckdb.connect(str(target), read_only=True) as connection:
        new_result = connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone()[0]
        assert new_result == old_result
        assert not hasattr(import_module, "_prepare_replace_children")
        assert connection.execute("select count(*) from strategy_results where strategy_id = ?", [strategy_id]).fetchone() == (1,)
        assert connection.execute("select final_balance from strategy_results where result_id = ?", [new_result]).fetchone() == (Decimal("1019.9"),)
        assert len(connection.execute("select * from strategy_actions where result_id = ?", [old_result]).fetchall()) == len(old_actions)
        assert len(connection.execute("select * from strategy_equity where result_id = ?", [old_result]).fetchall()) == len(old_equity)
        assert connection.execute("select count(*) from window_metrics where result_id = ?", [old_result]).fetchone() == (0,)
        assert connection.execute("select count(*) from equity_quality_metrics where result_id = ?", [old_result]).fetchone() == (0,)
        assert connection.execute("select report_end_utc from strategy_results where result_id = ?", [old_result]).fetchone()[0].date().isoformat() == "2026-01-10"
    assert (request.inbox / "strategies" / "alpha.json").read_bytes()
    assert (request.inbox / "inbox_manifest.json").read_bytes()


def test_replace_readback_batches_multiple_strategy_ids_and_keeps_manifest_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta", "gamma"))
    assert import_performance_v2(request).imported_count == 3
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        rows = connection.execute(
            "select strategy_name, strategy_id, current_result_id from strategies order by strategy_name"
        ).fetchall()
    strategy_ids = {str(name): int(strategy_id) for name, strategy_id, _ in rows}
    expected = {str(name): int(result_id) for name, _, result_id in rows}
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids=strategy_ids,
        expected_current_result_ids=expected,
        listing_dates_path=request.listing_dates_path,
    )

    raw_connect = import_module.duckdb.connect
    batch_queries: list[tuple[str, object]] = []
    scalar_queries: list[tuple[str, object]] = []

    class _Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

    class _Connection:
        def __init__(self, raw: object) -> None:
            self._raw = raw

        def execute(self, sql: str, parameters=None):
            compact = " ".join(str(sql).casefold().split())
            if "selectstrategy_id,current_result_idfromstrategies" in compact.replace(" ", ""):
                batch_queries.append((str(sql), parameters))
                rows = self._raw.execute(sql, parameters).fetchall()
                return _Result(list(reversed(rows)))
            if "select current_result_id from strategies where strategy_id = ?" in compact:
                scalar_queries.append((str(sql), parameters))
            return self._raw.execute(sql, parameters) if parameters is not None else self._raw.execute(sql)

        def close(self) -> None:
            self._raw.close()

        def __getattr__(self, name: str):
            return getattr(self._raw, name)

    monkeypatch.setattr(import_module.duckdb, "connect", lambda *args, **kwargs: _Connection(raw_connect(*args, **kwargs)))
    result = import_performance_v2(replacement)

    assert result.successful_replacements == tuple(
        {
            "strategy_id": strategy_ids[name],
            "old_result_id": expected[name],
            "new_result_id": expected[name],
        }
        for name in ("alpha", "beta", "gamma")
    )
    assert len(batch_queries) == 2
    assert scalar_queries == []
    assert [query[1] for query in batch_queries] == [
        [list(strategy_ids.values())],
        [list(strategy_ids.values())],
    ]


def test_replace_timestamp_batch_updates_only_admitted_strategies_with_one_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta", "gamma"))
    assert import_performance_v2(request).imported_count == 3
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_ids = dict(connection.execute(
            "select strategy_name, strategy_id from strategies order by strategy_name"
        ).fetchall())
        old_times = {
            name: datetime(2026, 9, 20 + index, 12, tzinfo=timezone.utc)
            for index, name in enumerate(("alpha", "beta", "gamma"))
        }
        for name, timestamp in old_times.items():
            connection.execute(
                "update strategies set updated_at_utc = ? where strategy_id = ?",
                [timestamp, strategy_ids[name]],
            )

    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed, _failures = import_module._prepare_listing_ranges(request, prepared, (parsed, None, parsed))
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids={name: int(strategy_id) for name, strategy_id in strategy_ids.items()},
        listing_dates_path=request.listing_dates_path,
    )
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module, "_utc_now", lambda: now)
    with duckdb.connect(str(target)) as connection:
        assert import_module._publish(
            connection, replacement, prepared, parsed, "timestamp-batch"
        ) == (2, 0, 1)

    with duckdb.connect(str(target), read_only=True) as connection:
        actual = dict(connection.execute(
            "select strategy_name, updated_at_utc from strategies order by strategy_name"
        ).fetchall())
    assert actual["alpha"] == now and actual["gamma"] == now
    assert actual["beta"] == old_times["beta"]


def test_add_mode_keeps_add_timestamp_inline_when_replace_is_batched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path, names=("alpha",))
    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        alpha_id = int(connection.execute(
            "select strategy_id from strategies where strategy_name = 'alpha'"
        ).fetchone()[0])

    second_root = tmp_path / "second"
    inbox, report_root, _ = _inbox(second_root, names=("alpha", "gamma"))
    listing_dates = second_root / "Input" / "dates.xlsx"
    listing_dates.parent.mkdir()
    workbook = Workbook()
    workbook.active.append(["ONUSDT", datetime(2025, 12, 25)])
    workbook.save(listing_dates)
    alpha_report = report_root / "alpha.html"
    alpha_bytes = alpha_report.read_bytes()
    widened = alpha_bytes.replace(b"2026-01-01 - 2026-01-09", b"2026-01-01 - 2026-01-10", 1)
    assert widened != alpha_bytes
    alpha_report.write_bytes(widened)
    manifest_path = inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["source_report_sha256"] = sha256(widened).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    second_request = PerformanceV2ImportRequest(
        inbox, report_root, request.config, listing_dates_path=Path("Input/dates.xlsx")
    )

    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module, "_utc_now", lambda: now)
    calls: list[tuple[int, ...]] = []
    update_timestamps = import_module._update_replacement_timestamps
    monkeypatch.setattr(
        import_module,
        "_update_replacement_timestamps",
        lambda connection, strategy_ids, timestamp: (
            calls.append(tuple(strategy_ids)), update_timestamps(connection, strategy_ids, timestamp)
        )[1],
    )
    assert import_performance_v2(second_request).imported_count == 2
    with duckdb.connect(str(target), read_only=True) as connection:
        rows = {
            name: (strategy_id, timestamp)
            for name, strategy_id, timestamp in connection.execute(
                "select strategy_name, strategy_id, updated_at_utc from strategies order by strategy_name"
            ).fetchall()
        }
    assert calls == [(alpha_id,)]
    assert rows["alpha"][1] == now and rows["gamma"][1] == now


def test_replace_timestamp_batch_failure_rolls_back_and_retry_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(request).imported_count == 2
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        rows = connection.execute(
            "select strategy_name, strategy_id, current_result_id from strategies order by strategy_name"
        ).fetchall()
        before = {
            name: (int(strategy_id), int(result_id), datetime(2026, 9, 20 + index, 12, tzinfo=timezone.utc))
            for index, (name, strategy_id, result_id) in enumerate(rows)
        }
        for strategy_id, _result_id, timestamp in before.values():
            connection.execute(
                "update strategies set updated_at_utc = ? where strategy_id = ?",
                [timestamp, strategy_id],
            )
        publication_tables = {
            "strategy_results": "select * from strategy_results order by result_id",
            "strategy_actions": "select * from strategy_actions order by result_id, action_index",
            "strategy_equity": "select * from strategy_equity order by result_id, sample_index",
            "window_metrics": "select * from window_metrics order by result_id, requested_start_utc",
            "equity_quality_metrics": "select * from equity_quality_metrics order by result_id, algo_version",
            "optimizer_prepared_inputs": "select * from optimizer_prepared_inputs order by result_id",
            "import_runs": "select * from import_runs order by import_run_id",
            "import_files": "select * from import_files order by import_run_id, source_html_sha256",
            "strategy_tags": "select * from strategy_tags order by strategy_id, tag",
        }
        before_publication = {
            table: connection.execute(sql).fetchall()
            for table, sql in publication_tables.items()
        }
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids={name: values[0] for name, values in before.items()},
        listing_dates_path=request.listing_dates_path,
    )
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module, "_utc_now", lambda: now)
    update_timestamps = import_module._update_replacement_timestamps

    def fail_batch(connection, strategy_ids, timestamp):
        if len(strategy_ids) > 1:
            update_timestamps(connection, strategy_ids, timestamp)
            raise RuntimeError("injected timestamp batch failure")
        return update_timestamps(connection, strategy_ids, timestamp)

    monkeypatch.setattr(import_module, "_update_replacement_timestamps", fail_batch)
    with pytest.raises(PerformanceV2ImportError, match="injected timestamp batch failure"):
        import_performance_v2(replacement)
    with duckdb.connect(str(target), read_only=True) as connection:
        after_failure = {
            name: (int(strategy_id), int(result_id), timestamp)
            for name, strategy_id, result_id, timestamp in connection.execute(
                "select s.strategy_name, s.strategy_id, s.current_result_id, s.updated_at_utc "
                "from strategies s order by s.strategy_name"
            ).fetchall()
        }
        after_failure_publication = {
            table: connection.execute(sql).fetchall()
            for table, sql in publication_tables.items()
        }
    assert after_failure == before
    assert after_failure_publication == before_publication

    monkeypatch.setattr(import_module, "_update_replacement_timestamps", update_timestamps)
    assert import_performance_v2(replacement).imported_count == 2
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select updated_at_utc from strategies order by strategy_name").fetchall() == [
            (now,), (now,)
        ]


def test_replace_readback_omits_missing_and_null_rows_and_filtered_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta", "gamma", "delta"))
    assert import_performance_v2(request).imported_count == 4
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        rows = connection.execute(
            "select strategy_name, strategy_id, current_result_id from strategies order by strategy_name"
        ).fetchall()
    strategy_ids = {str(name): int(strategy_id) for name, strategy_id, _ in rows}
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids=strategy_ids,
        listing_dates_path=request.listing_dates_path,
    )
    invalid = replacement.report_root / "delta.html"
    invalid.write_text("<html>invalid</html>", encoding="utf-8")
    manifest_path = replacement.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][3]["source_report_sha256"] = sha256(invalid.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    raw_connect = import_module.duckdb.connect
    batch_queries: list[object] = []

    class _Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

    class _Connection:
        def __init__(self, raw: object) -> None:
            self._raw = raw

        def execute(self, sql: str, parameters=None):
            compact = " ".join(str(sql).casefold().split())
            if "selectstrategy_id,current_result_idfromstrategies" in compact.replace(" ", ""):
                batch_queries.append(parameters)
                all_rows = self._raw.execute(sql, parameters).fetchall()
                alpha_id = strategy_ids["alpha"]
                gamma_id = strategy_ids["gamma"]
                rows = [row for row in all_rows if int(row[0]) == alpha_id]
                rows.append((gamma_id, None))
                return _Result(list(reversed(rows)))
            return self._raw.execute(sql, parameters) if parameters is not None else self._raw.execute(sql)

        def close(self) -> None:
            self._raw.close()

        def __getattr__(self, name: str):
            return getattr(self._raw, name)

    monkeypatch.setattr(import_module.duckdb, "connect", lambda *args, **kwargs: _Connection(raw_connect(*args, **kwargs)))
    result = import_performance_v2(replacement)

    assert len(batch_queries) == 1
    assert len(result.successful_replacements) == 1
    assert result.successful_replacements[0]["strategy_id"] == strategy_ids["alpha"]
    assert result.successful_replacements[0]["old_result_id"] == result.successful_replacements[0]["new_result_id"]
    assert any(row["strategy_name"] == "delta" and row["reason"] == "INVALID_REPORT" for row in result.failures)


@pytest.mark.parametrize(
    (
        "candidate_count", "expected_batch_sizes", "expected_scalar_queries", "duplicate_ids",
        "explicit_expected", "result_delta", "strict_mismatch", "filtered_name",
    ),
    [
        (0, [], 0, False, False, 0, False, None), (1, [], 1, False, False, 0, False, None),
        (2, [2], 0, False, False, 0, False, None), (3, [3], 0, False, False, 0, False, None),
        (409, [409], 0, False, False, 0, False, None), (1025, [1024, 1], 0, False, False, 0, False, None),
        (2, [], 1, True, False, 0, False, None), (2, [2, 2], 0, False, True, 100, False, None),
        (2, [], 0, False, False, 0, True, None), (3, [2], 0, False, False, 0, False, "candidate-1"),
    ],
)
def test_replace_readback_query_counts_cover_scalar_and_chunk_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_count: int,
    expected_batch_sizes: list[int],
    expected_scalar_queries: int,
    duplicate_ids: bool,
    explicit_expected: bool,
    result_delta: int,
    strict_mismatch: bool,
    filtered_name: str | None,
) -> None:
    base, _ = _request(tmp_path)
    names = tuple(f"candidate-{index}" for index in range(candidate_count))
    strategy_ids = {name: 10_000 if duplicate_ids else 10_000 + index for index, name in enumerate(names)}
    expected = {name: 20_000 + index for index, name in enumerate(names)}
    request = PerformanceV2ImportRequest(
        base.inbox,
        base.report_root,
        base.config,
        mode="REPLACE",
        replacement_strategy_ids=strategy_ids,
        expected_current_result_ids=expected if explicit_expected else None,
        listing_dates_path=base.listing_dates_path,
    )
    prepared = SimpleNamespace(
        entries=tuple(
            SimpleNamespace(strategy_name=name, identity=SimpleNamespace(symbol="BTCUSDT"))
            for name in names
        ),
        test_start=None,
        test_end=None,
        inbox_snapshot_sha256=None,
    )
    reports = tuple(SimpleNamespace(excluded_trade_count=0) for _ in names)
    if strict_mismatch:
        reports = reports[:-1]
    monkeypatch.setattr(import_module, "read_performance_v2_inbox", lambda *args, **kwargs: prepared)
    monkeypatch.setattr(import_module, "create_v2_parser_staging", lambda *args, **kwargs: None)
    monkeypatch.setattr(import_module, "_parse_reports", lambda *args, **kwargs: reports)
    monkeypatch.setattr(import_module, "_validate_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        import_module,
        "_prepare_listing_ranges",
        lambda *args, **kwargs: (reports, [{"strategy_name": filtered_name, "reason": "TEST"}] if filtered_name else []),
    )
    monkeypatch.setattr(import_module, "_publish", lambda *args, **kwargs: (1, 0, 0))

    raw_connect = import_module.duckdb.connect
    batch_sizes: list[int] = []
    batch_ids: list[list[int]] = []
    scalar_queries = 0
    batch_query_count = 0

    class _Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

        def fetchone(self) -> tuple[object, ...] | None:
            return self._rows[0] if self._rows else None

    class _Connection:
        def __init__(self, raw: object) -> None:
            self._raw = raw

        def execute(self, sql: str, parameters=None):
            nonlocal scalar_queries, batch_query_count
            compact = " ".join(str(sql).casefold().split())
            if "selectstrategy_name,current_result_idfromstrategies" in compact.replace(" ", ""):
                return _Result([(name, expected[name]) for name in names])
            if "selectstrategy_id,current_result_idfromstrategies" in compact.replace(" ", ""):
                ids = list(parameters[0])
                batch_sizes.append(len(ids))
                batch_ids.append(ids)
                batch_query_count += 1
                query_delta = 0 if explicit_expected and batch_query_count == 1 else result_delta
                return _Result([(strategy_id, expected_result) for strategy_id, expected_result in (
                        (strategy_ids[name], expected[name] + query_delta)
                        for name in names if strategy_ids[name] in ids
                )])
            if "select current_result_id from strategies where strategy_id = ?" in compact:
                scalar_queries += 1
                strategy_id = int(parameters[0])
                return _Result([(
                    next(result for name, result in expected.items() if strategy_ids[name] == strategy_id) + result_delta,
                )])
            return self._raw.execute(sql, parameters) if parameters is not None else self._raw.execute(sql)

        def close(self) -> None:
            self._raw.close()

        def __getattr__(self, name: str):
            return getattr(self._raw, name)

    monkeypatch.setattr(import_module.duckdb, "connect", lambda *args, **kwargs: _Connection(raw_connect(*args, **kwargs)))
    if strict_mismatch:
        with pytest.raises(PerformanceV2ImportError, match="Performance v2 import failed"):
            import_performance_v2(request)
        assert batch_sizes == []
        assert scalar_queries == 0
        return
    result = import_performance_v2(request)

    expected_count = candidate_count - (filtered_name is not None)
    assert len(result.successful_replacements) == expected_count
    assert batch_sizes == expected_batch_sizes
    assert scalar_queries == expected_scalar_queries
    if explicit_expected:
        expected_ids = list(strategy_ids.values())
        assert batch_ids == [expected_ids, expected_ids]
        assert [row["old_result_id"] for row in result.successful_replacements] == [expected[name] for name in names]
        assert [row["new_result_id"] for row in result.successful_replacements] == [expected[name] + result_delta for name in names]


def test_replace_rollback_restores_old_result_after_publish_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path)
    import_performance_v2(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id, old_result = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
        old_actions = connection.execute("select count(*) from strategy_actions where result_id = ?", [old_result]).fetchone()[0]
        old_equity = connection.execute("select count(*) from strategy_equity where result_id = ?", [old_result]).fetchone()[0]
        connection.execute(
            "insert into strategy_tags values (?, 'RETEST', 'RETEST_WORKFLOW', 'test', now())",
            [strategy_id],
        )
        connection.execute(
            "insert into equity_quality_metrics values (?, 'old-source', 'equity-quality-r7.3-v1', '{\"state\":\"GROWING\"}', 'old-digest', now())",
            [old_result],
        )
    _rewrite_report(request, FIXTURE.read_bytes().replace(b"1009.9", b"1019.9"))
    original_inbox = {path.relative_to(request.inbox): path.read_bytes() for path in request.inbox.rglob("*") if path.is_file()}
    import mrs3.performance_v2_import as import_module
    append_rows = import_module._append_rows
    equity_append_calls = 0

    def fail_after_new_result(connection, table, columns, rows, *args, **kwargs):
        nonlocal equity_append_calls
        if table == "strategy_equity":
            equity_append_calls += 1
            if equity_append_calls == 2:
                raise RuntimeError("injected later child failure")
        append_rows(connection, table, columns, rows, *args, **kwargs)

    monkeypatch.setattr(import_module, "_APPEND_BATCH_ROWS", 1)
    monkeypatch.setattr(import_module, "_append_rows", fail_after_new_result)

    with pytest.raises(PerformanceV2ImportError, match="transaction failed|injected"):
        import_performance_v2(
            PerformanceV2ImportRequest(
                request.inbox,
                request.report_root,
                request.config,
                mode="REPLACE",
                replacement_strategy_ids={"alpha": strategy_id},
                clear_retest_on_success=True,
                listing_dates_path=request.listing_dates_path,
            )
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (old_result,)
        assert connection.execute("select count(*) from strategy_actions where result_id = ?", [old_result]).fetchone() == (old_actions,)
        assert connection.execute("select count(*) from strategy_equity where result_id = ?", [old_result]).fetchone() == (old_equity,)
        assert connection.execute(
            "select count(*) from strategy_tags where strategy_id = ? and tag = 'RETEST'", [strategy_id]
        ).fetchone() == (1,)
        assert connection.execute(
            "select source_revision, facts_json, facts_sha256 from equity_quality_metrics where result_id = ?",
            [old_result],
        ).fetchone() == ("old-source", '{"state":"GROWING"}', "old-digest")
    assert list(request.config.database_root.glob("performance_v2_failures_*.csv"))
    assert {path.relative_to(request.inbox): path.read_bytes() for path in request.inbox.rglob("*") if path.is_file()} == original_inbox
    assert equity_append_calls == 2


def test_retest_replace_rejects_a_shorter_effective_period_without_mutation(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
        actions = connection.execute(
            "select count(*) from strategy_actions where result_id = ?", [result_id]
        ).fetchone()[0]
        equity = connection.execute(
            "select count(*) from strategy_equity where result_id = ?", [result_id]
        ).fetchone()[0]
    shorter = FIXTURE.read_bytes().replace(b"2026-01-01 - 2026-01-09", b"2026-01-01 - 2026-01-04")
    assert parse_current_performance_v2_html(shorter, request.config).metrics["Report range"] == "2026-01-01 - 2026-01-04"
    _rewrite_report(request, shorter)

    with pytest.raises(PerformanceV2ImportError, match="shorter effective period"):
        import_performance_v2(
            PerformanceV2ImportRequest(
                request.inbox, request.report_root, request.config,
                mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
                listing_dates_path=request.listing_dates_path,
            )
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_id]
        ).fetchone() == (result_id,)
        assert connection.execute("select count(*) from strategy_actions where result_id = ?", [result_id]).fetchone() == (actions,)
        assert connection.execute("select count(*) from strategy_equity where result_id = ?", [result_id]).fetchone() == (equity,)


def test_mapped_retest_replace_rejects_shorter_period_per_strategy_and_imports_siblings(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(request).imported_count == 2
    strategy_ids, expected = _current_strategy_map(request)
    before_balances = _current_final_balances(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        before_alpha = connection.execute(
            "select r.imported_at_utc, r.effective_start_utc, r.effective_end_utc "
            "from strategies s join strategy_results r on r.result_id=s.current_result_id "
            "where s.strategy_name='alpha'"
        ).fetchone()
        alpha_result_id = expected["alpha"]
        alpha_action_count = connection.execute(
            "select count(*) from strategy_actions where result_id=?", [alpha_result_id]
        ).fetchone()[0]
        alpha_equity_count = connection.execute(
            "select count(*) from strategy_equity where result_id=?", [alpha_result_id]
        ).fetchone()[0]

    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shorter = FIXTURE.read_bytes().replace(b"2026-01-01 - 2026-01-09", b"2026-01-01 - 2026-01-04")
    changed_sibling = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    for entry in manifest["entries"]:
        content = shorter if entry["strategy_name"] == "alpha" else changed_sibling
        Path(entry["report_path"]).write_bytes(content)
        entry["source_report_sha256"] = sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids,
        expected_current_result_ids=expected,
        listing_dates_path=request.listing_dates_path,
    )

    result = import_performance_v2(replacement)

    assert result.status == "COMMITTED"
    assert (result.imported_count, result.skipped_count, result.rejected_count) == (1, 0, 1)
    assert result.failures == ({
        "strategy_id": strategy_ids["alpha"],
        "strategy_name": "alpha",
        "symbol": "ONUSDT",
        "reason": "SHORTER_EFFECTIVE_PERIOD",
        "error": "REPLACE target 'alpha' has a shorter effective period",
    },)
    assert result.successful_replacements == ({
        "strategy_id": strategy_ids["beta"],
        "old_result_id": expected["beta"],
        "new_result_id": expected["beta"],
    },)
    assert result.failure_report_path is not None
    assert "SHORTER_EFFECTIVE_PERIOD" in result.failure_report_path.read_text(encoding="utf-8")
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_name='alpha'"
        ).fetchone() == (alpha_result_id,)
        assert connection.execute(
            "select r.imported_at_utc, r.effective_start_utc, r.effective_end_utc "
            "from strategies s join strategy_results r on r.result_id=s.current_result_id "
            "where s.strategy_name='alpha'"
        ).fetchone() == before_alpha
        assert connection.execute(
            "select count(*) from strategy_actions where result_id=?", [alpha_result_id]
        ).fetchone() == (alpha_action_count,)
        assert connection.execute(
            "select count(*) from strategy_equity where result_id=?", [alpha_result_id]
        ).fetchone() == (alpha_equity_count,)
    assert _current_final_balances(replacement)["alpha"] == before_balances["alpha"]
    assert _current_final_balances(replacement)["beta"] != before_balances["beta"]
    with duckdb.connect(str(target), read_only=True) as connection:
        run_id, expected_count, imported_count, skipped_count, rejected_count, run_status = connection.execute(
            "select import_run_id, expected_report_count, imported_count, skipped_count, rejected_count, status "
            "from import_runs order by import_run_id desc limit 1"
        ).fetchone()
        assert (expected_count, imported_count, skipped_count, rejected_count, run_status) == (2, 1, 0, 1, "COMMITTED")
        file_statuses = dict(connection.execute(
            "select source_filename, status from import_files where import_run_id = ?", [run_id]
        ).fetchall())
        assert file_statuses == {"alpha.html": "REJECTED:SHORTER_EFFECTIVE_PERIOD", "beta.html": "REPLACED"}


def test_mapped_retest_replace_rejects_invalid_effective_period_per_strategy(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(request).imported_count == 2
    strategy_ids, expected = _current_strategy_map(request)
    before_balances = _current_final_balances(request)
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        connection.execute(
            "update strategy_results set effective_start_utc='2026-01-05 00:00:00+00', "
            "effective_end_utc='2026-01-04 00:00:00+00' where result_id=?",
            [expected["alpha"]],
        )
        before_alpha = connection.execute(
            "select imported_at_utc, effective_start_utc, effective_end_utc from strategy_results where result_id=?",
            [expected["alpha"]],
        ).fetchone()
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_sibling = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    for entry in manifest["entries"]:
        if entry["strategy_name"] == "beta":
            Path(entry["report_path"]).write_bytes(changed_sibling)
            entry["source_report_sha256"] = sha256(changed_sibling).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids, expected_current_result_ids=expected,
        listing_dates_path=request.listing_dates_path,
    )

    result = import_performance_v2(replacement)

    assert result.status == "COMMITTED"
    assert (result.imported_count, result.skipped_count, result.rejected_count) == (1, 0, 1)
    assert result.failures == ({
        "strategy_id": strategy_ids["alpha"], "strategy_name": "alpha", "symbol": "ONUSDT",
        "reason": "INVALID_EFFECTIVE_PERIOD",
        "error": "REPLACE target 'alpha' has an invalid effective period",
    },)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select imported_at_utc, effective_start_utc, effective_end_utc from strategy_results where result_id=?",
            [expected["alpha"]],
        ).fetchone() == before_alpha
        assert connection.execute(
            "select current_result_id from strategies where strategy_name='alpha'"
        ).fetchone() == (expected["alpha"],)
    assert _current_final_balances(replacement)["beta"] != before_balances["beta"]


def test_empty_expected_result_mapping_fails_closed_as_mapped_retest(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    assert import_performance_v2(request).imported_count == 1
    strategy_ids, expected = _current_strategy_map(request)
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids, listing_dates_path=request.listing_dates_path,
    )
    object.__setattr__(replacement, "expected_current_result_ids", {})

    result = import_performance_v2(replacement)

    assert result.status == "FAILED"
    assert result.imported_count == 0 and result.rejected_count == 1
    assert result.failures[0]["reason"] == "MISSING_EXPECTED_RESULT"
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_name='alpha'"
        ).fetchone() == (expected["alpha"],)


def test_finalist_retest_http_status_reports_mixed_import_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    assert import_performance_v2(request).imported_count == 2
    strategy_ids, expected = _current_strategy_map(request)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shorter = FIXTURE.read_bytes().replace(b"2026-01-01 - 2026-01-09", b"2026-01-01 - 2026-01-04")
    changed_sibling = FIXTURE.read_bytes().replace(b"1009.9", b"1019.9")
    for entry in manifest["entries"]:
        content = shorter if entry["strategy_name"] == "alpha" else changed_sibling
        Path(entry["report_path"]).write_bytes(content)
        entry["source_report_sha256"] = sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, mode="REPLACE",
        replacement_strategy_ids=strategy_ids, expected_current_result_ids=expected,
        listing_dates_path=request.listing_dates_path,
    )
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    registry = controller._panel_jobs
    parent_id = "mixed-period-parent"
    registry.submit(
        "strategies.performance.v2.finalist-retest", {}, parent_id,
        ("strategies.tester", "performance-v2-finalist-retest"), job_id=parent_id,
    )
    registry.transition(parent_id, "RUNNING")
    registry.sync(parent_id, {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True}, runtime={
        "bulk_retest": True, "scope": "FINALIST", "test_start": "2026-01-01", "test_end": "2026-10-01",
        "cohort_members": [
            {"strategy_id": strategy_ids[name], "strategy_name": name, "result_id": expected[name]}
            for name in ("alpha", "beta")
        ],
    })

    def execute_import(_payload: dict[str, object], *, _internal: bool = False, job_id: str | None = None) -> dict[str, str]:
        assert _internal and job_id is not None
        imported = import_performance_v2(replacement)
        registry.submit("strategies.performance.v2.import", {}, job_id, ("performance-v2-db",), job_id=job_id)
        registry.transition(job_id, "RUNNING")
        registry.sync(job_id, {
            "state": "COMMITTED",
            "result": {
                "imported_count": imported.imported_count,
                "skipped_count": imported.skipped_count,
                "rejected_count": imported.rejected_count,
            },
        }, runtime={
            "successful_replacements": [dict(item) for item in imported.successful_replacements],
            "failures": [dict(item) for item in imported.failures],
        })
        return {"job_id": job_id}

    monkeypatch.setattr(controller, "strategies_performance_v2_import", execute_import)
    monkeypatch.setattr(
        controller, "_single_mode_strategy_test",
        lambda: SimpleNamespace(status=lambda _job_id: (_ for _ in ()).throw(KeyError())),
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request(
            "POST", "/api/v2/strategies/performance-v2/finalist-retest/import",
            body=json.dumps({"tester_job_id": parent_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        queued = json.loads(response.read())
        assert response.status == 200
        connection.request("GET", f"/api/v2/strategies/performance-v2/finalist-retest/status?job_id={parent_id}")
        response = connection.getresponse()
        status = json.loads(response.read())
        assert response.status == 200
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status["import_job_id"] == queued["job_id"]
    assert status["import_job_state"] == "COMMITTED"
    assert (status["imported_count"], status["skipped_count"], status["rejected_count"], status["expected_count"]) == (1, 0, 1, 2)
    assert status["successful_replacements"] == [{
        "strategy_id": strategy_ids["beta"],
        "old_result_id": expected["beta"],
        "new_result_id": expected["beta"],
    }]
    assert status["failures"] == [{
        "strategy_id": strategy_ids["alpha"], "strategy_name": "alpha", "symbol": "ONUSDT",
        "reason": "SHORTER_EFFECTIVE_PERIOD",
        "error": "REPLACE target 'alpha' has a shorter effective period",
    }]


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


def test_writer_lock_fails_closed_before_opening_database(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)

    with PerformanceV2WriterLock(request.config.database_root):
        with pytest.raises(PerformanceV2LockedError):
            import_performance_v2(request)


def test_writer_lock_does_not_create_missing_database_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-v2"

    with pytest.raises(PerformanceV2StoreError, match="root does not exist"):
        with PerformanceV2WriterLock(missing_root):
            pass

    assert not missing_root.exists()


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

    with pytest.raises(PerformanceV2ImportError, match="supported schema"):
        import_performance_v2(request)

    assert target.exists()
    assert not (request.config.database_root / ".staging").exists()
    assert not (request.config.database_root / "import_audit.v2.json").exists()


def test_import_migrates_existing_v4_target_before_current_schema_gate(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True)
    with duckdb.connect(str(target)) as connection:
        schema = _SCHEMA.split("\nCREATE TABLE IF NOT EXISTS optimizer_prepared_inputs", 1)[0]
        for definition in (
            "    sizing_use_upnl BOOLEAN,\n",
            "    sizing_use_frozen_balance BOOLEAN,\n",
            "    sizing_use_fix BOOLEAN,\n",
            "    sizing_balance_percentage_long DECIMAL(38,12),\n",
            "    sizing_risk_long DECIMAL(38,12),\n",
            "    sizing_max_balance DECIMAL(38,12),\n",
            "    price DECIMAL(38,12),\n",
            "    cost DECIMAL(38,12),\n",
        ):
            schema = schema.replace(definition, "")
        schema = schema.replace(
            "    commission_rate DECIMAL(38,12),\n",
            "    commission_rate DECIMAL(38,12) NOT NULL,\n",
        )
        connection.execute("create table schema_info (key varchar primary key, value varchar not null)")
        connection.execute(schema)
        connection.execute(_SELECTION_SCHEMA)
        connection.executemany(
            "insert into schema_info values (?, ?)",
            [
                ("schema_version", "4"),
                ("database_kind", "unified_performance_v2"),
                ("database_instance_id", "00000000-0000-0000-0000-000000000001"),
            ],
        )
    _rewrite_report(request, _report_with_source_metadata())

    result = import_performance_v2(request)

    assert result.status == "COMMITTED"
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select value from schema_info where key = 'schema_version'"
        ).fetchone() == ("9",)


def test_bare_duckdb_target_is_not_initialized_by_import(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True)
    with duckdb.connect(str(target)):
        pass

    with pytest.raises(PerformanceV2ImportError, match="supported schema"):
        import_performance_v2(request)

    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select count(*) from information_schema.tables where table_schema = 'main'"
        ).fetchone() == (0,)
    assert not (request.config.database_root / ".staging").exists()
    assert not (request.config.database_root / "import_audit.v2.json").exists()


def test_warmup_does_not_publish_an_open_only_report(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        actions=(replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 7, tzinfo=timezone.utc)),),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == "NO_EFFECTIVE_TRADE"


def test_no_warmup_drops_open_at_end_lifecycle_and_counts_it(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    open_action = replace(
        parsed.actions[0], action_index=2, timestamp_utc=datetime(2026, 1, 4, tzinfo=timezone.utc),
    )
    parsed = replace(parsed, actions=parsed.actions + (open_action,))

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2025, 12, 25, tzinfo=timezone.utc)}
    )

    assert failure is None and result is not None
    assert len(result.actions) == 2
    assert result.metrics["Total Trades"] == "1"
    assert result.excluded_trade_count == 1


def test_no_warmup_open_only_report_is_not_published(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(parsed, actions=(replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 4, tzinfo=timezone.utc)),))

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2025, 12, 25, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == "NO_EFFECTIVE_TRADE"
    assert failure["excluded_trade_count"] == 1


def test_warmup_excludes_crossing_trade_pnl_and_fees(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    actions = (
        replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 2, tzinfo=timezone.utc), fee=Decimal("0.5"), balance=Decimal("999.5")),
        replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 7, tzinfo=timezone.utc), fee=Decimal("0.5"), pnl=Decimal("10"), balance=Decimal("1009")),
        replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 8, tzinfo=timezone.utc), fee=Decimal("1"), balance=Decimal("1008")),
        replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 9, tzinfo=timezone.utc), fee=Decimal("1"), pnl=Decimal("4"), balance=Decimal("1011")),
    )
    metrics = dict(parsed.metrics)
    metrics["Report range"] = "2026-01-01 - 2026-01-10"
    parsed = replace(parsed, metrics=metrics, actions=actions, wallet_series=(), equity_series=())

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 3, tzinfo=timezone.utc)}
    )

    assert failure is None and result is not None
    assert len(result.actions) == 2
    assert result.wallet_series == () and result.equity_series == ()
    assert result.inventory.wallet_sample_count == result.inventory.equity_sample_count == 0
    assert result.metrics["Initial balance"] == "1000.0"
    assert result.metrics["Max Drawdown"] == "N/A"
    assert result.metrics["Total Trades"] == "1"
    assert result.metrics["Total PnL"] == "2"
    assert result.metrics["Total fees"] == "2"
    assert result.reported_start_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert result.reported_end_utc == datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert result.listing_date_utc == datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert result.effective_start_utc == datetime(2026, 1, 8, tzinfo=timezone.utc)
    assert result.effective_end_utc == result.reported_end_utc
    assert result.warmup_hours == 120
    assert result.excluded_trade_count == 1
    assert result.exclusion_reason is None


def test_warmup_drawdown_peak_starts_at_baseline_for_first_sample_below_it(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        actions=(
            replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 8, tzinfo=timezone.utc), fee=Decimal("1"), balance=Decimal("999")),
            replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 9, tzinfo=timezone.utc), pnl=Decimal("0"), fee=Decimal("0"), balance=Decimal("999")),
        ),
        wallet_series=((datetime(2026, 1, 8, tzinfo=timezone.utc), Decimal("999")),),
        equity_series=(),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 3, tzinfo=timezone.utc)}
    )

    assert failure is None and result is not None
    assert result.wallet_series == ((datetime(2026, 1, 8, tzinfo=timezone.utc), Decimal("999")),)
    assert result.inventory.wallet_sample_count == 1
    assert result.metrics["Max Drawdown"] == "1"


def test_warmup_pins_signed_gross_loss_for_a_losing_trade(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        actions=(
            replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 8, tzinfo=timezone.utc), fee=Decimal("0"), pnl=Decimal("0"), balance=Decimal("1000")),
            replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 9, tzinfo=timezone.utc), fee=Decimal("0"), pnl=Decimal("-2"), balance=Decimal("998")),
        ),
        wallet_series=(),
        equity_series=(),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 3, tzinfo=timezone.utc)}
    )

    assert failure is None and result is not None
    # Gross loss is a positive magnitude, matching the window calculator.
    assert result.metrics["Gross loss"] == "2"


def test_result_values_persist_full_precision_effective_provenance(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    effective_start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    effective_end = datetime(2026, 1, 9, 18, 30, tzinfo=timezone.utc)
    parsed = replace(parsed, effective_start_utc=effective_start, effective_end_utc=effective_end)

    values = import_module._result_values(
        prepared.entries[0], parsed, {"TakerFee": "0.0004"}, datetime(2026, 1, 10, tzinfo=timezone.utc)
    )

    assert values["report_start_utc"] == effective_start
    assert values["report_end_utc"] == effective_end


def test_result_values_keep_actual_fees_when_commission_rate_is_unknown(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)

    with_rate = import_module._result_values(prepared.entries[0], parsed, {"TakerFee": "0.0004"}, now)
    without_rate = import_module._result_values(prepared.entries[0], parsed, {}, now)

    assert with_rate["commission_rate"] == Decimal("0.0004")
    assert without_rate["commission_rate"] is None
    assert {key: value for key, value in with_rate.items() if key != "commission_rate"} == {
        key: value for key, value in without_rate.items() if key != "commission_rate"
    }


def test_warmup_does_not_publish_when_the_only_trade_crosses_warmup(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        actions=(
            replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 8, tzinfo=timezone.utc)),
        ),
        wallet_series=(),
        equity_series=(),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 3, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None
    assert failure["reason"] == "NO_EFFECTIVE_TRADE"
    assert failure["excluded_trade_count"] == 1


def test_warmup_rejects_unknown_action_without_dropping_it(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(parsed, actions=(replace(parsed.actions[0], action="mystery"), parsed.actions[1]))

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2025, 12, 25, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == "UNKNOWN_ACTION"


def test_import_persists_warmup_provenance(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    dates = tmp_path / "Input" / "dates.xlsx"
    dates.parent.mkdir(exist_ok=True)
    workbook = Workbook()
    workbook.active.append(["ONUSDT", datetime(2025, 12, 25)])
    workbook.save(dates)
    request = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config, listing_dates_path=Path("Input/dates.xlsx")
    )

    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        row = connection.execute(
            """select report_start_utc, report_end_utc, reported_start_utc, reported_end_utc,
                      listing_date_utc, listing_date_raw, listing_date_source,
                      effective_start_utc, effective_end_utc, warmup_hours,
                      excluded_trade_count, exclusion_reason
                 from strategy_results"""
        ).fetchone()
    assert row[0].date().isoformat() == "2026-01-01"
    assert row[1].date().isoformat() == "2026-01-09"
    assert row[2].date().isoformat() == "2026-01-01"
    assert row[3].date().isoformat() == "2026-01-09"
    assert row[4].date().isoformat() == "2025-12-25"
    assert row[5] and row[6] == "configured_listing_dates_path"
    assert row[7].date().isoformat() == "2026-01-01"
    assert row[8].date().isoformat() == "2026-01-09"
    assert row[9:] == (120, 0, None)
def test_warmup_normalizes_listing_timezone_and_keeps_inclusive_report_end(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    metrics = dict(parsed.metrics)
    metrics["Report range"] = "2026-01-01 - 2026-01-10"
    parsed = replace(
        parsed,
        metrics=metrics,
        actions=(
            replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 6, 10, tzinfo=timezone.utc)),
            replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 10, tzinfo=timezone.utc)),
        ),
        wallet_series=(),
        equity_series=(),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0],
        parsed,
        {"ONUSDT": datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))},
    )

    assert failure is None and result is not None
    assert len(result.actions) == 2
    assert result.actions[-1].timestamp_utc == datetime(2026, 1, 10, tzinfo=timezone.utc)


def test_warmup_drops_a_trade_closed_after_inclusive_report_end(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    metrics = dict(parsed.metrics)
    metrics["Report range"] = "2026-01-01 - 2026-01-10"
    parsed = replace(
        parsed,
        metrics=metrics,
        actions=(
            replace(parsed.actions[0], timestamp_utc=datetime(2026, 1, 9, 10, tzinfo=timezone.utc)),
            replace(parsed.actions[1], timestamp_utc=datetime(2026, 1, 10, 0, 1, tzinfo=timezone.utc)),
        ),
        wallet_series=(),
        equity_series=(),
    )

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2026, 1, 4, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == "NO_EFFECTIVE_TRADE"


@pytest.mark.parametrize(
    ("field", "reason"),
    [("actions", "ACTIONS_OUT_OF_ORDER"), ("wallet_series", "WALLET_OUT_OF_ORDER"), ("equity_series", "EQUITY_OUT_OF_ORDER")],
)
def test_warmup_rejects_out_of_order_source_rows(tmp_path: Path, field: str, reason: str) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    values = {
        "actions": tuple(reversed(parsed.actions)),
        "wallet_series": tuple(reversed(parsed.wallet_series)),
        "equity_series": tuple(reversed(parsed.equity_series)),
    }
    parsed = replace(parsed, **{field: values[field]})

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2025, 12, 25, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == reason


def test_warmup_rejects_orphan_increased_action(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(parsed, actions=(replace(parsed.actions[0], action="increased"), parsed.actions[1]))

    result, failure = import_module._warmup_report(
        prepared.entries[0], parsed, {"ONUSDT": datetime(2025, 12, 25, tzinfo=timezone.utc)}
    )

    assert result is None
    assert failure is not None and failure["reason"] == "INVALID_ACTION_STATE"


def test_single_mode_without_listing_dates_fails_closed(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    request = PerformanceV2ImportRequest(request.inbox, request.report_root, request.config)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"run_mode": "SINGLE_MODE", "test_start": "2026-01-01", "test_end": "2026-01-09"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)

    filtered, failures = import_module._prepare_listing_ranges(request, prepared, (parsed,))

    assert filtered == (None,)
    assert failures and failures[0]["reason"] == "LISTING_MISSING"


def test_inbox_rejects_test_end_after_yesterday(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "run_mode": "SINGLE_MODE",
        "test_start": "2026-01-01",
        "test_end": (date.today() + timedelta(days=1)).isoformat(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PerformanceV2InputError, match="test end date must not be later than yesterday"):
        read_performance_v2_inbox(request.inbox, request.report_root)


def test_import_rejects_parsed_report_end_after_yesterday(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    future_end = (date.today() + timedelta(days=1)).isoformat()
    replacement = FIXTURE.read_bytes().replace(
        b"2026-01-01 - 2026-01-09", f"2026-01-01 - {future_end}".encode()
    )
    _rewrite_report(request, replacement)

    result = import_performance_v2(request)

    assert result.status == "FAILED"
    assert result.imported_count == 0
    assert result.rejected_count == 1
    assert "yesterday" in str(result.failures[0]["error"])


def test_check_range_false_requires_parseable_report_range_and_rejects_future_end(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root, config=request.config)
    prepared = replace(prepared, test_start="2026-01-01", test_end="2026-01-09")
    report = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    entry = prepared.entries[0]

    validate_report = import_module._validate_report
    with pytest.raises(PerformanceV2ImportError, match="report period is invalid"):
        validate_report(entry, replace(report, metrics={"Report range": "not-a-period"}), prepared, request, check_range=False)

    future_end = (date.today() + timedelta(days=1)).isoformat()
    future_report = replace(report, metrics={"Report range": f"2026-01-01 - {future_end}"})
    with pytest.raises(PerformanceV2ImportError, match="later than yesterday"):
        validate_report(entry, future_report, prepared, request, check_range=False)


def test_all_invalid_reports_fail_without_empty_in_clause(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for index, name in enumerate(("alpha", "beta")):
        report_path = request.report_root / f"{name}.html"
        report_path.write_text("<html>invalid</html>", encoding="utf-8")
        manifest["entries"][index]["source_report_sha256"] = sha256(report_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = import_performance_v2(request)

    assert result.status == "FAILED"
    assert (result.imported_count, result.skipped_count, result.rejected_count) == (0, 0, 2)
    assert result.failure_count == 2
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (0,)
        for table in (
            "strategy_results",
            "strategy_orders",
            "strategy_actions",
            "strategy_equity",
            "window_metrics",
            "analysis_plateaus",
        ):
            assert connection.execute(f"select count(*) from {table}").fetchone() == (0,)
        assert connection.execute("select status from import_runs").fetchone() == ("FAILED",)
        assert connection.execute("select distinct status from import_files").fetchall() == [("REJECTED:INVALID_REPORT",)]
    failure_csv = result.failure_report_path
    assert failure_csv is not None
    failure_text = failure_csv.read_text(encoding="utf-8")
    assert failure_text.splitlines()[0].split(",")[:5] == [
        "reason", "strategy_name", "symbol", "import_id", "outcome"
    ]
    failure_rows = list(csv.DictReader(failure_text.splitlines()))
    assert all(row["outcome"] == "FAILED" and row["error"] for row in failure_rows)
    assert result.failure_report_xlsx_path is not None and result.failure_report_xlsx_path.is_file()


def test_failure_report_blanks_none_and_strips_control_characters(tmp_path: Path) -> None:
    config = PerformanceV2Config(tmp_path / "v2")
    csv_path, xlsx_path = import_module._write_failure_reports(
        config,
        [{"reason": "INVALID_REPORT", "strategy_name": "alpha", "symbol": "ONUSDT", "error": None},
         {"reason": "LISTING_MISSING", "strategy_name": "beta", "symbol": "ONUSDT", "error": "bad\npath\x00"}],
        import_id="test", status="FAILED",
    )

    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["error"] == ""
    assert rows[1]["error"] == "bad path"
    assert xlsx_path is not None and xlsx_path.is_file()


def test_clear_retest_on_success_uses_replaced_strategy_ids_only(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        # Seed an unrelated strategy so strategy and result IDs are visibly
        # out of phase before the replacement transaction.
        connection.execute(
            """insert into strategies (
                strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                analysis_run_id, candidate_identity, lifecycle_status, current_result_id,
                created_at_utc, updated_at_utc
             ) values ('seed', 'SEEDUSDT', 'LONG', '1h', 3, 1, 'seed-run', 'seed-candidate', 'DISCARDED', null, now(), now())"""
        )
    import_performance_v2(request)
    with duckdb.connect(str(target)) as connection:
        ids = dict(
            connection.execute(
                "select strategy_name, strategy_id from strategies where strategy_name in ('alpha', 'beta')"
            ).fetchall()
        )
        connection.executemany(
            "insert into strategy_tags values (?, 'RETEST', 'RETEST_WORKFLOW', 'test', now())",
            [(ids["alpha"],), (ids["beta"],)],
        )
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        listing_date_utc=datetime(2025, 12, 25, tzinfo=timezone.utc),
        reported_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reported_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        effective_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        warmup_hours=120,
    )
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids=ids,
        clear_retest_on_success=True,
        listing_dates_path=request.listing_dates_path,
    )
    with duckdb.connect(str(target)) as connection:
        import_module._publish(connection, replacement, prepared, (parsed, None), "selective-clear")

    with duckdb.connect(str(target), read_only=True) as connection:
        pairs = connection.execute("select strategy_id, current_result_id from strategies").fetchall()
        assert all(strategy_id != result_id for strategy_id, result_id in pairs)
        assert connection.execute(
            "select count(*) from strategy_tags where strategy_id = ? and tag = 'RETEST'", [ids["alpha"]]
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from strategy_tags where strategy_id = ? and tag = 'RETEST'", [ids["beta"]]
        ).fetchone() == (1,)


@pytest.mark.parametrize("admitted_count", [1, 2])
def test_replace_batches_child_deletes_for_admitted_results(tmp_path: Path, admitted_count: int) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta", "gamma"))
    assert import_performance_v2(request).imported_count == 3
    target = performance_v2_database_path(request.config)
    child_tables = (
        "strategy_actions", "strategy_equity", "window_metrics",
        "equity_quality_metrics", "optimizer_prepared_inputs",
    )
    order_by = {
        "strategy_actions": "action_index",
        "strategy_equity": "sample_index",
        "window_metrics": "requested_start_utc, requested_end_utc, metrics_version",
        "equity_quality_metrics": "algo_version",
        "optimizer_prepared_inputs": "result_id",
    }
    with duckdb.connect(str(target), read_only=True) as connection:
        ids = dict(connection.execute(
            "select strategy_name, strategy_id from strategies order by strategy_name"
        ).fetchall())
        current = dict(connection.execute(
            "select strategy_name, current_result_id from strategies order by strategy_name"
        ).fetchall())
    with duckdb.connect(str(target)) as connection:
        connection.execute(
            """insert into window_metrics (
                result_id, requested_start_utc, requested_end_utc, metrics_version,
                availability_status, calculated_at_utc
            ) values (?, ?, ?, 'test-v1', 'AVAILABLE', now())""",
            [
                current["gamma"],
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 9, tzinfo=timezone.utc),
            ],
        )
        connection.execute(
            """insert into equity_quality_metrics (
                result_id, source_revision, algo_version, facts_json, facts_sha256, calculated_at_utc
            ) values (?, 'gamma-source', 'test-v1', '{\"state\":\"GROWING\"}', 'gamma-digest', now())""",
            [current["gamma"]],
        )
        connection.executemany(
            """insert into optimizer_prepared_inputs (
                result_id, preparation_version, source_digest, availability_status,
                unavailable_reason, prepared_json, prepared_at_utc
            ) values (?, 'test-v1', ?, 'UNAVAILABLE', 'MISSING_TYPED_FACTS', null, now())""",
            [[current[name], f"{name}-digest"] for name in ("alpha", "beta", "gamma")],
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        before = {
            name: {
                table: connection.execute(
                    f"select * from {table} where result_id = ? order by {order_by[table]}",
                    [current[name]],
                ).fetchall()
                for table in child_tables
            }
            for name in ("alpha", "beta", "gamma")
        }

    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    parsed = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    parsed = replace(
        parsed,
        listing_date_utc=datetime(2025, 12, 25, tzinfo=timezone.utc),
        reported_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reported_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        effective_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_end_utc=datetime(2026, 1, 9, tzinfo=timezone.utc),
        warmup_hours=120,
    )
    replacement = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        mode="REPLACE",
        replacement_strategy_ids=ids,
        expected_current_result_ids=current,
        listing_dates_path=request.listing_dates_path,
    )

    class RecordingConnection:
        def __init__(self, raw):
            self.raw = raw
            self.delete_sql: list[str] = []

        def execute(self, sql, parameters=None):
            if sql.casefold().startswith("delete from") and "where result_id" in sql.casefold():
                self.delete_sql.append(sql)
            return self.raw.execute(sql, parameters) if parameters is not None else self.raw.execute(sql)

        def executemany(self, sql, parameters):
            return self.raw.executemany(sql, parameters)

        def append(self, table, frame):
            return self.raw.append(table, frame)

    raw = duckdb.connect(str(target))
    recording = RecordingConnection(raw)
    try:
        imported, skipped, rejected = import_module._publish(
            recording,
            replacement,
            prepared,
            tuple(parsed if index < admitted_count else None for index in range(3)),
            "batched-replace",
        )
    finally:
        raw.close()

    assert (imported, skipped, rejected) == (admitted_count, 0, 3 - admitted_count)
    assert len(recording.delete_sql) == 5
    assert [
        sql.casefold().split("delete from ", 1)[1].split(None, 1)[0]
        for sql in recording.delete_sql
    ] == list(child_tables)
    if admitted_count == 1:
        assert all(
            "where result_id = ?" in sql.casefold() and " in (" not in sql.casefold()
            for sql in recording.delete_sql
        )
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select strategy_name, current_result_id from strategies order by strategy_name"
        ).fetchall() == [(name, current[name]) for name in ("alpha", "beta", "gamma")]
        after = {
            name: {
                table: connection.execute(
                    f"select * from {table} where result_id = ? order by {order_by[table]}",
                    [current[name]],
                ).fetchall()
                for table in child_tables
            }
            for name in ("alpha", "beta", "gamma")
        }
        assert connection.execute("select count(*) from optimizer_prepared_inputs").fetchone() == (3 - admitted_count,)
    for name in ("beta", "gamma")[admitted_count - 1:]:
        assert after[name] == before[name]
    for name in ("alpha", "beta")[:admitted_count]:
        assert after[name]["strategy_actions"] == before[name]["strategy_actions"]
        assert after[name]["strategy_equity"] == before[name]["strategy_equity"]
        assert after[name]["optimizer_prepared_inputs"] == []


def test_import_request_rejects_absolute_listing_dates_path(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)

    with pytest.raises(ValueError, match="relative"):
        PerformanceV2ImportRequest(
            request.inbox,
            request.report_root,
            request.config,
            listing_dates_path=tmp_path / "Input" / "dates.xlsx",
        )


def test_relative_input_listing_dates_path_is_resolved_from_inbox_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _request(tmp_path)
    dates_path = tmp_path / "Input" / "dates.xlsx"
    dates_path.parent.mkdir(exist_ok=True)
    dates_path.touch()
    seen: list[Path] = []
    monkeypatch.setattr(
        import_module,
        "load_listing_dates",
        lambda path: (seen.append(path), {"ONUSDT": datetime(2025, 12, 1, tzinfo=timezone.utc)})[1],
    )
    request = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        listing_dates_path=Path("Input/dates.xlsx"),
    )

    result = import_performance_v2(request)

    assert result.status == "COMMITTED"
    assert result.imported_count == 1
    assert seen == [dates_path]


def test_listing_dates_path_reports_missing_file_distinctly(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    request = PerformanceV2ImportRequest(
        request.inbox,
        request.report_root,
        request.config,
        listing_dates_path=Path("missing/dates.xlsx"),
    )

    with pytest.raises(PerformanceV2ImportError, match="not found"):
        import_performance_v2(request)


def test_listing_dates_path_reports_non_regular_file_distinctly(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    dates_path = tmp_path / "Input" / "dates.xlsx"
    dates_path.unlink()
    dates_path.mkdir()

    with pytest.raises(PerformanceV2ImportError, match="not a regular file"):
        import_performance_v2(request)


def test_valid_strategy_is_published_when_sibling_report_is_invalid(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    invalid = request.report_root / "beta.html"
    invalid.write_text("<html>invalid</html>", encoding="utf-8")
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    beta_entry = manifest["entries"][1]
    beta_entry["source_report_sha256"] = sha256(invalid.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = import_performance_v2(request)

    assert result.status == "COMMITTED"
    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert result.rejected_count == 1
    assert result.failure_report_path is not None and result.failure_report_path.is_file()
    assert result.failure_report_xlsx_path is not None and result.failure_report_xlsx_path.is_file()
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select status from import_runs").fetchone() == ("COMMITTED",)
        assert connection.execute("select strategy_name from strategies order by strategy_name").fetchall() == [("alpha",)]


def test_misaligned_equity_series_rejects_only_that_report(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    report_path = request.report_root / "beta.html"
    broken = report_path.read_bytes().replace(
        b'const equitySeries = [[1767225600000,"1000"],[1767229200000,"999.95"],[1767402000000,"1009.9"]];',
        b'const equitySeries = [[1767225600000,"1000"],[1767229200000,"999.95"]];',
    )
    report_path.write_bytes(broken)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][1]["source_report_sha256"] = sha256(broken).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = import_performance_v2(request)

    assert (result.imported_count, result.rejected_count) == (1, 1)
    assert result.failure_report_path is not None
    assert "wallet/equity sample counts must match" in result.failure_report_path.read_text(encoding="utf-8")


def test_misaligned_equal_length_equity_series_fails_with_deterministic_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    request, _ = _request(tmp_path)
    report_path = request.report_root / "alpha.html"
    original = FIXTURE.read_bytes()
    broken = original.replace(
        b'const equitySeries = [[1767225600000,"1000"],[1767229200000,"999.95"],[1767402000000,"1009.9"]];',
        b'const equitySeries = [[1767225600000,"1000"],[1767232800000,"999.95"],[1767402000000,"1009.9"]];',
    )
    report_path.write_bytes(broken)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = sha256(broken).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    original_parser = import_module.parse_current_performance_v2_html

    def parse_misaligned(data: bytes, config: PerformanceV2Config):
        parsed = original_parser(original if data == broken else data, config)
        if data == broken:
            parsed = replace(
                parsed,
                equity_series=tuple(
                    (timestamp + timedelta(hours=1), value) if index == 1 else (timestamp, value)
                    for index, (timestamp, value) in enumerate(parsed.equity_series)
                ),
            )
        return parsed

    monkeypatch.setattr(import_module, "parse_current_performance_v2_html", parse_misaligned)
    object.__setattr__(request, "config", replace(request.config, workers=1))

    with pytest.raises(PerformanceV2ImportError, match="wallet/equity timestamps are misaligned"):
        import_performance_v2(request)


def test_invalid_schema_fails_before_staging_or_audit(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, initialize_db=False)
    target = performance_v2_database_path(request.config)
    target.parent.mkdir(parents=True)
    with duckdb.connect(str(target)) as connection:
        connection.execute("create table not_v2 (value integer)")

    with pytest.raises(PerformanceV2ImportError, match="supported schema"):
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


def test_add_reuses_active_strategy_for_a_different_name(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1

    second, _ = _request(tmp_path / "second", names=("beta",))
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )
    result = import_performance_v2(second)

    assert (result.imported_count, result.skipped_count) == (0, 1)
    with duckdb.connect(str(performance_v2_database_path(first.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (1,)
        assert connection.execute("select strategy_name from strategies").fetchone() == ("alpha",)


def test_add_scopes_invalid_active_lookup_to_incoming_typed_base(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target)) as connection:
        connection.execute(
            """insert into strategies (
                strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                analysis_run_id, candidate_identity, lifecycle_status, current_result_id,
                created_at_utc, updated_at_utc
             ) values ('unrelated-legacy', 'OTHERUSDT', 'LONG', '1h', 3, 1,
                       'legacy-run', 'legacy-candidate', 'ACTIVE', null, now(), now())"""
        )

    second, _ = _request(tmp_path / "second", names=("beta",))
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )
    result = import_performance_v2(second)

    assert (result.imported_count, result.skipped_count) == (0, 1)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (2,)


def test_lot_x_is_part_of_the_canonical_key(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    second, _ = _request(tmp_path / "second", names=("beta",))
    strategy_path = second.inbox / "strategies" / "beta.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["mrs3"]["ma_long"][0]["lot_x"] = "0.75"  # type: ignore[index]
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path.write_bytes(strategy_bytes)
    manifest_path = second.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = _canonical_strategy_hash(strategy)
    manifest["entries"][0]["source_strategy_sha256"] = sha256(strategy_bytes).hexdigest()
    manifest["entries"][0]["strategy_version_id"] = digest
    manifest["v6_provenance"]["strategy_json_sha256"]["beta.json"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )

    assert import_performance_v2(first).imported_count == 1
    assert import_performance_v2(second).imported_count == 1
    with duckdb.connect(str(performance_v2_database_path(first.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (2,)
        assert {row[0] for row in connection.execute("select lot_x from strategy_orders").fetchall()} == {
            Decimal("1.000000000000"), Decimal("0.750000000000")
        }


def test_lot_x_key_survives_decimal_round_trip_at_schema_precision(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    strategy_path = request.inbox / "strategies" / "alpha.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["mrs3"]["ma_long"][0]["lot_x"] = "123.456789012345"  # type: ignore[index]
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path.write_bytes(strategy_bytes)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = _canonical_strategy_hash(strategy)
    manifest["entries"][0]["source_strategy_sha256"] = sha256(strategy_bytes).hexdigest()
    manifest["entries"][0]["strategy_version_id"] = digest
    manifest["v6_provenance"]["strategy_json_sha256"]["alpha.json"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert import_performance_v2(request).imported_count == 1
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        existing, orders, _results = import_module._load_existing(
            connection,
            ("alpha",),
            typed_prefixes=(import_module._typed_key(prepared.entries[0])[:5],),
        )
    row = existing["alpha"]
    assert import_module._stored_typed_key(row, orders[int(row[1])]) == import_module._typed_key(prepared.entries[0])


def test_add_replaces_canonical_result_only_for_a_strict_interval_superset(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        old_strategy_id, old_result_id, old_name = connection.execute(
            "select strategy_id, current_result_id, strategy_name from strategies"
        ).fetchone()
        old_orders = connection.execute(
            "select order_id, open_ma_len, open_multiplier, shift_bp, lot_x, plateau_id, base_point_trades "
            "from strategy_orders where strategy_id = ? order by order_id", [old_strategy_id]
        ).fetchall()
    second, _ = _request(tmp_path / "second", names=("beta",))
    report_path = second.report_root / "beta.html"
    report = report_path.read_bytes().replace(
        b"2026-01-01 - 2026-01-09", b"2025-12-20 - 2026-01-10"
    )
    report_path.write_bytes(report)
    manifest_path = second.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = sha256(report).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )

    result = import_performance_v2(second)

    assert (result.imported_count, result.skipped_count) == (1, 0)
    with duckdb.connect(str(target), read_only=True) as connection:
        new_strategy_id, new_result_id, new_name = connection.execute(
            "select strategy_id, current_result_id, strategy_name from strategies"
        ).fetchone()
        assert (new_strategy_id, new_name) == (old_strategy_id, old_name)
        assert new_result_id == old_result_id
        assert connection.execute(
            "select order_id, open_ma_len, open_multiplier, shift_bp, lot_x, plateau_id, base_point_trades "
            "from strategy_orders where strategy_id = ? order by order_id", [new_strategy_id]
        ).fetchall() == old_orders
        assert connection.execute("select count(*) from strategies").fetchone() == (1,)
        assert connection.execute("select count(*) from strategy_results").fetchone() == (1,)
        assert connection.execute("select report_end_utc from strategy_results").fetchone()[0].date().isoformat() == "2026-01-10"


def test_add_equal_reimport_stays_deduped_after_detail_delete_then_wider_rebuilds_details(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
        # The importer rebuilds its action/equity facts; optimizer inputs are
        # produced by a separate preparation path and are empty in this fixture.
        for table in ("strategy_actions", "strategy_equity"):
            assert connection.execute(f"select count(*) from {table} where result_id = ?", [result_id]).fetchone()[0] > 0
            connection.execute(f"delete from {table} where result_id = ?", [result_id])

    equal = import_performance_v2(request)
    assert (equal.imported_count, equal.skipped_count) == (0, 1)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (result_id,)
        for table in ("strategy_actions", "strategy_equity"):
            assert connection.execute(f"select count(*) from {table} where result_id = ?", [result_id]).fetchone() == (0,)

    wider = FIXTURE.read_bytes().replace(
        b"2026-01-01 - 2026-01-09", b"2025-12-20 - 2026-01-10"
    )
    _rewrite_report(request, wider)
    rebuilt = import_performance_v2(request)
    assert (rebuilt.imported_count, rebuilt.skipped_count) == (1, 0)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("select current_result_id from strategies where strategy_id = ?", [strategy_id]).fetchone() == (result_id,)
        for table in ("strategy_actions", "strategy_equity"):
            assert connection.execute(f"select count(*) from {table} where result_id = ?", [result_id]).fetchone()[0] > 0


def test_add_superset_does_not_clear_a_retest_tag(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute(
            "insert into strategy_tags values (?, 'RETEST', 'RETEST_WORKFLOW', 'test', now())",
            [strategy_id],
        )
    second, _ = _request(tmp_path / "second", names=("beta",))
    report_path = second.report_root / "beta.html"
    report = report_path.read_bytes().replace(b"2026-01-01 - 2026-01-09", b"2025-12-20 - 2026-01-10")
    report_path.write_bytes(report)
    manifest_path = second.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["source_report_sha256"] = sha256(report).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = import_performance_v2(PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        clear_retest_on_success=True, listing_dates_path=second.listing_dates_path,
    ))

    assert result.imported_count == 1
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select count(*) from strategy_tags where strategy_id = ? and tag = 'RETEST'", [strategy_id]
        ).fetchone() == (1,)


def test_typed_key_canonicalizes_order_slots_and_excludes_provenance(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, orders=2)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    entry = prepared.entries[0]
    reversed_orders = tuple(
        replace(order, order_id=3 - order.order_id)
        for order in reversed(entry.identity.orders)
    )
    alternate = replace(
        entry,
        strategy_name="other-name",
        analysis_run_id="other-run",
        candidate_identity="other-candidate",
        identity=replace(entry.identity, strategy_name="other-name", orders=reversed_orders),
    )

    assert import_module._typed_key(entry) == import_module._typed_key(alternate)


def test_interval_relation_requires_a_strict_proper_superset() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 10, tzinfo=timezone.utc)
    current = (start, end)

    assert import_module._interval_relation(current, current) == "EQUAL"
    assert import_module._interval_relation((start - timedelta(days=1), end), current) == "SUPERSET"
    assert import_module._interval_relation((start, end + timedelta(days=1)), current) == "SUPERSET"
    assert import_module._interval_relation((start + timedelta(hours=1), end), current) == "SKIP"
    assert import_module._interval_relation((start - timedelta(days=1), end - timedelta(hours=1)), current) == "SKIP"
    assert import_module._interval_relation((None, end), current) == "UNKNOWN"
    assert import_module._interval_relation(current, (start, None)) == "UNKNOWN"


def test_lot_quantization_is_shared_for_float_and_decimal_values() -> None:
    assert import_module._quantized_lot(0.87524499704) == Decimal("0.875244997040")
    assert import_module._quantized_lot(Decimal("0.875244997040000")) == Decimal("0.875244997040")
    assert import_module._quantized_lot(Decimal("-0.0000000000001")) == Decimal("0.000000000000")
    assert import_module._quantized_lot(Decimal("99999999999999999999999999.999999999999")) == Decimal(
        "99999999999999999999999999.999999999999"
    )


def test_comparison_interval_rejects_naive_listing_timestamp(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    report = parse_current_performance_v2_html(FIXTURE.read_bytes(), request.config)
    naive = replace(report, listing_date_utc=datetime(2025, 12, 25))
    assert import_module._comparison_interval(naive) is None


def test_add_fails_closed_when_active_typed_configuration_is_unreadable(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute("delete from strategy_orders where strategy_id = ?", [strategy_id])

    second, _ = _request(tmp_path / "second", names=("beta",))
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )
    with pytest.raises(PerformanceV2ImportError, match="invalid typed configuration"):
        import_performance_v2(second)


def test_add_fails_closed_when_stored_multiplier_disagrees_with_shift(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target)) as connection:
        connection.execute("update strategy_orders set open_multiplier = 0.9")
    second, _ = _request(tmp_path / "second", names=("beta",))

    with pytest.raises(PerformanceV2ImportError, match="invalid typed configuration"):
        import_performance_v2(PerformanceV2ImportRequest(
            second.inbox, second.report_root, first.config,
            listing_dates_path=second.listing_dates_path,
        ))


def test_add_fails_closed_when_active_current_result_is_dangling(tmp_path: Path) -> None:
    first, _ = _request(tmp_path)
    assert import_performance_v2(first).imported_count == 1
    target = performance_v2_database_path(first.config)
    with duckdb.connect(str(target)) as connection:
        strategy_id = connection.execute("select strategy_id from strategies").fetchone()[0]
        connection.execute("update strategies set current_result_id = 999999 where strategy_id = ?", [strategy_id])

    second, _ = _request(tmp_path / "second", names=("beta",))
    second = PerformanceV2ImportRequest(
        second.inbox, second.report_root, first.config,
        listing_dates_path=second.listing_dates_path,
    )
    with pytest.raises(PerformanceV2ImportError, match="no current result"):
        import_performance_v2(second)


@pytest.mark.parametrize(
    "expected",
    ["not-a-mapping", {}, {"symbol": "ONUSDT"}, {"symbol": "ONUSDT", "orders": None}],
)
def test_replace_rejects_malformed_expected_identity(tmp_path: Path, expected: object) -> None:
    request, _ = _request(tmp_path)
    assert import_performance_v2(request).imported_count == 1
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config,
        mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
        expected_strategy_identities={"alpha": expected},
        listing_dates_path=request.listing_dates_path,
    )
    with pytest.raises(PerformanceV2ImportError, match="typed strategy mismatch"):
        import_performance_v2(replacement)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_id]
        ).fetchone() == (result_id,)


def test_replace_rejects_complete_expected_identity_with_bad_order(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    assert import_performance_v2(request).imported_count == 1
    prepared = read_performance_v2_inbox(request.inbox, request.report_root)
    entry = prepared.entries[0]
    target = performance_v2_database_path(request.config)
    with duckdb.connect(str(target), read_only=True) as connection:
        strategy_id, result_id = connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_name = 'alpha'"
        ).fetchone()
    order = entry.identity.orders[0]
    expected = {
        "symbol": entry.identity.symbol,
        "side": entry.identity.side,
        "timeframe": entry.identity.timeframe,
        "close_ma_len": entry.identity.close_ma_len,
        "order_count": entry.identity.order_count,
        "orders": [{"open_ma_len": order.open_ma_len, "shift_bp": order.shift_bp + 1, "lot_x": str(order.lot_x)}],
    }
    replacement = PerformanceV2ImportRequest(
        request.inbox, request.report_root, request.config,
        mode="REPLACE", replacement_strategy_ids={"alpha": strategy_id},
        expected_strategy_identities={"alpha": expected},
        listing_dates_path=request.listing_dates_path,
    )
    with pytest.raises(PerformanceV2ImportError, match="typed strategy mismatch"):
        import_performance_v2(replacement)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategy_id]
        ).fetchone() == (result_id,)


def test_same_batch_equal_typed_entries_keep_manifest_first(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    strategy_path = request.inbox / "strategies" / "beta.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["mrs3"]["ma_close_long"]["len"] = 3  # type: ignore[index]
    strategy["mrs3"]["ma_close_short"]["len"] = 3  # type: ignore[index]
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path.write_bytes(strategy_bytes)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    beta = manifest["entries"][1]
    beta["source_strategy_sha256"] = sha256(strategy_bytes).hexdigest()
    beta["strategy_version_id"] = _canonical_strategy_hash(strategy)
    manifest["v6_provenance"]["strategy_json_sha256"]["beta.json"] = beta["strategy_version_id"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = import_performance_v2(request)

    assert (result.imported_count, result.skipped_count) == (1, 1)
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select strategy_name from strategies").fetchone() == ("alpha",)


def test_same_batch_incomparable_typed_intervals_roll_back(tmp_path: Path) -> None:
    request, _ = _request(tmp_path, names=("alpha", "beta"))
    strategy_path = request.inbox / "strategies" / "beta.json"
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
    strategy["mrs3"]["ma_close_long"]["len"] = 3  # type: ignore[index]
    strategy["mrs3"]["ma_close_short"]["len"] = 3  # type: ignore[index]
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path.write_bytes(strategy_bytes)
    manifest_path = request.inbox / "inbox_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][1]["source_strategy_sha256"] = sha256(strategy_bytes).hexdigest()
    manifest["entries"][1]["strategy_version_id"] = _canonical_strategy_hash(strategy)
    manifest["v6_provenance"]["strategy_json_sha256"]["beta.json"] = manifest["entries"][1]["strategy_version_id"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for name, replacement in (("alpha", b"2025-12-20 - 2026-01-09"),
                              ("beta", b"2026-01-01 - 2026-01-20")):
        report_path = request.report_root / f"{name}.html"
        report_path.write_bytes(FIXTURE.read_bytes().replace(b"2026-01-01 - 2026-01-09", replacement))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        index = 0 if name == "alpha" else 1
        manifest["entries"][index]["source_report_sha256"] = sha256(report_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PerformanceV2ImportError, match="incomparable"):
        import_performance_v2(request)
    with duckdb.connect(str(performance_v2_database_path(request.config)), read_only=True) as connection:
        assert connection.execute("select count(*) from strategies").fetchone() == (0,)
