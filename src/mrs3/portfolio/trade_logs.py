"""Чтение базы сделок стратегий (второй этап входа) и аудит покрытия.

Здесь НЕТ симуляции сета. Модуль решает две задачи:

1. Выводит `median_hold_min` и окно истории из журналов сделок. В перечне выхода
   tick-теста этого поля нет, поэтому без журналов слой A нечем заполнять.
2. Даёт аудит покрытия: по каким стратегиям журналы есть, сколько в них циклов,
   какое окно они покрывают. Это и есть проверка входного контракта, которую
   требует `PRD.md` перед реализацией симулятора.

Формат источника — DuckDB с таблицей сделок. Обязательные колонки:

    strategy_id  TEXT
    entry_ts     TIMESTAMP   время входа в позицию
    exit_ts      TIMESTAMP   время выхода
    pnl          DOUBLE      результат цикла в валюте счёта

Необязательные, но используемые если есть:

    notional     DOUBLE      номинал позиции
    fees         DOUBLE
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REQUIRED_COLUMNS = ("strategy_id", "entry_ts", "exit_ts", "pnl")
OPTIONAL_COLUMNS = ("notional", "fees", "mae")


class TradeLogError(ValueError):
    """Нарушен контракт базы сделок."""


@dataclass(frozen=True)
class StrategyTradeStats:
    """Сводка по журналу одной стратегии."""

    strategy_id: str
    trades: int
    window_start: datetime
    window_end: datetime
    median_hold_min: float
    mean_hold_min: float
    max_hold_min: float
    total_pnl: float
    has_notional: bool

    @property
    def d_eff_days(self) -> float:
        return (self.window_end - self.window_start).total_seconds() / 86400.0


def _safe_table(name: str) -> str:
    """Имя таблицы подставляется в SQL, поэтому допускаем только простые имена."""
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise TradeLogError(
            f"недопустимое имя таблицы {name!r}: только буквы, цифры и подчёркивание"
        )
    return name


def _require_duckdb():
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise TradeLogError(
            "нужен пакет duckdb: pip install duckdb"
        ) from exc
    return duckdb


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raise TradeLogError(f"ожидался timestamp, получено {type(value).__name__}")


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise TradeLogError("пустая выборка")
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def inspect_schema(db_path: Path, table: str = "trades") -> dict:
    """Проверить, что таблица существует и содержит обязательные колонки."""
    table = _safe_table(table)
    if not Path(db_path).exists():
        raise TradeLogError(f"файл базы не найден: {db_path}")
    duckdb = _require_duckdb()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        names = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        if table not in names:
            raise TradeLogError(
                f"в базе нет таблицы {table!r}; доступны: {', '.join(sorted(names)) or '—'}"
            )
        columns = {row[0].lower() for row in con.execute(f'DESCRIBE "{table}"').fetchall()}
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise TradeLogError(
                f"в таблице {table!r} нет обязательных колонок: {', '.join(missing)}"
            )
        return {
            "table": table,
            "columns": sorted(columns),
            "optional_present": [c for c in OPTIONAL_COLUMNS if c in columns],
        }
    finally:
        con.close()


def load_stats(db_path: Path, table: str = "trades") -> list[StrategyTradeStats]:
    """Сводка по каждой стратегии: циклы, окно, медианное удержание."""
    schema = inspect_schema(db_path, table)
    has_notional = "notional" in schema["optional_present"]
    duckdb = _require_duckdb()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            f'SELECT strategy_id, entry_ts, exit_ts, pnl FROM "{table}" '
            "WHERE entry_ts IS NOT NULL AND exit_ts IS NOT NULL "
            "ORDER BY strategy_id, entry_ts"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        raise TradeLogError(f"таблица {table!r} не содержит закрытых циклов")

    grouped: dict[str, list[tuple[datetime, datetime, float]]] = {}
    for sid, entry, exit_, pnl in rows:
        start, end = _as_utc(entry), _as_utc(exit_)
        if end < start:
            raise TradeLogError(f"{sid}: exit_ts раньше entry_ts ({end} < {start})")
        grouped.setdefault(str(sid), []).append((start, end, float(pnl or 0.0)))

    out: list[StrategyTradeStats] = []
    for sid in sorted(grouped):
        cycles = grouped[sid]
        holds = [(e - s).total_seconds() / 60.0 for s, e, _ in cycles]
        out.append(
            StrategyTradeStats(
                strategy_id=sid,
                trades=len(cycles),
                window_start=min(s for s, _, _ in cycles),
                window_end=max(e for _, e, _ in cycles),
                median_hold_min=_median(holds),
                mean_hold_min=sum(holds) / len(holds),
                max_hold_min=max(holds),
                total_pnl=sum(p for _, _, p in cycles),
                has_notional=has_notional,
            )
        )
    return out


def coverage_report(
    stats: Sequence[StrategyTradeStats],
    expected_ids: Sequence[str] = (),
) -> list[dict]:
    """Лист аудита: по каким стратегиям журналы есть, а по каким нет."""
    by_id = {s.strategy_id: s for s in stats}
    ids = sorted(set(by_id) | set(expected_ids))
    rows = []
    for sid in ids:
        s = by_id.get(sid)
        if s is None:
            rows.append(
                {
                    "strategy_id": sid,
                    "status": "MISSING_TRADE_LOG",
                    "trades": 0,
                    "window_start": "",
                    "window_end": "",
                    "d_eff_days": "",
                    "median_hold_min": "",
                    "has_notional": "",
                }
            )
            continue
        rows.append(
            {
                "strategy_id": sid,
                "status": "OK" if not expected_ids or sid in expected_ids else "EXTRA_IN_DB",
                "trades": s.trades,
                "window_start": s.window_start.isoformat(),
                "window_end": s.window_end.isoformat(),
                "d_eff_days": round(s.d_eff_days, 3),
                "median_hold_min": round(s.median_hold_min, 3),
                "has_notional": "yes" if s.has_notional else "no",
            }
        )
    return rows


def load_trades(db_path: Path, table: str = "trades") -> dict[str, list["TradeRecord"]]:
    """Полные журналы по стратегиям для симуляции сета.

    PnL приводится к долям номинала: только так сделку можно пересчитать под
    другой размер лота. Если колонки ``notional`` нет, пересчёт невозможен и
    журнал отклоняется — молча делить на единицу нельзя.
    """
    from .models import TradeRecord

    schema = inspect_schema(db_path, table)
    present = set(schema["optional_present"])
    if "notional" not in present:
        raise TradeLogError(
            f"в таблице {table!r} нет колонки notional: без неё нельзя пересчитать "
            "PnL под другой размер лота"
        )
    duckdb = _require_duckdb()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        fee_expr = "fees" if "fees" in present else "0.0"
        mae_expr = "mae" if "mae" in present else "NULL"
        rows = con.execute(
            f'SELECT strategy_id, entry_ts, exit_ts, pnl, notional, {fee_expr}, {mae_expr} '
            f'FROM "{table}" WHERE entry_ts IS NOT NULL AND exit_ts IS NOT NULL '
            "ORDER BY strategy_id, entry_ts"
        ).fetchall()
    finally:
        con.close()

    out: dict[str, list[TradeRecord]] = {}
    for sid, entry, exit_, pnl, notional, fees, mae in rows:
        if not notional:
            raise TradeLogError(f"{sid}: notional равен нулю, пересчёт невозможен")
        start, end = _as_utc(entry), _as_utc(exit_)
        if end < start:
            raise TradeLogError(f"{sid}: exit_ts раньше entry_ts ({end} < {start})")
        out.setdefault(str(sid), []).append(
            TradeRecord(
                strategy_id=str(sid),
                entry_ts=start,
                exit_ts=end,
                pnl_frac=float(pnl or 0.0) / float(notional),
                fee_frac=float(fees or 0.0) / float(notional),
                mae_frac=None if mae is None else float(mae) / float(notional),
            )
        )
    return out
