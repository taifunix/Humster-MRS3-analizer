"""Pair registry stage 3: decisions, per-pair settings, finalists, pair tests, track record and pipeline.

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 3 contract».
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import duckdb
import pytest

from mrs3.pair_registry.pipeline import compute_pipeline, stage_of
from mrs3.pair_registry.records import (
    current_decision, current_finalists, effective_ready_rule, effective_ready_rules, import_finalists,
    import_tester_samples, import_tick_tests, record_decision, record_finalist, record_setting,
)
from mrs3.pair_registry.refresh import refresh_exchange
from mrs3.pair_registry.store import RegistryError, connect
from mrs3.pair_registry.track import track_record
from mrs3.ready_store import ReadyRule, build_ready
from tests.test_ready_store import REPO, _store_with_periods

NOW = 1_760_000_000_000


def _registry(tmp_path: Path, symbols=("GOODUSDT", "YOUNGUSDT")):
    con = connect(tmp_path / "registry")
    refresh_exchange(con, "bybit", [{"symbol": symbol, "status": "TRADING", "api_symbol": symbol,
                                     "listing_ms": int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)}
                                    for symbol in symbols], [], now_ms=NOW)
    return con


def test_decisions_are_validated_and_the_latest_is_in_force(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    record_decision(con, "bybit", "goodusdt", "ALL", "WATCH", "thin liquidity", "user", now_ms=NOW)
    assert current_decision(con, "bybit", "GOODUSDT") == "WATCH"
    record_decision(con, "bybit", "GOODUSDT", "LONG", "EXCLUDE", "earnings week", "user", now_ms=NOW + 1)
    assert current_decision(con, "bybit", "GOODUSDT", "LONG") == "EXCLUDE"
    assert current_decision(con, "bybit", "GOODUSDT", "SHORT") == "WATCH"           # ALL still covers SHORT
    for bad in (("MISSINGUSDT", "ALL", "WATCH", "r", "a"), ("GOODUSDT", "UP", "WATCH", "r", "a"),
                ("GOODUSDT", "ALL", "MAYBE", "r", "a"), ("GOODUSDT", "ALL", "WATCH", " ", "a"),
                ("GOODUSDT", "ALL", "WATCH", "r", "")):
        with pytest.raises(RegistryError):
            record_decision(con, "bybit", *bad)
    assert con.execute("select count(*) from decisions").fetchone()[0] == 2           # history is never edited


def test_pair_settings_override_the_ready_rule_and_reset(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    assert effective_ready_rule(con, "bybit", "GOODUSDT") == (ReadyRule(), {})
    record_setting(con, "bybit", "GOODUSDT", "ALL", "ready_v4", "admit_score", "25", "volatile pair", "user")
    record_setting(con, "bybit", "GOODUSDT", "LONG", "ready_v4", "max_dd", 5, "tighter", "user")
    rule, overridden = effective_ready_rule(con, "bybit", "GOODUSDT")
    assert (rule.admit_score, rule.max_dd, overridden) == (25.0, 5.0, {"admit_score": 25.0, "max_dd": 5.0})
    record_setting(con, "bybit", "GOODUSDT", "ALL", "ready_v4", "admit_score", None, "back to common", "user")
    assert effective_ready_rule(con, "bybit", "GOODUSDT")[1] == {"max_dd": 5.0}
    assert set(effective_ready_rules(con, "bybit")) == {"GOODUSDT"}
    for parameter, value in (("unknown", 1), ("min_trades", "2.5"), ("pool", 1), ("admit_score", "abc"),
                             ("low_dd", 6)):                                            # low_dd > moderate_dd
        with pytest.raises(RegistryError):
            record_setting(con, "bybit", "GOODUSDT", "ALL", "ready_v4", parameter, value, "r", "user")
    with pytest.raises(RegistryError):
        record_setting(con, "bybit", "GOODUSDT", "ALL", "other_filter", "x", 1, "r", "user")


def _performance_db(path: Path) -> Path:
    from mrs3.performance_v2_store import initialize_performance_v2

    with duckdb.connect(str(path)) as con:
        initialize_performance_v2(con, evaluation_protocol="v2", exchange="Bybit")
        now = "2026-10-01 00:00:00+00"
        for strategy_id, name in ((1, "GOODUSDT_15m_LONG_A"), (2, "GOODUSDT_1h_LONG_B")):
            con.execute("insert into strategies (strategy_id, strategy_name, symbol, side, timeframe, close_ma_len, "
                        "order_count, analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, "
                        "updated_at_utc) values (?, ?, 'GOODUSDT', 'LONG', '15m', 2, 2, 'run', ?, 'ACTIVE', ?, ?)",
                        [strategy_id, name, name, now, now])
            con.execute("insert into strategy_results (result_id, strategy_id, report_start_utc, report_end_utc, "
                        "exchange, initial_balance, final_balance, total_pnl_pct, max_drawdown_pct, total_trades, "
                        "imported_at_utc) values (?, ?, '2026-09-01 00:00:00+00', '2026-10-01 00:00:00+00', 'Bybit', "
                        "10000, 10300, 3, 1.5, 40, ?)", [10 + strategy_id, strategy_id, now])
    return path


def test_tick_tests_and_tester_samples_import_once(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    assert import_tick_tests(con, database, "bybit", now_ms=NOW) == 2
    assert import_tick_tests(con, database, "bybit", now_ms=NOW) == 0
    row = con.execute("select kind, structure, window_start, pnl_pct, maxdd_pct, trades from pair_tests "
                      "where source = 'perfdb:11'").fetchone()
    assert row == ("mrs3_tick", "GOODUSDT_15m_LONG_A", "2026-09-01", "3.000000000000", "1.500000000000", 40)
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    assert import_tester_samples(con, store, "bybit", now_ms=NOW) == 0                 # no tester segments yet


def test_finalists_append_only_changes(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    assert record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "FINALIST", "panel", now_ms=NOW)
    assert not record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "FINALIST", "panel", now_ms=NOW + 1)
    assert record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "RESERVE", "panel", now_ms=NOW + 2)
    assert current_finalists(con, "bybit") == [
        {"symbol": "GOODUSDT", "side": "LONG", "strategy": "S_A", "status": "RESERVE", "user_rank": None}]
    with pytest.raises(RegistryError):
        record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "MAYBE", "panel")
    database = _performance_db(tmp_path / "perf.duckdb")
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 0                   # no selection reviews


@pytest.mark.parametrize(("kwargs", "stage"), [
    ({"decision": "EXCLUDE", "finalists": 1, "tick_tests": 1, "tester_on_newest": True, "ready_now": True, "periods": 3}, "EXCLUDED"),
    ({"decision": None, "finalists": 1, "tick_tests": 1, "tester_on_newest": True, "ready_now": True, "periods": 3}, "FINALIST"),
    ({"decision": "WATCH", "finalists": 0, "tick_tests": 2, "tester_on_newest": True, "ready_now": True, "periods": 3}, "TICK"),
    ({"decision": None, "finalists": 0, "tick_tests": 0, "tester_on_newest": True, "ready_now": True, "periods": 3}, "TESTER"),
    ({"decision": None, "finalists": 0, "tick_tests": 0, "tester_on_newest": False, "ready_now": True, "periods": 3}, "READY"),
    ({"decision": None, "finalists": 0, "tick_tests": 0, "tester_on_newest": False, "ready_now": False, "periods": 2}, "HISTORY"),
    ({"decision": None, "finalists": 0, "tick_tests": 0, "tester_on_newest": False, "ready_now": False, "periods": 1}, "NEW"),
])
def test_stage_rule_first_match_wins(kwargs: dict, stage: str) -> None:
    assert stage_of(**kwargs) == stage


def test_track_record_walks_forward_with_the_production_rule(tmp_path: Path) -> None:
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 4, 35.0)
    record = track_record(store, "GOODUSDT", "bybit", ReadyRule())
    assert (record["cuts"], record["admitted_cuts"]) == (2, 2)
    assert record["admitted_candidates"] >= 2 and len(record["inputs_sha256"]) == 64
    strict = track_record(store, "GOODUSDT", "bybit", ReadyRule(admit_score=500))
    assert (strict["admitted_cuts"], strict["forward_loss_rate"]) == (0, None)
    assert strict["inputs_sha256"] != record["inputs_sha256"]


def test_pipeline_rows_stages_and_track_reuse(tmp_path: Path, monkeypatch) -> None:
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    _store_with_periods(tmp_path / "store", "YOUNGUSDT", 1, 30.0)
    template = json.loads((REPO / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    build_ready(store, ["GOODUSDT", "YOUNGUSDT"], tmp_path / "ready", template)
    con = _registry(tmp_path, ("GOODUSDT", "YOUNGUSDT", "NEWUSDT"))
    summary = compute_pipeline(con, store, "bybit", date(2026, 10, 18), "pc1", now_ms=NOW)
    assert summary["stages"] == {"READY": 1, "NEW": 2} and summary["tracked"] == 1
    good = con.execute("select stage, periods, ready_status, admitted, best_category, track_cuts, machine, "
                       "history_status from pipeline where symbol = 'GOODUSDT'").fetchone()
    assert good == ("READY", 3, "READY", 1, "low", 1, "pc1", "READY возможен")
    calls = []
    monkeypatch.setattr("mrs3.pair_registry.pipeline.track_record", lambda *args: calls.append(args) or {})
    record_decision(con, "bybit", "GOODUSDT", "ALL", "EXCLUDE", "news", "user")
    again = compute_pipeline(con, store, "bybit", date(2026, 10, 19), "pc1", now_ms=NOW + 1)
    assert again["stages"] == {"EXCLUDED": 1, "NEW": 2} and calls == []                 # same inputs: reused
    assert con.execute("select count(*) from pipeline").fetchone()[0] == 6
    assert con.execute("select status from journal where step = 'pipeline'").fetchall() == [("OK",), ("OK",)]


def test_ready_uses_a_pair_override_and_records_it(tmp_path: Path) -> None:
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    template = json.loads((REPO / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    plain = build_ready(store, ["GOODUSDT"], tmp_path / "plain", template)
    strict = build_ready(store, ["GOODUSDT"], tmp_path / "strict", template, rules={"GOODUSDT": ReadyRule(admit_score=500)})
    assert (plain["ready_pairs"], strict["ready_pairs"]) == (1, 0)
    plain_entry = json.loads((tmp_path / "plain" / "ready_manifest.json").read_text(encoding="utf-8"))["pairs"][0]
    strict_entry = json.loads((tmp_path / "strict" / "ready_manifest.json").read_text(encoding="utf-8"))["pairs"][0]
    assert (plain_entry["overridden"], strict_entry["overridden"]) == ([], ["admit_score"])
    assert strict_entry["rule"]["admit_score"] == 500
    assert plain_entry["inputs_sha256"] != strict_entry["inputs_sha256"]


def test_ready_from_store_reads_the_registry_overrides(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ready_from_store", REPO / "scripts" / "ready_from_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry")}}), encoding="utf-8")
    assert module.registry_rules(config) == {}                                       # no registry yet
    con = _registry(tmp_path)
    record_setting(con, "bybit", "GOODUSDT", "ALL", "ready_v4", "admit_score", 40, "r", "user")
    con.close()
    assert module.registry_rules(config) == {"GOODUSDT": ReadyRule(admit_score=40.0)}


def test_cli_decide_and_setting(tmp_path: Path, capsys) -> None:
    from mrs3.pair_registry import cli

    _registry(tmp_path).close()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry")}}), encoding="utf-8")
    assert cli.main(["decide", "--config", str(config), "--symbol", "GOODUSDT", "--decision", "WATCH",
                     "--reason", "check", "--author", "me"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "WATCH"
    assert cli.main(["setting", "--config", str(config), "--symbol", "GOODUSDT", "--parameter", "pool",
                     "--value", "30", "--reason", "x", "--author", "me"]) == 1
    assert "pool" in json.loads(capsys.readouterr().out)["error"]


def _review(database: Path, review_id: str, imported: str, rows: list[tuple[int, str, int | None]]) -> None:
    with duckdb.connect(str(database)) as con:
        if con.execute("select count(*) from selection_runs").fetchone()[0] == 0:
            con.execute("insert into selection_runs values ('S1', 'db', 'GOODUSDT', 'LONG', 'v', '{}', ?, '{}', ?, "
                        "2, 2, 1, 5, ?, '2026-10-01 00:00:00+00')", ["a" * 64, "b" * 64, "c" * 64])
        con.execute("insert into selection_review_imports (review_import_id, selection_run_id, workbook_sha256, "
                    "imported_at_utc, row_count) values (?, 'S1', ?, ?, ?)",
                    [review_id, review_id * 64, imported, len(rows)])
        for strategy_id, status, rank in rows:
            con.execute("insert into selection_review_rows (review_import_id, strategy_id, user_status, user_rank) "
                        "values (?, ?, ?, ?)", [review_id, strategy_id, status, rank])


def test_finalists_import_takes_the_latest_review_and_appends_changes_only(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    _review(database, "1", "2026-10-02 00:00:00+00", [(1, "FINALIST", 1), (2, "ANALOG", None)])
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 1                  # ANALOG is not a mark
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 0                  # nothing changed
    _review(database, "2", "2026-10-03 00:00:00+00", [(1, "RESERVE", 2), (2, "REJECTED", None)])
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 2
    assert [(row["strategy"], row["status"], row["user_rank"]) for row in current_finalists(con, "bybit")] == [
        ("GOODUSDT_15m_LONG_A", "RESERVE", 2), ("GOODUSDT_1h_LONG_B", "REJECTED", None)]
    assert con.execute("select count(*) from finalists where source = 'performance_db'").fetchone()[0] == 3


def test_a_protocol_v1_database_counts_as_bybit(tmp_path: Path) -> None:
    # live run 2026-10-18: the v1 PerformanceDB has no schema_info.exchange and strategy_results.exchange = 'tester'
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    with duckdb.connect(str(database)) as source:
        source.execute("delete from schema_info where key = 'exchange'")
        source.execute("update strategy_results set exchange = 'tester'")
    assert import_tick_tests(con, database, "bingx", now_ms=NOW) == 0
    assert import_tick_tests(con, database, "bybit", now_ms=NOW) == 2


def test_resetting_an_override_shows_the_current_ready_again(tmp_path: Path) -> None:
    # review of registry stage 3: a rebuild back to earlier inputs must become the newest catalog row again
    from mrs3.pair_registry.pipeline import _latest_ready

    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    template = json.loads((REPO / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    build_ready(store, ["GOODUSDT"], tmp_path / "r1", template)
    build_ready(store, ["GOODUSDT"], tmp_path / "r2", template, rules={"GOODUSDT": ReadyRule(admit_score=500)})
    assert _latest_ready(store, "bybit")["GOODUSDT"]["payload"]["status"] == "NO_ADMITTED_STRUCTURE"
    build_ready(store, ["GOODUSDT"], tmp_path / "r3", template)                       # override reset
    assert _latest_ready(store, "bybit")["GOODUSDT"]["payload"]["status"] == "READY"


def test_a_finalist_demoted_in_the_performance_db_is_no_longer_in_force(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    _review(database, "1", "2026-10-02 00:00:00+00", [(1, "FINALIST", 1)])
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 1
    _review(database, "2", "2026-10-03 00:00:00+00", [(1, "ANALOG", None)])
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 1                  # NONE appended
    assert [(row["strategy"], row["status"]) for row in current_finalists(con, "bybit")] == [("GOODUSDT_15m_LONG_A", "NONE")]
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 0


def test_short_settings_keep_the_dd_ordering_and_short_finalists_do_not_mark_the_long_row(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    with pytest.raises(RegistryError):
        record_setting(con, "bybit", "GOODUSDT", "SHORT", "ready_v4", "low_dd", 6, "r", "user")
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 1, 35.0)
    record_finalist(con, "bybit", "GOODUSDT", "SHORT", "S_SHORT", "FINALIST", "panel", now_ms=NOW)
    compute_pipeline(con, store, "bybit", date(2026, 10, 18), "pc1", now_ms=NOW, track=False)
    assert con.execute("select stage, finalist_strategies from pipeline where symbol = 'GOODUSDT'").fetchone() == ("NEW", 0)


def test_cli_sync_runs_refresh_liquidity_and_pipeline_with_progress(tmp_path: Path, monkeypatch, capsys) -> None:
    # Panel stage 3: «Синхронизировать» runs `sync`; every step reports progress, a failing step does not stop others
    from mrs3.pair_registry import cli
    from tests.test_pair_registry import BINGX, BYBIT, RISK

    store = _store_with_periods(tmp_path / "store", "XOMUSDT", 2, 35.0)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry")},
                                  "trade_store": {"root": str(store.root)}}), encoding="utf-8")
    monkeypatch.setattr(cli, "fetch_bybit", lambda: (BYBIT, RISK))
    monkeypatch.setattr(cli, "fetch_bingx", lambda: BINGX)
    code = cli.main(["sync", "--config", str(config), "--today", "2026-10-19", "--machine", "pc1", "--skip-track"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    steps = [event["progress"]["step"] for event in events if "progress" in event]
    assert steps == ["pull", "refresh", "liquidity", "liquidity_bingx", "pipeline", "pipeline_bingx", "workbook",
                     "push", "done"]                                                     # sync off: no-ops
    errors = [event for event in events if "error" in event]
    assert code == 1 and [error["step"] for error in errors] == ["liquidity", "liquidity_bingx"]   # no data folder
    summary = events[-1]["sync"]
    assert summary["refresh"]["bybit"]["pairs"] == 3 and summary["pipeline"]["pairs"] == 3
    assert summary["workbook"]["bybit"] == 3 and (tmp_path / "registry" / "pairs.xlsx").is_file()   # registry stage 4


def test_one_performance_db_splits_tick_tests_and_strategies_by_exchange(tmp_path: Path) -> None:
    # ADR-0083: a BingX strategy sits in the same database; each reader takes the rows of its exchange
    from mrs3.panel_strategies import _read_tested_rows

    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    with duckdb.connect(str(database)) as source:
        source.execute("update strategies set current_result_id = strategy_id + 10")
        source.execute("insert into strategies (strategy_id, strategy_name, symbol, side, timeframe, close_ma_len, "
                       "order_count, analysis_run_id, candidate_identity, lifecycle_status, current_result_id, "
                       "created_at_utc, updated_at_utc) values (3, 'BINGX_GOODUSDT_1h_LONG_C', 'GOODUSDT', 'LONG', "
                       "'1h', 2, 2, 'run', 'c', 'ACTIVE', 13, now(), now())")
        source.execute("insert into strategy_results (result_id, strategy_id, report_start_utc, report_end_utc, "
                       "exchange, initial_balance, final_balance, total_pnl_pct, max_drawdown_pct, total_trades, "
                       "imported_at_utc) values (13, 3, '2026-09-01 00:00:00+00', '2026-10-01 00:00:00+00', 'BingX', "
                       "10000, 10100, 1, 1, 10, now())")
        source.execute("update strategy_results set exchange = 'tester' where result_id = 12")   # a legacy row
    assert import_tick_tests(con, database, "bingx", now_ms=NOW) == 1
    assert import_tick_tests(con, database, "bybit", now_ms=NOW) == 2                  # the legacy row is Bybit
    assert con.execute("select exchange, structure from pair_tests where source = 'perfdb:13'").fetchone() == (
        "bingx", "BINGX_GOODUSDT_1h_LONG_C")
    bybit, _notes = _read_tested_rows(database, "bybit", None, metrics=False)
    bingx, _notes = _read_tested_rows(database, "bingx", None, metrics=False)
    assert sorted(bybit) == ["GOODUSDT_15m_LONG_A", "GOODUSDT_1h_LONG_B"] and list(bingx) == ["BINGX_GOODUSDT_1h_LONG_C"]


def test_finalists_are_imported_per_exchange(tmp_path: Path) -> None:
    # review of 5e-1: a BingX finalist goes to the BingX registry rows only; a legacy 'tester' result is Bybit
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    with duckdb.connect(str(database)) as source:
        source.execute("update strategy_results set exchange = 'BingX' where strategy_id = 2")
        source.execute("update strategy_results set exchange = 'tester' where strategy_id = 1")
    _review(database, "1", "2026-10-02 00:00:00+00", [(1, "FINALIST", 1), (2, "FINALIST", 2)])
    assert import_finalists(con, database, "bingx", now_ms=NOW) == 1
    assert import_finalists(con, database, "bybit", now_ms=NOW) == 1
    assert [row["strategy"] for row in current_finalists(con, "bingx")] == ["GOODUSDT_1h_LONG_B"]
    assert [row["strategy"] for row in current_finalists(con, "bybit")] == ["GOODUSDT_15m_LONG_A"]
