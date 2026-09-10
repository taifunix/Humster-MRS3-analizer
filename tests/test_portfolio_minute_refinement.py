from decimal import Decimal
from datetime import datetime, timezone

from mrs3.portfolio.minute_refinement import refine_pretest_shortlist


def _path(values):
    return tuple(
        {"timestamp_utc": datetime(2026, 1, 1, hour, tzinfo=timezone.utc), "equity": Decimal(str(value))}
        for hour, value in enumerate(values)
    )


def test_minute_refinement_scales_member_paths_and_preserves_daily_sizes():
    candidate = {
        "candidate_id": "candidate",
        "campaign_equity": Decimal("100"),
        "members": (
            {"symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1, "initial_balance": Decimal("100"), "tested_size_usdt": Decimal("100"), "actual_size_usdt": Decimal("50")},
            {"symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 2, "initial_balance": Decimal("100"), "tested_size_usdt": Decimal("100"), "actual_size_usdt": Decimal("50")},
        ),
    }
    paths = {
        "A:LONG:1:1": _path((100, 120)),
        "B:LONG:2:2": _path((100, 100)),
    }

    result = refine_pretest_shortlist(candidate and (candidate,), paths, start_utc=_path((0,))[0]["timestamp_utc"], end_utc=_path((0,))[0]["timestamp_utc"] + __import__("datetime").timedelta(hours=2), max_gap_days=3)

    assert result.status == "PASS"
    assert result.budget_consumed == 0
    assert result.candidates[0]["daily_sizes_preserved"] is True
    assert result.candidates[0]["minute_metrics"]["proxy_pnl_usdt"] == Decimal("10.00000000")


def test_minute_refinement_falls_back_to_daily_shortlist_on_any_missing_path():
    candidate = {"candidate_id": "candidate", "members": ({"symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1},), "daily_sizes": "kept"}
    result = refine_pretest_shortlist((candidate,), {}, max_gap_days=3)

    assert result.status == "MINUTE_REFINEMENT_UNAVAILABLE"
    assert result.reason == "MINUTE_SERIES_EMPTY"
    assert result.basis == "DAILY"
    assert result.candidates[0]["daily_sizes"] == "kept"


def test_minute_refinement_seeds_initial_balance_without_leading_backfill():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = {
        "candidate_id": "candidate",
        "campaign_equity": Decimal("100"),
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "initial_balance": Decimal("100"), "tested_size_usdt": Decimal("100"),
            "actual_size_usdt": Decimal("50"),
        },),
    }
    path = (
        {"timestamp_utc": start + __import__("datetime").timedelta(minutes=1), "equity": Decimal("110")},
    )

    result = refine_pretest_shortlist(
        (candidate,),
        {"A:LONG:1:1": path},
        start_utc=start,
        end_utc=start + __import__("datetime").timedelta(minutes=2),
        max_gap_days=3,
    )

    assert result.status == "PASS"
    assert [point["equity"] for point in result.candidates[0]["minute_metrics"]["equity_path"]] == [Decimal("100.00000000"), Decimal("105.00000000"), Decimal("105.00000000")]


def test_minute_refinement_includes_terminal_interval_endpoint():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + __import__("datetime").timedelta(minutes=2)
    candidate = {
        "candidate_id": "candidate",
        "campaign_equity": Decimal("100"),
        "initial_balance": Decimal("100"),
    }
    path = (
        {"timestamp_utc": start, "equity": Decimal("100")},
        {"timestamp_utc": end - __import__("datetime").timedelta(seconds=30), "equity": Decimal("110")},
    )

    result = refine_pretest_shortlist(
        (candidate,), {"candidate": path}, start_utc=start, end_utc=end,
    )

    assert result.status == "PASS"
    equity_path = result.candidates[0]["minute_metrics"]["equity_path"]
    assert equity_path[-1]["timestamp_utc"] == end
    assert equity_path[-1]["equity"] == Decimal("110.00000000")


def test_minute_refinement_rejects_action_before_inferred_start_without_prior_sample():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = {
        "candidate_id": "candidate",
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "initial_balance": Decimal("100"),
            "actions": ({"timestamp_utc": start},),
        },),
    }
    path = ({"timestamp_utc": start + __import__("datetime").timedelta(minutes=1), "equity": Decimal("110")},)

    result = refine_pretest_shortlist((candidate,), {"A:LONG:1:1": path})

    assert result.status == "MINUTE_REFINEMENT_UNAVAILABLE"
    assert result.reason == "MINUTE_SERIES_REQUIRES_START_SAMPLE"


def test_minute_refinement_rejects_nonpositive_initial_seed():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = {
        "candidate_id": "candidate",
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "initial_balance": Decimal("0"),
        },),
    }

    result = refine_pretest_shortlist(
        (candidate,),
        {"A:LONG:1:1": ({"timestamp_utc": start + __import__("datetime").timedelta(minutes=1), "equity": Decimal("110")},)},
        start_utc=start,
        end_utc=start + __import__("datetime").timedelta(minutes=2),
    )

    assert result.status == "MINUTE_REFINEMENT_UNAVAILABLE"
    assert result.reason == "MINUTE_INVALID_START_SEED"


def test_minute_refinement_recomputes_sizes_through_evaluator():
    candidate = {
        "candidate_id": "candidate",
        "campaign_equity": Decimal("100"),
        "metrics": {"proxy_pnl_usdt": Decimal("1")},
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "initial_balance": Decimal("100"), "tested_size_usdt": Decimal("100"),
            "actual_size_usdt": Decimal("50"),
        },),
    }
    seen = []

    def evaluate(members, _candidate):
        seen.append(members)
        return {
            "status": "PASS",
            "proxy_pnl_usdt": Decimal("2"),
            "proxy_recovery_factor": Decimal("2"),
            "proxy_max_drawdown_pct": Decimal("1"),
            "members": ({**dict(members[0]), "actual_size_usdt": Decimal("25")},),
        }

    result = refine_pretest_shortlist(
        (candidate,),
        {"A:LONG:1:1": _path((100, 120))},
        start_utc=_path((0,))[0]["timestamp_utc"],
        end_utc=_path((0,))[0]["timestamp_utc"] + __import__("datetime").timedelta(hours=2),
        evaluator=evaluate,
    )

    assert result.status == "PASS"
    assert len(seen) == 1
    assert result.candidates[0]["daily_metrics"] == candidate["metrics"]
    assert result.candidates[0]["minute_sizes_recomputed"] is True
    assert result.candidates[0]["members"][0]["actual_size_usdt"] == Decimal("25")


def test_minute_refinement_skips_short_members_without_changing_daily_shortlist():
    candidate = {
        "candidate_id": "candidate",
        "calendar_days": 14,
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "in_window_observation_count": 14,
        },),
        "metrics": {"proxy_pnl_usdt": Decimal("7")},
    }

    result = refine_pretest_shortlist((candidate,), {}, evaluator=lambda *_: {"status": "PASS"})

    assert result.status == "PASS"
    assert result.basis == "DAILY"
    assert result.refined_count == 0
    assert result.candidates[0]["metrics"] == candidate["metrics"]


def test_minute_refinement_forward_fills_large_sparse_gaps_and_keeps_larger_daily_dd():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = {
        "candidate_id": "candidate",
        "campaign_equity": Decimal("100"),
        "calendar_days": 1,
        "members": ({
            "symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 1,
            "in_window_observation_count": 2,
            "initial_balance": Decimal("100"), "tested_size_usdt": Decimal("100"),
            "actual_size_usdt": Decimal("50"),
        },),
        "metrics": {
            "proxy_max_drawdown_usdt": Decimal("20"),
            "proxy_max_drawdown_pct": Decimal("20"),
        },
    }
    path = (
        {"timestamp_utc": start, "equity": Decimal("100")},
        {"timestamp_utc": start + __import__("datetime").timedelta(days=3), "equity": Decimal("110")},
    )

    result = refine_pretest_shortlist(
        (candidate,), {"A:LONG:1:1": path}, start_utc=start,
        end_utc=start + __import__("datetime").timedelta(days=3), max_gap_days=0,
    )

    assert result.status == "PASS"
    assert result.candidates[0]["minute_metrics"]["proxy_max_drawdown_usdt"] == Decimal("20.00000000")
