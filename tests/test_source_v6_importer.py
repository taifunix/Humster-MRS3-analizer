from __future__ import annotations

import json
from pathlib import Path
import importlib.util
import sys

import pytest

from mrs3.locking import OutputDirectoryBusyError
from mrs3.source_v6_importer import (
    SourceV6ImportCancelled,
    SourceV6ImportError,
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


def test_debian_cli_routes_writes_through_shared_importer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_worker_failure_is_quarantined_without_publishing_bad_input(tmp_path: Path) -> None:
    reports = _reports(tmp_path)
    target = tmp_path / "source-v6.duckdb"
    preflight = preflight_source_v6(reports, target)

    def fail_one(snapshot: object) -> object:
        if str(getattr(snapshot, "relative_path", "")).endswith("_b.html"):
            raise ValueError("injected worker failure")
        return prepare_source_v6_snapshot(snapshot)

    # The injected worker is intentionally supplied by the implementation as a
    # deterministic test seam; the normal path uses ProcessPoolExecutor.
    result = import_source_v6(
        reports,
        target,
        preflight=preflight,
        workers=1,
        worker_fn=fail_one,
    )
    assert result.quarantined_count == 1
    assert target.exists()
