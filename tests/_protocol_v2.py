"""Synthetic protocol v2 strategy templates for render tests (protocol v2 §4.2)."""

from __future__ import annotations

import json
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
RETEST_TEMPLATE = REPOSITORY / "templates" / "strategies" / "retest-mrs3" / "base.json"


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
