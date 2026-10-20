"""«Синхронизировать» of the selected exchange, the window days before the liquidity, «Пересчитать ликвидность».

Contract: docs/specs/2026-10-20-panel-sync-by-exchange.md. Temporary folders and fakes: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from mrs3.pair_registry.liquidity import ensure_window, read_liquidity, refresh_liquidity
from mrs3.pair_registry.store import RegistrySettings
from tests.test_pair_registry_liquidity import MONDAY, _minutes, _registry_with_pair, _write_day

TODAY = date(2026, 10, 19)                                   # Monday: the window ends on Friday 10-16
NOON = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)       # 10-18 is in the Bybit archive


def _fake_archive(monkeypatch, root: Path, calls: list) -> None:
    """`ensure_days` of the Bybit archive: writes the missing day files of the span."""
    import mrs3.pair_registry.minute_days as minute_days

    def ensure_days(data_root, symbol, first, last, **_options):
        calls.append((symbol, first, last))
        created = []
        day = first
        while day <= last:
            path = Path(data_root) / symbol / f"{symbol}{day.isoformat()}_1m.parquet"
            if not path.is_file():
                _write_day(Path(data_root), symbol, day, _minutes(day))
                created.append(day)
            day += timedelta(days=1)
        return {"existing": [], "created": created, "failed": []}

    monkeypatch.setattr(minute_days, "ensure_days", ensure_days)


def test_the_bybit_window_days_are_downloaded_before_the_liquidity(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"                                  # an empty Bybit folder, as on a new machine
    con = _registry_with_pair(tmp_path)
    settings = RegistrySettings(liquidity_data_root=root)
    assert refresh_liquidity(con, "bybit", TODAY, settings, now=NOON)["unavailable"] == 1
    calls: list = []
    _fake_archive(monkeypatch, root, calls)
    counts = ensure_window(con, settings, "bybit", TODAY, TODAY, now=NOON)
    assert calls == [("XOMUSDT", date(2026, 10, 8), date(2026, 10, 18))]       # 7 weekdays .. the day before today
    assert counts["pairs"] == 1 and counts["accepted"] == 11 and counts["failed_pairs"] == 0
    assert refresh_liquidity(con, "bybit", TODAY, settings, now=NOON)["ready"] == 1
    assert read_liquidity(con, "bybit", "XOMUSDT")["as_of_day"] == "2026-10-16"


def test_the_bybit_window_never_asks_for_a_day_not_yet_archived(tmp_path: Path, monkeypatch) -> None:
    con = _registry_with_pair(tmp_path)
    calls: list = []
    _fake_archive(monkeypatch, tmp_path / "data", calls)
    early = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)   # 03:00 UTC: 10-18 is not in the archive yet
    ensure_window(con, RegistrySettings(liquidity_data_root=tmp_path / "data"), "bybit", TODAY, TODAY, now=early)
    assert calls == [("XOMUSDT", date(2026, 10, 8), date(2026, 10, 17))]


def test_a_bybit_run_before_the_archive_lag_keeps_the_last_rows(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for offset in range(14):
        day = MONDAY + timedelta(days=offset)
        _write_day(root, "XOMUSDT", day, _minutes(day))
    con = _registry_with_pair(tmp_path)
    settings = RegistrySettings(liquidity_data_root=root)
    assert refresh_liquidity(con, "bybit", TODAY, settings, now=NOON)["ready"] == 1
    # Saturday 10-17 at 03:00 UTC: Friday 10-16 is not in the archive yet; the window of 10-16 is not overwritten
    early = refresh_liquidity(con, "bybit", date(2026, 10, 17), settings,
                              now=datetime(2026, 10, 17, 3, tzinfo=timezone.utc))
    assert early["not_yet_available"] == "2026-10-16" and read_liquidity(con, "bybit", "XOMUSDT")["status"] == "READY"


# -- the CLI ----------------------------------------------------------------------------------------------------------

def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry"),
                                                    "liquidity": {"data_root": str(tmp_path / "data")}}}),
                      encoding="utf-8")
    return config


def _recorded_sync(tmp_path: Path, monkeypatch) -> list:
    from mrs3.pair_registry import cli

    _registry_with_pair(tmp_path).close()
    calls: list = []
    monkeypatch.setattr(cli, "sync_pull", lambda con, path, machine: calls.append(("pull",)) or {})
    monkeypatch.setattr(cli, "sync_push", lambda con, path, machine, message: calls.append(("push",)) or {})
    monkeypatch.setattr(cli, "_refresh", lambda con, settings, exchanges: (calls.append(("refresh", *exchanges))
                                                                           or ({}, False)))
    monkeypatch.setattr(cli, "ensure_window", lambda con, settings, exchange, first, today: (
        calls.append(("minute_days", exchange)) or {"pairs": 0}))
    monkeypatch.setattr(cli, "refresh_liquidity", lambda con, exchange, today, settings: (
        calls.append(("liquidity", exchange)) or {"pairs": 0}))
    monkeypatch.setattr(cli, "_pipeline", lambda con, config, today, machine, skip, exchange="bybit": (
        calls.append(("pipeline", exchange)) or {}))
    monkeypatch.setattr(cli, "write_listing_workbook", lambda con, path: calls.append(("workbook",)) or {})
    return calls


@pytest.mark.parametrize("exchange", ["bybit", "bingx"])
def test_the_sync_of_one_exchange_touches_only_that_exchange(tmp_path: Path, monkeypatch, capsys, exchange) -> None:
    from mrs3.pair_registry import cli

    calls = _recorded_sync(tmp_path, monkeypatch)
    assert cli.main(["sync", "--config", str(_config(tmp_path)), "--exchange", exchange]) == 0
    assert calls == [("pull",), ("refresh", exchange), ("minute_days", exchange), ("liquidity", exchange),
                     ("pipeline", exchange), ("workbook",), ("push",)]
    steps = [json.loads(line)["progress"]["step"] for line in capsys.readouterr().out.splitlines()
             if "progress" in json.loads(line)]
    suffix = "" if exchange == "bybit" else "_bingx"
    assert steps == ["pull", "refresh", f"minute_days{suffix}", f"liquidity{suffix}", f"pipeline{suffix}", "workbook",
                     "push", "done"]


def test_pairs_whose_window_days_failed_are_an_error_of_the_sync(tmp_path: Path, monkeypatch, capsys) -> None:
    from mrs3.pair_registry import cli

    _recorded_sync(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "ensure_window", lambda con, settings, exchange, first, today: {
        "pairs": 2, "accepted": 0, "refused": 0, "failed_pairs": 2, "failed": {"AUSDT": "ConnectError", "BUSDT": "OSError"}})
    assert cli.main(["sync", "--config", str(_config(tmp_path)), "--exchange", "bybit"]) == 1
    errors = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"error"' in line]
    assert errors == [{"error": "window days not downloaded for 2 pairs: AUSDT, BUSDT", "step": "minute_days",
                       "pair": None}]


def test_the_sync_without_an_exchange_keeps_both(tmp_path: Path, monkeypatch) -> None:
    from mrs3.pair_registry import cli

    calls = _recorded_sync(tmp_path, monkeypatch)
    assert cli.main(["sync", "--config", str(_config(tmp_path))]) == 0
    assert calls == [("pull",), ("refresh", "bybit", "bingx"), ("minute_days", "bybit"), ("liquidity", "bybit"),
                     ("pipeline", "bybit"), ("minute_days", "bingx"), ("liquidity", "bingx"), ("pipeline", "bingx"),
                     ("workbook",), ("push",)]


def test_the_liquidity_command_ensures_the_bybit_window_first(tmp_path: Path, monkeypatch, capsys) -> None:
    from mrs3.pair_registry import cli
    import mrs3.pair_registry.liquidity as liquidity
    import mrs3.minute_data as minute_data

    _registry_with_pair(tmp_path).close()
    calls: list = []
    _fake_archive(monkeypatch, tmp_path / "data", calls)
    monkeypatch.setattr(minute_data, "last_archived_day", lambda now: date(2026, 10, 18))
    monkeypatch.setattr(liquidity, "last_archived_day", lambda now: date(2026, 10, 18), raising=False)
    assert cli.main(["liquidity", "--config", str(_config(tmp_path)), "--today", "2026-10-19"]) == 0
    result = json.loads(capsys.readouterr().out)["bybit"]
    assert result["ready"] == 1 and result["minute_days"]["accepted"] == 11 and calls


# -- the Panel ----------------------------------------------------------------------------------------------------------

def test_the_panel_sync_and_liquidity_run_for_the_selected_exchange(tmp_path: Path) -> None:
    from tests.test_panel_main import _world

    service, runner, _store = _world(tmp_path)
    for exchange in ("bybit", "bingx"):
        service.run("sync", exchange, [])
        steps = json.loads(Path(runner.started[-1][2][2]).read_text(encoding="utf-8"))["steps"]
        assert steps[0]["argv"][1:4] == ["-m", "mrs3.pair_registry", "sync"]
        assert steps[0]["argv"][-2:] == ["--exchange", exchange]
        plan = service.plan("liquidity", exchange)
        assert plan["items"] == [] and "disabled" not in plan and "история не пересчитываются" in plan["note"]
        service.run("liquidity", exchange, [])
        kind, label, argv, total, _stop = runner.started[-1]
        assert (kind, label, total) == ("liquidity", "Пересчитать ликвидность", 1)
        steps = json.loads(Path(argv[2]).read_text(encoding="utf-8"))["steps"]
        assert steps[0]["argv"][1:4] == ["-m", "mrs3.pair_registry", "liquidity"]
        assert steps[0]["argv"][-2:] == ["--exchange", exchange]
    assert "Bybit" in service.plan("sync", "bybit")["note"] and "BingX" in service.plan("sync", "bingx")["note"]
