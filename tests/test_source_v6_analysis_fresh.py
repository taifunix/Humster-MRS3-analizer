from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"


def _publish_fixture() -> tuple[dict[str, object], str, str, list[dict[str, object]]]:
    return (
        {"fingerprint": "analysis-v6-fresh-compact-v2"},
        "analysis-id",
        "config-hash",
        [{
            "scope_key": "ONUSDT|LONG|1h",
            "scope_digest": "scope-digest",
            "frames": {"points": [{"point_id": "point-1", "value": "stable"}]},
        }],
    )


def test_publish_commits_before_checkpoint_close_validation_and_replace(tmp_path: Path, monkeypatch) -> None:
    import duckdb
    import mrs3.source_v6_analysis_fresh as fresh

    events: list[str] = []
    real_connect = duckdb.connect
    real_replace = fresh.os.replace

    class SpyConnection:
        def __init__(self, connection, read_only: bool) -> None:
            self._connection = connection
            self._read_only = read_only

        def execute(self, sql, *args, **kwargs):
            command = str(sql).strip().split(None, 1)[0].upper()
            events.append(f"{'read' if self._read_only else 'write'}:{command}")
            return self._connection.execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            events.append(f"{'read' if self._read_only else 'write'}:DML")
            return self._connection.executemany(sql, *args, **kwargs)

        def close(self) -> None:
            events.append(f"{'read' if self._read_only else 'write'}:CLOSE")
            self._connection.close()

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def connect(path, *args, **kwargs):
        read_only = bool(kwargs.get("read_only", False))
        return SpyConnection(real_connect(path, *args, **kwargs), read_only)

    def replace(source, target):
        events.append("REPLACE")
        return real_replace(source, target)

    monkeypatch.setattr(fresh.duckdb, "connect", connect)
    monkeypatch.setattr(fresh.os, "replace", replace)
    identity, analysis_id, config_hash, results = _publish_fixture()

    fresh._publish(tmp_path, tmp_path / "target.duckdb", identity, analysis_id, config_hash, results)

    begin = events.index("write:BEGIN")
    commit = events.index("write:COMMIT")
    checkpoint = events.index("write:CHECKPOINT")
    writer_close = events.index("write:CLOSE")
    read_validation = events.index("read:SELECT")
    replacement = events.index("REPLACE")
    dml = [index for index, event in enumerate(events) if event in {"write:DML", "write:INSERT"}]
    assert begin > max(index for index, event in enumerate(events) if event == "write:CREATE")
    assert dml and begin < min(dml) <= max(dml) < commit
    assert commit < checkpoint < writer_close < read_validation < replacement
    assert events.count("write:BEGIN") == 1
    assert events.count("write:COMMIT") == 1


def test_publish_rolls_back_manifest_and_later_dml_without_partial_publish(tmp_path: Path, monkeypatch) -> None:
    import duckdb
    import pytest
    import mrs3.source_v6_analysis_fresh as fresh

    real_connect = duckdb.connect
    current = {"failure": "", "message": "", "rollback_fails": False, "rollback_attempts": 0}

    class FailingConnection:
        def __init__(self, connection, read_only: bool) -> None:
            self._connection = connection
            self._read_only = read_only

        def execute(self, sql, *args, **kwargs):
            normalized = str(sql).strip().upper()
            if not self._read_only and normalized.startswith(current["failure"]):
                raise ValueError(current["message"])
            if not self._read_only and normalized == "ROLLBACK":
                current["rollback_attempts"] += 1
                if current["rollback_fails"]:
                    raise RuntimeError("rollback failed")
            return self._connection.execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            normalized = str(sql).strip().upper()
            if not self._read_only and normalized.startswith(current["failure"]):
                raise ValueError(current["message"])
            return self._connection.executemany(sql, *args, **kwargs)

        def close(self) -> None:
            self._connection.close()

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def connect(path, *args, **kwargs):
        return FailingConnection(real_connect(path, *args, **kwargs), bool(kwargs.get("read_only", False)))

    monkeypatch.setattr(fresh.duckdb, "connect", connect)
    for name, failure, message, rollback_fails in (
        ("manifest", "INSERT INTO MANIFEST", "manifest dml failed", False),
        ("scope-runs", "INSERT INTO SCOPE_RUNS", "scope runs dml failed", False),
        ("rollback-failure", "INSERT INTO SCOPE_RUNS", "scope runs dml failed before rollback failure", True),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        target = case_dir / "target.duckdb"
        with real_connect(str(target)) as connection:
            connection.execute("create table existing(value varchar)")
            connection.execute("insert into existing values ('old')")
        before_bytes = target.read_bytes()
        before_stat = target.stat()
        current.update(failure=failure, message=message, rollback_fails=rollback_fails, rollback_attempts=0)
        identity, analysis_id, config_hash, results = _publish_fixture()

        with pytest.raises(ValueError, match=message):
            fresh._publish(case_dir, target, identity, analysis_id, config_hash, results)

        assert current["rollback_attempts"] == 1
        assert target.read_bytes() == before_bytes
        after_stat = target.stat()
        assert after_stat.st_size == before_stat.st_size
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        with real_connect(str(target), read_only=True) as connection:
            assert connection.execute("select value from existing").fetchone() == ("old",)
        assert [path.name for path in case_dir.iterdir()] == [target.name]


def test_publish_accepts_zero_all_empty_and_one_row_results(tmp_path: Path) -> None:
    import duckdb
    import mrs3.source_v6_analysis_fresh as fresh

    identity = {"fingerprint": "analysis-v6-fresh-compact-v2"}
    cases = [
        ("zero", []),
        ("empty", [{"scope_key": "empty", "scope_digest": "digest", "frames": {name: [] for name in fresh._TABLES}}]),
        ("one", [{"scope_key": "one", "scope_digest": "digest", "frames": {"points": [{"point_id": "p", "value": "v"}]}}]),
    ]
    for name, results in cases:
        target = fresh._publish(tmp_path, tmp_path / f"{name}.duckdb", identity, name, "config", results)
        with duckdb.connect(str(target), read_only=True) as connection:
            assert connection.execute("select count(*) from scope_runs").fetchone()[0] == len(results)
            if name == "one":
                assert connection.execute("select payload_json from points").fetchone() == ('{"point_id":"p","value":"v"}',)


def test_fresh_frames_strip_admission_fields_except_plateau_audit_count() -> None:
    import mrs3.source_v6_analysis_fresh as fresh

    point = {
        "point_id": "ONUSDT|LONG|1h|100|200",
        "plateau_id": "PLAT_1",
        "symbol": "ONUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "close_ma": 200,
        "open_ma": 100,
        "shift_bp": 250,
        "shift_pct": 2.5,
        "pnl_pct": 12.0,
        "dd_pct": 4.0,
        "efficiency": 3.0,
        "trades": 11,
        "plateau_point_count": 3,
        "base_point_trades": 11,
        "plateau_total_trades": 31,
        "standalone_eligible": True,
        "depth_eligible": True,
        "events_last_30d": 22,
        "plateau_event_count": 44,
    }
    stages = SimpleNamespace(
        points=pd.DataFrame([point]),
        refine_requests=pd.DataFrame(),
        plateaus=pd.DataFrame([{
            "plateau_id": point["plateau_id"],
            "plateau_event_count": point["plateau_event_count"],
        }]),
        close_profiles=pd.DataFrame(),
        base_one_order=pd.DataFrame([point]),
        structures=pd.DataFrame([{
            "structure_id": "STR_legacy",
            "events_last_30d": 22,
            "plateau_event_count": 44,
            "orders": ({"point_id": point["point_id"], "events_last_30d": 22, "plateau_event_count": 44},),
        }]),
        structure_diagnostics=pd.DataFrame(),
    )

    frames = fresh._frames(stages)

    assert "events_last_30d" not in frames["points"][0]
    assert "plateau_event_count" not in frames["points"][0]
    assert frames["plateaus"] == [{
        "plateau_id": point["plateau_id"],
        "plateau_event_count": point["plateau_event_count"],
    }]
    assert all(
        key not in row
        for row in frames["structures"]
        for key in ("events_last_30d", "plateau_event_count")
    )
    legacy = next(row for row in frames["structures"] if row["structure_id"] == "STR_legacy")
    assert all(
        key not in order
        for order in legacy["orders"]
        for key in ("events_last_30d", "plateau_event_count")
    )
    base = next(row for row in frames["structures"] if row["structure_id"].startswith("BASE_"))
    assert base["order_count"] == 1
    assert base["status"] == "READY_MRS3_STRUCTURE"
    for key, value in {
        "plateau_point_count": 3,
        "base_point_trades": 11,
        "plateau_total_trades": 31,
    }.items():
        assert base[key] == base["orders"][0][key] == value


def test_fresh_fallback_adds_events_last_30d_before_frame_conversion(monkeypatch) -> None:
    from datetime import date

    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_coverage import ReadyInterval
    import mrs3.source_v6_analysis_fresh as fresh

    fragment = normalize_source_v6(FIXTURE.read_bytes())
    from mrs3.source_v6_stitch import calculate_metrics
    full = calculate_metrics((fragment,))
    monkeypatch.setattr(
        "mrs3.source_v6_materializer.calculate_metrics",
        lambda _fragments, **_kwargs: full,
    )
    scope = SimpleNamespace(
        ready_witness=ReadyInterval("ONUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 31)),
        facts=(fragment,),
        scope_digest="scope-digest",
    )
    _witness, rows = fresh._measured_rows(scope)

    assert rows
    assert all(type(row["events_last_30d"]) is int for row in rows)


def test_fresh_frames_preserve_legacy_order_and_append_sorted_base_records() -> None:
    import mrs3.source_v6_analysis_fresh as fresh

    def base_point(point_id: str, close_ma: int) -> dict[str, object]:
        return {
            "point_id": point_id,
            "plateau_id": f"PLAT_{point_id}",
            "symbol": "ONUSDT",
            "side": "LONG",
            "timeframe": "1h",
            "close_ma": close_ma,
            "open_ma": 3,
            "shift_bp": 250,
            "shift_pct": 2.5,
            "pnl_pct": 12.0,
            "dd_pct": 4.0,
            "efficiency": 3.0,
            "trades": 11,
            "standalone_eligible": True,
            "depth_eligible": True,
        }

    base_one_order = pd.DataFrame([base_point("BASE_INPUT_B", 200), base_point("BASE_INPUT_A", 220)])
    stages = SimpleNamespace(
        points=pd.DataFrame(),
        refine_requests=pd.DataFrame(),
        plateaus=pd.DataFrame(),
        close_profiles=pd.DataFrame(),
        base_one_order=base_one_order,
        structures=pd.DataFrame([{"structure_id": "LEGACY_Z"}, {"structure_id": "LEGACY_A"}]),
        structure_diagnostics=pd.DataFrame(),
    )

    frames = fresh._frames(stages)
    expected_base_ids = sorted(
        fresh._base_structure(row)["structure_id"]
        for _, row in base_one_order.iterrows()
    )

    assert [row["structure_id"] for row in frames["structures"]] == [
        "LEGACY_Z", "LEGACY_A", *expected_base_ids,
    ]


def test_fresh_analysis_does_not_reuse_legacy_algorithm_artifact(tmp_path: Path) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.pipeline import ALGORITHM_VERSION
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(
        identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close)))
        for shift in CANONICAL_READINESS_SHIFTS_BP
        for close in CANONICAL_READINESS_CLOSE_LENGTHS
    )
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    output = tmp_path / "analysis"
    legacy = run_multiscope_analysis(
        surface, output, AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
        algorithm_version="0.7-canonical-phase1",
    )
    current = run_multiscope_analysis(
        surface, output, AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
    )

    assert current != legacy
    with duckdb.connect(str(legacy), read_only=True) as connection:
        legacy_manifest = dict(connection.execute("select key, value from manifest").fetchall())
    with duckdb.connect(str(current), read_only=True) as connection:
        current_manifest = dict(connection.execute("select key, value from manifest").fetchall())
    assert legacy_manifest["algorithm_version"] == "0.7-canonical-phase1"
    assert current_manifest["algorithm_version"] == ALGORITHM_VERSION == "0.7-canonical-phase1-base-1ord-v3"
    assert legacy_manifest["analysis_id"] != current_manifest["analysis_id"]


def test_fresh_analysis_is_separate_and_binds_the_supplied_gap_rules(tmp_path: Path, monkeypatch) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    import mrs3.source_v6_analysis_fresh as fresh
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    default = AlgorithmConfig.defaults()
    changed = replace(default, gap_rules=((30, 551, 10),))
    admission_changed = replace(default, multi_order_min_plateau_points=4)
    read_surface = fresh.read_multiscope_surface
    decode_calls = []
    monkeypatch.setattr(fresh, "read_multiscope_surface", lambda path, *, decode=True: decode_calls.append(decode) or read_surface(path, decode=decode))

    first = run_multiscope_analysis(surface, tmp_path / "analysis", default, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)
    second = run_multiscope_analysis(surface, tmp_path / "analysis", changed, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)
    third = run_multiscope_analysis(surface, tmp_path / "analysis", admission_changed, listing_dates={"ONUSDT": "2020-01-01"}, workers=1)

    assert first != second
    assert first != third
    assert decode_calls == [False, False, False]
    assert first.name.endswith(".analysis-v6.duckdb")
    connection = duckdb.connect(str(second), read_only=True)
    try:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
        assert manifest["fingerprint"] == "analysis-v6-fresh-compact-v2"
        assert manifest["surface_fingerprint"] == "surface-v6-fresh-compact-v3"
        assert manifest["algorithm_config_sha256"] != dict(duckdb.connect(str(first), read_only=True).execute("select key, value from manifest").fetchall())["algorithm_config_sha256"]
        assert manifest["algorithm_config_sha256"] != dict(duckdb.connect(str(third), read_only=True).execute("select key, value from manifest").fetchall())["algorithm_config_sha256"]
        assert connection.execute("select count(*) from points").fetchone()[0] == len(facts)
    finally:
        connection.close()


def test_fresh_analysis_uses_one_read_only_worker_per_scope(tmp_path: Path) -> None:
    import duckdb
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    first = [identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS]
    second = [identified(replace(item, point=replace(item.point, symbol="BTCUSDT"))) for item in first]
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6((*first, *second), ("ONUSDT|LONG|1h", "BTCUSDT|LONG|1h")))

    artifact = run_multiscope_analysis(surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01", "BTCUSDT": "2020-01-01"}, workers=2)

    connection = duckdb.connect(str(artifact), read_only=True)
    try:
        assert connection.execute("select count(*) from scope_runs").fetchone()[0] == 2
    finally:
        connection.close()


def test_analysis_identity_binds_surface_analysis_input_digest(tmp_path: Path, monkeypatch) -> None:
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    import mrs3.source_v6_analysis_fresh as fresh

    base = normalize_source_v6(FIXTURE.read_bytes())
    facts = tuple(
        replace(item, fragment_id=sha256(canonical_fragment_bytes(item)).hexdigest())
        for item in (
            replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))
            for shift in CANONICAL_READINESS_SHIFTS_BP
            for close in CANONICAL_READINESS_CLOSE_LENGTHS
        )
    )
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    first = fresh.run_multiscope_analysis(
        surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
    )
    read_surface = fresh.read_multiscope_surface

    def changed_digest(path, *, decode=False):
        identity = dict(read_surface(path, decode=decode))
        identity["analysis_input_digest"] = "e" * 64
        return identity

    monkeypatch.setattr(fresh, "read_multiscope_surface", changed_digest)
    second = fresh.run_multiscope_analysis(
        surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
    )

    assert second != first
    import duckdb
    with duckdb.connect(str(second), read_only=True) as connection:
        manifest = dict(connection.execute("select key, value from manifest").fetchall())
    assert manifest["analysis_input_digest"] == "e" * 64


def test_fresh_analysis_cancellation_prevents_publication(tmp_path: Path) -> None:
    import pytest
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis

    base = normalize_source_v6(FIXTURE.read_bytes())
    def identified(fragment):
        return replace(fragment, fragment_id=sha256(canonical_fragment_bytes(fragment)).hexdigest())
    facts = tuple(identified(replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))) for shift in CANONICAL_READINESS_SHIFTS_BP for close in CANONICAL_READINESS_CLOSE_LENGTHS)
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))
    with pytest.raises(RuntimeError, match="cancelled"):
        run_multiscope_analysis(surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"}, cancel_check=lambda: True)
    assert not tuple((tmp_path / "analysis").glob("*.analysis-v6.duckdb"))


def test_fresh_analysis_accepts_an_editable_human_filename(tmp_path: Path) -> None:
    import pytest
    from mrs3.config import AlgorithmConfig
    from mrs3.source_v6 import canonical_fragment_bytes, normalize_source_v6
    from mrs3.source_v6_coverage import CANONICAL_READINESS_CLOSE_LENGTHS, CANONICAL_READINESS_SHIFTS_BP
    from mrs3.source_v6_analysis_fresh import run_multiscope_analysis
    from mrs3.source_v6_materializer import materialize_source_v6
    from mrs3.source_v6_surface_fresh import publish_multiscope_surface

    base = normalize_source_v6(FIXTURE.read_bytes())
    facts = tuple(
        replace(item, fragment_id=sha256(canonical_fragment_bytes(item)).hexdigest())
        for item in (
            replace(base, point=replace(base.point, shift_bp=shift, close_ma_length=close))
            for shift in CANONICAL_READINESS_SHIFTS_BP
            for close in CANONICAL_READINESS_CLOSE_LENGTHS
        )
    )
    surface = publish_multiscope_surface(tmp_path / "surfaces", materialize_source_v6(facts, ("ONUSDT|LONG|1h",)))

    artifact = run_multiscope_analysis(
        surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
        filename="ON_2026-01-01_2026-01-31.analysis-v6.duckdb",
    )

    assert artifact.name == "ON_2026-01-01_2026-01-31.analysis-v6.duckdb"
    with pytest.raises(FileExistsError, match="already exists"):
        run_multiscope_analysis(
            surface, tmp_path / "analysis", AlgorithmConfig.defaults(), listing_dates={"ONUSDT": "2020-01-01"},
            filename="ON_2026-01-01_2026-01-31.analysis-v6.duckdb",
        )
