"""Optional smoke tests on real tester CSV bundles (never committed to git).

 MRS3_TEST_CSV_NEW_ZIP=<162-run fixed-lot bundle>
 MRS3_TEST_CSV_PARTIAL_ZIP=<16-run bundle with partial closing fills>

Listing dates below are smoke-test values before report_start, not the registry.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mrs3.csv_direct_input import load_csv_direct

DATES = {"KOUSDT": "2026-09-01", "GMESTOCKUSDT": "2026-09-01"}


def _archive(env_key: str) -> Path:
    value = os.environ.get(env_key)
    if not value:
        pytest.skip(f"Set {env_key} to test the real archive")
    path = Path(value)
    assert path.is_file(), f"Real test archive is unavailable at {path}"
    return path


def test_fixed_lot_archive_counts_completed_trades():
    result = load_csv_direct([_archive("MRS3_TEST_CSV_NEW_ZIP")], listing_dates=DATES, allow_shared_snapshot_template=True)
    assert (len(result.frame), result.total_trades, result.total_fills, len(result.scope_keys)) == (162, 2179, 2179, 6)
    assert (result.frame["wins"] + result.frame["losses"] <= result.frame["trades"]).all()


def test_partial_fill_archive_separates_fills_from_trades():
    path = _archive("MRS3_TEST_CSV_PARTIAL_ZIP")
    # This bundle was run with use_fix=false, which Source v6 would not admit either.
    with pytest.raises(ValueError, match="only fixed-lot runs"):
        load_csv_direct([path], listing_dates=DATES)
    result = load_csv_direct([path], listing_dates=DATES, require_fixed_lot=False)
    assert (len(result.frame), result.total_trades, result.total_fills) == (16, 44, 421)
    assert (result.frame["fills"] > result.frame["trades"]).any()
