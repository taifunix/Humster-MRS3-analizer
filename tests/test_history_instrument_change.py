"""History with tick size and qty step from the pair registry; a changed instrument recomputes the pair.

Contract: docs/specs/2026-10-20-panel-history-incremental.md, requirement 5.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from mrs3.trade_history import run_history
from mrs3.trade_store import TradeStore
from tests.test_trade_store import POINTS, _write_days, ms, walk


def _kwargs(data_root: Path, tick: float, qty: float = 0.01) -> dict:
    return dict(data_root=data_root, pairs=[("TESTUSDT", date(2026, 6, 1))], end_day=date(2026, 7, 20),
                instruments={"TESTUSDT": (tick, qty)}, points=POINTS, workers=1, download=False)


def test_a_changed_tick_size_recomputes_every_planned_segment_of_the_pair(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    assert run_history(store, **_kwargs(data_root, 0.01))["written"] == 2
    before = {row["segment_id"]: row["trades_sha256"] for row in store.segments("TESTUSDT")}
    assert run_history(store, **_kwargs(data_root, 0.01))["written"] == 0              # unchanged: nothing to do
    changed = run_history(store, **_kwargs(data_root, 0.05))
    assert (changed["written"], changed["failed"]) == (2, 0)
    after = store.segments("TESTUSDT")
    assert [(row["start_ms"], row["end_ms"]) for row in after] == [
        (ms("2026-06-05"), ms("2026-06-30")), (ms("2026-06-30"), ms("2026-07-20"))]
    assert {row["segment_id"] for row in after} == set(before)                       # the same spans and ids
    assert all(row["tick_size"] == 0.05 for row in after)
    assert any(row["trades_sha256"] != before[row["segment_id"]] for row in after)
    assert store.read_trades(after[0]).num_rows > 0                                  # the files match the catalog
    assert run_history(store, **_kwargs(data_root, 0.05))["written"] == 0


def test_a_changed_qty_step_recomputes_too(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01, 0.01))
    assert run_history(store, **_kwargs(data_root, 0.01, 1.0))["written"] == 2
    assert {row["qty_step"] for row in store.segments("TESTUSDT")} == {1.0}


def test_a_superseded_segment_of_another_tick_is_not_reactivated(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 51, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01))                                   # end 07-20
    later = {**_kwargs(data_root, 0.05), "end_day": date(2026, 7, 21)}
    run_history(store, **later)                                                      # supersedes the 07-20 plan
    back = run_history(store, **_kwargs(data_root, 0.05))                            # the 07-20 plan again
    assert back["reactivated"] == 0 and back["written"] == 2
    assert all(row["tick_size"] == 0.05 for row in store.segments("TESTUSDT"))


def test_the_history_plan_marks_a_pair_whose_instrument_changed(tmp_path: Path, monkeypatch) -> None:
    from tests.test_panel_main import _world

    service, _runner, store = _world(tmp_path)
    monkeypatch.setattr(service, "_history_end_day", lambda exchange="bybit": date(2026, 9, 10))
    stored = store.segments("GOODUSDT")[0]
    same = (stored["tick_size"], stored["qty_step"])
    monkeypatch.setattr(service, "_instruments", lambda exchange: {"GOODUSDT": same})
    plain = {item["key"]: item for item in service.plan("history", "bybit")["items"]}
    assert not plain["GOODUSDT"]["detail"].startswith("tick")
    monkeypatch.setattr(service, "_instruments", lambda exchange: {"GOODUSDT": (same[0] * 10, same[1])})
    changed = {item["key"]: item for item in service.plan("history", "bybit")["items"]}
    assert changed["GOODUSDT"]["detail"].startswith("tick или шаг лота изменились") and changed["GOODUSDT"]["checked"]


def test_a_plan_with_nothing_to_recompute_says_so(tmp_path: Path, monkeypatch) -> None:
    from tests.test_panel_main import _world

    service, _runner, _store = _world(tmp_path)
    monkeypatch.setattr(service, "_history_end_day", lambda exchange="bybit": date(2026, 9, 10))
    monkeypatch.setattr(service, "_trading_pairs", lambda exchange: [("GOODUSDT", None)])   # nothing planned
    plan = service.plan("history", "bybit")
    assert plan["up_to_date"] is True and plan["note"].startswith("Пересчёт не требуется") and "2026-09-09" in plan["note"]
    monkeypatch.undo()
    monkeypatch.setattr(service, "_history_end_day", lambda exchange="bybit": date(2026, 9, 10))
    assert "up_to_date" not in service.plan("history", "bybit")             # NEWUSDT is new: something to do


def test_the_bybit_history_takes_tick_and_qty_step_from_the_registry(tmp_path: Path, monkeypatch, capsys) -> None:
    import json
    import sys

    import pytest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import simulate_history

    seen = {}
    monkeypatch.setattr(simulate_history, "history_pairs", lambda config, **_: [("XOMUSDT", date(2026, 6, 1)),
                                                                                 ("BAREUSDT", date(2026, 6, 1))])
    monkeypatch.setattr(simulate_history, "registry_instruments", lambda config, exchange, **_: {"XOMUSDT": (0.05, 1.0)})
    monkeypatch.setattr(simulate_history, "instrument", lambda *args: pytest.fail("the old cache was read"))
    monkeypatch.setattr(simulate_history, "run_history", lambda store, **kwargs: seen.update(kwargs) or {"failed": 0})
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"tester_runner": {"bot_root": str(tmp_path / "bot")},
                                  "trade_store": {"root": str(tmp_path / "store")}}), encoding="utf-8")
    assert simulate_history.main(["--config", str(config), "--no-download", "--end-day", "2026-10-18"]) == 0
    assert seen["instruments"] == {"XOMUSDT": (0.05, 1.0)} and [s for s, _ in seen["pairs"]] == ["XOMUSDT"]
    assert "skipped, no tick size or qty step in the registry: BAREUSDT" in capsys.readouterr().out


# -- review 2026-10-20: the active chain never has a gap ----------------------------------------------------------

def test_a_failed_recompute_keeps_the_old_segment_active_and_the_chain_whole(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01))
    (data_root / "TESTUSDT" / "TESTUSDT2026-07-10_1m.parquet").unlink()            # the newest window cannot run
    changed = run_history(store, **_kwargs(data_root, 0.05))
    assert (changed["written"], changed["skipped"]) == (1, 1)
    rows = store.segments("TESTUSDT")
    assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
        (ms("2026-06-05"), ms("2026-06-30")), (ms("2026-06-30"), ms("2026-07-20"))]     # no gap
    assert [row["tick_size"] for row in rows] == [0.05, 0.01]                         # the old one stays until it runs
    for row in rows:
        assert store.read_metrics(row).num_rows > 0                                   # every file matches the catalog


def test_a_run_killed_after_writing_the_new_files_recovers(tmp_path: Path) -> None:
    from mrs3.trade_history import ENGINE, SOURCE, simulate_and_write
    from mrs3.trade_store import SegmentKey, instrument_variant
    from mrs3.mrs2_simulator import RunSettings

    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01))
    key = SegmentKey("bybit", "TESTUSDT", SOURCE, date(2026, 6, 5), date(2026, 6, 30), ENGINE)
    simulate_and_write(str(store.root), key, str(data_root), 0.05, 0.01, POINTS, RunSettings(), True,
                       variant=instrument_variant(0.05, 0.01))                       # killed before the catalog swap
    assert store.segments("TESTUSDT")[0]["tick_size"] == 0.01                        # still the old, whole row
    assert store.read_trades(store.segments("TESTUSDT")[0]).num_rows > 0
    assert run_history(store, **_kwargs(data_root, 0.05))["written"] == 2
    assert all(row["tick_size"] == 0.05 for row in store.segments("TESTUSDT"))


def test_the_tester_window_of_the_old_instrument_is_taken_out_of_use(tmp_path: Path) -> None:
    from mrs3.mrs2_simulator import RunSettings
    from mrs3.trade_history import ENGINE
    from mrs3.trade_store import SegmentKey, SegmentTables, write_segment_files

    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01))
    newest = store.segments("TESTUSDT")[-1]
    tester_key = SegmentKey("bybit", "TESTUSDT", "tester", date(2026, 6, 30), date(2026, 7, 20), ENGINE)
    tables = SegmentTables(store.read_trades(newest), store.read_hourly(newest), store.read_metrics(newest))
    store.register(write_segment_files(store.root, tester_key, tables, settings=RunSettings(), tick_size=0.01,
                                       qty_step=0.01))
    assert len(store.segments("TESTUSDT", source="tester")) == 1
    run_history(store, **_kwargs(data_root, 0.05))
    assert store.segments("TESTUSDT", source="tester") == []
    assert "tester_segments_superseded" in (store.root / "journal.jsonl").read_text(encoding="utf-8")


def test_a_catalog_row_without_tick_never_stops_the_run(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    run_history(store, **_kwargs(data_root, 0.01))
    with store.connect() as con:
        con.execute("update segments set tick_size = null, qty_step = null")       # an old catalog row
    assert store.instruments("bybit", "TESTUSDT") == {}
    assert run_history(store, **_kwargs(data_root, 0.05))["failed"] == 0            # counts as current, as the plan


def test_a_busy_registry_stops_the_bybit_history(tmp_path: Path, monkeypatch) -> None:
    import json
    import sys

    import pytest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import simulate_history

    monkeypatch.setattr(simulate_history, "history_pairs", lambda config, **_: [("XOMUSDT", date(2026, 6, 1))])
    monkeypatch.setattr(simulate_history, "registry_instruments", lambda config, exchange, **_: None)   # held
    monkeypatch.setattr(simulate_history, "_registry_exists", lambda config: True)
    monkeypatch.setattr(simulate_history, "instrument", lambda *args: pytest.fail("the old cache was read"))
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"tester_runner": {"bot_root": str(tmp_path / "bot")},
                                  "trade_store": {"root": str(tmp_path / "store")}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        simulate_history.main(["--config", str(config), "--no-download", "--end-day", "2026-10-18"])


def test_a_pair_without_tick_in_the_registry_is_named_and_not_checked(tmp_path: Path, monkeypatch) -> None:
    from tests.test_panel_main import _world

    service, _runner, _store = _world(tmp_path)
    monkeypatch.setattr(service, "_history_end_day", lambda exchange="bybit": date(2026, 9, 10))
    monkeypatch.setattr(service, "_instruments", lambda exchange: {})
    items = {item["key"]: item for item in service.plan("history", "bybit")["items"]}
    assert (items["NEWUSDT"]["detail"], items["NEWUSDT"]["checked"]) == ("нет tick или шага лота в справочнике", False)
