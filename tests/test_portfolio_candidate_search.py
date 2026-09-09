from decimal import Decimal

import pytest

from mrs3.portfolio.candidate_search import search_portfolio_candidates


def finalist(symbol, side, strategy, *, pnl="10", dd="5", recovery="2", rank=1):
    return {
        "user_status": "FINALIST",
        "symbol": symbol,
        "side": side,
        "strategy_id": strategy,
        "result_id": strategy * 10,
        "user_rank": rank,
        "total_pnl": Decimal(pnl),
        "max_drawdown_pct": Decimal(dd),
        "recovery_factor": Decimal(recovery),
        "position_size_usdt": Decimal("500"),
        "sizing_digest": f"size-{strategy}",
        "reference_digest": "reference-a",
    }


def search(rows, **overrides):
    options = {
        "selected_symbols": ("BTCUSDT", "ETHUSDT"),
        "profile_id": "BALANCED",
        "scenario_id": "BALANCED",
        "individual_max_dd_pct": "20",
        "individual_net_pnl_min_exclusive": "0",
        "top_n_per_direction": 2,
        "max_enumerated_combinations": 100,
    }
    options.update(overrides)
    return search_portfolio_candidates(rows, **options)


def test_candidates_use_any_nonempty_subset_and_same_symbol_both_counts_twice():
    rows = [
        finalist("BTCUSDT", "LONG", 1),
        finalist("BTCUSDT", "SHORT", 2),
        finalist("ETHUSDT", "LONG", 3),
    ]

    result = search(rows)

    assert result.status == "PASS"
    assert result.total_combinations == 7
    assert {len(item.members) for item in result.candidates} == {1, 2, 3}
    assert any({member["symbol"] for member in item.members} == {"BTCUSDT"} for item in result.candidates)
    assert any({member["symbol"] for member in item.members} == {"ETHUSDT"} for item in result.candidates)
    assert any({member["symbol"] for member in item.members} == {"BTCUSDT", "ETHUSDT"} for item in result.candidates)
    assert all(len({(m["symbol"], m["side"]) for m in item.members}) == len(item.members) for item in result.candidates)
    assert result.candidates[0].schema_version == "portfolio_candidate_v1"
    assert result.candidates[0].profile_id == "BALANCED"
    assert result.candidates[0].scenario_id == "BALANCED"


def test_individual_gates_and_profile_ranking_reduce_each_direction_before_combinations():
    rows = [
        finalist("BTCUSDT", "LONG", 1, pnl="11", dd="5", recovery="2", rank=2),
        finalist("BTCUSDT", "LONG", 2, pnl="10", dd="4", recovery="3", rank=1),
        finalist("BTCUSDT", "LONG", 3, pnl="0", dd="1", recovery="1"),
        finalist("BTCUSDT", "SHORT", 4, pnl="9", dd="21", recovery="4"),
    ]

    result = search(
        rows,
        selected_symbols=("BTCUSDT",),
        profile_id="AGGRESSIVE",
        scenario_id="A",
        individual_max_dd_pct="20",
        top_n_per_direction=1,
    )

    assert result.status == "PASS"
    assert [member["strategy_id"] for member in result.candidates[0].members] == [1]
    assert {item.reason for item in result.excluded} == {"INDIVIDUAL_PNL_BELOW_FLOOR", "INDIVIDUAL_DD_EXCEEDED", "DIRECTION_TOP_N"}


def test_shuffle_is_deterministic_and_sizing_or_reference_changes_identity():
    rows = [finalist("BTCUSDT", "LONG", 1), finalist("ETHUSDT", "SHORT", 2)]
    first = next(candidate for candidate in search(rows).candidates if any(member["symbol"] == "BTCUSDT" for member in candidate.members))
    second = next(candidate for candidate in search(list(reversed(rows))).candidates if any(member["symbol"] == "BTCUSDT" for member in candidate.members))
    changed = [dict(row) for row in rows]
    changed[0]["position_size_usdt"] = Decimal("550")
    changed[0]["sizing_digest"] = "size-changed"
    third = next(candidate for candidate in search(changed).candidates if any(member["symbol"] == "BTCUSDT" for member in candidate.members))

    assert first.identity == second.identity
    assert first.members == second.members
    assert third.identity != first.identity


def test_spread_evidence_changes_candidate_identity():
    first_row = finalist("BTCUSDT", "LONG", 1)
    second_row = dict(first_row)
    first_row.update({"spread_status": "CLEAR", "spread_mean_bps": Decimal("10")})
    second_row.update({"spread_status": "CLEAR", "spread_mean_bps": Decimal("11")})

    first = search([first_row], selected_symbols=("BTCUSDT",)).candidates[0]
    second = search([second_row], selected_symbols=("BTCUSDT",)).candidates[0]

    assert first.identity != second.identity


def test_exact_count_limit_and_full_output_are_explicit():
    rows = [
        finalist("BTCUSDT", "LONG", 1),
        finalist("BTCUSDT", "SHORT", 2),
        finalist("ETHUSDT", "LONG", 3),
        finalist("ETHUSDT", "SHORT", 4),
    ]

    blocked = search(rows, max_enumerated_combinations=8)
    full = search(rows)

    assert blocked.status == "FAIL"
    assert blocked.reason == "COMBINATION_LIMIT_EXCEEDED"
    assert blocked.total_combinations == 15
    assert blocked.candidates == ()
    assert full.status == "PASS"
    assert full.total_combinations == 15
    assert len(full.candidates) == 15


def test_missing_selected_symbol_is_skipped_and_missing_runtime_size_fails_closed():
    missing = search([finalist("BTCUSDT", "LONG", 1)])
    bad = finalist("BTCUSDT", "LONG", 1)
    bad.pop("position_size_usdt")
    runtime = search([bad], selected_symbols=("BTCUSDT",))

    assert (missing.status, missing.reason) == ("PASS", None)
    assert {member["symbol"] for member in missing.candidates[0].members} == {"BTCUSDT"}
    assert (runtime.status, runtime.reason) == ("FAIL", "MISSING_RUNTIME_FACTS")


def test_search_rejects_empty_selection_and_nonpositive_dd_cap():
    with pytest.raises(ValueError):
        search([finalist("BTCUSDT", "LONG", 1)], selected_symbols=())
    with pytest.raises(ValueError):
        search([finalist("BTCUSDT", "LONG", 1)], individual_max_dd_pct="0")
    with pytest.raises(ValueError):
        search([finalist("BTCUSDT", "LONG", 1)], individual_max_dd_pct="-1")


def test_negative_candidate_drawdown_is_not_a_valid_option():
    result = search([finalist("BTCUSDT", "LONG", 1, dd="-1")], selected_symbols=("BTCUSDT",))

    assert result.status == "FAIL"
    assert result.reason == "INSUFFICIENT_DIRECTIONAL_UNIVERSE"
    assert result.candidates == ()


def test_optional_symbols_allow_twelve_selected_with_four_unusable_and_one_to_eight_pairs():
    usable = [finalist(f"SYM{i:02d}USDT", "LONG", i) for i in range(8)]

    result = search(
        usable,
        selected_symbols=tuple(f"SYM{i:02d}USDT" for i in range(12)),
        top_n_per_direction=1,
        max_enumerated_combinations=255,
    )

    assert result.status == "PASS"
    assert result.total_combinations == 255
    assert len(result.candidates) == 255
    assert {len(candidate.members) for candidate in result.candidates} == set(range(1, 9))
    assert all(
        {member["symbol"] for member in candidate.members} <= {f"SYM{i:02d}USDT" for i in range(8)}
        for candidate in result.candidates
    )


def test_mixed_long_short_and_both_directions_keep_one_pair_candidates():
    result = search(
        [
            finalist("BTCUSDT", "LONG", 1),
            finalist("ETHUSDT", "SHORT", 2),
            finalist("SOLUSDT", "LONG", 3),
            finalist("SOLUSDT", "SHORT", 4),
        ],
        selected_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        top_n_per_direction=1,
        max_enumerated_combinations=15,
    )

    assert result.status == "PASS"
    assert result.total_combinations == 15
    assert {len(candidate.members) for candidate in result.candidates} == {1, 2, 3, 4}
    assert any(len(candidate.members) == 1 for candidate in result.candidates)
    assert any(
        {(member["symbol"], member["side"]) for member in candidate.members}
        == {("BTCUSDT", "LONG"), ("ETHUSDT", "SHORT"), ("SOLUSDT", "LONG"), ("SOLUSDT", "SHORT")}
        for candidate in result.candidates
    )


def test_all_selected_symbols_without_usable_options_fail():
    result = search([], selected_symbols=("BTCUSDT", "ETHUSDT"))

    assert (result.status, result.reason) == ("FAIL", "INSUFFICIENT_DIRECTIONAL_UNIVERSE")
    assert result.total_combinations == 0
