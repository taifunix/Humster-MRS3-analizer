from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from mrs3.bingx_downloader import BingXDownloadError
from mrs3.panel import PanelController, PanelTestingError
from mrs3.panel_testing import LocalTestingService, expected_screener_runs, render_strategy, render_tester_config
from mrs3.screener import listing as listing_module
from mrs3.screener.render import render_screener_tester_config
from tests.test_bingx_downloader import CONTRACTS, FakeClient
from tests.test_panel_testing import _runner_config

REPO = Path(__file__).parents[1]
LONG_TEMPLATE = (REPO / "templates/tester/mrs2/config_tester_long.json").read_text(encoding="utf-8")
STRATEGY_TEMPLATE = (REPO / "templates/strategies/source-v6-mrs2/long.json").read_text(encoding="utf-8")


def _symbols(document: dict) -> list[str]:
    return next(item["values"] for item in document["parameter_mining"] if item["name"] == "settings[*].basic.symbol")


def test_bingx_tester_config_uses_api_symbols_fees_and_no_update_data():
    rendered = json.loads(
        render_tester_config(LONG_TEMPLATE, ("NCSKPANW2USD-USDT",), "2026-07-15", "2026-08-06", exchange="bingx")
    )
    assert _symbols(rendered) == ["NCSKPANW2USD-USDT"]
    assert (rendered["MakerFee"], rendered["TakerFee"], rendered["UpdateData"]) == (0.00012, 0.0003, False)
    assert rendered["StartDate"] == "2026-07-15T00:00:00"

    bybit = json.loads(render_tester_config(LONG_TEMPLATE, ("CXUSDT",), "2026-07-15", "2026-08-06"))
    template = json.loads(LONG_TEMPLATE)
    assert (bybit["MakerFee"], bybit["TakerFee"], bybit["UpdateData"]) == (
        template["MakerFee"], template["TakerFee"], template["UpdateData"],
    )
    with pytest.raises(PanelTestingError):
        render_tester_config(LONG_TEMPLATE, ("NCSKPANW2USD-USDT",), "2026-07-15", "2026-08-06")


def test_bingx_strategy_uses_bingx_exchange_account_and_api_symbol():
    _, strategy = render_strategy(STRATEGY_TEMPLATE, "NCSKPANW2USD-USDT", "LONG", exchange="bingx")
    assert strategy["exchange"]["name"] == "BingX"
    assert strategy["exchange"]["account"] == "bingx"
    assert strategy["basic"]["symbol"] == "NCSKPANW2USD-USDT"
    _, bybit = render_strategy(STRATEGY_TEMPLATE, "CXUSDT", "LONG")
    assert bybit["exchange"] == json.loads(STRATEGY_TEMPLATE)["exchange"]


def test_bingx_screener_config_and_expected_runs_accept_api_symbols():
    template_path = "templates/tester/mrs2/config_tester_long_screen.json"
    template = (REPO / template_path).read_text(encoding="utf-8")
    symbols = ("NCSKPANW2USD-USDT", "NCSKBABA2USD-USDT")
    rendered = json.loads(render_screener_tester_config(template, symbols, "2026-08-05", "2026-09-18", exchange="bingx"))
    assert _symbols(rendered) == list(symbols)
    assert rendered["UpdateData"] is False
    assert expected_screener_runs(REPO, template_path, symbols, exchange="bingx") == 304 * 2


def test_local_testing_fill_in_bingx_mode_installs_bingx_strategy(tmp_path: Path):
    config = _runner_config(tmp_path)
    installed = []

    def install(_config, source, *, selected_names, preserve_raw_artifacts):
        installed.append(json.loads(next(source.glob("*.json")).read_text(encoding="utf-8")))

    service = LocalTestingService(config, REPO, install_batch=install, stop_bot=lambda _config: None)
    filled = service.fill(side="LONG", symbols=("NCSKPANW2USD-USDT",), start="2026-07-15", end="2026-08-06", exchange="bingx")
    assert filled["symbols"] == ["NCSKPANW2USD-USDT"]
    assert installed[0]["exchange"]["name"] == "BingX"
    tester_config = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert tester_config["UpdateData"] is False
    service.stop()


def _controller(tmp_path: Path, client: FakeClient) -> tuple[PanelController, object]:
    config = _runner_config(tmp_path)
    document = {
        "tester_runner": {
            "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
            "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
        },
        "bingx": {"download_mode": "incremental"},
    }
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)
    controller._bingx_client_factory = lambda _keys: client
    controller._bingx_now = lambda: datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    return controller, config


def _download(controller: PanelController, payload: dict) -> dict:
    started = controller.bingx_download_start(payload)
    assert started["state"] == "running"
    controller._bingx_thread.join(timeout=30)
    return controller.bingx_download_status()


def test_panel_bingx_download_tops_up_from_one_day_before_start(tmp_path: Path):
    client = FakeClient()
    controller, config = _controller(tmp_path, client)
    status = _download(controller, {"symbols": "PANWUSDT, BABA", "start": "2026-10-07", "end": "2026-10-08"})
    assert status["state"] == "done" and status["errors"] == []
    assert status["contracts"] == {"PANW": "NCSKPANW2USD-USDT", "BABA": "NCSKBABA2USD-USDT"}
    folder = config.bot_root / "tester" / "data" / "bingx" / "NCSKPANW2USD-USDT"
    assert sorted(path.name for path in folder.iterdir()) == [
        "PANWUSDT2026-10-06_1m.parquet", "PANWUSDT2026-10-07_1m.parquet", "PANWUSDT2026-10-08_1m.parquet",
    ]
    assert str(config.bot_root) not in json.dumps(status)


def test_panel_bingx_fill_requires_local_data_and_maps_symbols(tmp_path: Path):
    controller, config = _controller(tmp_path, FakeClient())
    payload = {"symbols": "PANW", "side": "LONG", "start": "2026-10-07", "end": "2026-10-08", "bingx": True}
    with pytest.raises(PanelTestingError, match="2026-10-06"):
        controller.local_testing_fill(payload)
    assert _download(controller, payload)["state"] == "done"
    prepared = controller.local_testing_fill(payload)
    assert prepared["symbols"] == ["NCSKPANW2USD-USDT"]
    strategy = json.loads(next(config.strategy_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert strategy["exchange"]["name"] == "BingX"
    controller.local_testing_stop()
    changed = dict(payload, end="2026-10-09")
    with pytest.raises(PanelTestingError, match="2026-10-09"):
        controller.local_testing_fill(changed)


def test_panel_bingx_mode_without_download_uses_existing_local_data(tmp_path: Path):
    client = FakeClient()
    controller, config = _controller(tmp_path, client)
    payload = {"symbols": "PANWUSDT", "side": "LONG", "start": "2026-10-07", "end": "2026-10-08", "bingx": True}
    _download(controller, payload)
    calls_after_download = len(client.kline_calls)
    fresh = PanelController(tmp_path, tmp_path / "config.local.json")  # no top-up state at all
    fresh._bingx_client_factory = lambda _keys: client
    fresh._bingx_now = controller._bingx_now
    prepared = fresh.local_testing_fill(payload)
    assert prepared["symbols"] == ["NCSKPANW2USD-USDT"]
    assert len(client.kline_calls) == calls_after_download  # no candles downloaded by fill
    fresh.local_testing_stop()


def test_panel_bingx_download_errors_block_fill(tmp_path: Path):
    controller, _ = _controller(tmp_path, FakeClient())
    payload = {"symbols": "PANW, NOPE", "side": "LONG", "start": "2026-10-07", "end": "2026-10-08", "bingx": True}
    status = _download(controller, payload)
    assert status["state"] == "failed"
    assert [error["pair"] for error in status["errors"]] == ["NOPE"]
    with pytest.raises(PanelTestingError):
        controller.local_testing_fill(payload)


def test_panel_screener_bingx_fill_returns_expected_runs(tmp_path: Path):
    controller, _ = _controller(tmp_path, FakeClient())
    payload = {"symbols": "PANWUSDT\nBABAUSDT", "side": "LONG", "start": "2026-10-07", "end": "2026-10-08", "bingx": True}
    _download(controller, payload)
    prepared = controller.local_screener_fill(payload)
    assert prepared["symbols"] == ["NCSKPANW2USD-USDT", "NCSKBABA2USD-USDT"]
    assert prepared["expected_runs"] == 304 * 2
    controller.local_screener_stop()


def test_screener_listing_dates_for_bingx_symbols_come_from_bingx(monkeypatch):
    monkeypatch.setattr(listing_module, "_bingx_contracts", lambda: CONTRACTS)
    resolved = listing_module.resolve_listing_dates(
        ("NCSKPANW2USD-USDT",), registry_path=None, dates_path=None
    )
    assert resolved["NCSKPANW2USD-USDT"] == pd.Timestamp("2026-06-11 10:00", tz="UTC")
    with pytest.raises(listing_module.ScreenerEvaluationError):
        listing_module.resolve_listing_dates(("NCSKPANW2USD-USDT", "CXUSDT"), registry_path=None, dates_path=None)


def test_screener_does_not_write_bingx_verdicts_to_registry(monkeypatch, tmp_path: Path):
    from mrs3.screener import evaluate as evaluate_module

    class Verdict:
        symbol = "NCSKPANW2USD-USDT"

    writes = []
    monkeypatch.setattr(evaluate_module, "evaluate_pairs", lambda *args, **kwargs: (Verdict(),))
    monkeypatch.setattr(evaluate_module, "write_screening_results", lambda *args: writes.append(args))

    class Config:
        liquidity_registry_path = tmp_path / "registry.xlsx"

    result = evaluate_module.evaluate_and_record(
        tmp_path, algorithm_config=None, screener_config=Config(), dates_path=None
    )
    assert len(result) == 1 and writes == []


def test_panel_ui_has_bingx_checkbox_on_screener_and_local_runner_only():
    html = (REPO / "src/mrs3/panel_web/index.html").read_text(encoding="utf-8")
    js = (REPO / "src/mrs3/panel_web/app.js").read_text(encoding="utf-8")
    screener = html[html.index('id="screener-local"'):html.index('id="runner-local"')]
    local = html[html.index('id="runner-local"'):html.index('id="runner-remote"')]
    remote = html[html.index('id="runner-remote"'):html.index('id="source-db"')]
    assert 'id="screener-bingx"' in screener and 'id="local-bingx"' in local
    assert "bingx" not in remote
    assert screener.index('id="screener-bingx"') < screener.index('id="screener-fill"')
    for card, prefix in ((screener, "screener"), (local, "local")):
        assert ">Режим BingX<" in card and ">Докачать данные BingX<" in card
        download = card[card.index(f'id="{prefix}-bingx-download"'):]
        assert download.split(">", 1)[0].rstrip().endswith("disabled")  # inactive until BingX mode is on
    assert "докачать данные BingX перед подготовкой" not in html
    assert "'/api/v2/testing/bingx/download'" in js and "'/api/v2/testing/bingx/status'" in js
    # RUNNER 01 must be able to show the actionable "BingX: ..." fill error.
    assert "new Set(['/api/v2/settings/analysis-profile', '/api/v2/testing/local/fill'])" in js


def test_panel_bingx_fill_accepts_days_without_trades(tmp_path: Path):
    weekend = {date(2026, 10, 3), date(2026, 10, 4)}
    controller, config = _controller(tmp_path, FakeClient(empty_days=weekend))
    payload = {"symbols": "PANW", "side": "LONG", "start": "2026-10-03", "end": "2026-10-06", "bingx": True}
    status = _download(controller, payload)
    assert status["state"] == "done" and status["errors"] == []
    assert any("без сделок" in line for line in status["summary"])
    prepared = controller.local_testing_fill(payload)
    assert prepared["symbols"] == ["NCSKPANW2USD-USDT"]
    controller.local_testing_stop()


def test_bingx_tester_config_omits_the_bybit_only_margin_rate():
    rendered = json.loads(render_tester_config(LONG_TEMPLATE, ("NCSKPANW2USD-USDT",), "2026-07-15", "2026-08-06", exchange="bingx"))
    assert "maintenance_margin_rate" not in rendered
    bybit = json.loads(render_tester_config(LONG_TEMPLATE, ("CXUSDT",), "2026-07-15", "2026-08-06"))
    assert bybit["maintenance_margin_rate"] == 0.005
