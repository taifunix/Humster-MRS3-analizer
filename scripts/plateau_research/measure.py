"""Step 2: measure every point of every study scope and run the training analysis.

One worker = one scope. Each point is decoded once (`decode_fragment_slice`);
then, in the same worker:

* `calculate_metrics` on the full READY window (`measure_points` semantics),
  on every training window (`_pretest_ab_evidence` semantics) and on the §5.2
  reconciliation windows -> `analysis_input_row` rows;
* `calculate_metrics(fragments)` over the whole history -> balance / equity
  series, from which forward windows and the outcome-side DD_train are read
  (prompt §5.5: windowed `calculate_metrics` anchors PnL on the first sample
  inside the window and loses trades opened before T);
* the production `_analyze_points` on each training window.

Event ids never leave the worker. Results are cached per scope; a restart
skips every scope whose `window_points.parquet` exists.

Run:  set PYTHONPATH=src
      .venv\\Scripts\\python.exe scripts\\plateau_research\\measure.py [--workers 15] [--limit N] [--scope DB::SCOPE]
Output: WORK/scopes/<db-stem>/<scope>/*.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DAY_MS, FORWARD_DAYS, FORWARD_DAYS_SENS, ROLLING_TRAIN_DAYS, SOURCE_ROOT, STEP_DAYS,
    TRAIN_MIN_DAYS, WORK, listing_dates, load_config, ms, read_parquet, write_parquet,
)

FORWARDS = (FORWARD_DAYS, FORWARD_DAYS_SENS)

# §5.2 reconciliation windows: (table row, start, end exclusive, symbols without USDT)
RECONCILE = [
    (1, "2026-07-13", "2026-09-25", "ASTS KORU PURR TSM"),
    (2, "2026-05-15", "2026-10-05", "BABA BMNR LLY PLTR"),
    (3, "2026-07-21", "2026-10-06", "BITO PANW REGN TSLL TTWO"),
    (4, "2026-07-21", "2026-10-07", "CLSK GMESTOCK KO WENSTOCK XOM"),
    (5, "2026-07-10", "2026-10-04", "IONQ RDW SNOW TQQQ TSEM"),
    (6, "2026-07-18", "2026-09-12", "MARA ZM"),
    (7, "2026-07-19", "2026-10-03", "ARKK CXMT FLY FWDI MARA ZM"),
    (8, "2026-09-10", "2026-10-01", "BTDR CGNX FORMSTOCK HORIZON HUAHONG LENOVO MEITU SENSETIME"),
    (9, "2026-08-15", "2026-10-02", "EWZ MRK OKTA PDD SMIC VST XIAOMI"),
    (10, "2026-08-23", "2026-09-27", "FUTU MCD MSTU RAM XLP XLU"),
    (11, "2026-07-01", "2026-08-25", "BBX BMNR CRDO LLY QNTX TQQQ TSM"),
    (11, "2026-07-01", "2026-08-26", "BBX BMNR CRDO LLY QNTX TQQQ TSM"),
    (12, "2026-07-15", "2026-09-14", "AAOI KSTR SONY"),
    (13, "2026-09-01", "2026-09-30", "CVXSTOCK GPRO LAOPU TEAM TEM YOFC"),
    (14, "2026-08-05", "2026-10-02", "RIOT SHOP SPOT UBER VRTXSTOCK"),
    (15, "2026-07-10", "2026-09-24", "SHAZ SKHYNIX SMCI SOXX TBT"),
    (15, "2026-07-10", "2026-09-25", "SHAZ SKHYNIX SMCI SOXX TBT"),
]


def reconcile_windows(symbol: str) -> list[dict]:
    base = symbol.removesuffix("USDT")
    return [
        {"wtype": "reconcile", "cut": f"R{row}_{end}", "reconcile_row": row, "start_ms": ms(start), "end_ms": ms(end)}
        for row, start, end, symbols in RECONCILE if base in symbols.split()
    ]


def cut_grid(ready_start_ms: int, ready_end_ms: int) -> list[dict]:
    """Cuts T = start + 42 + 14*i days while at least the F=14 window fits (fixed before measuring)."""
    cuts, index = [], 0
    while True:
        t = ready_start_ms + (TRAIN_MIN_DAYS + STEP_DAYS * index) * DAY_MS
        if t + FORWARD_DAYS_SENS * DAY_MS > ready_end_ms:
            return cuts
        cuts.append({
            "cut": f"C{index}", "t_ms": t,
            "train_exp": (ready_start_ms, t),
            "train_roll": (max(ready_start_ms, t - ROLLING_TRAIN_DAYS * DAY_MS), t),
            "forwards": [f for f in FORWARDS if t + f * DAY_MS <= ready_end_ms],
        })
        index += 1


class Series:
    """Full-history balance/equity of one point as float arrays.

    Wallet samples are an exact but sparse copy of the `action.balance` chain:
    every sample equals the chain at its timestamp, yet a closing action can go
    unsampled for days. So balance at time x is read from the action chain
    (`chain=True`, primary) or, literally as prompt §5.5 says, from samples
    (`chain=False`, secondary). The chain-mode equity path is the equity
    samples plus every action that leaves the position flat (there equity
    equals balance exactly).
    """

    def __init__(self, metrics, fragment, cutoff: int | None) -> None:
        self.initial = float(fragment.initial_balance)
        bs, es = metrics.balance_series, metrics.equity_series
        offset = float(bs[0].value) - float(fragment.wallet_samples[0].value) if bs and fragment.wallet_samples else 0.0
        self.offset = offset
        self.bt = np.array([s.timestamp_ms for s in bs], dtype=np.int64)
        self.bv = np.array([float(s.value) for s in bs], dtype=np.float64)
        self.et = np.array([s.timestamp_ms for s in es], dtype=np.int64)
        self.ev = np.array([float(s.value) for s in es], dtype=np.float64)
        self.eu = np.array([float(s.upnl) for s in es], dtype=np.float64)
        limit = cutoff if cutoff is not None else fragment.report_end_ms
        acts = sorted((a for a in fragment.actions if a.balance is not None and a.timestamp_ms < limit),
                      key=lambda a: (a.timestamp_ms, a.action_id))
        self.at = np.array([a.timestamp_ms for a in acts], dtype=np.int64)
        self.av = np.array([float(a.balance) + offset for a in acts], dtype=np.float64)
        flat = [(a.timestamp_ms, float(a.balance) + offset) for a in acts
                if a.action == "closed" or (a.post_size is not None and a.post_size == 0)]
        merged = sorted([(int(t), float(v), 0) for t, v in zip(self.et, self.ev)] + [(t, v, 1) for t, v in flat])
        self.mt = np.array([m[0] for m in merged], dtype=np.int64)
        self.mv = np.array([m[1] for m in merged], dtype=np.float64)
        self.mflat = np.array([m[2] for m in merged], dtype=np.int8)

    def _last(self, ts: np.ndarray, vs: np.ndarray, t: int) -> float:
        i = int(np.searchsorted(ts, t, "left"))
        return float(vs[i - 1]) if i > 0 else self.initial + self.offset

    def balance_before(self, t: int, chain: bool = True) -> float:
        return self._last(self.at, self.av, t) if chain else self._last(self.bt, self.bv, t)

    def equity_before(self, t: int, chain: bool = True) -> float:
        return self._last(self.mt, self.mv, t) if chain else self._last(self.et, self.ev, t)

    def upnl_before(self, t: int) -> tuple[float, float | None]:
        """Unrealized PnL at t: 0 if the latest known point is a flat-out action,
        else the last equity sample's upnl; plus that point's age in hours."""
        i = int(np.searchsorted(self.mt, t, "left"))
        if i == 0:
            return 0.0, None
        if self.mflat[i - 1]:
            return 0.0, (t - int(self.mt[i - 1])) / 3_600_000
        j = int(np.searchsorted(self.et, t, "left"))
        return float(self.eu[j - 1]), (t - int(self.et[j - 1])) / 3_600_000

    def drawdown(self, a: int, b: int, chain: bool = True) -> tuple[float, float]:
        """Max equity DD on [a, b) from a running peak that starts at the anchor
        (last equity point strictly before a); % of the peak at the maximum,
        like `_drawdown_details` / `max_equity_drawdown_percent`."""
        ts, vs = (self.mt, self.mv) if chain else (self.et, self.ev)
        i0 = int(np.searchsorted(ts, a, "left"))
        i1 = int(np.searchsorted(ts, b, "left"))
        seg = np.concatenate(([vs[i0 - 1] if i0 > 0 else self.initial + self.offset], vs[i0:i1]))
        peak = np.maximum.accumulate(seg)
        dd = peak - seg
        j = int(np.argmax(dd))
        return float(dd[j]), (float(dd[j] / peak[j] * 100.0) if peak[j] else 0.0)

    def rolling_drawdown_pct(self, a: int, b: int, days: int) -> float | None:
        """Max of `drawdown` over every F-day sub-window of [a, b), daily step."""
        span = days * DAY_MS
        values = [self.drawdown(start, start + span)[1] for start in range(a, b - span + 1, DAY_MS)]
        return max(values) if values else None


def forward_stats(series: Series, fragment, metrics, entry_ts: dict, funding: dict, t: int, e: int) -> dict:
    bal_anchor, eq_anchor = series.balance_before(t), series.equity_before(t)
    bal_end, eq_end = series.balance_before(e), series.equity_before(e)
    dd_abs, dd_pct = series.drawdown(t, e)
    s_bal_anchor, s_bal_end = series.balance_before(t, False), series.balance_before(e, False)
    closed = [trip for trip in metrics.round_trips if t <= trip.timestamp_ms < e]
    crossing = [trip for trip in closed if entry_ts.get(trip.round_trip_id) is not None and entry_ts[trip.round_trip_id] < t]
    orphans = [trip for trip in closed if entry_ts.get(trip.round_trip_id) is None]
    open_end = [trip for trip in metrics.round_trips
                if entry_ts.get(trip.round_trip_id) is not None and entry_ts[trip.round_trip_id] < e <= trip.timestamp_ms]
    actions = [a for a in fragment.actions if t <= a.timestamp_ms < e]
    sum_pnl = sum(float(a.pnl) for a in actions if a.action in ("decreased", "closed"))
    sum_fee = sum(float(a.fee) for a in actions)
    sum_funding = sum(funding.get(a.action_id, 0.0) for a in actions)
    delta, s_delta = bal_end - bal_anchor, s_bal_end - s_bal_anchor
    weeks, previous, boundary = [], bal_anchor, t
    while boundary < e:
        boundary = min(boundary + 7 * DAY_MS, e)
        current = series.balance_before(boundary)
        weeks.append(current - previous)
        previous = current
    upnl_end, upnl_end_age = series.upnl_before(e)
    upnl_anchor, upnl_anchor_age = series.upnl_before(t)
    return {
        "bal_anchor": bal_anchor, "eq_anchor": eq_anchor, "bal_end": bal_end, "eq_end": eq_end,
        "pnl_fwd_pct": (delta / bal_anchor * 100.0) if bal_anchor else 0.0,
        "dd_fwd_abs": dd_abs, "dd_fwd_pct": dd_pct,
        "pnl_fwd_pct_samples": (s_delta / s_bal_anchor * 100.0) if s_bal_anchor else 0.0,
        "dd_fwd_pct_samples": series.drawdown(t, e, False)[1],
        "anchor_sample_stale_pnl": bal_anchor - s_bal_anchor, "end_sample_stale_pnl": bal_end - s_bal_end,
        "trades_fwd": len(closed), "wins_fwd": sum(1 for x in closed if x.realized_pnl > 0),
        "losses_fwd": sum(1 for x in closed if x.realized_pnl < 0),
        "gross_realized_fwd": sum(float(x.realized_pnl) for x in closed),
        "crossing_trades": len(crossing), "crossing_gross_pnl": sum(float(x.realized_pnl) for x in crossing),
        "orphan_trades": len(orphans),
        "open_positions_at_end": len(open_end), "upnl_at_end": upnl_end, "upnl_at_end_age_h": upnl_end_age,
        "upnl_at_anchor": upnl_anchor, "upnl_at_anchor_age_h": upnl_anchor_age,
        "weeks": json.dumps(weeks), "weeks_total": len(weeks), "weeks_profitable": sum(1 for w in weeks if w > 0),
        "chk_delta_balance": delta, "chk_sum_pnl": sum_pnl, "chk_sum_fee": sum_fee, "chk_sum_funding": sum_funding,
        "chk_diff": delta - (sum_pnl - sum_fee + sum_funding), "chk_diff_no_funding": delta - (sum_pnl - sum_fee),
        "chk_diff_samples": s_delta - (sum_pnl - sum_fee + sum_funding),
        "actions_in_window": len(actions),
    }


def point_checks(fragment, metrics, funding: dict, cutoff: int | None) -> dict:
    """Independent currency check of the whole history against the tester's own
    report header (prompt §5.5 p.6): action sums vs declared totals, and every
    wallet sample vs the action balance chain."""
    header = fragment.metrics

    def declared(name):
        try:
            return float(header[name])
        except (KeyError, ValueError, TypeError):
            return None

    acts = fragment.actions
    realizing = [a for a in acts if a.action in ("decreased", "closed")]
    chain = [a for a in sorted(acts, key=lambda a: (a.timestamp_ms, a.action_id)) if a.balance is not None]
    mismatch = 0
    if chain:
        at = np.array([a.timestamp_ms for a in chain], dtype=np.int64)
        for sample in fragment.wallet_samples:
            i = int(np.searchsorted(at, sample.timestamp_ms, "right"))
            value = chain[i - 1].balance if i else fragment.initial_balance
            mismatch += abs(value - sample.value) > Decimal("0.000001")
    return {
        "chk_actions": len(acts), "chk_wallet_samples": len(fragment.wallet_samples),
        "chk_open_tail_cutoff_ms": cutoff, "chk_samples_vs_chain_mismatch": int(mismatch),
        "chk_sum_fee_all": sum(float(a.fee) for a in acts), "decl_total_fees": declared("Total fees"),
        "chk_funding_all": sum(funding.values()), "decl_funding_net": declared("Funding net"),
        "chk_gross_profit": sum(float(a.pnl) for a in realizing if a.pnl > 0), "decl_gross_profit": declared("Gross profit"),
        "chk_gross_loss": -sum(float(a.pnl) for a in realizing if a.pnl < 0), "decl_gross_loss": declared("Gross loss"),
        "chk_final_chain_balance": float(chain[-1].balance) if chain else float(fragment.initial_balance),
        "decl_final_balance": declared("Final balance"),
        "chk_metrics_total_pnl": float(metrics.total_pnl), "decl_total_pnl": declared("Total PnL"),
    }


def _funding_by_action(fragment) -> dict:
    """Funding is not stored as events; it is folded into `action.balance`.
    Per action: balance - previous balance - (pnl - fee), along the fragment chain."""
    result, previous = {}, float(fragment.initial_balance)
    for action in sorted(fragment.actions, key=lambda a: (a.timestamp_ms, a.action_id)):
        if action.balance is None:
            continue
        balance = float(action.balance)
        result[action.action_id] = balance - previous - (float(action.pnl) - float(action.fee))
        previous = balance
    return result


def _open_tail_cutoff(fragment) -> int | None:
    """Same cutoff `calculate_metrics` applies to the first fragment's samples."""
    return min((c.open_timestamp_ms for c in fragment.cycles
                if not c.closed and c.open_timestamp_ms < fragment.report_end_ms), default=None)


def _daily(series: Series, metrics, point_key: str) -> pd.DataFrame:
    """Per UTC day: balance at day close (action chain), equity close/min/max
    over the chain-mode equity path, round trips closed that day. Days without
    any point are absent; carry the previous close forward."""
    if not len(series.mt):
        return pd.DataFrame()
    eq = pd.DataFrame({"d": series.mt // DAY_MS, "v": series.mv}).groupby("d")["v"].agg(["last", "min", "max"])
    trips = pd.Series([trip.timestamp_ms // DAY_MS for trip in metrics.round_trips], dtype="int64").value_counts()
    days = sorted(set(eq.index) | set(trips.index) | set((series.at // DAY_MS).tolist()))
    frame = pd.DataFrame(index=pd.Index(days, name="day"))
    frame["balance_close"] = [series.balance_before((d + 1) * DAY_MS) for d in days]
    frame["equity_close"] = eq["last"].reindex(frame.index)
    frame["equity_min"] = eq["min"].reindex(frame.index)
    frame["equity_max"] = eq["max"].reindex(frame.index)
    frame["trades_closed"] = trips.reindex(frame.index).fillna(0).astype("int32")
    frame = frame.reset_index()
    frame["utc_day"] = pd.to_datetime(frame["day"] * DAY_MS, unit="ms").dt.strftime("%Y-%m-%d")
    frame.insert(0, "point_id", point_key)
    return frame.drop(columns="day")


def _jsonish(value):
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str)
    return value


def _stage_frame(frame: pd.DataFrame, keep: list[str] | None = None) -> pd.DataFrame:
    frame = frame.drop(columns=["_event_ids"], errors="ignore").copy()
    if keep is not None:
        frame = frame[[c for c in keep if c in frame.columns]]
    for column in frame.columns:
        if str(frame[column].dtype).startswith("datetime"):
            frame[column] = frame[column].astype(str)
        elif frame[column].dtype == object:
            frame[column] = frame[column].map(_jsonish)
    return frame


POINT_STAGE_COLUMNS = [
    "point_id", "shift_bp", "open_ma", "close_ma", "pnl_pct", "dd_pct", "trades", "win_rate_pct",
    "efficiency", "pnl_dd5_theoretical", "point_event_count", "events_last_30d", "effective_days",
    "history_pass", "required_min_trades", "standalone_sample_pass", "economic_pass", "event_eligible",
    "refine_required", "missing_test_count", "plateau_id", "plateau_role", "standalone_eligible",
    "depth_eligible", "plateau_point_count", "plateau_total_trades", "reject_reasons",
]


def run_scope(database: str, scope: str, groups: dict[str, tuple[str, ...]], ready: tuple[int, int],
              listing: dict, config, out_dir: str, mode: str) -> dict:
    from mrs3.pipeline import _analyze_points
    from mrs3.source_v6_analysis_fresh import _analysis_frame_row
    from mrs3.source_v6_materializer import analysis_input_row
    from mrs3.source_v6_stitch import (
        GENUINE_ZERO_ACTIVITY, WINDOW_EXCLUDES_MEASURABLE_DATA, SourceV6EmptySeriesError,
        calculate_metrics, flat_result_metrics,
    )
    from mrs3.source_v6_storage import decode_fragment_slice
    import mrs3.source_v6_materializer as materializer

    # PRETEST B of train_exp and train_roll of one cut is the same [T-14d, T)
    # window; memoise it per point (same fragments object), results unchanged.
    memo: dict = {}

    def cached_metrics(fragments_arg, **kwargs):
        key = (id(fragments_arg), kwargs.get("start_ms"), kwargs.get("end_ms"), kwargs.get("truncate_unresolved_tail", True))
        if key not in memo:
            try:
                memo[key] = (True, calculate_metrics(fragments_arg, **kwargs))
            except Exception as error:  # noqa: BLE001 - re-raised identically below
                memo[key] = (False, error)
        ok, value = memo[key]
        if not ok:
            raise value
        return value

    materializer.calculate_metrics = cached_metrics
    started = time.time()
    out = Path(out_dir)
    symbol = scope.split("|")[0]
    cuts = cut_grid(*ready) if mode == "study" else []
    windows = [{"wtype": "reconcile", **w} for w in reconcile_windows(symbol)]
    if mode == "study":
        windows.insert(0, {"wtype": "full", "cut": "full", "start_ms": ready[0], "end_ms": ready[1]})
        for cut in cuts:
            windows.append({"wtype": "train_exp", "cut": cut["cut"], "start_ms": cut["train_exp"][0], "end_ms": cut["train_exp"][1]})
            if cut["train_roll"] != cut["train_exp"]:
                windows.append({"wtype": "train_roll", "cut": cut["cut"], "start_ms": cut["train_roll"][0], "end_ms": cut["train_roll"][1]})
    # Which flat-result causes a window may turn into a flat result (§4.2).
    flat_ok = {"full": (GENUINE_ZERO_ACTIVITY,), "reconcile": (GENUINE_ZERO_ACTIVITY,),
               "train_exp": (GENUINE_ZERO_ACTIVITY, WINDOW_EXCLUDES_MEASURABLE_DATA),
               "train_roll": (GENUINE_ZERO_ACTIVITY, WINDOW_EXCLUDES_MEASURABLE_DATA)}

    rows, forwards, daily, errors, checks = [], [], [], [], []
    analysis_rows: dict[tuple[str, str], list[dict]] = {}
    for point_key, ids in groups.items():
        memo.clear()
        fragments = decode_fragment_slice(database, ids)
        fragment = fragments[0]
        hist_start = min(f.report_start_ms for f in fragments)
        hist_end = max(f.report_end_ms for f in fragments)
        for window in windows:
            start_ms, end_ms, wtype = window["start_ms"], window["end_ms"], window["wtype"]
            base = {"point_id": point_key, "wtype": wtype, "cut": window["cut"], "start_ms": start_ms,
                    "end_ms": end_ms, "reconcile_row": window.get("reconcile_row")}
            if wtype == "reconcile" and not (hist_start <= start_ms and end_ms <= hist_end):
                errors.append({**base, "reason": "WINDOW_OUTSIDE_POINT_HISTORY",
                               "detail": f"history {hist_start}..{hist_end}"})
                continue
            flat_reason = None
            try:
                metrics = calculate_metrics(fragments, start_ms=start_ms, end_ms=end_ms)
            except SourceV6EmptySeriesError as error:
                if error.reason not in flat_ok[wtype]:
                    errors.append({**base, "reason": error.reason, "detail": str(error)[:300]})
                    continue
                metrics, flat_reason = flat_result_metrics(), error.reason
            except Exception as error:  # noqa: BLE001 - recorded; the scope is excluded for this window
                errors.append({**base, "reason": type(error).__name__, "detail": str(error)[:300]})
                continue
            try:
                row = analysis_input_row(point_key, fragment.point, metrics, fragments, (start_ms, end_ms))
            except Exception as error:  # noqa: BLE001
                errors.append({**base, "reason": "ROW_" + type(error).__name__, "detail": str(error)[:300]})
                continue
            rows.append({**base, **{k: v for k, v in row.items() if k not in ("event_ids", "pretest_ab")},
                         "pretest_ab": json.dumps(row["pretest_ab"], sort_keys=True),
                         "flat_reason": flat_reason, "event_count": len(row["event_ids"])})
            if wtype != "reconcile":
                analysis_rows.setdefault((wtype, window["cut"]), []).append(row)

        if mode != "study":
            continue
        try:
            history = calculate_metrics(fragments)
        except Exception as error:  # noqa: BLE001
            history = None
            errors.append({"point_id": point_key, "wtype": "history", "cut": "history",
                           "reason": "FORWARD_UNCOVERED_HISTORY_FAILED", "detail": f"{type(error).__name__}: {str(error)[:280]}"})
        if history is None:
            for cut in cuts:
                for f in cut["forwards"]:
                    forwards.append({"point_id": point_key, "cut": cut["cut"], "forward_days": f,
                                     "covered": False, "uncovered_reason": "HISTORY_FAILED"})
            continue
        cutoff = _open_tail_cutoff(fragment)
        series = Series(history, fragment, cutoff)
        stitched_ids = {p.fragment_id for p in history.period_metrics}
        actions = {a.action_id: a for f in fragments for a in f.actions}
        entry_ts = {}
        for trip in history.round_trips:
            times = [actions[a].timestamp_ms for a in trip.entry_action_ids if a in actions]
            entry_ts[trip.round_trip_id] = min(times) if times else None
        funding = _funding_by_action(fragment)
        checks.append({"point_id": point_key, **point_checks(fragment, history, funding, cutoff)})
        for cut in cuts:
            t = cut["t_ms"]
            dd_train = {}
            for variant in ("train_exp", "train_roll"):
                a, b = cut[variant]
                dd_train[f"dd_series_{variant}_abs"], dd_train[f"dd_series_{variant}_pct"] = series.drawdown(a, b)
                dd_train[f"dd_series_{variant}_pct_samples"] = series.drawdown(a, b, False)[1]
                dd_train[f"pnl_series_{variant}_pct"] = (series.balance_before(b) / series.balance_before(a) - 1) * 100.0
                for f in FORWARDS:
                    dd_train[f"dd_series_{variant}_roll{f}_pct"] = series.rolling_drawdown_pct(a, b, f)
            for f in cut["forwards"]:
                e = t + f * DAY_MS
                record = {"point_id": point_key, "cut": cut["cut"], "forward_days": f, "t_ms": t, "e_ms": e,
                          "open_tail_cutoff_ms": cutoff, **dd_train}
                intersecting = [x.fragment_id for x in fragments if x.report_start_ms < e and x.report_end_ms > t]
                reason = None
                if hist_end < e:
                    reason = "HISTORY_ENDS_BEFORE_T_PLUS_F"
                elif any(fid not in stitched_ids for fid in intersecting):
                    reason = "FRAGMENT_NOT_STITCHED"
                elif cutoff is not None and fragment.report_end_ms > t and cutoff < e:
                    reason = "OPEN_TAIL_CUTOFF_BEFORE_T_PLUS_F"
                record.update(covered=reason is None, uncovered_reason=reason)
                if reason is None:
                    record.update(forward_stats(series, fragment, history, entry_ts, funding, t, e))
                forwards.append(record)
        frame = _daily(series, history, point_key)
        if not frame.empty:
            frame["initial_balance"] = series.initial
            daily.append(frame)
        del fragments, history, series, actions

    # A failure of any point on a window excludes the whole scope for it (§4.2).
    failed_keys = {(e["wtype"], e["cut"]) for e in errors if e["wtype"] in ("full", "train_exp", "train_roll")}
    stage_tables: dict[str, list[pd.DataFrame]] = {k: [] for k in ("points", "plateaus", "close_profiles", "base_one_order", "structures")}
    analysis_log = []
    by_key = {(w["wtype"], w["cut"]): w for w in windows}
    for key, point_rows in sorted(analysis_rows.items()):
        if key in failed_keys:
            analysis_log.append({"wtype": key[0], "cut": key[1], "status": "SCOPE_EXCLUDED_POINT_ERROR", "error": None})
            continue
        window = by_key[key]
        bounds = (datetime.fromtimestamp(window["start_ms"] / 1000, timezone.utc),
                  datetime.fromtimestamp(window["end_ms"] / 1000, timezone.utc))
        try:
            frame = pd.DataFrame([_analysis_frame_row(row, bounds, listing) for row in point_rows])
            stages = _analyze_points(frame, config)
        except Exception as error:  # noqa: BLE001
            analysis_log.append({"wtype": key[0], "cut": key[1], "status": "ANALYSIS_ERROR",
                                 "error": (repr(error) + "\n" + traceback.format_exc())[-1500:]})
            continue
        analysis_log.append({"wtype": key[0], "cut": key[1], "status": "OK", "error": None})
        tables = {
            "points": _stage_frame(stages.points, POINT_STAGE_COLUMNS),
            "plateaus": _stage_frame(stages.plateaus),
            "close_profiles": _stage_frame(stages.close_profiles),
            "base_one_order": _stage_frame(stages.base_one_order, POINT_STAGE_COLUMNS + ["selection_type", "selection_role"]),
            "structures": _stage_frame(stages.structures),
        }
        for name, table in tables.items():
            table.insert(0, "cut", key[1])
            table.insert(0, "wtype", key[0])
            stage_tables[name].append(table)

    out.mkdir(parents=True, exist_ok=True)

    def finish(frame: pd.DataFrame) -> pd.DataFrame:
        frame.insert(0, "scope", scope)
        frame.insert(0, "database", Path(database).name)
        return frame

    for name, parts in stage_tables.items():
        parts = [p for p in parts if not p.empty]
        if parts:
            write_parquet(finish(pd.concat(parts, ignore_index=True)), out / f"stages_{name}.parquet")
    for name, data in (("forward_points", forwards), ("errors", errors), ("analysis_log", analysis_log), ("point_checks", checks)):
        if data:
            write_parquet(finish(pd.DataFrame(data)), out / f"{name}.parquet")
    if daily:
        write_parquet(finish(pd.concat(daily, ignore_index=True)), out / "daily_equity.parquet")
    write_parquet(finish(pd.DataFrame(rows)), out / "window_points.parquet")  # written last = done marker
    return {"scope": scope, "database": Path(database).name, "mode": mode, "cuts": len(cuts),
            "seconds": round(time.time() - started, 1), "rows": len(rows), "forwards": len(forwards),
            "errors": len(errors), "failed_windows": sorted(f"{a}:{b}" for a, b in failed_keys)}


def scope_dir(database: str, scope: str) -> Path:
    return WORK / "scopes" / Path(database).stem.replace(" ", "_").replace(",", "") / scope.replace("|", "_")


def study_plan() -> pd.DataFrame:
    """One kept copy per scope (longest READY window; ties: later end, then name)."""
    scopes = read_parquet(WORK / "inventory" / "scopes.parquet")
    scopes = scopes.loc[scopes["ready"]].copy()
    scopes = scopes.sort_values(["scope", "ready_days", "ready_end_ms", "database"], ascending=[True, False, False, True])
    scopes["kept"] = ~scopes.duplicated("scope", keep="first")
    scopes["mode"] = np.where(scopes["kept"], "study", "reconcile_only")
    has_reconcile = scopes["symbol"].map(lambda s: bool(reconcile_windows(s)))
    return scopes.loc[scopes["kept"] | has_reconcile].reset_index(drop=True)


def main() -> None:
    from mrs3.source_v6_materializer import _point_groups
    from mrs3.source_v6_storage import fragment_metadata

    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=15)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--scope", action="append", default=[], help="DBNAME::SCOPE")
    args = parser.parse_args()
    config = load_config()
    listing, _ = listing_dates()
    plan = study_plan()
    write_parquet(plan, WORK / "inventory" / "plan.parquet")
    if args.scope:
        wanted = {tuple(item.split("::")) for item in args.scope}
        plan = plan.loc[[(d, s) in wanted for d, s in zip(plan["database"], plan["scope"])]]
    todo = [r for r in plan.itertuples(index=False)
            if not (scope_dir(str(SOURCE_ROOT / r.database), r.scope) / "window_points.parquet").exists()]
    todo.sort(key=lambda r: (r.database, -r.ready_days, r.scope))
    if args.limit:
        todo = todo[: args.limit]
    print(f"scopes to measure: {len(todo)}", flush=True)
    groups_cache: dict[str, dict] = {}

    def groups_for(database: str) -> dict:
        if database not in groups_cache:
            groups_cache.clear()
            views = fragment_metadata(database)
            all_scopes = sorted({f"{v.point.symbol}|{v.point.side}|{v.point.timeframe}" for v in views})
            groups_cache[database] = _point_groups(views, all_scopes)
        return groups_cache[database]

    log = WORK / "measure_log.jsonl"
    started = time.time()
    tasks = iter(todo)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {}

        def submit() -> None:
            r = next(tasks, None)
            if r is None:
                return
            database = str(SOURCE_ROOT / r.database)
            groups = groups_for(database)[r.scope]
            future = pool.submit(run_scope, database, r.scope, groups, (int(r.ready_start_ms), int(r.ready_end_ms)),
                                 listing, config, str(scope_dir(database, r.scope)), r.mode)
            pending[future] = r

        for _ in range(args.workers):
            submit()
        done_count = 0
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                r = pending.pop(future)
                done_count += 1
                try:
                    result = future.result()
                except Exception as error:  # noqa: BLE001
                    result = {"scope": r.scope, "database": r.database, "fatal": repr(error)[:500]}
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(result) + "\n")
                print(f"[{done_count}/{len(todo)} {time.time() - started:.0f}s] {result}", flush=True)
                submit()


if __name__ == "__main__":
    main()
