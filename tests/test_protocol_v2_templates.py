"""The repository templates meet protocol v2 with one base (protocol v2 §4.2, §4.3)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from mrs3.evaluation_protocol import EvaluationTarget, require_protocol_v2_tester_config


def test_repository_templates_meet_protocol_v2_with_one_base() -> None:
    from mrs3.evaluation_protocol import protocol_v2_unmet, tester_base
    from mrs3.panel_testing import mrs3_tester_config_template

    root = Path(__file__).resolve().parents[1]
    base = tester_base(mrs3_tester_config_template(root))
    tester = json.loads(mrs3_tester_config_template(root).read_text(encoding="utf-8"))
    strategy = json.loads((root / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))

    # Protocol v2 §4.3: one base for the strategy and the tester.
    assert Decimal(str(strategy["basic"]["my_fix_balance"])) == base
    require_protocol_v2_tester_config(tester, EvaluationTarget(base, strategy["exchange"]["name"]))
    assert protocol_v2_unmet(strategy, EvaluationTarget(base, strategy["exchange"]["name"])) == ()
    for key in ("ma_long", "ma_short"):
        assert {(entry["type"], entry["source"]) for entry in strategy["mrs3"][key]} == {("SMA", "ohlc4")}
