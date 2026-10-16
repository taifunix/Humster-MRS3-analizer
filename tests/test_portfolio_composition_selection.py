from decimal import Decimal
import importlib
from itertools import product
import random
from types import SimpleNamespace

import pytest

from mrs3.portfolio.weighted_search import _coefficients, _solve_lp, slot_composition_frontier


def _delta(seed: int, t: int, n: int) -> tuple[tuple[Decimal, ...], ...]:
    rng = random.Random(seed)
    drift = [rng.uniform(0.00002, 0.0004) for _ in range(n)]
    return tuple(
        tuple(Decimal(str(round(rng.gauss(drift[column], 0.004), 8))) for column in range(n))
        for _ in range(t)
    )


def _sub(delta, caps, columns, margin):
    sub_delta = tuple(tuple(row[column] for column in columns) for row in delta)
    sub_caps = tuple(caps[column] for column in columns)
    kwargs = {}
    if margin is not None:
        kwargs = {
            "margin_a": tuple(margin[0][column] for column in columns),
            "margin_b": tuple(margin[1][column] for column in columns),
            "max_mm_load": margin[2],
        }
    return sub_delta, sub_caps, kwargs


def _best_at_bank(delta, caps, slots, *, max_dd, bank, margin=None):
    """Exhaustive reference: best LP P30 over every composition at one bank."""
    days = Decimal(len(delta)) / Decimal(288)
    best = None
    for choice in product(*slots):
        sub_delta, sub_caps, kwargs = _sub(delta, caps, list(choice), margin)
        coefficients = _coefficients(sub_delta, len(choice), days)
        outcome = _solve_lp(
            sub_delta, sub_caps, coefficients,
            max_dd=max_dd, target=None, bank_available=bank, maximize=True,
            symbol_cap_groups={}, **kwargs,
        )
        if outcome.status != "PASS":
            continue
        p30 = sum(c * x for c, x in zip(coefficients, outcome.solution.x))
        if best is None or p30 > best[0]:
            best = (p30, choice)
    return best


SLOTS = ((0, 1), (2, 3, 4), (5,), (6, 7))
CAPS = tuple(Decimal(value) for value in (200, 150, 100, 220, 180, 120, 90, 160))


def test_each_frontier_level_matches_the_exhaustive_best_composition_at_that_bank() -> None:
    delta = _delta(3, 288 * 2, 8)
    days = Decimal(len(delta)) / Decimal(288)

    points = slot_composition_frontier(
        delta, CAPS, SLOTS, max_dd=Decimal("0.2"), common_days=days, levels=4,
    )

    assert len(points) == 4
    assert [point.bank_limit for point in points] == sorted((point.bank_limit for point in points), reverse=True)
    assert all(later.p30 < earlier.p30 for earlier, later in zip(points, points[1:]))
    for point in points:
        reference = _best_at_bank(delta, CAPS, SLOTS, max_dd=Decimal("0.2"), bank=point.bank_limit)
        assert reference is not None
        assert abs(point.p30 - reference[0]) <= Decimal("0.0001") * max(Decimal(1), abs(reference[0]))
    # The top level is the saturation bank: the full-capacity P30 is reachable there.
    unbounded = _best_at_bank(delta, CAPS, SLOTS, max_dd=Decimal("0.2"), bank=Decimal("1000000"))
    assert abs(points[0].p30 - unbounded[0]) <= Decimal("0.0001") * unbounded[0]


def test_frontier_respects_the_profile_bank_ceiling_and_margin_bounds() -> None:
    delta = _delta(11, 288, 8)
    days = Decimal(1)
    margin = (
        tuple(Decimal(value) for value in ("0.5", "0.1", "0.5", "0.1", "0.3", "0.2", "0.4", "0.1")),
        (Decimal("0.05"),) * 8,
        Decimal("0.35"),
    )

    points = slot_composition_frontier(
        delta, CAPS, SLOTS, max_dd=Decimal("0.2"), common_days=days, bank_available=Decimal("150"),
        margin_a=margin[0], margin_b=margin[1], max_mm_load=margin[2], levels=3,
    )

    assert points[0].bank_limit <= Decimal("150")
    for point in points:
        reference = _best_at_bank(delta, CAPS, SLOTS, max_dd=Decimal("0.2"), bank=point.bank_limit, margin=margin)
        assert abs(point.p30 - reference[0]) <= Decimal("0.0001") * max(Decimal(1), abs(reference[0]))


def test_lower_levels_drop_or_shrink_members_instead_of_copying_the_top_portfolio() -> None:
    delta = _delta(5, 288 * 2, 8)

    points = slot_composition_frontier(
        delta, CAPS, SLOTS, max_dd=Decimal("0.2"), common_days=Decimal(2), levels=5,
    )

    assert len({(point.choice, point.p30) for point in points}) == len(points) == 5
    # Binding banks choose other finalists, not only smaller copies of the top portfolio.
    assert len({point.choice for point in points}) >= 2


def test_time_limited_incumbents_are_kept_and_progress_counts_every_solve(monkeypatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    real_milp = module.milp

    def limited(*args, **kwargs):
        result = real_milp(*args, **kwargs)
        return SimpleNamespace(status=1, x=result.x, fun=result.fun, message="Time limit reached")

    monkeypatch.setattr(module, "milp", limited)
    events = []

    points = slot_composition_frontier(
        _delta(3, 288, 8), CAPS, SLOTS, max_dd=Decimal("0.3"), common_days=Decimal(1), levels=3, progress=events.append,
    )

    assert points and all(point.proven_optimal is False for point in points)
    assert [event["completed"] for event in events] == [1, 2, 3, 4, 5]
    assert {event["total"] for event in events} == {5}


def test_frontier_without_any_solution_raises_explicit_code(monkeypatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(module, "milp", lambda *args, **kwargs: SimpleNamespace(status=1, x=None, fun=None, message="Time limit reached"))

    with pytest.raises(ValueError, match="COMPOSITION_SELECTION_NO_SOLUTION"):
        slot_composition_frontier(_delta(3, 288, 8), CAPS, SLOTS, max_dd=Decimal("0.3"), common_days=Decimal(1), levels=3)


def test_levels_below_the_minimum_bank_are_skipped() -> None:
    tiny = (Decimal("0.01"),) * 8

    points = slot_composition_frontier(_delta(3, 288, 8), tiny, SLOTS, max_dd=Decimal("0.3"), common_days=Decimal(1), levels=4)

    assert [point.bank_limit for point in points] == [Decimal("1.00")]


@pytest.mark.parametrize("slots", [((0, 1), (1, 2)), ((0,), (1,)), ((0, 1, 2, 3),), ((0, 1), ())])
def test_frontier_rejects_slots_that_are_not_a_partition(slots) -> None:
    with pytest.raises(ValueError, match="COMPOSITION_SLOTS_INVALID"):
        slot_composition_frontier(_delta(1, 10, 3), (Decimal(1),) * 3, slots, max_dd=Decimal("0.2"), common_days=Decimal(1), levels=1)
