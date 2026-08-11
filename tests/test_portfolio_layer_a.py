"""Тесты пайплайна слоя A, чтения журналов сделок и проводки в панель."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mrs3.panel import PanelController
from mrs3.portfolio.layer_a import LayerAError
from mrs3.portfolio.layer_a_pipeline import LayerAInputs, read_candidates, run_layer_a
from mrs3.portfolio.trade_logs import TradeLogError, coverage_report

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)

HEADER = (
    "strategy_id;pair;side;timeframe;trades;median_hold_min;lot_x_base;"
    "pnl_pct;dd_pct;window_start;window_end;turnover_24h;target_share;target_share_source"
)


def write_csv(path: Path, rows: list[str], header: str = HEADER) -> Path:
    path.write_text("\n".join([header, *rows]), encoding="utf-8")
    return path


def row(sid: str, *, hold: str = "60", start: str = "2026-06-01T00:00:00Z",
        end: str = "2026-08-01T00:00:00Z", trades: int = 100) -> str:
    return (
        f"{sid};XLKUSDT;LONG;1h;{trades};{hold};0.5;20.0;5.0;{start};{end};"
        "6270000;0.115;ESTIMATED"
    )


# --- чтение входа ----------------------------------------------------------


def test_missing_required_column_is_reported(tmp_path):
    bad = "strategy_id;pair;side;timeframe;trades\nA;X;LONG;1h;10"
    path = tmp_path / "c.csv"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(LayerAError, match="нет обязательных колонок"):
        read_candidates(path)


def test_missing_median_hold_without_trade_db_is_explicit(tmp_path):
    path = write_csv(tmp_path / "c.csv", [row("A", hold="")])
    with pytest.raises(LayerAError, match="median_hold_min"):
        read_candidates(path)


def test_bad_timestamp_is_reported_with_strategy_id(tmp_path):
    path = write_csv(tmp_path / "c.csv", [row("A", start="not-a-date")])
    with pytest.raises(LayerAError, match="A: window_start"):
        read_candidates(path)


def test_target_share_defaults_when_column_absent(tmp_path):
    header = (
        "strategy_id;pair;side;timeframe;trades;median_hold_min;lot_x_base;"
        "pnl_pct;dd_pct;window_start;window_end;turnover_24h"
    )
    line = "A;XLKUSDT;LONG;1h;100;60;0.5;20.0;5.0;2026-06-01T00:00:00Z;2026-08-01T00:00:00Z;6270000"
    path = write_csv(tmp_path / "c.csv", [line], header=header)
    candidates = read_candidates(path)
    assert candidates[0].target_share == pytest.approx(0.115)
    assert candidates[0].target_share_source == "ESTIMATED"


# --- прогон и артефакты ----------------------------------------------------


def test_run_writes_all_artifacts_and_manifest(tmp_path):
    csv_path = write_csv(
        tmp_path / "c.csv",
        [row("A"), row("B", hold="90"), row("C", hold="45")],
    )
    out = tmp_path / "out"
    manifest = run_layer_a(
        LayerAInputs(candidates_csv=csv_path, output_dir=out, limiters=(2, 3))
    )
    assert (out / "02_Candidates.csv").exists()
    assert (out / "03_Layer_A_Screen.csv").exists()
    assert (out / "portfolio_manifest.json").exists()
    assert manifest["candidates"] == 3
    assert manifest["limiters"] == [2, 3]
    assert manifest["combinations_screened"] > 0
    # без журналов сделок отчёт покрытия не создаётся
    assert not (out / "00_Trade_Log_Coverage.csv").exists()


def test_manifest_declares_what_is_not_implemented(tmp_path):
    csv_path = write_csv(tmp_path / "c.csv", [row("A"), row("B")])
    manifest = run_layer_a(
        LayerAInputs(candidates_csv=csv_path, output_dir=tmp_path / "out")
    )
    text = json.dumps(manifest, ensure_ascii=False)
    assert "set simulation" in text
    assert "limiter contract" in text


def test_run_is_reproducible(tmp_path):
    csv_path = write_csv(tmp_path / "c.csv", [row("A"), row("B"), row("C")])
    first = run_layer_a(LayerAInputs(candidates_csv=csv_path, output_dir=tmp_path / "a"))
    second = run_layer_a(LayerAInputs(candidates_csv=csv_path, output_dir=tmp_path / "b"))
    for key in ("candidates", "combinations_screened", "combinations_accepted", "reject_reasons"):
        assert first[key] == second[key]
    assert (tmp_path / "a" / "03_Layer_A_Screen.csv").read_text(encoding="utf-8-sig") == (
        tmp_path / "b" / "03_Layer_A_Screen.csv"
    ).read_text(encoding="utf-8-sig")


def test_estimated_target_share_is_counted(tmp_path):
    csv_path = write_csv(tmp_path / "c.csv", [row("A"), row("B")])
    manifest = run_layer_a(
        LayerAInputs(candidates_csv=csv_path, output_dir=tmp_path / "out")
    )
    assert manifest["estimated_target_share"] == 2


# --- журналы сделок --------------------------------------------------------


class _Stats:
    """Замена StrategyTradeStats там, где DuckDB не нужен."""

    def __init__(self, sid, trades, start, end, median_hold):
        self.strategy_id = sid
        self.trades = trades
        self.window_start = start
        self.window_end = end
        self.median_hold_min = median_hold
        self.has_notional = True

    @property
    def d_eff_days(self):
        return (self.window_end - self.window_start).total_seconds() / 86400.0


def test_derived_fields_fill_gaps_in_csv(tmp_path):
    path = write_csv(tmp_path / "c.csv", [row("A", hold="", start="", end="")])
    stats = _Stats("A", 100, T0, T0 + timedelta(days=60), 37.5)
    candidates = read_candidates(path, {"A": stats})
    assert candidates[0].median_hold_min == pytest.approx(37.5)
    assert candidates[0].d_eff_days == pytest.approx(60.0)


def test_csv_value_wins_over_derived(tmp_path):
    path = write_csv(tmp_path / "c.csv", [row("A", hold="60")])
    stats = _Stats("A", 100, T0, T0 + timedelta(days=60), 999.0)
    candidates = read_candidates(path, {"A": stats})
    assert candidates[0].median_hold_min == pytest.approx(60.0)


def test_coverage_marks_missing_logs():
    stats = [_Stats("A", 10, T0, T0 + timedelta(days=30), 20.0)]
    rows = coverage_report(stats, ["A", "B"])
    by_id = {r["strategy_id"]: r for r in rows}
    assert by_id["A"]["status"] == "OK"
    assert by_id["B"]["status"] == "MISSING_TRADE_LOG"
    assert by_id["B"]["trades"] == 0


def test_missing_database_file_is_reported(tmp_path):
    from mrs3.portfolio.trade_logs import inspect_schema

    with pytest.raises(TradeLogError, match="не найден"):
        inspect_schema(tmp_path / "absent.duckdb")


# --- проводка в панель -----------------------------------------------------


def _controller(tmp_path) -> PanelController:
    return PanelController(root=tmp_path, default_config=Path("config.example.json"))


def test_panel_builds_portfolio_command(tmp_path):
    controller = _controller(tmp_path)
    command, artifacts = controller._build_command(
        "portfolio-layer-a",
        {
            "config": "config.example.json",
            "candidates_csv": "results/x.csv",
            "output_dir": "portfolio_long",
            "limiters": "2,3,4",
            "max_size_factor": "3",
        },
    )
    assert "portfolio-layer-a" in command
    assert "--candidates-csv" in command and "--output-dir" in command
    assert "--trades-db" not in command
    assert set(artifacts) == {"candidates", "screen", "manifest"}


def test_panel_adds_trade_db_and_coverage_artifact(tmp_path):
    controller = _controller(tmp_path)
    command, artifacts = controller._build_command(
        "portfolio-layer-a",
        {
            "config": "config.example.json",
            "candidates_csv": "results/x.csv",
            "output_dir": "portfolio_long",
            "trades_db": "trades.duckdb",
            "trades_table": "trades",
        },
    )
    assert "--trades-db" in command
    assert "--trades-table" in command
    assert "coverage" in artifacts


@pytest.mark.parametrize("limiters", ["0,2", "a,b", "-1"])
def test_panel_rejects_bad_limiters(tmp_path, limiters):
    controller = _controller(tmp_path)
    with pytest.raises(ValueError, match="limiters"):
        controller._build_command(
            "portfolio-layer-a",
            {
                "config": "config.example.json",
                "candidates_csv": "results/x.csv",
                "output_dir": "portfolio_long",
                "limiters": limiters,
            },
        )


def test_panel_requires_candidates_csv(tmp_path):
    controller = _controller(tmp_path)
    with pytest.raises(ValueError, match="candidates_csv"):
        controller._build_command(
            "portfolio-layer-a",
            {"config": "config.example.json", "output_dir": "portfolio_long"},
        )


def test_panel_html_contains_portfolio_tab():
    from mrs3.panel import PANEL_HTML

    assert "Анализатор портфеля" in PANEL_HTML
    assert "portfolio-layer-a" in PANEL_HTML
    assert "portfolio-run" in PANEL_HTML
    assert 'id="panel-portfolio"' in PANEL_HTML
    assert "activateTab" in PANEL_HTML
