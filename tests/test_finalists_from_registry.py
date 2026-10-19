"""The finalist retest and the optimizer read the registry marks (Panel rework stage 4b, ADR-0080).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 4 contract» (4b).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from mrs3.pair_registry.records import finalist_marks, record_finalist
from mrs3.performance_v2_finalist_retest import current_effective_finalist_members, freeze_finalist_cohort
from mrs3.portfolio.input import read_current_finalists, registry_finalist_provenance
from tests.test_pair_registry_records import NOW, _performance_db, _registry, _review

A, B = "GOODUSDT_15m_LONG_A", "GOODUSDT_1h_LONG_B"


def _marked(tmp_path: Path):
    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    with duckdb.connect(str(database)) as source:
        for strategy_id in (1, 2):
            source.execute("update strategies set current_result_id = ? where strategy_id = ?", [10 + strategy_id, strategy_id])
    record_finalist(con, "bybit", "GOODUSDT", "LONG", A, "FINALIST", "panel", user_rank=2, now_ms=NOW)
    record_finalist(con, "bybit", "GOODUSDT", "LONG", B, "RESERVE", "excel", now_ms=NOW + 1)
    return con, database


def test_finalist_marks_carry_their_provenance(tmp_path: Path) -> None:
    con, _database = _marked(tmp_path)
    marks = finalist_marks(con, "bybit")
    assert set(marks) == {A, B}
    assert (marks[A]["status"], marks[A]["user_rank"], marks[A]["source"], marks[A]["symbol"]) == (
        "FINALIST", 2, "panel", "GOODUSDT")
    assert marks[A]["seq"] < marks[B]["seq"] and marks[B]["created_ms"] == NOW + 1
    record_finalist(con, "bybit", "GOODUSDT", "LONG", B, "NONE", "panel", now_ms=NOW + 2)
    assert set(finalist_marks(con, "bybit")) == {A}                         # NONE is no mark


def test_retest_cohort_uses_the_given_decisions_not_the_reviews(tmp_path: Path) -> None:
    con, database = _marked(tmp_path)
    _review(database, "1", "2026-10-02 00:00:00+00", [(1, "REJECTED", None), (2, "FINALIST", 1)])
    decisions = {1: ("FINALIST", 2, None), 2: ("RESERVE", None, None)}
    listings = {"GOODUSDT": "2026-06-01T00:00:00+00:00"}
    with duckdb.connect(str(database), read_only=True) as connection:
        def considered(**kwargs):
            cohort = freeze_finalist_cohort(connection, test_start="2026-09-01", test_end="2026-10-01",
                                            listing_dates=listings, **kwargs)
            return ({int(member["strategy_id"]) for member in cohort.members}
                    | {item.strategy_id for item in cohort.exclusions})
        assert considered(decisions=decisions) == {1}
        assert considered(decisions=decisions, include_reserve=True) == {1, 2}
        from mrs3.performance_v2_selection_review import effective_selection_decisions

        reviewed = {sid for sid, decision in effective_selection_decisions(connection).items() if decision[0] == "FINALIST"}
        assert considered() == reviewed                                     # without decisions: the reviews, as before
        members = current_effective_finalist_members(connection, include_reserve=True, decisions=decisions)
        assert [(member["strategy_id"], member["effective_status"], member["selection_run_id"]) for member in members] == [
            (1, "FINALIST", None), (2, "RESERVE", None)]


def test_optimizer_finalists_come_from_the_registry_marks(tmp_path: Path) -> None:
    con, database = _marked(tmp_path)
    marks = finalist_marks(con, "bybit")
    rows = read_current_finalists(database, [("GOODUSDT", "LONG")], False, marks=marks)
    assert [(row["strategy_id"], row["user_status"], row["user_rank"], row["registry_mark_seq"]) for row in rows] == [
        (1, "FINALIST", 2, marks[A]["seq"])]
    assert rows[0]["selection_run_id"] is None and rows[0]["review_import_id"] is None
    assert read_current_finalists(database, [("OTHERUSDT", "LONG")], False, marks=marks) == ()
    assert read_current_finalists(database, [("GOODUSDT", "LONG")], False) == ()          # no reviews: none


def test_registry_provenance_binds_the_mark_and_the_result() -> None:
    mark = {"seq": 7, "source": "panel", "created_ms": NOW, "status": "FINALIST", "user_rank": 1}
    strategy = {"analysis_run_id": "run", "candidate_identity": "c", "created_at_utc": "2026-10-01T00:00:00+00:00",
                "updated_at_utc": "2026-10-01T00:00:00+00:00"}
    provenance = registry_finalist_provenance(mark, strategy, {"imported_at_utc": "2026-10-02T00:00:00+00:00"})
    assert provenance["source"] == "pair_registry" and provenance["mark_seq"] == 7
    assert provenance["mark_source"] == "panel" and provenance["analysis_run_id"] == "run"
    assert provenance["result_imported_at_utc"].startswith("2026-10-02")
    from mrs3.portfolio.input import PortfolioInputError

    with pytest.raises(PortfolioInputError):
        registry_finalist_provenance({**mark, "seq": None}, strategy, {"imported_at_utc": "2026-10-02T00:00:00+00:00"})


def _panel_config(tmp_path: Path) -> None:
    (tmp_path / "config.local.json").write_text(json.dumps({"pair_registry": {"root": "registry"}}), encoding="utf-8")


def test_the_panel_retest_reads_the_registry_decisions(tmp_path: Path) -> None:
    from mrs3.panel import PanelController

    con, database = _marked(tmp_path)
    con.close()
    _panel_config(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    with duckdb.connect(str(database), read_only=True) as connection:
        assert controller._registry_finalist_decisions(connection) == {1: ("FINALIST", 2, None), 2: ("RESERVE", None, None)}
    (tmp_path / "registry" / "registry.duckdb").unlink()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert controller._registry_finalist_decisions(connection) is None          # no registry: the reviews


def test_the_panel_optimizer_reads_the_registry_marks(tmp_path: Path, monkeypatch) -> None:
    import mrs3.panel_portfolio as portfolio_module
    from mrs3.panel_portfolio import PortfolioPanelService

    con, database = _marked(tmp_path)
    con.close()
    _panel_config(tmp_path)
    seen = {}

    def reader(database_path, pairs, include_series=True, *, marks=None):
        seen.update(pairs=tuple(pairs), marks=marks)
        return ()

    monkeypatch.setattr(portfolio_module, "read_current_finalists", reader)
    service = PortfolioPanelService(tmp_path)
    service._invoke_finalists(database, [("GOODUSDT", "LONG")], False)
    assert set(seen["marks"]) == {A, B}
    assert service._finalist_pairs(database) == (("GOODUSDT", "LONG"),)            # pairs from the marks
    (tmp_path / "registry" / "registry.duckdb").unlink()
    seen.clear()
    service._invoke_finalists(database, [("GOODUSDT", "LONG")], False)
    assert seen["marks"] is None                                                    # no registry: the reviews
    assert service._finalist_pairs(database) == ()                                  # selection runs: none here


# -- review of 4b ------------------------------------------------------------------------------------------------

def test_the_manifest_freezes_the_same_registry_decisions(tmp_path: Path, monkeypatch) -> None:
    # review of 4b (BLOCKER): the retest manifest must freeze the cohort with the registry decisions too
    import mrs3.performance_v2_finalist_retest as retest

    seen = {}

    class Stop(Exception):
        pass

    def freeze(connection, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(retest, "freeze_finalist_cohort", freeze)
    with pytest.raises(Stop):
        retest.build_finalist_retest_manifest(None, {}, tmp_path, test_start="2026-09-01", test_end="2026-10-01",
                                              decisions={1: ("FINALIST", None, None)})
    assert seen["decisions"] == {1: ("FINALIST", None, None)}
    import inspect

    import mrs3.panel as panel_module

    source = inspect.getsource(panel_module.PanelController.strategies_performance_v2_finalist_retest_start)
    assert source.count("decisions=decisions") == 2                 # the cohort check and the manifest


def test_the_reviews_decide_until_the_registry_holds_a_mark_of_the_exchange(tmp_path: Path) -> None:
    from mrs3.pair_registry.store import connect
    from mrs3.panel import PanelController

    con = _registry(tmp_path)
    database = _performance_db(tmp_path / "perf.duckdb")
    con.close()
    _panel_config(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    with duckdb.connect(str(database), read_only=True) as connection:
        assert controller._registry_finalist_decisions(connection) is None          # no mark yet: the reviews
    con = connect(tmp_path / "registry")
    record_finalist(con, "bybit", "GOODUSDT", "LONG", A, "REJECTED", "panel", now_ms=NOW)
    record_finalist(con, "bingx", "GOODUSDT", "LONG", B, "FINALIST", "panel", now_ms=NOW)     # another exchange
    con.close()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert controller._registry_finalist_decisions(connection) == {1: ("REJECTED", None, None)}


def test_a_busy_registry_and_the_refused_review_imports(tmp_path: Path, monkeypatch) -> None:
    from mrs3.panel import PanelController
    from mrs3.performance_v2_finalist_retest import FinalistRetestError

    con, database = _marked(tmp_path)
    con.close()
    _panel_config(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    assert controller._registry_decides_for(database) is True                    # old review imports are refused
    held = duckdb.connect(str(tmp_path / "registry" / "registry.duckdb"))         # a writer in this process
    try:
        with duckdb.connect(str(database), read_only=True) as connection:
            with pytest.raises(FinalistRetestError) as error:
                controller._registry_finalist_decisions(connection)
        assert error.value.code == "REGISTRY_BUSY"
    finally:
        held.close()
    (tmp_path / "registry" / "registry.duckdb").unlink()
    assert controller._registry_decides_for(database) is False


def test_optimizer_marks_are_read_once_for_metadata(tmp_path: Path, monkeypatch) -> None:
    import mrs3.panel_portfolio as portfolio_module
    from mrs3.panel_portfolio import PortfolioPanelService

    con, database = _marked(tmp_path)
    con.close()
    _panel_config(tmp_path)
    reads = []
    original = PortfolioPanelService._registry_marks
    monkeypatch.setattr(PortfolioPanelService, "_registry_marks",
                        lambda self, path: reads.append(path) or original(self, path))
    monkeypatch.setattr(portfolio_module, "_require_exchange_data", lambda path: None)
    calls = []
    monkeypatch.setattr(portfolio_module, "read_current_finalists",
                        lambda database, pairs, include_series=True, *, marks=None: calls.append((tuple(pairs), marks)) or ())
    service = PortfolioPanelService(tmp_path)
    service._current_finalist_metadata({"inputs": {"performance_db": str(database)}})
    assert len(reads) == 1 and calls[0][0] == (("GOODUSDT", "LONG"),) and set(calls[0][1]) == {A, B}
