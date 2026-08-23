from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def test_ready_witness_does_not_filter_observed_factual_grid() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6

    base = normalize_source_v6(FIXTURE.read_bytes())
    scope = "ONUSDT|LONG|1h"
    witness_grid = [
        replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    ]
    extra_fact = replace(base, point=replace(base.point, shift_bp=35, close_ma_length=9))

    materialized = materialize_source_v6((*witness_grid, extra_fact), (scope,))

    result = materialized.scopes[0]
    assert result.scope_key == scope
    assert len(result.facts) == len(witness_grid) + 1
    assert extra_fact.point.canonical_key in {item.point.canonical_key for item in result.facts}
    assert result.ready_witness.scope_key == scope


def test_materializer_rejects_scope_without_ready_witness() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_materializer import materialize_source_v6

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    with pytest.raises(ValueError, match="not READY"):
        materialize_source_v6((fragment,), ("ONUSDT|LONG|1h",))


def test_materializer_keeps_whole_source_digest_when_given_selected_facts() -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6

    base = normalize_source_v6(FIXTURE.read_bytes())
    facts = tuple(
        replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    )

    materialized = materialize_source_v6(
        facts, ("ONUSDT|LONG|1h",), source_content_digest_value="whole-source-digest"
    )

    assert materialized.source_content_digest == "whole-source-digest"
