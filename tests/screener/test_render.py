from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.panel_testing import PanelTestingError
from mrs3.screener.render import render_screener_tester_config


def test_render_screener_tester_config_fixes_symbol_end_and_dates() -> None:
    template = '''{
      "StartDate": "2026-01-01T00:00:00",
      "EndDate": "2026-01-02T00:00:00",
      "parameter_mining": [
        {"name": "settings[*].mrs2.ma_long.len", "start": 1.0, "end": 1.0, "step": 1.0, "values": ["4"]},
        {"name": "settings[*].basic.symbol", "start": 1.0, "end": 5.0, "step": 1.0, "values": ["OLDUSDT", "AUSDT", "BUSDT", "CUSDT", "DUSDT"]},
      ],
      "report": {"enable_html_report": true}
    }'''

    rendered = json.loads(
        render_screener_tester_config(template, ("CXUSDT", "BABAUSDT"), "2026-07-15", "2026-08-06")
    )

    assert rendered["StartDate"] == "2026-07-15T00:00:00"
    assert rendered["EndDate"] == "2026-08-06T00:00:00"
    symbol_entry = next(
        entry for entry in rendered["parameter_mining"] if entry["name"] == "settings[*].basic.symbol"
    )
    assert symbol_entry["values"] == ["CXUSDT", "BABAUSDT"]
    assert symbol_entry["start"] == 1.0
    assert symbol_entry["step"] == 1.0
    assert symbol_entry["end"] == 2.0
    other_entry = next(
        entry for entry in rendered["parameter_mining"] if entry["name"] == "settings[*].mrs2.ma_long.len"
    )
    assert other_entry == {
        "name": "settings[*].mrs2.ma_long.len",
        "start": 1.0,
        "end": 1.0,
        "step": 1.0,
        "values": ["4"],
    }


def test_render_screener_tester_config_rejects_symbol_without_usdt_suffix() -> None:
    template = json.dumps(
        {
            "StartDate": "2026-01-01T00:00:00",
            "EndDate": "2026-01-02T00:00:00",
            "parameter_mining": [
                {"name": "settings[*].basic.symbol", "start": 1.0, "end": 1.0, "step": 1.0, "values": ["OLDUSDT"]},
            ],
        }
    )

    with pytest.raises(PanelTestingError, match="USDT"):
        render_screener_tester_config(template, ("CXBTC",), "2026-07-15", "2026-08-06")


def test_render_screener_tester_config_rejects_duplicate_symbols() -> None:
    template = json.dumps(
        {
            "StartDate": "2026-01-01T00:00:00",
            "EndDate": "2026-01-02T00:00:00",
            "parameter_mining": [
                {"name": "settings[*].basic.symbol", "start": 1.0, "end": 1.0, "step": 1.0, "values": ["OLDUSDT"]},
            ],
        }
    )

    with pytest.raises(PanelTestingError):
        render_screener_tester_config(template, ("CXUSDT", "cxusdt"), "2026-07-15", "2026-08-06")


@pytest.mark.parametrize(
    "template_name",
    ["config_tester_long_screen.json", "config_tester_short_screen.json"],
)
def test_real_screener_templates_render_304_combinations_per_pair(template_name: str) -> None:
    root = Path(__file__).parents[2]
    path = root / "templates" / "tester" / "mrs2" / template_name
    template = path.read_text(encoding="utf-8")
    original = json.loads(template)

    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    rendered = json.loads(
        render_screener_tester_config(template, symbols, "2026-08-01", "2026-09-18")
    )

    mining = rendered["parameter_mining"]
    total = 1
    for entry in mining:
        total *= len(entry["values"])
    assert total == 304 * len(symbols)

    symbol_entry = next(e for e in mining if e["name"] == "settings[*].basic.symbol")
    assert symbol_entry["values"] == list(symbols)
    assert symbol_entry["start"] == 1.0
    assert symbol_entry["step"] == 1.0
    assert symbol_entry["end"] == float(len(symbols))

    for entry in original["parameter_mining"]:
        if entry["name"] == "settings[*].basic.symbol":
            continue
        rendered_entry = next(item for item in mining if item["name"] == entry["name"])
        assert rendered_entry == entry
