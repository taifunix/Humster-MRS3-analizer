"""The «Стратегии» view of the main screen (Panel rework stage 4a).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 4 contract»; decision ADR-0080. Rows are the
READY structures of the exchange joined by strategy name with the ACTIVE PerformanceDB strategies (tick tests);
metrics are read only (cached selection metrics, report metrics); the two agreed hard filters are recomputed on
every read and never stored; the finalist marks come from the pair registry, their only store.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import threading
from typing import Mapping, Sequence

import duckdb

from .pair_registry.records import FINALIST_STATUSES, current_finalists, database_exchange
from .performance_v2_html import REPORT_ANALYTICS_LABELS
from .performance_v2_selection import (
    SelectionConfig, SelectionRequest, hard_cutoff_evidence, load_selection_candidates, retest_cohort_request,
    top5_evidence,
)
from .ready_store import CATEGORIES

BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("strategy", "Стратегия"), ("symbol", "Пара"), ("side", "Сторона"), ("timeframe", "ТФ"), ("orders", "Ордера"),
    ("score", "Score"), ("train_dd", "DD обучения"), ("category", "Категория"), ("ready_rank", "Ранг READY"),
    ("rank_in_pair", "Ранг в паре"), ("status", "Отметка"), ("user_rank", "Ранг"), ("reason", "Причина"),
    ("tested", "Тик-тест"), ("window", "Окно теста"), ("total_pnl_pct", "PnL, %"), ("pnl_30d_pct", "PnL/30, %"),
    ("max_drawdown_pct", "MaxDD, %"), ("total_trades", "Сделок"), ("history_days", "Дней истории"),
    ("top5_share_pct", "Топ-5, %"), ("ab_return_b_30d_pct", "PnL/30 B, %"), ("profit_factor", "Profit factor"),
    ("win_rate_pct", "Win rate, %"), ("completed_cycle_count", "Циклов"),
)
# every report metric can be shown; the columns present in the PerformanceDB are filled
REPORT_COLUMNS: tuple[tuple[str, str], ...] = tuple((key, label) for key, label, _kind in REPORT_ANALYTICS_LABELS)
ALL_COLUMNS: tuple[tuple[str, str], ...] = BASE_COLUMNS + tuple(
    item for item in REPORT_COLUMNS if item[0] not in dict(BASE_COLUMNS))
DEFAULT_COLUMNS = tuple(key for key, _label in BASE_COLUMNS) + ("ulcer_index_hourly_pct", "time_under_water_pct")
CANDIDATE_FIELDS = ("pnl_30d_pct", "history_days", "top5_share_pct", "ab_return_b_30d_pct", "profit_factor",
                    "win_rate_pct", "completed_cycle_count")
MARK_LABELS = {"Стратегия": "strategy", "Отметка": "status", "Ранг": "user_rank"}


class StrategiesError(ValueError):
    """A «Стратегии» request is refused (message safe to show)."""


def _plain(value: object) -> object:
    """JSON-safe value: Decimal and floats as numbers (non-finite as empty), timestamps as ISO text."""
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _orders_text(item: Mapping) -> str:
    orders = " · ".join(f"{order.get('shift_bp')}bp MA{order.get('open_ma')}" for order in item.get("orders", []))
    return f"{orders} · CMA{item.get('close_ma')}" if orders else ""


def _ready_rows(store_root: Path | None, exchange: str) -> dict[str, dict]:
    """READY structures of the latest catalog row of every pair (read only; empty without a store catalog)."""
    if store_root is None or not (Path(store_root) / "catalog.duckdb").is_file():
        return {}
    with duckdb.connect(str(Path(store_root) / "catalog.duckdb"), read_only=True) as catalog:
        payloads = catalog.execute(
            "select payload_json from (select *, row_number() over (partition by symbol order by created_utc desc, "
            "ready_id desc) as latest from ready where exchange = ?) where latest = 1 order by symbol",
            [exchange]).fetchall()
    rows: dict[str, dict] = {}
    for (payload,) in payloads:
        entry = json.loads(payload)
        if entry.get("status") != "READY":
            continue
        for item in entry.get("structures", []):
            name = item.get("strategy")
            if not name:
                continue
            rows[str(name)] = {"strategy": str(name), "symbol": entry.get("pair"), "side": "LONG",
                               "timeframe": item.get("timeframe"), "orders": _orders_text(item),
                               "score": item.get("score"), "train_dd": item.get("dd"),
                               "category": item.get("category"), "ready_rank": item.get("pair_rank")}
    return rows


def _report_metrics(connection: duckdb.DuckDBPyConnection, result_ids: Sequence[int]) -> dict[int, dict]:
    tables = {row[0] for row in connection.execute("select table_name from information_schema.tables").fetchall()}
    if "strategy_report_metrics" not in tables or not result_ids:
        return {}
    present = [row[0] for row in connection.execute(
        "select column_name from information_schema.columns where table_name = 'strategy_report_metrics'").fetchall()]
    keys = [key for key, _label in REPORT_COLUMNS if key in present]
    if not keys:
        return {}
    rows = connection.execute(f"select result_id, {', '.join(keys)} from strategy_report_metrics "
                              "where result_id in (select unnest(?::bigint[]))", [list(result_ids)]).fetchall()
    return {int(row[0]): dict(zip(keys, row[1:])) for row in rows}


_TESTED_CACHE: dict[tuple, tuple[dict[str, dict], list[str]]] = {}
_TESTED_CACHE_LOCK = threading.Lock()


def _tested_rows(performance_db: Path | None, exchange: str, config: SelectionConfig,
                 metrics: bool) -> tuple[dict[str, dict], list[str]]:
    """`_read_tested_rows` reused while the PerformanceDB file (and its WAL) is unchanged: the view is read often
    and the metrics read takes seconds on a full database."""
    if performance_db is None or not Path(performance_db).is_file():
        return {}, []
    database = Path(performance_db)
    if not metrics:                              # the light read (names, pairs) is cheap: never cached
        return _read_tested_rows(database, exchange, config, metrics)
    stamp = []
    for path in (database, database.with_name(database.name + ".wal")):
        try:
            status = path.stat()
            stamp.append((status.st_mtime_ns, status.st_size))
        except OSError:                          # no WAL (or it was checkpointed away meanwhile)
            stamp.append(None)
    key = (str(database.resolve()), tuple(stamp), exchange, repr(config))
    with _TESTED_CACHE_LOCK:
        cached = _TESTED_CACHE.get(key)
    if cached is None:
        cached = _read_tested_rows(database, exchange, config, metrics)
        with _TESTED_CACHE_LOCK:
            _TESTED_CACHE.clear()                # one entry: the newest state of the database
            _TESTED_CACHE[key] = cached
    return deepcopy(cached)


def _read_tested_rows(performance_db: Path, exchange: str, config: SelectionConfig,
                      metrics: bool) -> tuple[dict[str, dict], list[str]]:
    """ACTIVE PerformanceDB strategies of the exchange with their result, cached selection metrics and report
    metrics (read only). Returns rows by strategy name and notes about pairs whose metrics could not be read."""
    notes: list[str] = []
    with duckdb.connect(str(performance_db), read_only=True) as connection:
        if database_exchange(connection) != exchange:
            return {}, []
        rows = connection.execute(
            "select s.strategy_id, s.strategy_name, s.symbol, s.side, s.timeframe, r.result_id, r.report_start_utc, "
            "r.report_end_utc, r.total_pnl_pct, r.max_drawdown_pct, r.total_trades from strategies s "
            "join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id "
            "where s.lifecycle_status = 'ACTIVE' order by s.strategy_name").fetchall()
        tested: dict[str, dict] = {}
        cohorts: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for strategy_id, name, symbol, side, timeframe, result_id, start, end, pnl, dd, trades in rows:
            window = (f"{start.date().isoformat()}..{end.date().isoformat()}"
                      if isinstance(start, datetime) and isinstance(end, datetime) else None)
            tested[str(name)] = {"strategy": str(name), "symbol": symbol, "side": side, "timeframe": timeframe,
                                 "tested": True, "window": window, "total_pnl_pct": pnl, "max_drawdown_pct": dd,
                                 "total_trades": trades, "_strategy_id": int(strategy_id),
                                 "_result_id": int(result_id), "_candidate": None}
            cohorts.setdefault((str(symbol), str(side)), []).append((int(strategy_id), int(result_id)))
        if not metrics:
            return tested, notes
        by_id = {row["_strategy_id"]: row for row in tested.values()}
        for (symbol, side), members in cohorts.items():
            try:
                request = retest_cohort_request(SelectionRequest(symbol, side, ()), "panel-strategies", members)
                frame = load_selection_candidates(connection, request, config, cache_only=True)
            except Exception as error:          # one pair never blanks the view
                notes.append(f"{symbol} {side}: метрики не прочитаны ({type(error).__name__})")
                continue
            for candidate in frame.to_dict(orient="records"):
                row = by_id.get(int(candidate["strategy_id"]))
                if row is not None:
                    row["_candidate"] = candidate
                    for key in CANDIDATE_FIELDS:
                        row[key] = candidate.get(key)
                    if candidate.get("max_drawdown_pct") is not None:      # FIXED results: rebased drawdown
                        row["max_drawdown_pct"] = candidate.get("max_drawdown_pct")
        by_result = {row["_result_id"]: row for row in tested.values()}
        for result_id, values in _report_metrics(connection, list(by_result)).items():
            if result_id in by_result:
                by_result[result_id].update(values)
    return tested, notes


def _number(value: object) -> float | None:
    try:
        number = float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _hard_reason(evidence: str | None) -> str:
    try:
        payload = json.loads(str(evidence).split(":", 1)[1])
        values = payload.get("values", {})
        text = ", ".join(payload.get("triggered", []))
        return (f"Жёсткие отсечки: {text} (PnL/30 {values.get('full_pnl30_pct')}, DD {values.get('full_dd_pct')}, "
                f"B PnL/30 {values.get('b_pnl30_pct')})")
    except (IndexError, TypeError, ValueError, AttributeError):
        return "Жёсткие отсечки"


def _top5_reason(evidence: str | None) -> str:
    text = str(evidence or "")
    share = text.split("share=", 1)[1].split(",", 1)[0] if "share=" in text else "?"
    try:
        share = f"{float(share):.1f}"
    except ValueError:
        pass
    return f"Топ-5: доля {share} %"


def strategy_rows(*, exchange: str, store_root: Path | None, performance_db: Path | None,
                  registry: duckdb.DuckDBPyConnection | None = None, marks: Sequence[Mapping] | None = None,
                  config: SelectionConfig = SelectionConfig(), hard: bool = True, top5: bool = True,
                  metrics: bool = True) -> tuple[list[dict], list[str]]:
    """Rows of the «Стратегии» view (dicts keyed like ALL_COLUMNS) and notes; nothing is written anywhere. The marks
    in force come from `marks` (read by the caller on a short registry connection) or from `registry`."""
    ready = _ready_rows(store_root, exchange)
    tested, notes = _tested_rows(performance_db, exchange, config, metrics)
    if marks is None:
        marks = current_finalists(registry, exchange) if registry is not None else []
    marks = {row["strategy"]: row for row in marks}
    rows: list[dict] = []
    for name in sorted(set(ready) | set(tested)):
        row = {**tested.get(name, {}), **ready.get(name, {})}
        row.setdefault("tested", False)
        if name in tested:                      # the tested strategy's own pair and side win over READY defaults
            row["symbol"], row["side"] = tested[name]["symbol"], tested[name]["side"]
        mark = marks.get(name)
        row["status"] = mark["status"] if mark and mark["status"] != "NONE" else None
        row["user_rank"] = mark["user_rank"] if mark and mark["status"] != "NONE" else None
        candidate = row.pop("_candidate", None)
        reasons = []
        if candidate is not None and hard:
            cut, evidence = hard_cutoff_evidence(candidate, config)
            if cut:
                reasons.append(_hard_reason(evidence))
        if candidate is not None and top5:
            outcome, evidence = top5_evidence(candidate, config)
            if outcome == "cut":
                reasons.append(_top5_reason(evidence))
        row["reason"] = "; ".join(reasons) or None
        rows.append(row)
    by_pair: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("category") in CATEGORIES:
            by_pair.setdefault(str(row.get("symbol")), []).append(row)
    for members in by_pair.values():
        def order(row: dict) -> tuple:
            score, ulcer = _number(row.get("score")), _number(row.get("ulcer_index_hourly_pct"))
            return (CATEGORIES.index(row["category"]), -score if score is not None else math.inf,
                    ulcer if ulcer is not None else math.inf, row["strategy"])

        members.sort(key=order)
        for rank, row in enumerate(members, 1):
            row["rank_in_pair"] = rank
    keys = [key for key, _label in ALL_COLUMNS]
    return [{key: _plain(row.get(key)) for key in keys} for row in rows], notes


def hide_cut(rows: Sequence[Mapping]) -> list[Mapping]:
    return [row for row in rows if not row.get("reason")]


def parse_status(value: object) -> str:
    """A mark from the table or Excel: FINALIST / RESERVE / REJECTED; empty means no mark (NONE)."""
    text = "" if value is None else str(value).strip().upper()
    if text in ("", "NONE", "—", "-"):
        return "NONE"
    if text not in FINALIST_STATUSES:
        raise StrategiesError(f"отметка {value!r}: FINALIST, RESERVE, REJECTED или пусто")
    return text


def parse_rank(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(str(value).strip()) if not isinstance(value, (int, float)) or isinstance(value, bool) else float(value)
    except ValueError:
        raise StrategiesError(f"ранг {value!r}: целое число от 1 до 10000") from None
    if isinstance(value, bool) or not number.is_integer() or not 1 <= number <= 10000:
        raise StrategiesError(f"ранг {value!r}: целое число от 1 до 10000")
    return int(number)
