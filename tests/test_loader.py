from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.loader import InputError, load_points, normalize_shift
from mrs3.models import Side


def _config() -> AlgorithmConfig:
    return AlgorithmConfig.defaults()


def _source_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "StartDate": "2026-07-15 00:00:00",
        "EndDate": "2026-08-06 00:00:00",
        "TotalPnLPercent": 30.0,
        "TotalTrades": 20,
        "Win": 18,
        "Los": 2,
        "WinRate": 90.0,
        "MaxDrawdownPercent": 5.0,
        "ProfitFactor": 3.0,
        "Run id": 1,
        "settings[*].basic.symbol": "AAAUSDT",
        "settings[*].basic.time_frame": "2h",
        "settings[*].mrs2.ma_close_long.len": 4,
        "settings[*].mrs2.ma_long.len": 3,
        "settings[*].mrs2.ma_long.multiplier": 0.973,
    }
    row.update(overrides)
    return row


def _write_inputs(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[Path, Path]:
    csv_path = tmp_path / "input.csv"
    dates_path = tmp_path / "dates.xlsx"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    pd.DataFrame([["AAAUSDT", "2026-07-01"]]).to_excel(
        dates_path, index=False, header=False
    )
    return csv_path, dates_path


def _write_csv_dates(path: Path, rows: list[dict[str, object]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_long_and_short_shifts_use_integer_basis_points() -> None:
    assert normalize_shift(Side.LONG, "0.973") == 270
    assert normalize_shift(Side.SHORT, "1.027") == 270


def test_shift_grid_tolerance_is_configurable() -> None:
    assert (
        normalize_shift(
            Side.LONG,
            "0.99999995",
            tolerance_bp=Decimal("0.001"),
        )
        == 0
    )


def test_load_points_creates_stable_normalized_key(tmp_path: Path) -> None:
    csv_path, dates_path = _write_inputs(tmp_path, [_source_row()])

    points, audit = load_points(csv_path, dates_path, Side.LONG, _config())

    point = points.iloc[0]
    assert point["symbol"] == "AAAUSDT"
    assert point["timeframe"] == "2h"
    assert point["shift_bp"] == 270
    assert point["shift_pct"] == pytest.approx(2.7)
    assert point["point_id"] == "AAAUSDT|LONG|2h|270|3|4"
    assert audit.source_rows == 1
    assert audit.service_rows == 0


def test_load_points_accepts_bybit_csv_listing_dates(tmp_path: Path) -> None:
    csv_path, _ = _write_inputs(tmp_path, [_source_row()])
    dates_path = _write_csv_dates(
        tmp_path / "bybit dates_volume.csv",
        [{"ticker": "AAAUSDT", "launch": "2026-07-01", "volume": 123.0}],
    )

    points, _ = load_points(csv_path, dates_path, Side.LONG, _config())

    assert points.iloc[0]["listing_date"] == pd.Timestamp("2026-07-01", tz="UTC")


def test_load_points_normalizes_date_only_listing_and_report_times_to_utc(tmp_path: Path) -> None:
    row = _source_row(
        **{
            "StartDate": "2026-07-15T00:00:00+00:00",
            "EndDate": "2026-08-06T00:00:00+00:00",
        }
    )
    csv_path, _ = _write_inputs(tmp_path, [row])
    dates_path = _write_csv_dates(
        tmp_path / "bybit dates_volume.csv", [{"ticker": "AAAUSDT", "launch": "2026-07-01"}]
    )

    points, _ = load_points(csv_path, dates_path, Side.LONG, _config())

    assert str(points.iloc[0]["listing_date"].tz) == "UTC"
    assert str(points.iloc[0]["report_start"].tz) == "UTC"


@pytest.mark.parametrize(
    "rows",
    [
        [{"ticker": "", "launch": "2026-07-01"}],
        [{"ticker": "AAAUSDT", "launch": "not-a-date"}],
        [
            {"ticker": "AAAUSDT", "launch": "2026-07-01"},
            {"ticker": "AAAUSDT", "launch": "2026-07-02"},
        ],
    ],
)
def test_load_points_rejects_invalid_bybit_csv_listing_dates(
    tmp_path: Path, rows: list[dict[str, object]]
) -> None:
    csv_path, _ = _write_inputs(tmp_path, [_source_row()])
    dates_path = _write_csv_dates(tmp_path / "dates.csv", rows)

    with pytest.raises(InputError, match="listing dates|duplicate"):
        load_points(csv_path, dates_path, Side.LONG, _config())


def test_service_rows_are_excluded_and_counted(tmp_path: Path) -> None:
    rows = [_source_row(), _source_row(**{"settings[*].basic.symbol": None, "Run id": 2})]
    csv_path, dates_path = _write_inputs(tmp_path, rows)

    points, audit = load_points(csv_path, dates_path, Side.LONG, _config())

    assert len(points) == 1
    assert audit.service_rows == 1


def test_duplicate_parameter_cell_is_rejected(tmp_path: Path) -> None:
    rows = [_source_row(), _source_row(**{"Run id": 2})]
    csv_path, dates_path = _write_inputs(tmp_path, rows)

    with pytest.raises(InputError, match="duplicate parameter cell"):
        load_points(csv_path, dates_path, Side.LONG, _config())


def test_short_uses_short_specific_columns(tmp_path: Path) -> None:
    row = _source_row()
    row.pop("settings[*].mrs2.ma_close_long.len")
    row.pop("settings[*].mrs2.ma_long.len")
    row.pop("settings[*].mrs2.ma_long.multiplier")
    row.update(
        {
            "settings[*].mrs2.ma_close_short.len": 7,
            "settings[*].mrs2.ma_short.len": 6,
            "settings[*].mrs2.ma_short.multiplier": 1.027,
        }
    )
    csv_path, dates_path = _write_inputs(tmp_path, [row])

    points, _ = load_points(csv_path, dates_path, Side.SHORT, _config())

    assert points.iloc[0]["open_ma"] == 6
    assert points.iloc[0]["close_ma"] == 7
    assert points.iloc[0]["shift_bp"] == 270


def test_source_package_metadata_is_preserved_for_real_events(tmp_path: Path) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [
            _source_row(
                event_mode="real_independent_events",
                point_event_count=3,
                event_ids_hash="sha256:abc",
            )
        ],
    )

    points, _ = load_points(csv_path, dates_path, Side.LONG, _config())

    assert points[["event_mode", "point_event_count", "event_ids_hash"]].to_dict("records") == [
        {
            "event_mode": "real_independent_events",
            "point_event_count": 3,
            "event_ids_hash": "sha256:abc",
        }
    ]


@pytest.mark.parametrize("mode", [None, "", "unknown_mode"])
def test_source_package_rejects_missing_or_unknown_event_mode(tmp_path: Path, mode: object) -> None:
    csv_path, dates_path = _write_inputs(tmp_path, [_source_row(event_mode=mode)])

    with pytest.raises(InputError, match="exactly one known event_mode"):
        load_points(csv_path, dates_path, Side.LONG, _config())


def test_source_package_rejects_mixed_event_modes(tmp_path: Path) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [
            _source_row(event_mode="legacy_trades_proxy"),
            _source_row(
                **{
                    "Run id": 2,
                    "settings[*].mrs2.ma_long.multiplier": 0.974,
                    "event_mode": "real_independent_events",
                }
            ),
        ],
    )

    with pytest.raises(InputError, match="exactly one known event_mode"):
        load_points(csv_path, dates_path, Side.LONG, _config())


@pytest.mark.parametrize("count", [3.5, -1, float("inf")])
def test_declared_event_mode_rejects_invalid_point_event_count(tmp_path: Path, count: object) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [_source_row(event_mode="real_independent_events", point_event_count=count, event_ids_hash="sha256:abc")],
    )

    with pytest.raises(InputError, match="point_event_count"):
        load_points(csv_path, dates_path, Side.LONG, _config())


@pytest.mark.parametrize("event_ids_hash", [None, "", "   "])
def test_declared_event_mode_rejects_missing_event_ids_hash(
    tmp_path: Path, event_ids_hash: object
) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [_source_row(event_mode="real_independent_events", point_event_count=3, event_ids_hash=event_ids_hash)],
    )

    with pytest.raises(InputError, match="event_ids_hash"):
        load_points(csv_path, dates_path, Side.LONG, _config())


def test_legacy_package_requires_explicit_no_event_ids_sentinel(tmp_path: Path) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [_source_row(event_mode="legacy_trades_proxy", point_event_count=20, event_ids_hash="sha256:abc")],
    )

    with pytest.raises(InputError, match="LEGACY_PROXY_NO_EVENT_IDS"):
        load_points(csv_path, dates_path, Side.LONG, _config())


def test_legacy_package_requires_event_count_to_match_total_trades(tmp_path: Path) -> None:
    csv_path, dates_path = _write_inputs(
        tmp_path,
        [
            _source_row(
                event_mode="legacy_trades_proxy",
                point_event_count=3,
                event_ids_hash="LEGACY_PROXY_NO_EVENT_IDS",
            )
        ],
    )

    with pytest.raises(InputError, match="point_event_count must equal TotalTrades"):
        load_points(csv_path, dates_path, Side.LONG, _config())
