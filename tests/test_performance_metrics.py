from decimal import Decimal

import pytest

from mrs3.performance_metrics import PreciseMetricError, derive_precise_metrics


_DECLARED = {
    "Initial balance": "1000",
    "Final balance": "1439.53",
    "Total PnL": "439.53",
    "Total PnL, %": "43.95",
    "Max Drawdown": "74.44",
    "Max Drawdown, %": "5.80",
    "Win Rate, %": "76.45",
}


def test_derive_precise_metrics_reproduces_legacy_csv_values() -> None:
    metrics = derive_precise_metrics(
        Decimal("1000"),
        [
            Decimal("1000"),
            Decimal("1282.77898396"),
            Decimal("1208.3342077"),
            Decimal("1447.49436808"),
            Decimal("1439.532329415"),
        ],
        [
            Decimal("1000"),
            Decimal("1282.77898396"),
            Decimal("1208.3342077"),
            Decimal("1447.49436808"),
            Decimal("1439.532329415"),
        ],
        237,
        310,
        timestamps=[1, 2, 3, 4, 5],
        declared_metrics=_DECLARED,
    )

    assert metrics.final_balance == Decimal("1439.532329415")
    assert metrics.total_pnl == Decimal("439.532329415")
    assert metrics.total_pnl_pct == Decimal("43.9532329415")
    assert metrics.max_drawdown == Decimal("74.44477626")
    assert metrics.max_drawdown_pct == Decimal("74.44477626") / Decimal("1282.77898396") * 100
    assert metrics.win_rate_pct == Decimal("237") / Decimal("310") * 100


def test_derive_precise_metrics_uses_wallet_final_and_equity_drawdown_peak() -> None:
    metrics = derive_precise_metrics(
        Decimal("1000"),
        [
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1250"),
        ],
        [
            Decimal("1000"),
            Decimal("1100"),
            Decimal("100"),
            Decimal("5000"),
            Decimal("3500"),
        ],
        1,
        2,
        timestamps=[1, 2, 3, 4, 5],
    )

    assert metrics.final_balance == Decimal("1250")
    assert metrics.total_pnl == Decimal("250")
    assert metrics.total_pnl_pct == Decimal("25")
    assert metrics.max_drawdown == Decimal("1500")
    assert metrics.max_drawdown_pct == Decimal("1500") / Decimal("5000") * 100
    independent_max_pct = (Decimal("1100") - Decimal("100")) / Decimal("1100") * 100
    assert metrics.max_drawdown_pct != independent_max_pct


def test_derive_precise_metrics_rejects_non_monotonic_equity_timestamps() -> None:
    with pytest.raises(PreciseMetricError, match="strictly increasing"):
        derive_precise_metrics(
            Decimal("1000"),
            [Decimal("1000"), Decimal("1010")],
            [Decimal("1000"), Decimal("1010")],
            1,
            2,
            timestamps=[1, 1],
        )


def test_derive_precise_metrics_rejects_empty_or_mismatched_equity() -> None:
    with pytest.raises(PreciseMetricError, match="equity"):
        derive_precise_metrics(
            Decimal("1000"),
            [Decimal("1000")],
            [],
            1,
            2,
            timestamps=[1],
        )


def test_derive_precise_metrics_rejects_invalid_initial_balance() -> None:
    with pytest.raises(PreciseMetricError, match="initial balance"):
        derive_precise_metrics(
            Decimal("0"),
            [Decimal("1000")],
            [Decimal("1000")],
            1,
            2,
            timestamps=[1],
        )


def test_derive_precise_metrics_rejects_declared_mismatch_beyond_rounding() -> None:
    declared = dict(_DECLARED)
    declared["Final balance"] = "9999"
    with pytest.raises(PreciseMetricError, match="final balance"):
        derive_precise_metrics(
            Decimal("1000"),
            [
                Decimal("1000"),
                Decimal("1282.77898396"),
                Decimal("1208.3342077"),
                Decimal("1447.49436808"),
                Decimal("1439.532329415"),
            ],
            [
                Decimal("1000"),
                Decimal("1282.77898396"),
                Decimal("1208.3342077"),
                Decimal("1447.49436808"),
                Decimal("1439.532329415"),
            ],
            237,
            310,
            timestamps=[1, 2, 3, 4, 5],
            declared_metrics=declared,
        )


def test_derive_precise_metrics_accepts_tester_display_rounding() -> None:
    declared = {
        "Initial balance": "1000.00",
        "Final balance": "1149.50",
        "Total PnL": "149.50",
        "Total PnL, %": "14.99",
        "Max Drawdown": "58.07",
        "Max Drawdown, %": "4.90",
        "Win Rate, %": "75.00",
    }
    metrics = derive_precise_metrics(
        Decimal("1000"),
        [Decimal("1000"), Decimal("1100"), Decimal("1149.4983767442")],
        [Decimal("1000"), Decimal("1184.424901468"), Decimal("1126.386411388")],
        3,
        4,
        timestamps=[1, 2, 3],
        declared_metrics=declared,
    )

    assert metrics.max_drawdown == Decimal("58.038490080")
    declared["Max Drawdown"] = "59.00"
    with pytest.raises(PreciseMetricError, match="max drawdown"):
        derive_precise_metrics(
            Decimal("1000"),
            [Decimal("1000"), Decimal("1100"), Decimal("1149.4983767442")],
            [Decimal("1000"), Decimal("1184.424901468"), Decimal("1126.386411388")],
            3,
            4,
            timestamps=[1, 2, 3],
            declared_metrics=declared,
        )


def test_derive_precise_metrics_rejects_missing_or_invalid_trade_counts() -> None:
    with pytest.raises(PreciseMetricError, match="total_trades"):
        derive_precise_metrics(
            Decimal("1000"),
            [Decimal("1000")],
            [Decimal("1000")],
            0,
            0,
            timestamps=[1],
        )
    with pytest.raises(PreciseMetricError, match="win_trades"):
        derive_precise_metrics(
            Decimal("1000"),
            [Decimal("1000")],
            [Decimal("1000")],
            3,
            2,
            timestamps=[1],
        )
