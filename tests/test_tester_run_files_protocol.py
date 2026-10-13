"""Protocol v2 checks of RUNS snapshots (protocol v2 §7a)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.config import AlgorithmConfig
from mrs3.evaluation_protocol import EvaluationProtocolError, EvaluationTarget
from mrs3.tester_run_files import render_run_snapshot
from tests._protocol_v2 import protocol_v2_template

TARGET = EvaluationTarget(Decimal("10000"), "Bybit")


def _snapshot(tmp_path: Path, *, volume_check: bool = False, balance: float = 10000.0) -> Path:
    strategy = protocol_v2_template(exchange="Template", my_fix_balance=0.0)
    path = tmp_path / "run_snapshot.json"
    path.write_text(json.dumps({
        "settings": [strategy],
        "tester_config": {"InitialBalance": balance, "LimitOrderVolumeCheck": volume_check},
    }), encoding="utf-8")
    return path


def _structure() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "common_close_ma": 5, "order_count": 1,
        "structure_id": "STR_1", "orders": ({"open_ma": 7, "shift_bp": 50, "shift_pct": 0.5},),
    }


def test_runs_snapshot_renders_protocol_v2(tmp_path: Path) -> None:
    _name, rendered = render_run_snapshot(
        _snapshot(tmp_path), _structure(), "2026-01-01", "2026-02-01", 4, AlgorithmConfig.defaults(), evaluation=TARGET,
    )

    strategy = rendered["settings"][0]
    assert strategy["basic"]["my_fix_balance"] == 10000.0 and strategy["exchange"]["name"] == "Bybit"


@pytest.mark.parametrize(("volume_check", "balance", "field"), [(True, 10000.0, "LimitOrderVolumeCheck"), (False, 1000.0, "InitialBalance")])
def test_runs_snapshot_with_a_non_v2_tester_config_is_refused(tmp_path: Path, volume_check: bool, balance: float, field: str) -> None:
    with pytest.raises(EvaluationProtocolError, match=field):
        render_run_snapshot(
            _snapshot(tmp_path, volume_check=volume_check, balance=balance), _structure(),
            "2026-01-01", "2026-02-01", 4, AlgorithmConfig.defaults(), evaluation=TARGET,
        )
