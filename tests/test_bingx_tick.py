"""The BingX tick test (Panel stage 5e-3a).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 5e contract», 5e-3a; ADR-0083. Temporary
folders and fakes only: the real tester is never started and nothing leaves the machine.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from mrs3.evaluation_protocol import EvaluationTarget, require_protocol_v2_strategy
from mrs3.evaluation_protocol import tester_base as _tester_base
from mrs3.mrs2_simulator import RunSettings
from mrs3.panel_strategy_batch import validate_strategy_manifest
from mrs3.performance_v2_input import adapt_strategy_identity
from mrs3.ready_store import build_ready
from mrs3.store_ready_tick import StoreReadyError, prepare_tick_batch
from mrs3.trade_store import HOURLY_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore
from tests.test_ready_store import SHIFTS_BP, _metrics

REPO = Path(__file__).parents[1]
BINGX_TEMPLATE = REPO / "templates" / "exchanges" / "BingX" / "mrs3-strategy.json"
BINGX_TESTER = REPO / "templates" / "exchanges" / "BingX" / "mrs3-tester.json"
BINGX_TARGET = EvaluationTarget(_tester_base(BINGX_TESTER), "BingX")
API = {"GOODUSDT": "NCSKGOOD2USD-USDT"}


def _bingx_ready(tmp_path: Path) -> Path:
    store = TradeStore(tmp_path / "store")
    first = date(2026, 7, 1)
    for index in range(3):
        start, end = first + timedelta(days=20 * index), first + timedelta(days=20 * (index + 1))
        tables = SegmentTables(TRADE_SCHEMA.empty_table(), HOURLY_SCHEMA.empty_table(),
                               _metrics({shift: 35.0 for shift in SHIFTS_BP}, 20))
        store.register(store.write_files(SegmentKey("bingx", "GOODUSDT", "simulator", start, end, "eng"), tables,
                                         settings=RunSettings(), tick_size=0.01, qty_step=0.01))
    ready = tmp_path / "store" / "ready" / "2026-10-20_ready_120000"
    build_ready(store, ["GOODUSDT"], ready, json.loads(BINGX_TEMPLATE.read_text(encoding="utf-8")), exchange="bingx")
    return ready


def test_bingx_ready_names_carry_the_tag_and_keep_the_human_ticker(tmp_path: Path) -> None:
    ready = _bingx_ready(tmp_path)
    [pair] = json.loads((ready / "ready_manifest.json").read_text(encoding="utf-8"))["pairs"]
    [item] = pair["structures"]
    assert item["strategy"].startswith("BINGX_GOODUSDT_")
    strategy = json.loads((ready / "strategies" / f"{item['strategy']}.json").read_text(encoding="utf-8"))
    assert strategy["basic"]["symbol"] == "GOODUSDT" and strategy["exchange"]["name"] == "BingX"


def test_the_bingx_batch_runs_the_api_symbol_and_imports_the_human_ticker(tmp_path: Path) -> None:
    ready = _bingx_ready(tmp_path)
    result = prepare_tick_batch(ready, tmp_path / "out", BINGX_TEMPLATE, BINGX_TARGET, api_symbols=API)
    validated = validate_strategy_manifest(Path(result["manifest_path"]))
    [path] = list(Path(validated.strategy_source).glob("*.json"))
    strategy = json.loads(path.read_text(encoding="utf-8"))
    assert strategy["name"].startswith("BINGX_GOODUSDT_") and path.stem == strategy["name"]
    assert strategy["basic"]["symbol"] == "NCSKGOOD2USD-USDT"                   # the tester trades the API symbol
    require_protocol_v2_strategy(strategy, BINGX_TARGET)
    assert adapt_strategy_identity(strategy).symbol == "GOODUSDT"              # the database keeps the human ticker
    other = prepare_tick_batch(ready, tmp_path / "out", BINGX_TEMPLATE, BINGX_TARGET,
                               api_symbols={"GOODUSDT": "NCSKGOOD2JPY-USDT"})
    assert other["analysis_run_id"] != result["analysis_run_id"]               # the API symbols are in the identity


def test_a_folder_is_tested_only_on_its_own_exchange_and_needs_its_api_symbols(tmp_path: Path) -> None:
    ready = _bingx_ready(tmp_path)
    with pytest.raises(StoreReadyError, match="API"):
        prepare_tick_batch(ready, tmp_path / "out", BINGX_TEMPLATE, BINGX_TARGET, api_symbols={})
    with pytest.raises(StoreReadyError, match="биржи"):
        prepare_tick_batch(ready, tmp_path / "out", BINGX_TEMPLATE, replace(BINGX_TARGET, exchange="Bybit"),
                           api_symbols=API)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_the_bingx_tester_template_never_downloads() -> None:
    assert json.loads(BINGX_TESTER.read_text(encoding="utf-8"))["UpdateData"] is False


def test_missing_days_fail_the_single_mode_job_before_the_tester(tmp_path: Path) -> None:
    from mrs3.panel_fast_strategy_test import LocalSingleModeStrategyTestService
    from tests.test_single_mode_handoff import _generation, _runner_config, _wait_terminal

    manifest, _names = _generation(tmp_path, 1)
    touched = []
    service = LocalSingleModeStrategyTestService(
        _runner_config(tmp_path), start_bot=lambda _config: touched.append("start"),
        stop_bot=lambda _config: touched.append("stop"), client_factory=lambda _config: object())
    started = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31",
                            job_id="bingx-missing", prepare_data=lambda: "MISSING_DAYS: GOODUSDT ['2026-08-05']")
    terminal = _wait_terminal(service, str(started["job_id"]))
    assert terminal["state"] == "FAILED" and terminal["error"]["code"] == "MISSING_DAYS"
    assert "2026-08-05" in terminal["error"]["message"] and touched == []    # the tester was never touched


def test_the_controller_preparer_reports_unverified_days(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.minute_days as minute_days
    from mrs3.pair_registry.minute_days import MinuteDays
    from mrs3.panel import PanelController

    ready = _bingx_ready(tmp_path)
    result = prepare_tick_batch(ready, tmp_path / "out", BINGX_TEMPLATE, BINGX_TARGET, api_symbols=API)
    asked = []

    def fake_ensure(settings, symbol, first, last, *, api_symbol=None, **_kwargs):
        asked.append((symbol, api_symbol, first, last))
        return MinuteDays(accepted=[first + timedelta(days=offset) for offset in range((last - first).days + 1)
                                    if first + timedelta(days=offset) != date(2026, 8, 5)])

    monkeypatch.setattr(minute_days, "ensure_bingx_days_shared", fake_ensure)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry")}}), encoding="utf-8")
    controller = PanelController.__new__(PanelController)
    controller.default_config = config
    prepare = controller._bingx_data_preparer(Path(result["manifest_path"]), "2026-08-01", "2026-08-31", BINGX_TESTER)
    problem = prepare()
    warmup = json.loads(BINGX_TESTER.read_text(encoding="utf-8"))["WarmupDays"]
    assert asked == [("GOODUSDT", "NCSKGOOD2USD-USDT", date(2026, 8, 1) - timedelta(days=warmup), date(2026, 8, 31))]
    assert problem.startswith("MISSING_DAYS: GOODUSDT") and "2026-08-05" in problem


def test_an_api_symbol_report_matches_its_human_ticker_strategy() -> None:
    # review of 5e-3a (B1): the report names the API symbol it traded; the strategy is stored under the ticker
    from types import SimpleNamespace

    from mrs3.performance_v2_import import PerformanceV2ImportError, _validate_report

    metrics = {"Report range": "2026-08-01 - 2026-08-31"}
    entry = SimpleNamespace(identity=SimpleNamespace(symbol="GOODUSDT"), exchange_name="BingX",
                            strategy_name="BINGX_GOODUSDT_X")
    report = SimpleNamespace(settings={"basic": {"symbol": "NCSKGOOD2USD-USDT"}}, metrics=metrics)
    _validate_report(entry, report, check_range=False)
    with pytest.raises(PerformanceV2ImportError, match="symbol"):          # another pair is still refused
        _validate_report(entry, SimpleNamespace(settings={"basic": {"symbol": "NCSKBAD2USD-USDT"}}, metrics=metrics),
                         check_range=False)
    bybit = SimpleNamespace(identity=SimpleNamespace(symbol="GOODUSDT"), exchange_name="Bybit", strategy_name="X")
    with pytest.raises(PerformanceV2ImportError, match="symbol"):          # Bybit keeps the exact comparison
        _validate_report(bybit, report, check_range=False)


def test_a_bingx_job_is_imported_against_the_bingx_target_and_its_listing_sheet(tmp_path: Path) -> None:
    # review of 5e-3a (B2, M1): with Bybit configured, a BingX-marked job is checked against the BingX target
    from mrs3.evaluation_protocol import EvaluationProtocolError
    from mrs3.panel import PanelController

    targets = {None: EvaluationTarget(_tester_base(BINGX_TESTER), "Bybit", profile_digest="bybit"),
               "BingX": EvaluationTarget(_tester_base(BINGX_TESTER), "BingX", profile_digest="bingx")}
    controller = PanelController.__new__(PanelController)
    controller._evaluation_target = lambda exchange=None: targets[exchange]
    runtimes = {}

    class Jobs:
        def runtime(self, job_id):
            return runtimes[job_id]

    controller._panel_jobs = Jobs()
    runtimes["bx"] = PanelController._protocol_v2_runtime(targets["BingX"])
    assert controller._require_import_target("bx", False) == "BingX"
    runtimes["by"] = PanelController._protocol_v2_runtime(targets[None])
    runtimes["mixed"] = {"members": [{"tester_job_id": "bx"}, {"tester_job_id": "by"}]}
    with pytest.raises(EvaluationProtocolError) as error:
        controller._require_import_target("mixed", True)
    assert error.value.code == "IMPORT_EXCHANGE_MIXED"
    from types import SimpleNamespace

    controller.root = REPO
    controller._workflow_default = lambda name, side=None: tmp_path / "pairs.xlsx"
    controller._performance_v2_config = lambda: SimpleNamespace(exchange="Bybit", evaluation_protocol="v2")
    assert controller._listing_dates_source("BingX") == (tmp_path / "pairs.xlsx", "Пары_BingX")


def test_the_retest_takes_only_a_known_exchange() -> None:
    # Panel stage 5e-3b: the retest request names bybit or bingx; anything else is refused before any work
    from mrs3.panel import PanelController
    from mrs3.performance_v2_finalist_retest import FinalistRetestError

    controller = PanelController.__new__(PanelController)
    with pytest.raises(FinalistRetestError) as error:
        controller.strategies_performance_v2_finalist_retest_start({"exchange": "okx"})
    assert error.value.code == "INVALID_REQUEST"


def test_a_bingx_retest_starts_with_bingx_marks_api_symbols_and_the_data_step(tmp_path: Path, monkeypatch) -> None:
    # review of 5e-3b: the exchange never reaches the range check; the marks, symbols and data step are BingX's
    from types import SimpleNamespace

    import mrs3.panel as panel_module
    from mrs3.panel import PanelController
    from tests._protocol_v2 import use_protocol_v2_target

    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    use_protocol_v2_target(monkeypatch)
    database = tmp_path / "performance.duckdb"
    database.touch()
    template = tmp_path / "base.json"
    template.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    cohort = SimpleNamespace(scope="FINALIST", cohort_sha256="c", members=({"strategy_id": 1},), exclusions=())

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    asked, built, started, ranges, captured = {}, {}, {}, [], {}

    def listing(exchange=None):
        asked["listing"] = exchange
        return tmp_path / "d.xlsx", Path("d.xlsx"), "BingX sheet"

    def marks(connection, exchange=None):
        asked["marks"] = exchange
        return {1: ("FINALIST", 1, None)}

    def bulk_range(payload, *_args):
        ranges.append(dict(payload))
        return "2026-08-01", "2026-08-31"

    def build(*_args, **kwargs):
        built.update(kwargs)
        return SimpleNamespace(cohort=cohort, config_sha256="x", manifest_path=manifest, run_id="run")

    def start_tracked(*args, **kwargs):
        captured["submit"] = args[3]
        return {"job_id": "new"}

    monkeypatch.setattr(controller, "_retest_listing_context", listing)
    monkeypatch.setattr(controller, "_registry_finalist_decisions", marks)
    monkeypatch.setattr(controller, "_retest_api_symbols", lambda: {"GOODUSDT": "NCSKGOOD2USD-USDT"})
    monkeypatch.setattr(controller, "_bingx_data_preparer", lambda *_args: "prepare")
    monkeypatch.setattr(controller, "_performance_v2_config",
                        lambda: SimpleNamespace(strategy_root=tmp_path / "Output" / "s"))
    monkeypatch.setattr(controller, "_workflow_defaults", lambda: {"strategy_templates": {"LONG": str(template)}})
    monkeypatch.setattr(controller, "_bulk_retest_range", bulk_range)
    monkeypatch.setattr(controller, "_require_cohort_exchange", lambda *_args: None)
    monkeypatch.setattr(controller, "_start_tracked_panel_job", start_tracked)
    monkeypatch.setattr(panel_module, "performance_v2_database_path", lambda _config: database)
    monkeypatch.setattr(panel_module, "load_listing_dates", lambda *_args: {})
    monkeypatch.setattr(panel_module.duckdb, "connect", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(panel_module, "freeze_finalist_cohort", lambda *_args, **_kwargs: cohort)
    monkeypatch.setattr(panel_module, "build_finalist_retest_manifest", build)
    controller._panel_jobs = SimpleNamespace(list=lambda: [])
    monkeypatch.setattr(controller, "_single_mode_strategy_test",
                        lambda: SimpleNamespace(start=lambda *_args, **kwargs: started.update(kwargs)))

    assert controller.strategies_performance_v2_finalist_retest_start({"exchange": "bingx"})["job_id"] == "new"
    captured["submit"]("new")
    assert ranges == [{}]                                                     # `exchange` never reaches the range
    assert asked == {"listing": "bingx", "marks": "bingx"}
    assert built["api_symbols"] == {"GOODUSDT": "NCSKGOOD2USD-USDT"} and started["prepare_data"] == "prepare"


def test_a_bingx_cohort_refuses_a_result_of_another_exchange(tmp_path: Path) -> None:
    import duckdb

    from mrs3.panel import PanelController
    from mrs3.performance_v2_finalist_retest import FinalistRetestError
    from tests.test_pair_registry_records import _performance_db

    database = _performance_db(tmp_path / "perf.duckdb")
    with duckdb.connect(str(database)) as connection:
        connection.execute("update strategy_results set exchange = 'BingX' where result_id = 12")
        PanelController._require_cohort_exchange(connection, [{"result_id": 12}], "bingx")
        with pytest.raises(FinalistRetestError) as error:
            PanelController._require_cohort_exchange(connection, [{"result_id": 11}, {"result_id": 12}], "bingx")
    assert error.value.code == "COHORT_EXCHANGE_MISMATCH"
