from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import struct
import zlib

import pandas as pd
import pytest

import mrs3.source_packs as source_packs
from mrs3.source_packs import SourcePackError, build_csv_package, require_single_event_mode
from mrs3.duckdb_events import (
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


def test_duckdb_package_is_read_only_and_audits_reconstructed_events(tmp_path: Path) -> None:
    import duckdb
    from mrs3.duckdb_events import build_duckdb_package

    database = tmp_path / "source.duckdb"
    con = duckdb.connect(str(database))
    con.execute("create table schema_info(key varchar, value varchar)")
    con.execute("insert into schema_info values ('schema_version', '4')")
    con.execute("create table point_configs(point_id varchar, symbol varchar, side varchar, timeframe varchar)")
    con.execute("insert into point_configs values ('P1', 'AAAUSDT', 'LONG', '15m')")
    con.execute("create table report_runs(report_id varchar, point_id varchar, raw_action_count integer)")
    con.execute("insert into report_runs values ('R1', 'P1', 2)")
    rows = [
        {"Timestamp": "2026-07-15 01:00:00", "Symbol": "AAAUSDT", "Action": "opened", "Post Side": "long"},
        {"Timestamp": "2026-07-15 02:00:00", "Symbol": "AAAUSDT", "Action": "closed", "Post Side": ""},
    ]
    con.execute("create table report_payloads(report_id varchar, actions_codec varchar, actions_zlib blob)")
    con.execute("insert into report_payloads values ('R1', 'zlib-columnar-json-v1', ?)", [_actions_blob(rows)])
    con.close()

    before = sha256(database.read_bytes()).hexdigest()
    package = build_duckdb_package(database, WINDOW_START, WINDOW_END, tmp_path / "package")

    assert pd.read_csv(package.points_csv)[["event_mode", "point_event_count"]].to_dict("records") == [{"event_mode": "real_independent_events", "point_event_count": 1}]
    assert pd.read_csv(package.audit_csv)["included_cycles"].tolist() == [1]
    assert package.manifest["included_cycles"] == 1
    assert sha256(database.read_bytes()).hexdigest() == before


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
