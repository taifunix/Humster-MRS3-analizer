"""Point-local count slots: the legacy point schema stores a count as identifiers."""

from __future__ import annotations

from hashlib import sha256

import pytest

from mrs3.csv_legacy_compat import (
    CsvCompatibilityError,
    LEGACY_ANALYSIS_FINGERPRINT,
    LEGACY_SURFACE_FINGERPRINT,
    countslot_ids,
    event_ids_hash,
)


def test_legacy_profile_is_the_existing_reader_v1_contract():
    assert (LEGACY_ANALYSIS_FINGERPRINT, LEGACY_SURFACE_FINGERPRINT) == (
        "analysis-v6-fresh-compact-v1", "surface-v6-fresh-compact-v2",
    )


def test_countslot_is_deterministic_sorted_and_point_local():
    ids = countslot_ids("pA", 3)
    assert ids == countslot_ids("pA", 3) == sorted(ids)
    assert len(set(ids)) == 3
    assert set(ids).isdisjoint(countslot_ids("pB", 3))
    assert countslot_ids("pA", 0) == []


def test_event_ids_hash_matches_the_fresh_reader_rule():
    ids = countslot_ids("pA", 4)
    assert event_ids_hash(ids) == sha256("|".join(sorted(ids)).encode("utf-8")).hexdigest()
    assert event_ids_hash([]) == sha256(b"").hexdigest()


@pytest.mark.parametrize("invalid", [None, -1, 1.5, True, "3"])
def test_countslot_rejects_noninteger_counts(invalid):
    with pytest.raises(CsvCompatibilityError):
        countslot_ids("p", invalid)


def test_countslot_rejects_empty_point_id():
    with pytest.raises(CsvCompatibilityError):
        countslot_ids(" ", 1)
