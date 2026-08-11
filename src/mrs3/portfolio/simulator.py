"""Событийная симуляция сета — слой B, разделы 5 и 9 спецификации.

Что моделируется:

* единая временная линия сигналов всех стратегий сета;
* ограничитель одновременных позиций — заблокированный сигнал теряется;
* маржа: и позиции, и **висящие заявки**; нехватка маржи блокирует вход так же,
  как занятый слот;
* снятие встречных заявок по паре при открытии позиции;
* потолок ёмкости: номинал не может превысить измеренный предел.

Что НЕ моделируется и почему: очередь в стакане. Из журналов сделок она не
видна, нужен L2. Поэтому результат — верхняя граница исполнимости.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .models import RunConfig, SetResult, StrategyInput, StrategyOutcome, TradeRecord

__all__ = ["simulate_set", "common_window"]


@dataclass(slots=True)
class _Open:
    strategy_id: str
    pair: str
    side: str
    notional: float
    exit_ts: datetime
    net_frac: float
    mae_frac: float | None


def common_window(members: Sequence[StrategyInput]) -> tuple[datetime, datetime]:
    start = max(m.window_start for m in members)
    end = min(m.window_end for m in members)
    return start, end


def _reserved_margin(
    members: Sequence[StrategyInput],
    lots: dict[str, float],
    equity: float,
    open_positions: list[_Open],
    slots_free: bool,
    cfg: RunConfig,
    exclude: str | None = None,
) -> float:
    """Занятая маржа: позиции плюс висящие заявки.

    Стратегия резервирует маржу, если держит позицию либо если её заявки висят.
    Заявки снимаются в двух случаях: слоты заняты (лимит достигнут) и встречная
    сторона по паре уже в позиции при ``cancel_opposite``.

    ``exclude`` снимает у одной стратегии резерв **под висящую заявку**: её
    номинал в гейте входа прибавляется отдельно, иначе счёт был бы двойным.
    Уже открытые позиции той же стратегии при этом остаются в сумме — при
    перекрывающихся циклах они занимают маржу независимо от новой заявки.

    Позиции суммируются, а не берутся по одной на стратегию: журнал может
    содержать перекрывающиеся циклы, и каждый из них занимает маржу.
    """
    position_margin: dict[str, float] = {}
    for o in open_positions:
        position_margin[o.strategy_id] = position_margin.get(o.strategy_id, 0.0) + o.notional
    pairs_in_position = {(o.pair, o.side) for o in open_positions}
    total = 0.0
    for m in members:
        held = position_margin.get(m.strategy_id)
        if m.strategy_id == exclude:
            # позиции считаем, резерв под новую заявку — нет
            if held is not None:
                total += held * m.imr
            continue
        if held is not None:
            total += held * m.imr
            continue
        if not slots_free:
            continue  # заявки сняты по достижении лимита
        if cfg.cancel_opposite and any(
            p == m.pair and side != m.side for p, side in pairs_in_position
        ):
            continue  # встречные заявки сняты
        total += min(lots[m.strategy_id] * equity, m.capacity) * m.imr
    return total


def _slots_used(open_positions: list[_Open], cfg: RunConfig) -> int:
    """Сколько слотов занято.

    По умолчанию слот равен позиции. При ``long_short_same_slot`` обе стороны
    одной пары считаются за один слот — это открытый вопрос по контракту
    ограничителя, поэтому вынесен в настройку.
    """
    if not cfg.long_short_same_slot:
        return len(open_positions)
    return len({o.pair for o in open_positions})


def simulate_set(
    members: Sequence[StrategyInput],
    limiter: int,
    lots: dict[str, float],
    cfg: RunConfig,
    window: tuple[datetime, datetime] | None = None,
) -> SetResult:
    """Прогон сета. ``lots`` — доля депозита на стратегию (уже с множителем)."""
    if not members:
        raise ValueError("set must not be empty")
    ordered = sorted(members, key=lambda m: m.strategy_id)
    start, end = window or common_window(ordered)
    d_eff = (end - start).total_seconds() / 86400.0
    if d_eff <= 0:
        raise ValueError("history windows do not overlap")

    by_id = {m.strategy_id: m for m in ordered}

    # §10.2: при совпадении времени решает приоритет по PnL30_DD5, убыванию.
    # Алфавитный порядок здесь был бы произволом.
    #
    # Нормировка на DD берётся из конфига. На сам порядок она не влияет —
    # общий положительный множитель ранжирование не меняет, — но держать её
    # согласованной с гейтом дешевле, чем потом объяснять расхождение.
    def _priority_key(m: StrategyInput) -> tuple[float, float, str]:
        days = max(m.d_eff_days, 1e-9)
        norm = cfg.dd_target_pct
        pnl_dd = m.pnl_pct * norm / m.dd_pct * 30.0 / days if m.dd_pct else 0.0
        return (-pnl_dd, m.lot_x_base, m.strategy_id)

    priority = {
        m.strategy_id: i
        for i, m in enumerate(sorted(ordered, key=_priority_key))
    }

    # Вход обязан попасть в окно; выход за правую границу допустим — позиция
    # закрывается принудительно в конце. Иначе при разрезе истории пополам
    # вторая половина теряла бы сделки, начатые у самой границы.
    signals: list[tuple[datetime, int, str, TradeRecord]] = []
    truncated = 0
    for m in ordered:
        for trade in m.trades:
            if start <= trade.entry_ts < end:
                if trade.exit_ts > end:
                    truncated += 1
                signals.append((trade.entry_ts, priority[m.strategy_id], m.strategy_id, trade))
    signals.sort(key=lambda s: (s[0], s[1]))

    wallet = cfg.deposit
    open_positions: list[_Open] = []
    accepted: dict[str, int] = {m.strategy_id: 0 for m in ordered}
    blocked_slot: dict[str, int] = {m.strategy_id: 0 for m in ordered}
    blocked_margin: dict[str, int] = {m.strategy_id: 0 for m in ordered}
    pnl_by_id: dict[str, float] = {m.strategy_id: 0.0 for m in ordered}

    equity_points: list[float] = [wallet]
    max_margin_ratio = 0.0
    max_occupancy = 0.0
    min_buffer = float("inf")
    mae_total = sum(len(m.trades) for m in ordered)
    mae_known = sum(1 for m in ordered for t in m.trades if t.mae_frac is not None)
    has_mae = mae_known > 0
    mae_coverage = mae_known / mae_total if mae_total else 0.0

    def close_due(now: datetime) -> None:
        nonlocal wallet
        still: list[_Open] = []
        for pos in open_positions:
            if pos.exit_ts <= now:
                gain = pos.net_frac * pos.notional
                wallet += gain
                pnl_by_id[pos.strategy_id] += gain
                equity_points.append(wallet)
            else:
                still.append(pos)
        open_positions[:] = still

    for entry_ts, _, sid, trade in signals:
        close_due(entry_ts)
        member = by_id[sid]

        floating = 0.0
        if has_mae:
            floating = sum(
                (p.mae_frac or 0.0) * p.notional for p in open_positions
            )
        equity = wallet + floating
        equity_points.append(equity)

        slots_free = _slots_used(open_positions, cfg) < limiter
        occupied_all = _reserved_margin(
            ordered, lots, equity, open_positions, slots_free, cfg
        )
        max_occupancy = max(max_occupancy, occupied_all / equity if equity > 0 else 0.0)
        occupied_others = _reserved_margin(
            ordered, lots, equity, open_positions, slots_free, cfg, exclude=sid
        )

        mm = sum(p.notional * by_id[p.strategy_id].mmr for p in open_positions)
        notional_total = sum(p.notional for p in open_positions)
        if equity > 0:
            max_margin_ratio = max(max_margin_ratio, mm / equity)
        if notional_total > 0 and equity > 0:
            min_buffer = min(min_buffer, (equity - mm) / notional_total)

        if not slots_free:
            blocked_slot[sid] += 1
            continue

        if cfg.cancel_opposite and any(
            p.pair == member.pair and p.side != member.side for p in open_positions
        ):
            blocked_slot[sid] += 1
            continue

        notional = min(lots[sid] * equity, member.capacity)
        if notional <= 0:
            blocked_margin[sid] += 1
            continue
        if (occupied_others + notional * member.imr) / equity > cfg.margin_limit:
            blocked_margin[sid] += 1
            continue

        open_positions.append(
            _Open(
                strategy_id=sid,
                pair=member.pair,
                side=member.side,
                notional=notional,
                exit_ts=trade.exit_ts,
                net_frac=trade.net_frac,
                mae_frac=trade.mae_frac,
            )
        )
        accepted[sid] += 1

        # Замер ПОСЛЕ открытия: пик занятой маржи и ликвидационного отношения
        # приходится именно сюда, до следующего сигнала его никто бы не увидел.
        after = _reserved_margin(
            ordered,
            lots,
            equity,
            open_positions,
            _slots_used(open_positions, cfg) < limiter,
            cfg,
        )
        if equity > 0:
            max_occupancy = max(max_occupancy, after / equity)
            mm_after = sum(p.notional * by_id[p.strategy_id].mmr for p in open_positions)
            max_margin_ratio = max(max_margin_ratio, mm_after / equity)
            notional_after = sum(p.notional for p in open_positions)
            if notional_after > 0:
                min_buffer = min(min_buffer, (equity - mm_after) / notional_after)

    # позиции, доживающие до конца окна, закрываем принудительно
    for pos in open_positions:
        gain = pos.net_frac * pos.notional
        wallet += gain
        pnl_by_id[pos.strategy_id] += gain
        equity_points.append(wallet)
    open_positions.clear()

    peak = equity_points[0]
    max_dd = 0.0
    for value in equity_points:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    pnl_abs = wallet - cfg.deposit
    pnl_pct = pnl_abs / cfg.deposit * 100.0

    flags: list[str] = []
    if not has_mae:
        flags.append("CLOSED_TRADE_DD_ONLY")
    elif mae_coverage < 1.0:
        # часть циклов без MAE считается нулевым плавающим убытком,
        # значит просадка занижена — молчать об этом нельзя
        flags.append(f"PARTIAL_MAE_COVERAGE_{mae_coverage:.0%}")
    if truncated:
        flags.append(f"TRUNCATED_AT_WINDOW_END_{truncated}")
    if any(m.target_share_source == "ESTIMATED" for m in ordered):
        flags.append("TARGET_SHARE_ESTIMATED")
    if any(
        min(lots[m.strategy_id] * cfg.deposit, m.capacity) >= m.capacity
        for m in ordered
    ):
        flags.append("CAPACITY_BOUND")

    return SetResult(
        strategy_ids=tuple(m.strategy_id for m in ordered),
        limiter=limiter,
        weights=(),
        g=0.0,
        lots=tuple(lots[m.strategy_id] for m in ordered),
        pnl_abs=pnl_abs,
        pnl_pct=pnl_pct,
        pnl30_pct=pnl_pct * 30.0 / d_eff,
        max_dd_pct=max_dd * 100.0,
        max_margin_ratio=max_margin_ratio,
        max_occupancy_margin=max_occupancy,
        min_buffer=0.0 if min_buffer == float("inf") else min_buffer,
        d_eff_common_days=d_eff,
        outcomes=tuple(
            StrategyOutcome(
                strategy_id=m.strategy_id,
                accepted=accepted[m.strategy_id],
                blocked_slot=blocked_slot[m.strategy_id],
                blocked_margin=blocked_margin[m.strategy_id],
                pnl_abs=pnl_by_id[m.strategy_id],
            )
            for m in ordered
        ),
        flags=tuple(flags),
    )
