from __future__ import annotations

from decimal import Decimal
import json
from hashlib import sha256
from pathlib import Path
import struct
import zlib

import pandas as pd
import pytest

import mrs3.duckdb_events as duckdb_events
import mrs3.source_packs as source_packs
from mrs3.config import AlgorithmConfig
from mrs3.models import Side
from mrs3.package_loader import load_package
from mrs3.source_packs import SourcePackError, build_csv_package, require_single_event_mode
from mrs3.duckdb_events import (
    build_duckdb_package,
    calculate_point_metrics,
    decode_compact_actions,
    decode_compact_deltas,
    decode_wallet_changes,
    reconstruct_closed_cycles,
)


WINDOW_START = "2026-07-15T00:00:00Z"
WINDOW_END = "2026-08-06T00:00:00Z"


def _write_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_csv_package_keeps_exact_window_and_maps_trades_to_point_events(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [
            {"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7},
            {"StartDate": "2026-07-16 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 99},
        ],
    )

    package = build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")

    points = pd.read_csv(package.points_csv)
    assert points[["event_mode", "point_event_count", "event_ids_hash"]].to_dict("records") == [
        {"event_mode": "legacy_trades_proxy", "point_event_count": 7, "event_ids_hash": "LEGACY_PROXY_NO_EVENT_IDS"}
    ]
    assert package.manifest["accepted_rows"] == 1
    assert package.manifest["rejected_rows"] == 1
    assert json.loads(package.manifest_path.read_text(encoding="utf-8"))["event_mode"] == "legacy_trades_proxy"


def test_csv_package_audits_non_exact_period_rows(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "short.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-05 00:00:00", "TotalTrades": 7}],
    )

    package = build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")

    audit = pd.read_csv(package.audit_csv)
    assert audit[["status", "reason"]].to_dict("records") == [
        {"status": "REJECTED", "reason": "PERIOD_NOT_EXACT"}
    ]


def test_mixed_event_modes_are_rejected() -> None:
    with pytest.raises(SourcePackError, match="mixed event modes"):
        require_single_event_mode(pd.DataFrame({"event_mode": ["legacy_trades_proxy", "real_independent_events"]}))


def test_missing_event_mode_is_rejected() -> None:
    with pytest.raises(SourcePackError, match="missing event mode"):
        require_single_event_mode(pd.DataFrame({"event_mode": ["legacy_trades_proxy", None]}))


def test_csv_package_rejects_fractional_total_trades(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "fractional.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 3.5}],
    )

    with pytest.raises(SourcePackError, match="non-negative integers"):
        build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")


def test_csv_package_manifest_keeps_generator_source_hashes(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7}],
    )

    package = build_csv_package((path for path in [source]), WINDOW_START, WINDOW_END, tmp_path / "package")

    assert package.manifest["source_files"] == [{"name": "long.csv", "sha256": package.manifest["source_files"][0]["sha256"]}]


def test_csv_package_cleans_staging_after_write_failure(tmp_path: Path, monkeypatch) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7}],
    )
    original = pd.DataFrame.to_csv
    calls = 0

    def fail_second_csv(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(source_packs.pd.DataFrame, "to_csv", fail_second_csv)

    with pytest.raises(OSError, match="disk full"):
        build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")
    assert not (tmp_path / "package").exists()


def _actions_blob(rows: list[dict[str, str]]) -> bytes:
    for row in rows:
        if "Side" not in row:
            row["Side"] = "buy" if row["Action"] == "opened" else "sell"
    headers = list(rows[0])
    return zlib.compress(json.dumps({"headers": headers, "rows": [[row[header] for header in headers] for row in rows]}).encode("utf-8"))


def _compact_delta_blob(deltas: list[int]) -> bytes:
    return zlib.compress(struct.pack(f"<{len(deltas)}q", *deltas))


def _wallet_changes_blob(changes: list[tuple[int, int]]) -> bytes:
    return zlib.compress(b"".join(struct.pack("<Iq", index, value) for index, value in changes))


def _series_blob(values: list[int]) -> bytes:
    return _compact_delta_blob([values[0], *(right - left for left, right in zip(values, values[1:]))])


def _settings(symbol: str, open_ma: int) -> str:
    return json.dumps(
        {
            "basic": {"symbol": symbol, "time_frame": "15m", "use_long": True, "use_short": False},
            "mrs2": {
                "ma_long": {"len": open_ma, "multiplier": 0.99},
                "ma_close_long": {"len": 9},
                "ma_short": {"len": open_ma + 20, "multiplier": 1.01},
                "ma_close_short": {"len": 29},
            },
        }
    )


def _v4_database(tmp_path: Path, report_count: int = 3, *, duplicate_point: bool = False) -> Path:
    import duckdb

    database = tmp_path / "source.duckdb"
    start = pd.Timestamp(WINDOW_START).value // 1_000_000
    end = pd.Timestamp(WINDOW_END).value // 1_000_000
    hour = 60 * 60 * 1000
    timestamps = [start, start + hour, start + 2 * hour, start + 3 * hour, start + 4 * hour, end]
    scale = 100_000_000
    equity = [1000 * scale, 1010 * scale, 100_499_600_000, 1008 * scale, 100_500_400_000, 100_500_400_000]
    actions = [
        {"Timestamp": "2026-07-15 01:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
        {"Timestamp": "2026-07-15 02:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "10"},
        {"Timestamp": "2026-07-15 03:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy", "PnL": "0"},
        {"Timestamp": "2026-07-15 04:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "-4.996"},
    ]
    con = duckdb.connect(str(database))
    con.execute("create table schema_info(key varchar, value varchar)")
    con.execute("insert into schema_info values ('schema_version', '4')")
    con.execute(
        """create table point_configs(
               point_id varchar, symbol varchar, side varchar, timeframe varchar,
               open_ma_type varchar, open_ma_source varchar, open_ma_len integer,
               open_multiplier varchar, close_ma_type varchar, close_ma_source varchar,
               close_ma_len integer)"""
    )
    con.execute(
        """create table time_grids(
               grid_id varchar, sample_count integer, start_timestamp_ms bigint,
               end_timestamp_ms bigint, timestamps_zlib blob)"""
    )
    con.execute(
        """create table report_runs(
               report_id varchar, source_sha256 varchar, canonical_key varchar,
               point_id varchar, grid_id varchar, source_file varchar, source_size bigint,
               imported_at_utc timestamp, settings_json varchar, raw_action_count integer,
               equity_sample_count integer, wallet_change_count integer)"""
    )
    con.execute(
        """create table report_payloads(
               report_id varchar, series_codec varchar, actions_codec varchar,
               actions_zlib blob, equity_zlib blob, wallet_zlib blob)"""
    )
    for index in range(report_count):
        point_number = 1 if duplicate_point and index == report_count - 1 else index + 1
        point_id = f"P{point_number}"
        symbol = f"A{point_number:02d}USDT"
        grid_id = f"G{index + 1}"
        report_id = f"R{index + 1}"
        report_actions = [dict(action, Symbol=symbol) for action in actions]
        if not (duplicate_point and index == report_count - 1):
            con.execute(
                "insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?)",
                [point_id, symbol, "LONG", "15m", "ema", "close", 3 + point_number, "0.99", "ema", "close", 9],
            )
        con.execute("insert into time_grids values (?,?,?,?,?)", [grid_id, len(timestamps), start, end, _series_blob(timestamps)])
        con.execute(
            "insert into report_runs values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [report_id, f"hash-{report_id}", f"key-{report_id}", point_id, grid_id, f"C:/source/report-{index + 1}.html", 1, "2026-08-10", _settings(symbol, 3 + point_number), len(report_actions), len(equity), 3],
        )
        con.execute(
            "insert into report_payloads values (?,?,?,?,?,?)",
            [report_id, "zlib-int64-delta-v1", "zlib-columnar-json-v1", _actions_blob(report_actions), _series_blob(equity), _wallet_changes_blob([(0, 1000 * scale), (2, 1010 * scale), (4, 100_500_400_000)])],
        )
    con.close()
    return database


def _verification_html(
    root: Path,
    report_count: int,
    *,
    database: Path | None = None,
    mismatch_report: int | None = None,
) -> Path:
    root.mkdir()
    for index in range(1, report_count + 1):
        pnl = "6.00" if index == mismatch_report else "5.00"
        (root / f"report-{index}.html").write_text(
            "<html><body><table>"
            f"<tr><th>PnL</th><td>{pnl}</td></tr>"
            "<tr><th>DD</th><td>5.00</td></tr>"
            "<tr><th>TotalTrades</th><td>2</td></tr>"
            "<tr><th>WinRate</th><td>50.00%</td></tr>"
            "<tr><th>ProfitFactor</th><td>2.00</td></tr>"
            "</table></body></html>",
            encoding="utf-8",
        )
    if database is not None:
        import duckdb

        con = duckdb.connect(str(database))
        for index in range(1, report_count + 1):
            source = root / f"report-{index}.html"
            con.execute(
                "update report_runs set source_sha256=? where report_id=?",
                [sha256(source.read_bytes()).hexdigest(), f"R{index}"],
            )
        con.close()
    return root


def test_html_summary_reads_actual_profit_factor_label_and_absolute_drawdown(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<html><body><table>"
        "<tr><th>Total PnL</th><td>5.00</td></tr>"
        "<tr><th>Total PnL, %</th><td>0.50%</td></tr>"
        "<tr><th>Max Drawdown</th><td>5.00</td></tr>"
        "<tr><th>Max Drawdown, %</th><td>0.50%</td></tr>"
        "<tr><th>Total Trades</th><td>2</td></tr>"
        "<tr><th>Win Rate</th><td>50.00%</td></tr>"
        "<tr><th>Profit Factor (gross profit/gross loss)</th><td>2.1234</td></tr>"
        "</table></body></html>",
        encoding="utf-8",
    )

    summary = duckdb_events._html_summary(report)

    assert {metric: value for metric, (_, value, _) in summary.items()} == {
        "PnL": 5,
        "DD": 5,
        "TotalTrades": 2,
        "WinRate": 50,
        "ProfitFactor": Decimal("2.1234"),
    }
    assert summary["ProfitFactor"][2] == 4


def test_full_horizon_html_evidence_does_not_compare_the_selected_window(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<table><tr><th>Total PnL</th><td>12</td></tr>"
        "<tr><th>Max Drawdown</th><td>5</td></tr>"
        "<tr><th>Total Trades</th><td>3</td></tr>"
        "<tr><th>Win Rate</th><td>66.67%</td></tr>"
        "<tr><th>Profit Factor</th><td>3</td></tr></table>",
        encoding="utf-8",
    )
    source_metrics = {
        "TotalPnL": 12, "MaxDrawdown": 5, "TotalTrades": 3,
        "WinRate": 66.666666, "ProfitFactor": 3,
    }
    window_metrics = {
        "TotalPnL": 5, "MaxDrawdown": 2, "TotalTrades": 2,
        "WinRate": 50.0, "ProfitFactor": 2,
    }

    rows, status, cause = duckdb_events._verification(
        [
            {
                "report_id": f"R{index}", "source_file": "report.html",
                "source_sha256": sha256(report.read_bytes()).hexdigest(),
                "source_metrics": source_metrics, "metrics": window_metrics,
            }
            for index in range(1, 4)
        ],
        tmp_path,
        3,
    )

    assert status == "VERIFIED"
    assert cause == ""
    assert {row["calculated_value"] for row in rows if row["metric"] == "PnL"} == {12}


@pytest.mark.parametrize(
    ("source_sha256", "source_range_start", "cause"),
    [
        ("0" * 64, WINDOW_START, "SOURCE_IDENTITY_MISMATCH"),
        (None, "2026-07-15T00:00:01Z", "SOURCE_RANGE_DOES_NOT_CONTAIN_WINDOW"),
    ],
)
def test_full_horizon_html_evidence_fails_closed_on_identity_or_window_range(
    tmp_path: Path, source_sha256: str | None, source_range_start: str, cause: str
) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<table><tr><th>Total PnL</th><td>5</td></tr>"
        "<tr><th>Max Drawdown</th><td>5</td></tr>"
        "<tr><th>Total Trades</th><td>2</td></tr>"
        "<tr><th>Win Rate</th><td>50%</td></tr>"
        "<tr><th>Profit Factor</th><td>2</td></tr></table>",
        encoding="utf-8",
    )
    metrics = {"TotalPnL": 5, "MaxDrawdown": 5, "TotalTrades": 2, "WinRate": 50, "ProfitFactor": 2}
    rows, status, actual_cause = duckdb_events._verification(
        [
            {
                "report_id": f"R{index}", "source_file": "report.html",
                "source_sha256": source_sha256 or sha256(report.read_bytes()).hexdigest(),
                "source_metrics": metrics,
                "source_range_start": source_range_start,
                "source_range_end": WINDOW_END,
            }
            for index in range(1, 4)
        ],
        tmp_path,
        3,
        pd.Timestamp(WINDOW_START),
        pd.Timestamp(WINDOW_END),
    )

    assert status == "UNVERIFIED"
    assert actual_cause == cause
    assert {row["cause"] for row in rows} == {cause}


def test_duckdb_metric_materializer_decodes_series_and_counts_realised_actions() -> None:
    grid = [
        "2026-07-15T00:00:00Z",
        "2026-07-15T01:00:00Z",
        "2026-07-15T02:00:00Z",
        "2026-07-15T03:00:00Z",
        "2026-07-15T04:00:00Z",
    ]
    actions = (
        {"Timestamp": "2026-07-15T01:00:00Z", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy"},
        {"Timestamp": "2026-07-15T02:00:00Z", "Symbol": "AAAUSDT", "Action": "decreased", "Post Side": "long", "Side": "sell", "PnL": "5"},
        {"Timestamp": "2026-07-15T03:00:00Z", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "0"},
        {"Timestamp": "2026-07-15T03:30:00Z", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy"},
        {"Timestamp": "2026-07-15T04:00:00Z", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "100"},
    )

    equity = decode_compact_deltas(_compact_delta_blob([1000, 10, -6, 1, 0]), expected_count=5)
    wallet_changes = decode_wallet_changes(_wallet_changes_blob([(0, 1000), (2, 1005)]), expected_count=2)

    metrics = calculate_point_metrics(
        grid, equity, wallet_changes, actions, "2026-07-15T01:00:00Z", "2026-07-15T04:00:00Z"
    )

    assert equity == (1000, 1010, 1004, 1005, 1005)
    assert metrics == {
        "TotalPnL": 5,
        "TotalPnLPercent": 0.5,
        "MaxDrawdown": 6,
        "MaxDrawdownPercent": pytest.approx(0.594059405940594),
        "TotalTrades": 2,
        "Win": 1,
        "Los": 0,
        "WinRate": 50.0,
        "ProfitFactor": None,
        "flat_trades": 1,
    }


def test_duckdb_metric_materializer_rejects_a_non_covering_grid() -> None:
    with pytest.raises(SourcePackError, match="grid does not cover"):
        calculate_point_metrics(
            ["2026-07-15T01:00:00Z", "2026-07-15T02:00:00Z"],
            [1000, 1001],
            [(0, 1000)],
            (),
            "2026-07-15T00:00:00Z",
            "2026-07-15T02:00:00Z",
        )


def test_duckdb_metric_materializer_rejects_unknown_series_codecs() -> None:
    with pytest.raises(SourcePackError, match="unsupported equity codec"):
        decode_compact_deltas(b"", expected_count=0, codec="unknown")
    with pytest.raises(SourcePackError, match="unsupported wallet codec"):
        decode_wallet_changes(b"", expected_count=0, codec="unknown")


def test_duckdb_metric_materializer_keeps_same_side_cycles_separate_by_symbol() -> None:
    metrics = calculate_point_metrics(
        [
            "2026-07-15T00:00:00Z",
            "2026-07-15T01:00:00Z",
            "2026-07-15T02:00:00Z",
            "2026-07-15T03:00:00Z",
            "2026-07-15T04:00:00Z",
        ],
        [1000, 1000, 1000, 1000, 1000],
        [(0, 1000)],
        (
            {"Timestamp": "2026-07-15T00:30:00Z", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy"},
            {"Timestamp": "2026-07-15T01:00:00Z", "Symbol": "BBBUSDT", "Action": "opened", "Post Side": "long", "Side": "buy"},
            {"Timestamp": "2026-07-15T02:00:00Z", "Symbol": "BBBUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "-5"},
            {"Timestamp": "2026-07-15T03:00:00Z", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell", "PnL": "100"},
        ),
        "2026-07-15T01:00:00Z",
        "2026-07-15T04:00:00Z",
    )

    assert metrics["TotalTrades"] == 1
    assert metrics["Win"] == 0
    assert metrics["Los"] == 1
    assert metrics["ProfitFactor"] == 0.0


def test_duckdb_metric_materializer_requires_action_identity_fields() -> None:
    with pytest.raises(SourcePackError, match="required action columns"):
        calculate_point_metrics(
            ["2026-07-15T00:00:00Z", "2026-07-15T01:00:00Z", "2026-07-15T02:00:00Z"],
            [1000, 1000, 1000],
            [(0, 1000)],
            ({"Timestamp": "2026-07-15T01:00:00Z", "Action": "opened", "Post Side": "long", "Side": "buy"},),
            "2026-07-15T01:00:00Z",
            "2026-07-15T02:00:00Z",
        )


def test_duckdb_cycles_count_only_closed_events_inside_half_open_window() -> None:
    rows = [
        {"Timestamp": "2026-07-15 01:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long"},
        {"Timestamp": "2026-07-15 02:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": ""},
        {"Timestamp": "2026-07-15 03:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long"},
        {"Timestamp": "2026-07-14 23:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long"},
        {"Timestamp": "2026-07-15 04:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": ""},
        {"Timestamp": "2026-08-05 23:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long"},
        {"Timestamp": "2026-08-06 00:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": ""},
    ]

    actions = decode_compact_actions(_actions_blob(rows), expected_count=len(rows))
    result = reconstruct_closed_cycles(
        report_id="report-1",
        symbol="AAAUSDT",
        timeframe="15m",
        actions=actions,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert len(result.included) == 1
    assert result.exclusions == {"NO_CLOSE": 1, "OPEN_BEFORE_WINDOW": 1, "CLOSE_ON_OR_AFTER_WINDOW": 1}
    assert result.included[0].event_id == reconstruct_closed_cycles(
        "report-1", "AAAUSDT", "15m", actions, WINDOW_START, WINDOW_END
    ).included[0].event_id


def test_duckdb_package_is_selector_complete_and_keeps_sorted_event_mapping(tmp_path: Path) -> None:
    database = _v4_database(tmp_path)
    before = sha256(database.read_bytes()).hexdigest()
    package = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")

    points = pd.read_csv(package.points_csv)
    config = AlgorithmConfig.defaults()
    expected_columns = {
        *config.base_columns.values(),
        *(column for columns in config.side_columns.values() for column in columns.values()),
        "point_id",
        "event_mode",
        "point_event_count",
        "event_ids_hash",
        "window_metrics_status",
    }
    assert expected_columns.issubset(points.columns)
    assert points[["event_mode", "point_event_count", "window_metrics_status"]].to_dict("records") == [
        {"event_mode": "real_independent_events", "point_event_count": 2, "window_metrics_status": "UNVERIFIED_SOURCE_SUMMARY"},
        {"event_mode": "real_independent_events", "point_event_count": 2, "window_metrics_status": "UNVERIFIED_SOURCE_SUMMARY"},
        {"event_mode": "real_independent_events", "point_event_count": 2, "window_metrics_status": "UNVERIFIED_SOURCE_SUMMARY"},
    ]
    events = pd.read_csv(package.directory / "point_events.csv")
    assert events.to_records(index=False).tolist() == sorted(events.to_records(index=False).tolist())
    assert events.groupby("point_id")["event_id"].nunique().tolist() == [2, 2, 2]
    audit = pd.read_csv(package.audit_csv)
    assert audit["included_cycles"].tolist() == [2, 2, 2]
    assert audit["flat_trades"].tolist() == [0, 0, 0]
    assert package.manifest["included_cycles"] == 6
    assert package.manifest["package_version"] == 2
    assert package.manifest["source_summary_status"] == "UNVERIFIED"
    assert package.manifest["window_metrics_status"] == "UNVERIFIED_SOURCE_SUMMARY"
    assert sha256(database.read_bytes()).hexdigest() == before


def test_duckdb_package_excludes_non_covering_reports_and_keeps_empty_points_schema(tmp_path: Path) -> None:
    import duckdb

    database = _v4_database(tmp_path)
    start = pd.Timestamp(WINDOW_START).value // 1_000_000
    hour = 60 * 60 * 1000
    short_grid = [start + hour * index for index in range(6)]
    con = duckdb.connect(str(database))
    con.execute(
        "update time_grids set end_timestamp_ms=?, timestamps_zlib=?",
        [short_grid[-1], _series_blob(short_grid)],
    )
    con.close()

    package = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")

    assert pd.read_csv(package.points_csv).empty
    assert pd.read_csv(package.audit_csv)["coverage_status"].tolist() == ["REJECTED"] * 3
    assert package.manifest["coverage_accepted_reports"] == 0
    assert package.manifest["coverage_rejected_reports"] == 3


def test_duckdb_package_audits_noncovering_reports_without_decoding_their_series(tmp_path: Path) -> None:
    import duckdb

    database = _v4_database(tmp_path)
    baseline = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "baseline")
    start = pd.Timestamp(WINDOW_START).value // 1_000_000
    end = pd.Timestamp(WINDOW_END).value // 1_000_000
    con = duckdb.connect(str(database))
    con.execute(
        "update time_grids set start_timestamp_ms=?, end_timestamp_ms=?, timestamps_zlib=? where grid_id='G2'",
        [start + 1, end - 1, b"not a compressed timestamp series"],
    )
    con.execute(
        "update report_payloads set equity_zlib=?, wallet_zlib=? where report_id='R2'",
        [b"not a compressed equity series", b"not a compressed wallet series"],
    )
    con.close()

    package = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")

    expected_points = pd.read_csv(baseline.points_csv).query("point_id != 'A02USDT|LONG|15m|100|5|9'").reset_index(drop=True)
    actual_points = pd.read_csv(package.points_csv).reset_index(drop=True)
    pd.testing.assert_frame_equal(actual_points, expected_points)
    audit = pd.read_csv(package.audit_csv).set_index("report_id")
    assert audit.loc["R2", ["coverage_status", "coverage_reason", "raw_action_count", "reconstructed_cycles", "included_cycles"]].to_dict() == {
        "coverage_status": "REJECTED",
        "coverage_reason": "GRID_NOT_COVERED",
        "raw_action_count": 4,
        "reconstructed_cycles": 2,
        "included_cycles": 2,
    }


def test_duckdb_package_audits_every_report_across_action_batches(tmp_path: Path) -> None:
    database = _v4_database(tmp_path, report_count=501)

    package = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")

    audit = pd.read_csv(package.audit_csv)
    assert len(audit) == 501
    assert audit["report_id"].tolist() == sorted(f"R{index}" for index in range(1, 502))
    assert package.manifest["report_count"] == 501
    assert package.manifest["coverage_accepted_reports"] == 501
    assert package.manifest["included_cycles"] == 1002


def test_duckdb_package_marks_window_points_derived_after_three_matching_html_reports(tmp_path: Path) -> None:
    database = _v4_database(tmp_path)
    html_root = _verification_html(tmp_path / "html", 3, database=database)

    package = build_duckdb_package(
        database,
        WINDOW_START,
        WINDOW_END,
        tmp_path / "package",
        verification_html_root=html_root,
        verification_sample_count=3,
    )

    assert package.manifest["package_version"] == 2
    assert package.manifest["source_summary_status"] == "VERIFIED"
    assert package.manifest["window_metrics_status"] == "DERIVED_FROM_VERIFIED_SOURCE"
    assert pd.read_csv(package.points_csv)["window_metrics_status"].tolist() == ["DERIVED_FROM_VERIFIED_SOURCE"] * 3
    verification = pd.read_csv(package.directory / "metric_verification.csv")
    assert verification["report_id"].drop_duplicates().tolist() == ["R1", "R2", "R3"]
    assert verification["metric"].unique().tolist() == ["PnL", "DD", "TotalTrades", "WinRate", "ProfitFactor"]
    assert set(verification["comparison"]) == {"EQUAL"}
    assert str(html_root) not in package.manifest_path.read_text(encoding="utf-8")


def test_real_v2_materializer_output_passes_the_selector_evidence_gate(tmp_path: Path) -> None:
    database = _v4_database(tmp_path)
    html_root = _verification_html(tmp_path / "html", 3, database=database)
    package = build_duckdb_package(
        database,
        WINDOW_START,
        WINDOW_END,
        tmp_path / "package",
        verification_html_root=html_root,
        verification_sample_count=3,
    )
    dates = tmp_path / "bybit_dates.csv"
    pd.DataFrame(
        [{"ticker": f"A{index:02d}USDT", "launch": "2026-07-01"} for index in range(1, 4)]
    ).to_csv(dates, index=False)

    loaded = load_package(package.directory, dates, Side.LONG, AlgorithmConfig.defaults())

    assert loaded.event_mode == "real_independent_events"
    assert len(loaded.points) == 3


def test_duckdb_package_marks_every_point_unverified_when_one_html_metric_mismatches(tmp_path: Path) -> None:
    database = _v4_database(tmp_path)
    html_root = _verification_html(
        tmp_path / "html", 3, database=database, mismatch_report=2
    )

    package = build_duckdb_package(
        database,
        WINDOW_START,
        WINDOW_END,
        tmp_path / "package",
        verification_html_root=html_root,
        verification_sample_count=3,
    )

    assert package.manifest["source_summary_status"] == "UNVERIFIED"
    assert pd.read_csv(package.points_csv)["window_metrics_status"].tolist() == ["UNVERIFIED_SOURCE_SUMMARY"] * 3
    verification = pd.read_csv(package.directory / "metric_verification.csv")
    mismatch = verification.loc[verification["comparison"] == "MISMATCH"]
    assert mismatch[["report_id", "metric", "cause"]].to_dict("records") == [
        {"report_id": "R2", "metric": "PnL", "cause": "VALUE_MISMATCH"}
    ]


def test_duckdb_package_marks_every_point_unverified_when_requested_html_is_absent(tmp_path: Path) -> None:
    database = _v4_database(tmp_path)
    html_root = _verification_html(tmp_path / "html", 3, database=database)
    (html_root / "report-3.html").unlink()

    package = build_duckdb_package(
        database,
        WINDOW_START,
        WINDOW_END,
        tmp_path / "package",
        verification_html_root=html_root,
        verification_sample_count=3,
    )

    assert package.manifest["source_summary_status"] == "UNVERIFIED"
    assert set(pd.read_csv(package.points_csv)["window_metrics_status"]) == {"UNVERIFIED_SOURCE_SUMMARY"}
    verification = pd.read_csv(package.directory / "metric_verification.csv")
    assert verification.loc[verification["cause"] == "SOURCE_HTML_MISSING", "report_id"].tolist() == ["R3"]


@pytest.mark.parametrize(("sample_count", "report_count"), [(1, 3), (2, 3), (6, 6)])
def test_duckdb_package_rejects_verification_sample_counts_outside_three_to_five(
    tmp_path: Path, sample_count: int, report_count: int
) -> None:
    database = _v4_database(tmp_path, report_count)
    html_root = _verification_html(tmp_path / "html", report_count, database=database)

    package = build_duckdb_package(
        database,
        WINDOW_START,
        WINDOW_END,
        tmp_path / "package",
        verification_html_root=html_root,
        verification_sample_count=sample_count,
    )

    assert package.manifest["source_summary_status"] == "UNVERIFIED"
    assert package.manifest["source_summary_cause"] == "SAMPLE_COUNT_OUT_OF_RANGE"
    assert set(pd.read_csv(package.points_csv)["window_metrics_status"]) == {"UNVERIFIED_SOURCE_SUMMARY"}


def test_duckdb_package_rejects_duplicate_point_ids(tmp_path: Path) -> None:
    database = _v4_database(tmp_path, 4, duplicate_point=True)

    with pytest.raises(SourcePackError, match="duplicate point_id.*P1"):
        build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")


def test_duckdb_cycles_match_concurrent_position_sides_and_reject_malformed_action() -> None:
    rows = [
        {"Timestamp": "2026-07-15 01:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long", "Side": "buy"},
        {"Timestamp": "2026-07-15 01:01:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "short", "Side": "sell"},
        {"Timestamp": "2026-07-15 02:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "sell"},
        {"Timestamp": "2026-07-15 02:01:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": "", "Side": "buy"},
    ]
    actions = decode_compact_actions(_actions_blob(rows), len(rows))
    result = reconstruct_closed_cycles("R", "AAAUSDT", "15m", actions, WINDOW_START, WINDOW_END)

    assert {cycle.position_side for cycle in result.included} == {"long", "short"}
    with pytest.raises(SourcePackError, match="required action columns"):
        reconstruct_closed_cycles("R", "AAAUSDT", "15m", ({"Timestamp": "2026-07-15"},), WINDOW_START, WINDOW_END)
