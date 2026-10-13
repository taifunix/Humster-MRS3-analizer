from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from mrs3.evaluation_protocol import (
    DATABASE_PROTOCOL_V1,
    PROTOCOL_V2_RENDER_REJECTED,
    PROTOCOL_V2_TESTER_CONFIG_REJECTED,
    EvaluationProtocolError,
    EvaluationTarget,
    apply_protocol_v2,
    evaluation_target,
    require_protocol_v2_tester_config,
)
from mrs3.performance_v2_store import PerformanceV2Config, initialize_performance_v2, performance_v2_database_path

TARGET = EvaluationTarget(Decimal("10000"), "Bybit")


def _strategy(side: str = "LONG", **basic: object) -> dict[str, object]:
    suffix = side.lower()
    return {
        "name": "alpha",
        "exchange": {"name": "Template", "use_upnl": False, "use_frozen_balance": True},
        "basic": {
            "strategy": "mrs3", "symbol": "BTCUSDT", "use_long": side == "LONG", "use_short": side == "SHORT",
            "use_fix": True, "my_fix_balance": 0.0, "balance_percentage_long": 100.0,
            "balance_percentage_short": 100.0, "risk_long": 1.0, "risk_short": 1.0, "max_balance": 0.0,
            **basic,
        },
        "mrs3": {
            f"ma_{suffix}": [{"id": 1, "type": "SMA", "source": "ohlc4", "len": 7}],
            f"ma_close_{suffix}": {"type": "SMA", "source": "ohlc4", "len": 3},
        },
    }


@pytest.mark.parametrize("side", ("LONG", "SHORT"))
def test_apply_sets_base_and_exchange_and_accepts_protocol_v2(side: str) -> None:
    strategy = apply_protocol_v2(_strategy(side), TARGET)

    assert strategy["basic"]["my_fix_balance"] == 10000.0  # type: ignore[index]
    assert strategy["exchange"]["name"] == "Bybit"  # type: ignore[index]


@pytest.mark.parametrize(("basic", "field"), [
    ({"use_fix": False}, "basic.use_fix"),
    ({"risk_long": 0.5}, "basic.risk_long"),
    ({"max_balance": 100.0}, "basic.max_balance"),
])
def test_apply_names_the_unmet_fields(basic: dict[str, object], field: str) -> None:
    with pytest.raises(EvaluationProtocolError) as error:
        apply_protocol_v2(_strategy(**basic), TARGET)

    assert error.value.code == PROTOCOL_V2_RENDER_REJECTED
    assert field in str(error.value)


def test_apply_rejects_a_dynamic_template_and_missing_ma_facts() -> None:
    strategy = _strategy()
    strategy["exchange"]["use_upnl"] = True  # type: ignore[index]
    del strategy["mrs3"]["ma_close_long"]["source"]  # type: ignore[index]

    with pytest.raises(EvaluationProtocolError, match="exchange.use_upnl"):
        apply_protocol_v2(strategy, TARGET)
    strategy["exchange"]["use_upnl"] = False  # type: ignore[index]
    with pytest.raises(EvaluationProtocolError, match="ma_close_long.type/source"):
        apply_protocol_v2(strategy, TARGET)


def test_tester_config_needs_the_base_and_no_volume_check() -> None:
    require_protocol_v2_tester_config({"LimitOrderVolumeCheck": False, "InitialBalance": 10000}, TARGET)
    with pytest.raises(EvaluationProtocolError) as error:
        require_protocol_v2_tester_config({"LimitOrderVolumeCheck": True, "InitialBalance": 1000}, TARGET)

    assert error.value.code == PROTOCOL_V2_TESTER_CONFIG_REJECTED
    assert "LimitOrderVolumeCheck" in str(error.value) and "InitialBalance" in str(error.value)


def _tester_template(tmp_path: Path, balance: object = 10000.0) -> Path:
    path = tmp_path / "config_tester.json"
    path.write_text(json.dumps({"InitialBalance": balance, "LimitOrderVolumeCheck": False}), encoding="utf-8")
    return path


def test_target_refuses_a_v1_database_before_any_render(tmp_path: Path) -> None:
    template = _tester_template(tmp_path)
    for config in (PerformanceV2Config(tmp_path / "v1"), PerformanceV2Config(tmp_path / "v1x", exchange="Bybit")):
        with pytest.raises(EvaluationProtocolError) as error:
            evaluation_target(config, template)
        assert error.value.code == DATABASE_PROTOCOL_V1


def test_target_reads_the_base_and_checks_the_database_marker(tmp_path: Path) -> None:
    template = _tester_template(tmp_path)
    config = PerformanceV2Config(tmp_path / "v2", evaluation_protocol="v2", exchange="Bybit")

    assert evaluation_target(config, template) == TARGET
    assert evaluation_target(config, template, project_root=tmp_path).database == "v2"
    database = performance_v2_database_path(config)
    database.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as connection:
        initialize_performance_v2(connection, evaluation_protocol="v2", exchange="BingX")
    with pytest.raises(EvaluationProtocolError) as error:
        evaluation_target(config, template)
    assert error.value.code == "EXCHANGE_MISMATCH"



def test_use_upnl_must_be_exactly_false() -> None:
    for value in (None, "false", 0):
        strategy = _strategy()
        if value is None:
            del strategy["exchange"]["use_upnl"]  # type: ignore[attr-defined]
        else:
            strategy["exchange"]["use_upnl"] = value  # type: ignore[index]
        with pytest.raises(EvaluationProtocolError, match="exchange.use_upnl"):
            apply_protocol_v2(strategy, TARGET)


def test_stored_ma_facts_pair_by_order_and_keep_template_halves() -> None:
    from mrs3.evaluation_protocol import apply_stored_ma_facts

    strategy = {"mrs3": {
        "ma_short": [{"id": index, "type": "SMA", "source": "ohlc4"} for index in (1, 2, 3)],
        "ma_close_short": {"type": "SMA", "source": "ohlc4"},
    }}
    facts = (("EMA", None), {1: ("WMA", "hl2"), 2: (None, "close")})

    apply_stored_ma_facts(strategy, "SHORT", facts, (1, 2, 3))

    assert [(entry["type"], entry["source"]) for entry in strategy["mrs3"]["ma_short"]] == [
        ("WMA", "hl2"), ("SMA", "close"), ("SMA", "ohlc4"),
    ]
    assert (strategy["mrs3"]["ma_close_short"]["type"], strategy["mrs3"]["ma_close_short"]["source"]) == ("EMA", "ohlc4")
    with pytest.raises(EvaluationProtocolError):
        apply_stored_ma_facts(strategy, "SHORT", facts, (1, 2))


BINGX = EvaluationTarget(
    Decimal("10000"), "BingX",
    strategy_exchange=(("account", "bingx"),),
    tester_fields=(("MakerFee", 0.00012), ("TakerFee", 0.0003)),
    profile_digest="p" * 64,
)


def test_a_profile_sets_the_strategy_account_and_requires_it() -> None:
    from mrs3.evaluation_protocol import protocol_v2_unmet

    strategy = apply_protocol_v2(_strategy(), BINGX)

    assert strategy["exchange"]["name"] == "BingX"  # type: ignore[index]
    assert strategy["exchange"]["account"] == "bingx"  # type: ignore[index]
    strategy["exchange"]["account"] = "bybit_ytl"  # type: ignore[index]
    assert protocol_v2_unmet(strategy, BINGX) == ("exchange.account",)


def test_a_profile_requires_its_fees_in_the_tester_config() -> None:
    config = {"InitialBalance": 10000.0, "LimitOrderVolumeCheck": False, "MakerFee": 0.00012, "TakerFee": 0.0003}
    require_protocol_v2_tester_config(config, BINGX)

    with pytest.raises(EvaluationProtocolError, match="MakerFee, TakerFee") as error:
        require_protocol_v2_tester_config({**config, "MakerFee": 1e-05, "TakerFee": 0.0002}, BINGX)
    assert error.value.code == PROTOCOL_V2_TESTER_CONFIG_REJECTED


def test_the_target_carries_the_profile_and_its_digest(tmp_path: Path) -> None:
    from mrs3.exchange_profiles import ExchangeProfile

    template = _tester_template(tmp_path)
    config = PerformanceV2Config(tmp_path / "v2", evaluation_protocol="v2", exchange="BingX")
    profile = ExchangeProfile("BingX", (("account", "bingx"),), (("MakerFee", 0.00012),))

    target = evaluation_target(config, template, profile)
    bare = evaluation_target(config, template, ExchangeProfile("BingX"))

    assert (target.strategy_exchange, target.tester_fields) == (profile.strategy_exchange, profile.tester)
    assert target.profile_digest == profile.digest()
    assert bare.profile_digest is None
    with pytest.raises(EvaluationProtocolError) as error:
        evaluation_target(config, template, ExchangeProfile("Bybit"))
    assert error.value.code == "EXCHANGE_PROFILE_INVALID"


def test_the_finalist_digest_changes_only_with_a_profile() -> None:
    from mrs3.performance_v2_finalist_retest import finalist_retest_config_digest

    plain = EvaluationTarget(Decimal("10000"), "BingX")
    other = EvaluationTarget(Decimal("10000"), "BingX", profile_digest="q" * 64)

    digests = {finalist_retest_config_digest({}, {}, evaluation=target) for target in (plain, BINGX, other)}
    assert len(digests) == 3
    assert finalist_retest_config_digest({}, {}, evaluation=EvaluationTarget(Decimal("10000"), "BingX", database="x")) \
        == finalist_retest_config_digest({}, {}, evaluation=plain)
