from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import importlib.util
import sys

import pytest

from mrs3.locking import OutputDirectoryBusyError
from mrs3.source_v6_importer import (
    SourceV6ImportCancelled,
    SourceV6ImportError,
    SourceV6WorkerFailure,
    import_source_v6,
    prepare_source_v6_snapshot,
    preflight_source_v6,
    source_v6_import_lock,
)
from mrs3.source_v6_storage import database_info


FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (root / name).write_bytes((FIXTURES / name).read_bytes())
    return root


def _load_debian_cli():
    script_path = Path(__file__).parents[1] / "scripts" / "import_source_v6_debian.py"
    spec = importlib.util.spec_from_file_location("source_v6_debian_cli", script_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli, script_path


def test_import_uses_one_parent_writer_and_batches_at_most_32(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)

    result = import_source_v6(
        reports,
        target,
        preflight=preflight,
        workers=1,
        batch_size=32,
    )

    assert result.status == "COMMITTED"
    assert result.writer_count == 1
    assert result.batch_sizes == (2,)
    assert max(result.batch_sizes) <= 32
    assert database_info(target)["source_content_digest"] == result.source_content_digest


def test_import_publishes_the_compacted_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    import mrs3.source_v6_importer as importer
    calls: list[tuple[Path, Path]] = []
    original = importer.compact_v6_database

    def compact(source: Path, published: Path) -> None:
        calls.append((source, published))
        original(source, published)

    monkeypatch.setattr(importer, "compact_v6_database", compact)

    result = import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    assert result.status == "COMMITTED"
    assert calls and calls[0][0] != target and calls[0][1] != target


def test_held_target_lock_fails_without_mutating_target(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    target.write_bytes(b"published target")
    original = target.read_bytes()
    preflight = preflight_source_v6(reports, target)

    with source_v6_import_lock(target):
        with pytest.raises(OutputDirectoryBusyError, match="already being written"):
            import_source_v6(reports, target, preflight=preflight, workers=1)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.staging*"))


def test_publish_failure_cleans_staging_and_keeps_target_absent(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)

    def fail(event: str) -> None:
        if event == "before_publish":
            raise RuntimeError("injected publish failure")

    with pytest.raises(SourceV6ImportError, match="injected publish failure"):
        import_source_v6(reports, target, preflight=preflight, workers=1, fault_injector=fail)
    assert not target.exists()
    assert not list(tmp_path.glob("*.staging*"))


def test_cancelled_import_cleans_staging_and_keeps_target_absent(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)

    with pytest.raises(SourceV6ImportCancelled, match="cancelled"):
        import_source_v6(reports, target, preflight=preflight, workers=1, cancellation_requested=lambda: True)
    assert not target.exists()
    assert not list(tmp_path.glob("*.staging*"))


def test_debian_cli_routes_writes_through_shared_importer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    cli, script_path = _load_debian_cli()
    calls: list[Path] = []
    original = cli.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        calls.append(Path(database))
        return original(root, database, **kwargs)

    monkeypatch.setattr(cli, "import_source_v6", wrapped)
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target)])
    assert cli.main() == 0
    assert calls == [target.resolve()]
    payload = json.loads(capsys.readouterr().out)
    assert {item["status"] for item in payload["reports"]} == {"COMMITTED"}


def test_debian_cli_writes_atomic_report_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    progress = tmp_path / "job" / "progress"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 1}}), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    monkeypatch.setattr(sys, "argv", [
        str(script_path), str(reports), str(target), "--config", str(config), "--progress", str(progress),
    ])

    assert cli.main() == 0
    current, total, workers, started = progress.read_text(encoding="ascii").split()
    assert (current, total, workers) == ("2", "2", "1")
    assert int(started) > 0


def test_debian_cli_discovers_uppercase_html_without_case_sensitive_precheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "REPORT.HTML").write_bytes((FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    target = tmp_path / "source-v6.duckdb"
    cli, script_path = _load_debian_cli()
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target)])

    assert cli.main() == 0
    assert target.exists()


def test_debian_cli_uses_configured_import_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 2}}), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    calls: list[int] = []
    original = cli.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        calls.append(int(kwargs["workers"]))
        return original(root, database, **kwargs)

    monkeypatch.setattr(cli, "import_source_v6", wrapped)
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target), "--config", str(config)])

    assert cli.main() == 0
    assert calls == [2]


def test_debian_cli_uses_configured_source_v6_write_batch_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"source_v6_import": {"write_batch_size": 8}}), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    calls: list[int] = []
    original = cli.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        calls.append(int(kwargs["batch_size"]))
        return original(root, database, **kwargs)

    monkeypatch.setattr(cli, "import_source_v6", wrapped)
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target), "--config", str(config)])

    assert cli.main() == 0
    assert calls == [8]


def _concurrency_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    workers: int,
    max_in_flight_chunks: int,
    segment_writer_limit: int,
    chunk_count: int,
) -> list[str]:
    """Drive _run_chunks with a fake pool and record submit/result ordering."""
    from concurrent.futures import Future

    from mrs3 import source_v6_importer as importer

    events: list[str] = []

    class _Receipt:
        def __init__(self, ordinal_start: int) -> None:
            self.ordinal_start = ordinal_start

    class _RecordingFuture(Future):
        def result(self, timeout: float | None = None) -> object:
            events.append("result")
            return super().result(timeout)

    class _RecordingExecutor:
        def __init__(self, max_workers: int, initializer=None, initargs: tuple = ()) -> None:
            self.max_workers = max_workers
            if initializer is not None:
                initializer(*initargs)

        def __enter__(self) -> "_RecordingExecutor":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def submit(self, _fn, group, *_args) -> Future:
            events.append("submit")
            future = _RecordingFuture()
            future.set_result(_Receipt(group[0].input_ordinal))
            return future

    monkeypatch.setattr(importer, "ProcessPoolExecutor", _RecordingExecutor)

    snapshots = tuple(
        importer.SourceV6Snapshot(
            input_ordinal=index,
            path=tmp_path / f"r{index}.html",
            relative_path=f"r{index}.html",
            source_size=1,
            source_mtime_ns=1,
        )
        for index in range(chunk_count)
    )
    importer._run_chunks(
        snapshots,
        tmp_path,
        "token",
        workers=workers,
        worker_chunk_size=1,
        max_in_flight_chunks=max_in_flight_chunks,
            segment_writer_limit=segment_writer_limit,
            cancellation_requested=None,
            worker_fn=None,
            progress_callback=None,
        )
    return events


def _peak_in_flight(events: list[str]) -> int:
    """Largest number of chunks submitted before any of them was collected."""
    peak = 0
    current = 0
    for event in events:
        if event == "submit":
            current += 1
            peak = max(peak, current)
        else:
            current -= 1
    return peak


def test_import_without_hydration_publishes_the_same_database(tmp_path: Path) -> None:
    """Skipping fact hydration must not change the published artifact."""
    from mrs3.source_v6_storage import database_info

    reports = _reports(tmp_path)
    hydrated = tmp_path / "hydrated.duckdb"
    lean = tmp_path / "lean.duckdb"

    full = import_source_v6(reports, hydrated, preflight=preflight_source_v6(reports, hydrated), workers=1)
    thin = import_source_v6(
        reports, lean, preflight=preflight_source_v6(reports, lean), workers=1, hydrate_fragments=False
    )

    assert full.status == thin.status == "COMMITTED"
    assert full.source_content_digest == thin.source_content_digest
    assert full.accepted_count == thin.accepted_count
    assert full.quarantined_count == thin.quarantined_count
    assert database_info(hydrated)["source_content_digest"] == database_info(lean)["source_content_digest"]
    assert tuple(item.fragment_id for item in thin.accepted_fragments) == tuple(
        item.fragment_id for item in full.accepted_fragments
    )
    assert tuple(item.fragment_id for item in thin.active_fragments) == tuple(
        item.fragment_id for item in full.active_fragments
    )
    assert full.fragments_hydrated is True
    assert thin.fragments_hydrated is False

    # Every published table must be row-for-row identical between the modes.
    import duckdb

    # origin_database_id is a fresh uuid per database, so it is selected out.
    queries = {
        "compact_fragments": "select * from compact_fragments",
        "points": "select * from points",
        "fragment_origins": "select fragment_id, source_sha256, source_name from fragment_origins",
        "day_ownership": "select * from day_ownership",
        "quarantine": "select fragment_id, source_sha256, reason from quarantine",
        "fact_ownership": "select * from fact_ownership",
        "fragment_resolutions": "select * from fragment_resolutions",
    }
    for table, query in queries.items():
        rows = []
        for database in (hydrated, lean):
            connection = duckdb.connect(str(database), read_only=True)
            try:
                rows.append(sorted(connection.execute(query).fetchall(), key=repr))
            finally:
                connection.close()
        assert rows[0] == rows[1], f"{table} differs between hydrated and lean import"


def test_surface_publication_refuses_unhydrated_fragments(tmp_path: Path) -> None:
    from mrs3.source_v6_storage import fragment_metadata
    from mrs3.source_v6_surface import SourceV6SurfaceError, publish_surface_db

    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    with pytest.raises(SourceV6SurfaceError, match="hydrated"):
        publish_surface_db(tmp_path / "surfaces", fragment_metadata(target))


def test_in_flight_chunks_are_not_capped_by_segment_writer_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small writer limit must not throttle the whole parse/encode pipeline."""
    events = _concurrency_events(
        monkeypatch,
        tmp_path,
        workers=8,
        max_in_flight_chunks=8,
        segment_writer_limit=2,
        chunk_count=32,
    )

    assert _peak_in_flight(events) == 8


def test_in_flight_chunks_respect_max_in_flight_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = _concurrency_events(
        monkeypatch,
        tmp_path,
        workers=8,
        max_in_flight_chunks=3,
        segment_writer_limit=2,
        chunk_count=32,
    )

    assert _peak_in_flight(events) == 3


def test_debian_cli_rejects_explicit_writer_limit_above_worker_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 2}, "source_v6_import": {"segment_writer_limit": 4}}), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target), "--config", str(config)])

    assert cli.main() == 1
    assert "segment_writer_limit must be at most workers" in capsys.readouterr().out


def test_debian_cli_clamps_default_writer_limit_for_partial_source_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 2}, "source_v6_import": {"write_batch_size": 8}}), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    captured: list[int] = []
    original = cli.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        captured.append(int(kwargs["segment_writer_limit"]))
        return original(root, database, **kwargs)

    monkeypatch.setattr(cli, "import_source_v6", wrapped)
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target), "--config", str(config)])
    assert cli.main() == 0
    assert captured == [2]


def test_debian_cli_routes_all_source_v6_import_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "duckdb_import": {"workers": 2},
        "source_v6_import": {
            "write_batch_size": 7,
            "worker_chunk_size": 8,
            "max_in_flight_chunks": 2,
            "segment_writer_limit": 2,
        },
    }), encoding="utf-8")
    cli, script_path = _load_debian_cli()
    calls: dict[str, int] = {}
    original = cli.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        for name in ("workers", "batch_size", "worker_chunk_size", "max_in_flight_chunks", "segment_writer_limit"):
            calls[name] = int(kwargs[name])
        return original(root, database, **kwargs)

    monkeypatch.setattr(cli, "import_source_v6", wrapped)
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target), "--config", str(config)])

    assert cli.main() == 0
    assert calls == {
        "workers": 2,
        "batch_size": 7,
        "worker_chunk_size": 8,
        "max_in_flight_chunks": 2,
        "segment_writer_limit": 2,
    }


def test_debian_cli_reports_structured_worker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    cli, script_path = _load_debian_cli()
    monkeypatch.setattr(cli, "import_source_v6", lambda *_args, **_kwargs: (_ for _ in ()).throw(SourceV6WorkerFailure(0, "source_v6_fixed_lot_overlap_a.html", "input disappeared", "read")))
    monkeypatch.setattr(sys, "argv", [str(script_path), str(reports), str(target)])

    assert cli.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAILED"
    assert payload["worker_failure"]["ordinal"] == 0
    assert payload["worker_failure"]["relative_path"] == "source_v6_fixed_lot_overlap_a.html"
    assert payload["worker_failure"]["preflight_size"] > 0
    assert payload["worker_failure"]["preflight_mtime_ns"]
    assert payload["worker_failure"]["reason"] == "input disappeared"


def test_supplied_preflight_does_not_recompute_full_input_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)
    import mrs3.source_v6_importer as importer
    calls: list[tuple[Path, Path]] = []
    original = importer.preflight_source_v6

    def unexpected(root: Path, database: Path) -> object:
        calls.append((root, database))
        return original(root, database)

    monkeypatch.setattr(importer, "preflight_source_v6", unexpected)
    import_source_v6(reports, target, preflight=preflight, workers=1)
    assert calls == []


def test_orphan_staging_is_recovered_before_new_import(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    orphan = tmp_path / f".{target.name}.orphan.staging"
    orphan.write_bytes(b"orphan from interrupted process")
    Path(f"{orphan}.wal").write_bytes(b"orphan wal")

    import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    assert target.exists()
    assert not orphan.exists()
    assert not Path(f"{orphan}.wal").exists()


def test_worker_failure_aborts_without_publishing_or_fabricating_quarantine(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)

    def fail_one(snapshot: object) -> object:
        if str(getattr(snapshot, "relative_path", "")).endswith("_b.html"):
            raise ValueError("injected worker failure")
        return prepare_source_v6_snapshot(snapshot)

    # The injected worker is intentionally supplied by the implementation as a
    # deterministic test seam; the normal path uses ProcessPoolExecutor.
    with pytest.raises(SourceV6ImportError, match="worker failure"):
        import_source_v6(
            reports,
            target,
            preflight=preflight,
            workers=1,
            worker_fn=fail_one,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".*.segments*"))


def test_preflight_is_metadata_only_and_assigns_binary_path_ordinals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "z.html").write_bytes(b"z")
    nested = reports / "nested"
    nested.mkdir()
    (nested / "a.html").write_bytes(b"a")
    reads: list[Path] = []
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or b"unexpected")

    preflight = preflight_source_v6(reports, tmp_path / "source-v6.duckdb")

    assert reads == []
    assert [snapshot.input_ordinal for snapshot in preflight.snapshots] == [0, 1]
    assert [snapshot.relative_path for snapshot in preflight.snapshots] == ["nested/a.html", "z.html"]
    assert not hasattr(preflight.snapshots[0], "source_sha256")


def test_preflight_excludes_report_optimizer_html(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "trade.html").write_bytes(b"trade")
    (reports / "report_optimizer_my_test_auto_x.html").write_bytes(b"optimizer")

    preflight = preflight_source_v6(reports, tmp_path / "source-v6.duckdb")

    assert [snapshot.relative_path for snapshot in preflight.snapshots] == ["trade.html"]


def test_worker_reads_each_snapshot_once_and_hashes_the_read_bytes(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    preflight = preflight_source_v6(reports, tmp_path / "source-v6.duckdb")
    snapshot = preflight.snapshots[0]
    source = snapshot.path.read_bytes()
    calls = 0
    original = Path.read_bytes

    def read_once(path: Path) -> bytes:
        nonlocal calls
        calls += 1
        return original(path)

    # The helper must perform its one read itself; pre-reading above is outside
    # the worker and does not count.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(Path, "read_bytes", read_once)
        prepared = prepare_source_v6_snapshot(snapshot)
    finally:
        monkeypatch.undo()
    assert calls == 1
    assert prepared.fragment.source_sha256 == sha256(source).hexdigest()


def test_parse_failure_is_quarantined_with_real_sha(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    bad = reports / "bad.html"
    bad.write_bytes(b"not a valid source-v6 report")
    target = tmp_path / "source-v6.duckdb"

    result = import_source_v6(reports, target, preflight=preflight_source_v6(reports, target), workers=1)

    assert result.status == "COMMITTED"
    assert result.quarantined_count == 1
    assert result.quarantine_reasons
    assert target.exists()


def test_structured_worker_failure_has_no_sha(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    preflight = preflight_source_v6(reports, tmp_path / "source-v6.duckdb")

    def fail_one(snapshot: object) -> object:
        raise SourceV6WorkerFailure(getattr(snapshot, "input_ordinal"), getattr(snapshot, "relative_path"), "read_failed")

    with pytest.raises(SourceV6ImportError, match="read_failed"):
        import_source_v6(reports, tmp_path / "source-v6.duckdb", preflight=preflight, worker_fn=fail_one, workers=1)


def test_chunk_size_and_completion_order_do_not_change_source_digest(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    first = tmp_path / "first.duckdb"
    second = tmp_path / "second.duckdb"
    first_result = import_source_v6(
        reports,
        first,
        preflight=preflight_source_v6(reports, first),
        workers=1,
        worker_chunk_size=1,
    )
    second_result = import_source_v6(
        reports,
        second,
        preflight=preflight_source_v6(reports, second),
        workers=1,
        worker_chunk_size=64,
    )

    assert first_result.source_content_digest == second_result.source_content_digest
    assert tuple(item.fragment_id for item in first_result.accepted_fragments) == tuple(item.fragment_id for item in second_result.accepted_fragments)
    assert first_result.batch_sizes == (1, 1)
    assert second_result.batch_sizes == (2,)


def test_publication_restat_rejects_target_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)
    import mrs3.source_v6_importer as importer
    original = importer._assert_preflight_current
    calls = 0

    def restat(current: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_bytes(b"appeared after preflight")
        original(current)

    monkeypatch.setattr(importer, "_assert_preflight_current", restat)
    with pytest.raises(SourceV6ImportError, match="target changed"):
        import_source_v6(reports, target, preflight=preflight, workers=1)
    assert target.read_bytes() == b"appeared after preflight"
    assert not list(tmp_path.glob(".*.segments*"))


def test_cancellation_after_a_chunk_cleans_segments_and_target(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    calls = 0

    def worker(snapshot: object) -> object:
        nonlocal calls
        calls += 1
        return prepare_source_v6_snapshot(snapshot)

    def cancelled() -> bool:
        return calls >= 1

    with pytest.raises(SourceV6ImportCancelled, match="cancelled"):
        import_source_v6(
            reports,
            target,
            preflight=preflight_source_v6(reports, target),
            workers=1,
            worker_chunk_size=1,
            worker_fn=worker,
            cancellation_requested=cancelled,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".*.segments*"))
