"""Active PerformanceDB entry and exchange profiles of config.local.json (P7 spec §3)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.exchange_profiles import (
    ExchangeProfileError,
    load_exchange_profile,
    load_performance_databases,
    materialize_tester_template,
)
from mrs3.performance_v2_store import load_performance_v2_config

BINGX_PROFILE = {
    "strategy_exchange": {"account": "bingx"},
    "tester": {"MakerFee": 0.00012, "TakerFee": 0.0003, "FundingRate": 0.00025, "FundingIntervalHours": 8},
    "listing_dates": {"path": "input/liquidity.xlsx", "sheet": "Пары_BingX"},
}


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


def test_bybit_needs_no_profile_and_another_exchange_does(tmp_path: Path) -> None:
    local = tmp_path / "config.local.json"

    bybit = load_exchange_profile(local, "Bybit")
    assert (bybit.strategy_exchange, bybit.tester, bybit.listing_dates) == ((), (), None)
    with pytest.raises(ExchangeProfileError) as error:
        load_exchange_profile(local, "BingX")
    assert error.value.code == "EXCHANGE_PROFILE_MISSING"


def test_a_profile_is_matched_without_case(tmp_path: Path) -> None:
    local = _write(tmp_path / "config.local.json", {"exchange_profiles": {"bingx": BINGX_PROFILE}})

    profile = load_exchange_profile(local, "BingX")

    assert profile.strategy_exchange == (("account", "bingx"),)
    assert dict(profile.tester)["TakerFee"] == 0.0003
    assert (profile.listing_dates.path, profile.listing_dates.sheet) == (Path("input/liquidity.xlsx"), "Пары_BingX")


@pytest.mark.parametrize("profile, message", [
    ({"strategy_exchange": {"name": "BingX"}}, "strategy_exchange"),
    ({"tester": {"InitialBalance": 1}}, "tester"),
    ({"tester": {"MakerFee": "0.1"}}, "MakerFee"),
    ({"tester": {"TakerFee": -0.1}}, "TakerFee"),
    ({"tester": {"FundingIntervalHours": 0}}, "FundingIntervalHours"),
    ({"tester": {"FundingRate": 10 ** 400}}, "FundingRate"),
    ({"listing_dates": {"path": "../dates.xlsx"}}, "listing_dates"),
    ({"listing_dates": {"path": "input/d.xlsx", "sheet": ""}}, "sheet"),
    ({"margin": {}}, "unknown"),
])
def test_an_invalid_profile_is_refused(tmp_path: Path, profile: dict[str, object], message: str) -> None:
    local = _write(tmp_path / "config.local.json", {"exchange_profiles": {"BingX": profile}})

    with pytest.raises(ExchangeProfileError, match=message) as error:
        load_exchange_profile(local, "BingX")
    assert error.value.code == "EXCHANGE_PROFILE_INVALID"


def test_two_profiles_for_one_exchange_are_ambiguous(tmp_path: Path) -> None:
    local = _write(tmp_path / "config.local.json", {"exchange_profiles": {"BingX": {}, "bingx": {}}})

    with pytest.raises(ExchangeProfileError, match="more than one"):
        load_exchange_profile(local, "BingX")


def test_the_effective_tester_template_is_content_addressed(tmp_path: Path) -> None:
    template = _write(tmp_path / "config_tester.json", {"InitialBalance": 10000.0, "MakerFee": 1e-05, "TakerFee": 0.0002})
    directory = tmp_path / "results" / ".tester-templates"

    assert materialize_tester_template(template, (), directory) == template
    first = materialize_tester_template(template, (("MakerFee", 0.00012), ("TakerFee", 0.0003)), directory)
    second = materialize_tester_template(template, (("MakerFee", 0.00012), ("TakerFee", 0.0003)), directory)

    assert first == second and first.parent == directory
    document = json.loads(first.read_text(encoding="utf-8"))
    assert document == {"InitialBalance": 10000.0, "MakerFee": 0.00012, "TakerFee": 0.0003}
    assert Decimal(str(document["MakerFee"])) == Decimal("0.00012")


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


def test_concurrent_renders_write_one_template(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    template = _write(tmp_path / "config_tester.json", {"InitialBalance": 10000.0, "MakerFee": 1e-05})
    fields = (("MakerFee", 0.00012),)
    for round_ in range(10):
        directory = tmp_path / f"round-{round_}"
        with ThreadPoolExecutor(8) as pool:
            paths = set(pool.map(lambda _i: materialize_tester_template(template, fields, directory), range(16)))
        assert len(paths) == 1
        assert [path.name for path in directory.iterdir()] == [next(iter(paths)).name]
