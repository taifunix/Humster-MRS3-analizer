from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from mrs3.performance_v2_sizing import fixed_lot_base, load_fixed_lot_base
from mrs3.performance_v2_store import initialize_performance_v2
from mrs3.performance_v2_windows import METRICS_VERSION, _Action, _Equity, _calculate, get_or_calculate_window

UTC = timezone.utc


def _fixed(side: str = "LONG", **overrides: object) -> dict[str, object]:
    sizing: dict[str, object] = {
        "sizing_use_upnl": False, "sizing_use_frozen_balance": True, "sizing_use_fix": True,
        "sizing_max_balance": Decimal("0"), "sizing_my_fix_balance": Decimal("10000"),
        f"sizing_balance_percentage_{side.lower()}": Decimal("100"), f"sizing_risk_{side.lower()}": Decimal("1"),
    }
    sizing.update(overrides)
    return sizing


@pytest.mark.parametrize("side", ("LONG", "SHORT"))
def test_fixed_lot_base_reads_the_active_side(side: str) -> None:
    assert fixed_lot_base(_fixed(side), side) == Decimal("10000")
    other = "SHORT" if side == "LONG" else "LONG"
    assert fixed_lot_base(_fixed(side), other) is None


@pytest.mark.parametrize("override", [
    {"sizing_use_upnl": True}, {"sizing_use_upnl": None}, {"sizing_use_frozen_balance": False},
    {"sizing_use_fix": False}, {"sizing_max_balance": Decimal("1")}, {"sizing_my_fix_balance": None},
    {"sizing_my_fix_balance": Decimal("0")}, {"sizing_balance_percentage_long": Decimal("50")},
    {"sizing_risk_long": Decimal("0.5")},
])
def test_fixed_lot_base_rejects_any_other_sizing(override: dict[str, object]) -> None:
    assert fixed_lot_base(_fixed(**override), "LONG") is None


def _cycles(returns: list[Decimal], dips: list[Decimal], *, fixed_base: Decimal | None):
    """Equal per-cycle returns: DYNAMIC compounds r, FIXED books B·ln(1+r) (invariant 2)."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    balance = Decimal("1000")
    actions: list[_Action] = []
    equity: list[_Equity] = [_Equity(0, start, balance, balance)]
    for cycle, (r, dip) in enumerate(zip(returns, dips, strict=True)):
        opened = start + timedelta(days=2 * cycle, hours=1)
        closed = opened + timedelta(days=1)
        if fixed_base is None:
            pnl, low = balance * r, balance * (1 - dip)
        else:
            pnl, low = fixed_base * (1 + r).ln(), balance - fixed_base * -(1 - dip).ln()
        actions.append(_Action(2 * cycle, opened, "opened", Decimal("1"), Decimal("0"), Decimal("0"), balance))
        equity.append(_Equity(2 * cycle + 1, opened + timedelta(hours=12), balance, low))
        balance += pnl
        actions.append(_Action(2 * cycle + 1, closed, "closed", Decimal("0"), pnl, Decimal("0"), balance))
        equity.append(_Equity(2 * cycle + 2, closed, balance, balance))
    end = start + timedelta(days=2 * len(returns))
    return _calculate(
        1, start, end, METRICS_VERSION, start, end, tuple(actions), tuple(equity),
        initial_balance=Decimal("1000"), fixed_base=fixed_base,
    )


def test_fixed_window_metrics_match_dynamic_with_equal_cycle_returns() -> None:
    returns = [Decimal("0.05"), Decimal("-0.03"), Decimal("0.04")]
    dips = [Decimal("0.02"), Decimal("0.10"), Decimal("0.01")]
    dynamic = _cycles(returns, dips, fixed_base=None)
    fixed = _cycles(returns, dips, fixed_base=Decimal("1000"))

    assert dynamic.available and fixed.available
    tolerance = Decimal("1e-20")
    for field in ("growth_factor", "return_pct", "daily_log_return", "daily_growth_pct"):
        assert abs(getattr(fixed, field) - getattr(dynamic, field)) < tolerance, field
    # The peak is the opening balance of the deepest cycle: d = -ln(1 - DD) >= DD.
    assert abs(dynamic.max_drawdown_pct - Decimal("10")) < tolerance
    assert abs(fixed.max_drawdown_pct - (-(Decimal("0.9")).ln() * 100)) < tolerance
    assert fixed.max_drawdown_pct > dynamic.max_drawdown_pct


def test_window_uses_stored_fixed_base(tmp_path) -> None:
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len, order_count,
           analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc)
           values ('alpha', 'BTCUSDT', 'LONG', '1h', 3, 1, 'run', 'candidate', 'ACTIVE', ?, ?) returning strategy_id""",
        [now, now],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
           initial_balance, final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees,
           total_trades, imported_at_utc) values (?, ?, ?, 'Bybit', .0004, 10000, 10200, 200, 2, 0, 0, 0, 1, ?)
           returning result_id""",
        [strategy_id, now, now + timedelta(days=4), now],
    ).fetchone()[0]
    connection.executemany(
        """insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size,
           post_size, post_side, pnl, fee, balance, raw_action_json) values (?, ?, ?, 'BTCUSDT', 1, ?, 1, ?, ?, ?, 1, ?, null)""",
        [
            (result_id, 0, now + timedelta(days=1), "opened", 1, "long", 0, 10000),
            (result_id, 1, now + timedelta(days=2), "closed", 0, "", 200, 10200),
        ],
    )
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [(result_id, 0, now, 10000, 10000), (result_id, 1, now + timedelta(days=2), 10200, 10200)],
    )
    dynamic = get_or_calculate_window(connection, result_id, now, now + timedelta(days=4), metrics_version="dynamic")
    connection.execute(
        """update strategy_results set sizing_use_upnl = false, sizing_use_frozen_balance = true, sizing_use_fix = true,
           sizing_max_balance = 0, sizing_my_fix_balance = 5000, sizing_balance_percentage_long = 100, sizing_risk_long = 1"""
    )

    assert load_fixed_lot_base(connection, result_id) == Decimal("5000")
    fixed = get_or_calculate_window(connection, result_id, now, now + timedelta(days=4), metrics_version="fixed")
    connection.close()

    assert dynamic.return_pct == Decimal("2")
    # R = 200 / 5000 = 0.04 -> exp(0.04) - 1.
    assert abs(fixed.return_pct - (Decimal("0.04").exp() - 1) * 100) < Decimal("1e-11")  # cache keeps 12 places
    # Fees 2 USDT over B = 5000; the FIXED fee share uses the linear gross 100·ΔW/B + fees.
    assert fixed.fees_pct == Decimal("0.04")
    from mrs3.performance_v2_selection import _fee_efficiency
    share, _per_30d = _fee_efficiency(fixed, fixed=True)
    assert abs(share - Decimal("0.04") / Decimal("4.04") * 100) < Decimal("1e-9")


def _equity_paths() -> tuple[datetime, datetime, list[Decimal], list[Decimal]]:
    """FIXED equity E and its compounded twin Ẽ = E0·exp((E - E0) / B); same logs, different drawdowns."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    base, first = Decimal("1000"), Decimal("1000")
    fixed: list[Decimal] = []
    for step in range(50 * 4 + 1):
        dip = Decimal("60") if step in (120, 121, 180) else Decimal("0")
        fixed.append(first + Decimal(step) * Decimal("0.5") - dip)
    compounded = [first * ((value - first) / base).exp() for value in fixed]
    return start, start + timedelta(days=50), fixed, compounded


def test_fixed_equity_quality_uses_base_relative_logs_and_linear_drawdown() -> None:
    from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts

    start, end, fixed, compounded = _equity_paths()

    def facts(values, fixed_base):
        samples = [EquitySample(1, index, start + timedelta(hours=6 * index), value) for index, value in enumerate(values)]
        return calculate_equity_quality_facts(1, start, end, samples, fixed_base=fixed_base)

    fixed_facts = facts(fixed, Decimal("1000"))
    dynamic_facts = facts(compounded, None)

    tolerance = Decimal("1e-18")
    assert fixed_facts.windows and len(fixed_facts.windows) == len(dynamic_facts.windows)
    for left, right in zip(fixed_facts.windows, dynamic_facts.windows, strict=True):
        for field in ("trend30", "endpoint30", "return_pct", "er"):
            assert abs(getattr(left, field) - getattr(right, field)) < tolerance, field
    # Linear drawdown in fractions of B: the 60 USDT dip below the running peak.
    peak = max(fixed[:121])
    assert abs(fixed_facts.drawdown - (peak - fixed[120]) / Decimal("1000")) < tolerance
    assert fixed_facts.drawdown != dynamic_facts.drawdown


def test_fixed_equity_regime_uses_base_relative_logs_and_linear_drawdown() -> None:
    from mrs3.performance_v2_equity_regime import EquityRegimeSample, calculate_equity_regime_facts

    start, end, fixed, compounded = _equity_paths()

    def facts(values, fixed_base):
        samples = [EquityRegimeSample(1, index, start + timedelta(hours=6 * index), value) for index, value in enumerate(values)]
        return calculate_equity_regime_facts(1, start, end, samples, fixed_base=fixed_base)

    fixed_facts = facts(fixed, Decimal("1000"))
    dynamic_facts = facts(compounded, None)

    tolerance = Decimal("1e-18")
    for name in ("windows_28", "windows_14", "windows_7", "pre28"):
        left, right = getattr(fixed_facts, name), getattr(dynamic_facts, name)
        assert left is not None and right is not None, name
        assert abs(left.v - right.v) < tolerance and abs(left.p - right.p) < tolerance, name
    # The dip at step 180 lies in W14: 100 · (peak - E) / B.
    peak = max(fixed[:181])
    assert abs(fixed_facts.dd14 - Decimal(100) * (peak - fixed[180]) / Decimal("1000")) < tolerance


def test_fixed_drawdown_beyond_the_base_is_cached_and_scores_zero() -> None:
    from hashlib import sha256

    from mrs3.performance_v2_equity_cache import decode_equity_facts, encode_equity_facts
    from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts

    start = datetime(2026, 1, 1, tzinfo=UTC)
    # 10000 -> 50000 -> 25000 on a fixed lot inside the 28-day horizon: growth stays
    # positive while the linear drawdown and peak gap are (50000 - 25000) / 10000 = 2.5.
    values = [Decimal("10000")] * 20
    values += [Decimal("10000") + Decimal("40000") * step / 60 for step in range(1, 61)]
    values += [Decimal("50000") - Decimal("25000") * step / 60 for step in range(1, 61)]
    samples = [EquitySample(1, index, start + timedelta(hours=6 * index), value) for index, value in enumerate(values)]
    facts = calculate_equity_quality_facts(1, start, start + timedelta(hours=6 * (len(values) - 1)), samples, fixed_base=Decimal("10000"))

    assert facts.drawdown == Decimal("2.5") and facts.peak_gap == Decimal("2.5")
    payload = encode_equity_facts(facts)
    assert decode_equity_facts(payload, sha256(payload.encode("utf-8")).hexdigest()) == facts
    # Without the clamp two negative factors would multiply into a positive score.
    assert facts.score12 == 0
