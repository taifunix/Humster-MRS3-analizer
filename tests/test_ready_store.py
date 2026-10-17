from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from mrs3.mrs2_simulator import RunSettings
from mrs3.period_csv import MA_LENGTHS, SHIFTS_BP, grid
from mrs3.ready_store import RULE_VERSION, PeriodFeatures, ReadyRule, best_structures, build_ready, zone_scores
from mrs3.trade_store import (
    HOURLY_SCHEMA, METRICS_SCHEMA, TRADE_SCHEMA, SegmentKey, SegmentTables, TradeStore, point_id,
)

REPO = Path(__file__).parents[1]
TF15 = [p for p in grid(("15m",))]
IDS = np.array([point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len) for p in TF15])


def features(upi, dd=None, trades=None) -> PeriodFeatures:
    shifts = np.array([p.shift_bp for p in TF15], dtype=float)
    return PeriodFeatures(IDS, np.asarray(upi, dtype=float),
                          shifts / 100 if dd is None else np.asarray(dd, dtype=float),
                          np.full(len(IDS), 5) if trades is None else np.asarray(trades))


def test_zone_score_is_the_median_of_the_26_cube_neighbours():
    values = np.arange(len(IDS), dtype=float) ** 1.5
    scores = zone_scores(IDS, values)
    cell = {(p.shift_bp, p.open_len, p.close_len): i for i, p in enumerate(TF15)}
    s, o, c = SHIFTS_BP.index(230), 4, 5                      # an interior cell
    neighbours = [values[cell[(SHIFTS_BP[s + a], o + b, c + d)]] for a in (-1, 0, 1) for b in (-1, 0, 1)
                  for d in (-1, 0, 1) if (a, b, d) != (0, 0, 0)]
    assert scores[cell[(230, 4, 5)]] == pytest.approx(np.median(neighbours))
    corner = [values[cell[(SHIFTS_BP[a], 2 + b, 2 + d)]] for a in (0, 1) for b in (0, 1) for d in (0, 1)
              if (a, b, d) != (0, 0, 0)]
    assert scores[cell[(70, 2, 2)]] == pytest.approx(np.median(corner))          # 7 neighbours inside the grid


def test_best_structure_prefers_score_then_lower_drawdown_and_needs_active_points():
    flat = features(np.full(len(IDS), 30.0))
    [best] = best_structures([flat, flat, flat])
    assert (best["timeframe"], best["close_ma"]) == ("15m", 2)
    assert [(o["shift_bp"], o["open_ma"]) for o in best["orders"]] == [(70, 2), (110, 2)]   # 110 >= 1.5 x 70
    assert best["score"] == pytest.approx(30.0) and best["dd"] == pytest.approx(0.9) and best["admitted"]

    quiet = np.array([0 if p.shift_bp == 70 else 5 for p in TF15])
    [best] = best_structures([features(np.full(len(IDS), 30.0), trades=quiet), flat, flat])
    assert [o["shift_bp"] for o in best["orders"]] == [90, 140]         # 70 has no trade in the newest period

    risky = features(np.full(len(IDS), 30.0), dd=np.full(len(IDS), 7.0))
    [best] = best_structures([risky, flat])                            # DD = max over periods of the order mean
    assert best["dd"] == pytest.approx(7.0) and not best["admitted"]


def test_combined_score_weights_the_newest_period_most():
    rising = features([10.0 + p.shift_bp / 10 for p in TF15])
    falling = features([70.0 - p.shift_bp / 10 for p in TF15])
    [newest_rising] = best_structures([rising, falling])           # 0.6 x rising + 0.4 x falling grows with shift
    [newest_falling] = best_structures([falling, rising])
    assert newest_rising["orders"][-1]["shift_bp"] == 550 and newest_falling["orders"][0]["shift_bp"] == 70
    rule = ReadyRule()
    assert sum(rule.weights_3) == pytest.approx(1.0) and sum(rule.weights_2) == pytest.approx(1.0)


def _metrics(upi_by_shift: dict[int, float], days: float, trades: int = 5) -> pa.Table:
    rows = {name: [] for name in METRICS_SCHEMA.names}
    for pid, p in zip(IDS, TF15):
        pnl = upi_by_shift[p.shift_bp] * 1.0 * days / 30            # Ulcer 1.0 -> upi = pnl30
        values = dict(point_id=int(pid), final_balance=10_000 * (1 + pnl / 100), marked_final=10_000 * (1 + pnl / 100),
                      trades=trades, wins=trades, gross_profit=1.0, gross_loss=0.0, fees=0.1, max_drawdown=1.0,
                      max_drawdown_percent=p.shift_bp / 200, ulcer_index=1.0, position_avg_percent=99.0,
                      position_max_percent=99.0, last_ms=0)
        for name in METRICS_SCHEMA.names:
            rows[name].append(values[name])
    return pa.Table.from_pydict(rows, schema=METRICS_SCHEMA)


def _store_with_periods(root: Path, symbol: str, count: int, upi: float) -> TradeStore:
    store = TradeStore(root)
    first = date(2026, 7, 1)
    for index in range(count):
        start, end = first + timedelta(days=20 * index), first + timedelta(days=20 * (index + 1))
        tables = SegmentTables(TRADE_SCHEMA.empty_table(), HOURLY_SCHEMA.empty_table(),
                               _metrics({shift: upi for shift in SHIFTS_BP}, 20))
        record = store.write_files(SegmentKey("bybit", symbol, "simulator", start, end, "eng"), tables,
                                   settings=RunSettings(), tick_size=0.01, qty_step=0.01)
        store.register(record)
    return store


def test_ready_folder_manifest_and_catalog_are_deterministic(tmp_path: Path):
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 30.0)
    _store_with_periods(tmp_path / "store", "YOUNGUSDT", 1, 30.0)
    template = json.loads((REPO / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    summary = build_ready(store, ["GOODUSDT", "YOUNGUSDT", "GOODUSDT"], tmp_path / "r1", template)
    again = build_ready(store, ["GOODUSDT", "YOUNGUSDT"], tmp_path / "r2", template)
    assert summary == again == {"pairs": 2, "ready_pairs": 1, "errors": 0, "strategies": 1, "not_enough_periods": 1,
                                "tester_pending": 0}
    files = sorted(path.relative_to(tmp_path / "r1") for path in (tmp_path / "r1").rglob("*.json"))
    assert files == sorted(path.relative_to(tmp_path / "r2") for path in (tmp_path / "r2").rglob("*.json"))
    for name in files:
        assert (tmp_path / "r1" / name).read_bytes() == (tmp_path / "r2" / name).read_bytes()
    manifest = json.loads((tmp_path / "r1" / "ready_manifest.json").read_text(encoding="utf-8"))
    good, young = manifest["pairs"]
    assert manifest["rule_version"] == RULE_VERSION
    assert young["status"] == "NOT_ENOUGH_PERIODS" and len(young["periods"]) == 1
    assert good["status"] == "READY" and len(good["periods"]) == 3
    [item] = good["structures"]
    assert (item["category"], item["best_in_category"], item["pair_rank"]) == ("low", True, 1)
    assert [o["shift_bp"] for o in item["orders"]] == [70, 110]
    strategy = json.loads((tmp_path / "r1" / "strategies" / f"{item['strategy']}.json").read_text(encoding="utf-8"))
    entries = strategy["mrs3"]["ma_long"] if "ma_long" in strategy["mrs3"] else next(
        value for key, value in strategy["mrs3"].items() if isinstance(value, list))
    assert [entry["len"] for entry in entries] == [2, 2] and entries[0]["lot_x"] == entries[1]["lot_x"]
    assert strategy["basic"]["symbol"] == "GOODUSDT" and strategy["basic"]["time_frame"] == "15m"
    with store.connect() as con:
        rows = con.execute("select symbol, rule_version, inputs_sha256 from ready order by symbol").fetchall()
    assert [row[0] for row in rows] == ["GOODUSDT", "YOUNGUSDT"]          # same inputs: one row per pair
    assert rows[0][2] == good["inputs_sha256"]
    with store.connect() as con:
        payload = json.loads(con.execute("select payload_json from ready where symbol = 'GOODUSDT'").fetchone()[0])
    assert "best_in_category" not in payload["structures"][0]
    assert MA_LENGTHS[0] == 2


def test_periods_are_restricted_to_their_common_grid_points():
    from mrs3.ready_store import common_points
    newest = features(np.full(len(IDS), 30.0))
    extra = point_id("15m", 750, 2, 2)                                  # off the production grid
    older = PeriodFeatures(np.r_[IDS[::-1][:-10], extra], np.r_[np.full(len(IDS) - 10, 30.0), 99.0],
                           np.r_[np.full(len(IDS) - 10, 1.0), 1.0], np.full(len(IDS) - 9, 5))
    first, second = common_points([newest, older])
    assert list(first.point_ids) == list(second.point_ids) == sorted(IDS[10:])
    [best] = best_structures([first, second])
    assert best["admitted"]


def test_tester_mode_takes_the_newest_period_from_the_tester_segment(tmp_path: Path):
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 30.0)
    template = json.loads((REPO / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))
    pending = build_ready(store, ["GOODUSDT"], tmp_path / "pending", template, latest_source="tester")
    assert pending["tester_pending"] == 1 and pending["ready_pairs"] == 0
    newest = store.segments("GOODUSDT")[-1]
    tables = SegmentTables(TRADE_SCHEMA.empty_table(), HOURLY_SCHEMA.empty_table(),
                           _metrics({shift: 40.0 for shift in SHIFTS_BP}, 20))
    record = store.write_files(SegmentKey("bybit", "GOODUSDT", "tester", date(2026, 8, 10), date(2026, 8, 30),
                                          "hb-tester"), tables, settings=RunSettings(), tick_size=0.01, qty_step=0.01)
    assert (record["start_ms"], record["end_ms"]) == (newest["start_ms"], newest["end_ms"])
    store.register(record)
    summary = build_ready(store, ["GOODUSDT"], tmp_path / "tester", template, latest_source="tester")
    manifest = json.loads((tmp_path / "tester" / "ready_manifest.json").read_text(encoding="utf-8"))
    assert summary["ready_pairs"] == 1 and manifest["latest_source"] == "tester"
    assert manifest["pairs"][0]["periods"][-1]["segment_id"] == record["segment_id"]
    assert manifest["pairs"][0]["structures"][0]["score"] == pytest.approx(0.5 * 40 + 0.3 * 30 + 0.2 * 30)
