"""Synthetic protocol v2 strategy templates for render tests (protocol v2 §4.2)."""

from __future__ import annotations

import json
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
RETEST_TEMPLATE = REPOSITORY / "templates" / "exchanges" / "Bybit" / "mrs3-strategy.json"


def protocol_v2_template(exchange: str = "Template", my_fix_balance: float = 0.0) -> dict[str, object]:
    """The retest template forced to protocol v2, with a foreign exchange and base so a render must set both."""
    template = json.loads(RETEST_TEMPLATE.read_text(encoding="utf-8"))
    template["exchange"].update({"name": exchange, "use_upnl": False, "use_frozen_balance": True})
    template["basic"].update({
        "use_fix": True, "my_fix_balance": my_fix_balance, "balance_percentage_long": 100.0,
        "balance_percentage_short": 100.0, "risk_long": 1.0, "risk_short": 1.0, "max_balance": 0.0,
    })
    for key in ("ma_long", "ma_short"):
        for entry in template["mrs3"][key]:
            entry.update({"type": "SMA", "source": "ohlc4"})
    for key in ("ma_close_long", "ma_close_short"):
        template["mrs3"][key].update({"type": "SMA", "source": "ohlc4"})
    return template


def use_protocol_v2_target(monkeypatch, exchange: str = "Bybit", *, check_launch: bool = False):
    """Give a Panel test the protocol v2 target of a v2 database without writing its configuration.

    Launch checks read real manifests and RUNS snapshots; tests that stub those
    keep them off, and dedicated tests turn them on with `check_launch=True`.
    """
    from mrs3.evaluation_protocol import EvaluationTarget, tester_base
    from mrs3.panel import PanelController
    from mrs3.panel_testing import mrs3_tester_config_template

    target = EvaluationTarget(tester_base(mrs3_tester_config_template(REPOSITORY)), exchange)
    monkeypatch.setattr(PanelController, "_evaluation_target", lambda _self: target)
    if not check_launch:
        monkeypatch.setattr(PanelController, "_require_protocol_v2_manifest", lambda _self, _manifest, _target: None)
        monkeypatch.setattr(PanelController, "_require_protocol_v2_runs", lambda _self, _target: None)
        monkeypatch.setattr(PanelController, "_require_protocol_v2_tester_template", lambda _self, _target: None)
    return target


def protocol_v2_run_snapshot(base: float = 10000.0) -> dict[str, object]:
    """A RUNS snapshot whose strategy and tester config meet protocol v2."""
    return {
        "settings": [protocol_v2_template()],
        "tester_config": {"InitialBalance": base, "LimitOrderVolumeCheck": False},
    }
