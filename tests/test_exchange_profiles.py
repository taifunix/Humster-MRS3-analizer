"""Active PerformanceDB entry and exchange profiles of config.local.json (P7 spec §3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.exchange_profiles import (
    ExchangeProfileError,
    exchange_template_names,
    load_exchange_profile,
    load_performance_databases,
)
from mrs3.performance_v2_store import load_performance_v2_config

REPOSITORY = Path(__file__).resolve().parents[1]


def _write(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _performance_config(root: Path) -> Path:
    return _write(root / "config.performance.json", {"unified_performance_v2": {"database_root": "data/performance-v2"}})


def test_without_a_section_the_performance_config_is_unchanged(tmp_path: Path) -> None:
    config = load_performance_v2_config(_performance_config(tmp_path), v1_database_root=tmp_path)

    assert config.database_root == (tmp_path / "data" / "performance-v2").resolve()
    assert (config.evaluation_protocol, config.exchange) == ("v1", None)
    assert load_performance_databases(tmp_path / "config.local.json").active is None


def test_the_active_entry_overrides_root_protocol_and_exchange(tmp_path: Path) -> None:
    _write(tmp_path / "config.local.json", {"performance_databases": {"active": "bingx-v2", "items": {
        "bybit-v2": {"database_root": "data/performance-v2-bybit", "exchange": "Bybit"},
        "bingx-v2": {"database_root": "data/performance-v2-bingx", "exchange": "BingX"},
    }}})

    config = load_performance_v2_config(_performance_config(tmp_path), v1_database_root=tmp_path)

    assert config.database_root == (tmp_path / "data" / "performance-v2-bingx").resolve()
    assert (config.evaluation_protocol, config.exchange) == ("v2", "BingX")


def test_a_null_active_entry_keeps_the_default_database(tmp_path: Path) -> None:
    _write(tmp_path / "config.local.json", {"performance_databases": {"active": None, "items": {
        "bingx-v2": {"database_root": "data/performance-v2-bingx", "exchange": "BingX"},
    }}})

    config = load_performance_v2_config(_performance_config(tmp_path), v1_database_root=tmp_path)

    assert config.database_root == (tmp_path / "data" / "performance-v2").resolve()
    assert config.exchange is None


def test_a_v1_entry_may_register_the_current_database(tmp_path: Path) -> None:
    _write(tmp_path / "config.local.json", {"performance_databases": {"active": "old", "items": {
        "old": {"database_root": "data/performance-v2", "evaluation_protocol": "v1"},
    }}})

    config = load_performance_v2_config(_performance_config(tmp_path), v1_database_root=tmp_path)

    assert (config.evaluation_protocol, config.exchange) == ("v1", None)


@pytest.mark.parametrize("section, message", [
    ({"active": "missing", "items": {}}, "PERFORMANCE_DATABASE_UNKNOWN"),
    ({"active": "a", "items": {
        "a": {"database_root": "data/x", "exchange": "Bybit"}, "b": {"database_root": "data/x/y", "exchange": "BingX"},
    }}, "overlaps the root of a"),
    ({"active": "a", "items": {"a": {"database_root": "data", "exchange": "Bybit"}}}, "overlaps the default"),
    ({"active": "a", "items": {"a": {"database_root": "../outside", "exchange": "Bybit"}}}, "relative"),
    ({"active": "a", "items": {"a": {"database_root": "C:/abs", "exchange": "Bybit"}}}, "relative"),
    ({"active": "a", "items": {"a": {"database_root": "data/x"}}}, "exchange"),
    ({"active": "a", "items": {"a": {"database_root": "data/x", "exchange": "Bybit", "extra": 1}}}, "unknown"),
    ({"active": "a", "items": {"a": {"database_root": "data/x", "exchange": "Bybit", "evaluation_protocol": "v3"}}}, "v1 or v2"),
])
def test_an_invalid_section_is_refused(tmp_path: Path, section: dict[str, object], message: str) -> None:
    _write(tmp_path / "config.local.json", {"performance_databases": section})

    with pytest.raises(ValueError, match=message):
        load_performance_v2_config(_performance_config(tmp_path), v1_database_root=tmp_path)


def _copy_exchange(root: Path, exchange: str) -> Path:
    import shutil

    target = root / "templates" / "exchanges" / exchange
    shutil.copytree(REPOSITORY / "templates" / "exchanges" / exchange, target)
    return target


def test_repository_exchange_templates_load() -> None:
    bybit = load_exchange_profile(REPOSITORY, "Bybit")
    bingx = load_exchange_profile(REPOSITORY, " bingx ")

    assert exchange_template_names(REPOSITORY) == ("BingX", "Bybit")
    assert (bybit.strategy_exchange, bybit.listing_dates_sheet) == ((("account", "bybit_ytl"),), None)
    assert dict(bybit.tester) == {"MakerFee": 1e-05, "TakerFee": 0.0002, "FundingRate": 0.00025, "FundingIntervalHours": 8}
    assert bybit.portfolio_strategy is not None and bybit.portfolio_tester is not None
    assert (bingx.exchange, bingx.strategy_exchange, bingx.listing_dates_sheet) == ("BingX", (("account", "bingx"),), "Пары_BingX")
    assert dict(bingx.tester) == {"MakerFee": 0.00012, "TakerFee": 0.0003, "FundingRate": 0.00025, "FundingIntervalHours": 8}
    # The optimizer is not adapted to BingX yet.
    assert bingx.portfolio_strategy is None and bingx.portfolio_tester is None
    assert bybit.digest != bingx.digest


def test_bingx_templates_differ_from_bybit_only_in_exchange_values() -> None:
    base = REPOSITORY / "templates" / "exchanges"
    bybit = json.loads((base / "Bybit" / "mrs3-strategy.json").read_text(encoding="utf-8"))
    bingx = json.loads((base / "BingX" / "mrs3-strategy.json").read_text(encoding="utf-8"))
    bybit["exchange"].update({"name": "BingX", "account": "bingx"})
    assert bingx == bybit
    bybit_tester = json.loads((base / "Bybit" / "mrs3-tester.json").read_text(encoding="utf-8"))
    bingx_tester = json.loads((base / "BingX" / "mrs3-tester.json").read_text(encoding="utf-8"))
    bybit_tester.update({"MakerFee": 0.00012, "TakerFee": 0.0003})
    assert bingx_tester == bybit_tester


def test_an_exchange_without_templates_is_refused(tmp_path: Path) -> None:
    _copy_exchange(tmp_path, "Bybit")

    with pytest.raises(ExchangeProfileError) as error:
        load_exchange_profile(tmp_path, "OKX")
    assert error.value.code == "EXCHANGE_TEMPLATES_MISSING"


@pytest.mark.parametrize("file, change, message", [
    ("exchange.json", {"exchange": "Other"}, "exchange.json"),
    ("exchange.json", {"extra": 1}, "exchange.json"),
    ("exchange.json", {"listing_dates_sheet": ""}, "listing_dates_sheet"),
    ("mrs3-strategy.json", {"exchange": {"name": "Bybit", "account": "x"}}, "exchange.name"),
    ("mrs3-strategy.json", {"exchange": {"name": "BingX"}}, "exchange.account"),
    ("mrs3-tester.json", {"TakerFee": "0.1"}, "TakerFee"),
    ("mrs3-tester.json", {"MakerFee": -0.1}, "MakerFee"),
    ("mrs3-tester.json", {"FundingIntervalHours": 0}, "FundingIntervalHours"),
    ("mrs3-tester.json", {"FundingRate": 10 ** 400}, "FundingRate"),
])
def test_an_inconsistent_template_folder_is_refused(tmp_path: Path, file: str, change: dict[str, object], message: str) -> None:
    folder = _copy_exchange(tmp_path, "BingX")
    document = json.loads((folder / file).read_text(encoding="utf-8"))
    document.update(change)
    (folder / file).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ExchangeProfileError, match=message) as error:
        load_exchange_profile(tmp_path, "BingX")
    assert error.value.code == "EXCHANGE_TEMPLATES_INVALID"


def test_a_missing_template_file_is_refused(tmp_path: Path) -> None:
    folder = _copy_exchange(tmp_path, "BingX")
    (folder / "mrs3-tester.json").unlink()

    with pytest.raises(ExchangeProfileError, match="unreadable"):
        load_exchange_profile(tmp_path, "BingX")


def _workbook(path: Path) -> Path:
    import pandas as pd

    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"Пара": ["btcusdt"], "Дата листинга на Bybit (UTC)": ["2026-01-01"]}).to_excel(
            writer, sheet_name="Пары", index=False,
        )
        pd.DataFrame({"Пара": ["TSLAUSDT", "dellusdt"], "Листинг": ["2026-06-04", "2026-07-27"]}).to_excel(
            writer, sheet_name="Пары_BingX", index=False,
        )
        pd.DataFrame({"Пара": ["X"], "Дата": ["2026-01-01"]}).to_excel(writer, sheet_name="Bad", index=False)
    return path


def test_listing_dates_are_read_from_the_profile_sheet(tmp_path: Path) -> None:
    from mrs3.loader import load_listing_dates

    workbook = _workbook(tmp_path / "liquidity.xlsx")

    assert list(load_listing_dates(workbook)) == ["BTCUSDT"]
    bingx = load_listing_dates(workbook, "Пары_BingX")
    assert sorted(bingx) == ["DELLUSDT", "TSLAUSDT"]
    assert str(bingx["TSLAUSDT"].date()) == "2026-06-04"


@pytest.mark.parametrize("sheet, message", [("Missing", "no sheet"), ("Bad", "needs columns")])
def test_a_wrong_listing_sheet_is_refused(tmp_path: Path, sheet: str, message: str) -> None:
    from mrs3.loader import InputError, load_listing_dates

    with pytest.raises(InputError, match=message):
        load_listing_dates(_workbook(tmp_path / "liquidity.xlsx"), sheet)


def test_a_listing_sheet_needs_a_workbook(tmp_path: Path) -> None:
    from mrs3.loader import InputError, load_listing_dates

    dates = tmp_path / "dates.csv"
    dates.write_text("ticker,launch\nBTCUSDT,2026-01-01\n", encoding="utf-8")

    with pytest.raises(InputError, match="not CSV"):
        load_listing_dates(dates, "Пары_BingX")


def test_the_digest_ignores_line_endings_and_lists_only_valid_folders(tmp_path: Path) -> None:
    folder = _copy_exchange(tmp_path, "BingX")
    before = load_exchange_profile(tmp_path, "BingX").digest
    for name in ("mrs3-strategy.json", "mrs3-tester.json"):
        path = folder / name
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    assert load_exchange_profile(tmp_path, "BingX").digest == before
    broken = tmp_path / "templates" / "exchanges" / "Broken"
    broken.mkdir()
    (broken / "exchange.json").write_text("{}", encoding="utf-8")
    assert exchange_template_names(tmp_path) == ("BingX",)
