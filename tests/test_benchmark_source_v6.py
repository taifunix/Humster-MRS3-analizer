from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys

import duckdb

from mrs3.source_v6_importer import import_source_v6, preflight_source_v6
from mrs3.source_v6_storage import database_info, iter_fragments


FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def _load_benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark-source-v6.py"
    spec = importlib.util.spec_from_file_location("benchmark_source_v6", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (root / name).write_bytes((FIXTURES / name).read_bytes())
    return root


def test_semantic_signature_ignores_database_identity_and_volatile_audit_fields(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    reports = _reports(tmp_path)
    first = tmp_path / "first.duckdb"
    second = tmp_path / "second.duckdb"
    import_source_v6(reports, first, preflight=preflight_source_v6(reports, first), workers=1)
    import_source_v6(reports, second, preflight=preflight_source_v6(reports, second), workers=1)

    assert benchmark.source_v6_semantic_signature(first) == benchmark.source_v6_semantic_signature(second)
    assert tuple(item.fragment_id for item in iter_fragments(first)) == tuple(item.fragment_id for item in iter_fragments(second))
    assert database_info(first)["database_id"] != database_info(second)["database_id"]

    renamed_reports = tmp_path / "renamed-reports"
    renamed_reports.mkdir()
    for source, name in zip(sorted(reports.glob("*.html")), ("renamed-a.html", "renamed-b.html")):
        (renamed_reports / name).write_bytes(source.read_bytes())
    renamed = tmp_path / "renamed.duckdb"
    import_source_v6(renamed_reports, renamed, preflight=preflight_source_v6(renamed_reports, renamed), workers=1)
    assert tuple(item.fragment_id for item in iter_fragments(first)) == tuple(item.fragment_id for item in iter_fragments(renamed))

    perturbed = tmp_path / "perturbed.duckdb"
    shutil.copyfile(first, perturbed)
    connection = duckdb.connect(str(perturbed))
    try:
        connection.execute("update schema_info set value = 'perturbed-database-id' where key = 'database_id'")
        connection.execute("checkpoint")
    finally:
        connection.close()
    assert benchmark.source_v6_semantic_signature(first) == benchmark.source_v6_semantic_signature(perturbed)

    changed = tmp_path / "changed.duckdb"
    shutil.copyfile(first, changed)
    connection = duckdb.connect(str(changed))
    try:
        connection.execute("update schema_info set value = 'changed-fingerprint' where key = 'fingerprint'")
        connection.execute("checkpoint")
    finally:
        connection.close()
    assert benchmark.source_v6_semantic_signature(first) != benchmark.source_v6_semantic_signature(changed)


def test_benchmark_emits_reproducible_evidence_and_semantic_match(tmp_path: Path, monkeypatch, capsys) -> None:
    benchmark = _load_benchmark()
    reports = _reports(tmp_path)
    expected = tmp_path / "expected.duckdb"
    import_source_v6(reports, expected, preflight=preflight_source_v6(reports, expected), workers=1)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark-source-v6.py",
            str(reports),
            "--compare-database",
            str(expected),
            "--workers",
            "1",
            "--repeat",
            "1",
        ],
    )

    assert benchmark.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "COMMITTED"
    assert payload["semantic_match"] is True
    assert payload["runs"] == 1
    assert payload["accepted_count"] == 2
    assert payload["quarantined_count"] == 0
    assert payload["raw_input_bytes"] > 0
    assert payload["source_content_digest"]
    assert payload["semantic_signature"]
    assert payload["elapsed_seconds"] > 0
    assert payload["cpu_seconds"] >= 0
    assert payload["cpu_scope"] in {"process+children", "orchestrator-only-windows"}
    assert payload["database_bytes"] > 0


def _run_metrics(*, elapsed: float, cpu: float, match: bool) -> dict[str, object]:
    return {
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu,
        "database_bytes": 10,
        "report_count": 1,
        "raw_input_bytes": 20,
        "accepted_count": 1,
        "quarantined_count": 0,
        "source_content_digest": "digest",
        "semantic_signature": "signature",
        "semantic_match": match,
    }


def test_benchmark_uses_median_for_three_repeats(tmp_path: Path, monkeypatch, capsys) -> None:
    benchmark = _load_benchmark()
    samples = iter((_run_metrics(elapsed=3.0, cpu=30.0, match=True), _run_metrics(elapsed=1.0, cpu=10.0, match=True), _run_metrics(elapsed=2.0, cpu=20.0, match=True)))
    monkeypatch.setattr(benchmark, "_run_once", lambda *_args: next(samples))
    monkeypatch.setattr(sys, "argv", ["benchmark-source-v6.py", str(tmp_path), "--workers", "1", "--repeat", "3"])

    assert benchmark.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"] == 3
    assert payload["elapsed_seconds"] == 2.0
    assert payload["cpu_seconds"] == 20.0


def test_benchmark_semantic_mismatch_is_a_failed_result(tmp_path: Path, monkeypatch, capsys) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(benchmark, "_run_once", lambda *_args: _run_metrics(elapsed=1.0, cpu=1.0, match=False))
    monkeypatch.setattr(sys, "argv", ["benchmark-source-v6.py", str(tmp_path), "--workers", "1"])

    assert benchmark.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SEMANTIC_MISMATCH"
    assert payload["semantic_match"] is False


def test_benchmark_invalid_input_is_failed_json(tmp_path: Path, monkeypatch, capsys) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setattr(sys, "argv", ["benchmark-source-v6.py", str(tmp_path / "missing-reports"), "--workers", "1"])

    assert benchmark.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAILED"
    assert "does not exist" in payload["error"]


def test_benchmark_cpu_clock_accounts_for_children_when_supported(monkeypatch) -> None:
    benchmark = _load_benchmark()
    if benchmark.resource is None:
        return
    values = iter((
        type("Usage", (), {"ru_utime": 1.0, "ru_stime": 0.5})(),
        type("Usage", (), {"ru_utime": 2.0, "ru_stime": 1.5})(),
        type("Usage", (), {"ru_utime": 1.25, "ru_stime": 0.75})(),
        type("Usage", (), {"ru_utime": 2.25, "ru_stime": 1.75})(),
    ))
    monkeypatch.setattr(benchmark.resource, "getrusage", lambda _kind: next(values))
    assert benchmark._cpu_clock() == 5.0
    assert benchmark._cpu_clock() == 6.0
