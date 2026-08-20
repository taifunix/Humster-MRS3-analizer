from __future__ import annotations

import duckdb
import pytest

from mrs3.analysis_storage import publish_surface
from mrs3.published_surface import load_published_surface
from tests.test_analysis_storage import _real_surface, _surface, _v2_surface


def test_load_published_surface_reads_only_materialized_analysis_points() -> None:
    connection = duckdb.connect(":memory:")
    try:
        surface = publish_surface(connection, _v2_surface())

        loaded = load_published_surface(connection, surface.surface_id)

        assert loaded.surface_id == surface.surface_id
        assert loaded.points.loc[0, "point_id"] == "BTCUSDT|LONG|1h|100|3|9"
        assert loaded.points.loc[0, "event_mode"] == "real_independent_events"
        assert loaded.points.loc[0, "point_event_count"] == 7
        assert loaded.points.loc[0, ["pnl_pct", "dd_pct", "trades", "wins", "losses", "win_rate_pct", "profit_factor"]].to_dict() == {
            "pnl_pct": 10.0, "dd_pct": 2.0, "trades": 7, "wins": 5, "losses": 2,
            "win_rate_pct": 71.4, "profit_factor": 2.0,
        }
        assert "listing_date" not in loaded.points
    finally:
        connection.close()


def test_load_published_surface_restores_exact_event_membership() -> None:
    connection = duckdb.connect(":memory:")
    try:
        surface = publish_surface(connection, _v2_surface())

        loaded = load_published_surface(connection, surface.surface_id)

        assert loaded.points.loc[0, "event_mode"] == "real_independent_events"
        assert loaded.points.loc[0, "point_event_count"] == 7
        assert loaded.points.loc[0, "_event_ids"] == tuple(f"{index:064x}" for index in range(1, 8))
    finally:
        connection.close()


def test_load_published_surface_rejects_legacy_trades_proxy_surface() -> None:
    connection = duckdb.connect(":memory:")
    try:
        surface = publish_surface(connection, _surface())

        with pytest.raises(ValueError, match="fresh canonical"):
            load_published_surface(connection, surface.surface_id)
    finally:
        connection.close()


def test_load_published_surface_rejects_real_events_without_canonical_grid() -> None:
    connection = duckdb.connect(":memory:")
    try:
        surface = publish_surface(connection, _real_surface())

        with pytest.raises(ValueError, match="fresh canonical"):
            load_published_surface(connection, surface.surface_id)
    finally:
        connection.close()
