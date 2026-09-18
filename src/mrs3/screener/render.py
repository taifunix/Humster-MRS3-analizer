"""Screener tester-config rendering: same as RUNNER 01, plus a fixed symbol-entry end."""

from __future__ import annotations

import json
import re

from mrs3.panel_testing import PanelTestingError, render_tester_config

_USDT_SUFFIX = re.compile(r"USDT$")


def render_screener_tester_config(
    template: str,
    symbols: tuple[str, ...],
    start: str,
    end: str,
    *,
    max_parallel_runs: int | None = None,
) -> str:
    if not isinstance(symbols, tuple) or not symbols:
        raise PanelTestingError("invalid tester configuration")
    clean_symbols = tuple(symbol.strip().upper() for symbol in symbols)
    missing_suffix = [symbol for symbol in clean_symbols if not _USDT_SUFFIX.search(symbol)]
    if missing_suffix:
        raise PanelTestingError(
            "screener symbols must end with USDT: " + ", ".join(missing_suffix)
        )

    rendered = render_tester_config(
        template, symbols, start, end, max_parallel_runs=max_parallel_runs
    )
    document = json.loads(rendered)
    targets = [
        entry
        for entry in document["parameter_mining"]
        if entry.get("name") == "settings[*].basic.symbol"
    ]
    targets[0]["start"] = 1.0
    targets[0]["step"] = 1.0
    targets[0]["end"] = float(len(clean_symbols))
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
