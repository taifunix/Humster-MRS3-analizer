from decimal import Decimal
from itertools import product
import importlib
import random
from types import SimpleNamespace

import pytest

from mrs3.portfolio.weighted_search import _coefficients, _solve_lp, rank_slot_compositions


def _delta(seed: int, t: int, n: int) -> tuple[tuple[Decimal, ...], ...]:
    rng = random.Random(seed)
    drift = [rng.uniform(0.00002, 0.0004) for _ in range(n)]
    return tuple(
        tuple(Decimal(str(round(rng.gauss(drift[column], 0.004), 8))) for column in range(n))
        for _ in range(t)
    )


def _brute_force(delta, caps, slots, *, max_dd, bank, margin=None):
    days = Decimal(len(delta)) / Decimal(288)
    ranked = []
    for choice in product(*slots):
        columns = list(choice)
        sub_delta = tuple(tuple(row[column] for column in columns) for row in delta)
        sub_caps = tuple(caps[column] for column in columns)
        coefficients = _coefficients(sub_delta, len(columns), days)
        kwargs = {}
        if margin is not None:
            kwargs = {
                "margin_a": tuple(margin[0][column] for column in columns),
                "margin_b": tuple(margin[1][column] for column in columns),
                "max_mm_load": margin[2],
            }
        outcome = _solve_lp(
            sub_delta, sub_caps, coefficients,
            max_dd=max_dd, target=None, bank_available=bank, maximize=True,
            symbol_cap_groups={}, **kwargs,
        )
        assert outcome.status == "PASS", outcome
        p30 = sum(c * x for c, x in zip(coefficients, outcome.solution.x))
        ranked.append((p30, choice))
    ranked.sort(key=lambda item: -item[0])
    return ranked


def test_milp_ranking_matches_exhaustive_lp_order() -> None:
    slots = ((0, 1), (2, 3, 4), (5,), (6, 7))
    delta = _delta(3, 288 * 2, 8)
    caps = tuple(Decimal(value) for value in (200, 150, 100, 220, 180, 120, 90, 160))
    days = Decimal(len(delta)) / Decimal(288)
    expected = _brute_force(delta, caps, slots, max_dd=Decimal("0.2"), bank=Decimal("600"))

    ranked = rank_slot_compositions(
        delta, caps, slots, max_dd=Decimal("0.2"), common_days=days,
        bank_available=Decimal("600"), limit=12,
    )

    assert len(ranked) == 12
    assert len({item.choice for item in ranked}) == 12
    assert ranked[0].choice == expected[0][1]
    for item, (p30, _choice) in zip(ranked, expected):
        assert abs(item.p30 - p30) <= Decimal("0.0001") * max(Decimal(1), abs(p30))


def test_milp_ranking_respects_margin_bounds() -> None:
    slots = ((0, 1), (2, 3))
    delta = _delta(11, 288, 4)
    caps = (Decimal(300),) * 4
    margin = ((Decimal("0.5"), Decimal("0.1"), Decimal("0.5"), Decimal("0.1")), (Decimal("0.05"),) * 4, Decimal("0.35"))
    days = Decimal(len(delta)) / Decimal(288)
    expected = _brute_force(delta, caps, slots, max_dd=Decimal("0.2"), bank=Decimal("200"), margin=margin)

    ranked = rank_slot_compositions(
        delta, caps, slots, max_dd=Decimal("0.2"), common_days=days, bank_available=Decimal("200"),
        margin_a=margin[0], margin_b=margin[1], max_mm_load=margin[2], limit=4,
    )

    assert [item.choice for item in ranked][0] == expected[0][1]
    assert [round(item.p30, 4) for item in ranked] == [round(p30, 4) for p30, _ in expected]


def test_milp_ranking_stops_when_universe_is_exhausted_and_handles_single_options() -> None:
    delta = _delta(5, 288, 3)
    days = Decimal(1)
    caps = (Decimal(100),) * 3

    assert [item.choice for item in rank_slot_compositions(
        delta, caps, ((0,), (1,), (2,)), max_dd=Decimal("0.3"), common_days=days, limit=5,
    )] == [(0, 1, 2)]
    assert len(rank_slot_compositions(
        delta, caps, ((0, 1), (2,)), max_dd=Decimal("0.3"), common_days=days, limit=10,
    )) == 2


@pytest.mark.parametrize("slots", [((0, 1), (1, 2)), ((0,), (1,)), ((0, 1, 2, 3),), ((0, 1), ())])
def test_milp_ranking_rejects_slots_that_are_not_a_partition(slots) -> None:
    with pytest.raises(ValueError, match="COMPOSITION_SLOTS_INVALID"):
        rank_slot_compositions(_delta(1, 10, 3), (Decimal(1),) * 3, slots, max_dd=Decimal("0.2"), common_days=Decimal(1), limit=1)


def test_milp_ranking_does_not_repeat_a_portfolio_through_zero_weight_slot_choices() -> None:
    rng = random.Random(9)
    t = 288
    # Slot 0 options gain; slot 1 options only lose, so slot 1 always gets zero weight.
    delta = tuple(
        (
            Decimal(str(round(rng.gauss(0.0003, 0.002), 8))),
            Decimal(str(round(rng.gauss(0.0002, 0.002), 8))),
            Decimal(str(round(-0.001 + rng.gauss(0, 0.0001), 8))),
            Decimal(str(round(-0.002 + rng.gauss(0, 0.0001), 8))),
        )
        for _ in range(t)
    )

    ranked = rank_slot_compositions(
        delta, (Decimal(100),) * 4, ((0, 1), (2, 3)),
        max_dd=Decimal("0.3"), common_days=Decimal(1), limit=4,
    )

    assert [item.choice[0] for item in ranked] == [0, 1]
    assert all(item.active == (item.choice[0],) for item in ranked)


def test_milp_ranking_keeps_time_limited_incumbent_and_stops(monkeypatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    real_milp = module.milp
    calls = []
    def limited(*args, **kwargs):
        result = real_milp(*args, **kwargs)
        calls.append(1)
        return SimpleNamespace(status=1, x=result.x, fun=result.fun, message="Time limit reached")
    monkeypatch.setattr(module, "milp", limited)
    events = []

    ranked = rank_slot_compositions(
        _delta(3, 288, 4), (Decimal(100),) * 4, ((0, 1), (2, 3)),
        max_dd=Decimal("0.3"), common_days=Decimal(1), limit=3, progress=events.append,
    )

    assert len(ranked) == 1 and len(calls) == 1
    assert ranked[0].proven_optimal is False
    assert events == [{"completed": 1, "total": 3, "status": 1}]


def test_milp_ranking_without_any_solution_raises_explicit_code(monkeypatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    monkeypatch.setattr(module, "milp", lambda *args, **kwargs: SimpleNamespace(status=1, x=None, fun=None, message="Time limit reached"))
    with pytest.raises(ValueError, match="COMPOSITION_SELECTION_NO_SOLUTION"):
        rank_slot_compositions(
            _delta(3, 288, 4), (Decimal(100),) * 4, ((0, 1), (2, 3)),
            max_dd=Decimal("0.3"), common_days=Decimal(1), limit=3,
        )
