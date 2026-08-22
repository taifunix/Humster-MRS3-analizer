from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tarfile


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "performance"


def test_source_v6_debian_shell_runner_prefers_its_virtualenv() -> None:
    runner = (ROOT / "scripts" / "import-source-v6-debian.sh").read_text(encoding="utf-8")

    assert 'PYTHON="$ROOT/.venv/bin/python"' in runner
    assert 'exec "$PYTHON" "$SCRIPT_DIR/import_source_v6_debian.py" "$@"' in runner


def test_source_v6_debian_runner_fresh_import_and_stitch_input(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports = reports / "nested"
    reports.mkdir(parents=True)
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (reports / name).write_bytes((FIXTURES / name).read_bytes())
    database = tmp_path / "source-v6.duckdb"
    handoff = tmp_path / "handoff.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_source_v6_debian.py"), str(reports.parent), str(database), "--handoff-manifest", str(handoff)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"coverage":{"cells":' in result.stdout
    assert '"status":"COMMITTED"' in result.stdout
    assert database.stat().st_size > 0
    assert handoff.is_file()
    stdout_document = json.loads(result.stdout)
    handoff_document = json.loads(handoff.read_text(encoding="utf-8"))
    assert handoff_document == stdout_document
    assert all(receipt["safe_to_delete"] == "YES" for receipt in handoff_document["reports"])
    assert isinstance(handoff_document["coverage"]["canonical_ready_intervals"], list)


def test_source_v6_end_to_end_import_stitch_coverage_surface_analysis(tmp_path: Path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_analysis import export_plateau_report
    from mrs3.source_v6_coverage import coverage_cells
    from mrs3.source_v6_storage import create_v6_database, import_fragment, preflight_import, reconstruct_fragment
    from mrs3.source_v6_stitch import calculate_metrics
    from mrs3.source_v6_surface import publish_surface_db

    first = normalize_source_v6((FIXTURES / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    second = normalize_source_v6((FIXTURES / "source_v6_fixed_lot_overlap_b.html").read_bytes())
    database = tmp_path / "source-v6.duckdb"
    create_v6_database(database)
    for fragment in (first, second):
        import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
    restored = tuple(reconstruct_fragment(database, fragment.fragment_id) for fragment in (first, second))
    metrics = calculate_metrics(restored)
    assert metrics.total_pnl == metrics.balance_series[-1].value - metrics.balance_series[0].value
    assert coverage_cells(restored)
    surface = publish_surface_db(tmp_path / "surfaces", restored)
    report = export_plateau_report(surface, tmp_path / "plateau-report")
    assert report["surface_id"]


def test_generated_source_v6_bundle_runs_full_workflow(tmp_path: Path) -> None:
    archive = tmp_path / "source-v6-importer.tar.gz"
    build = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build-source-v6-debian-bundle.py"), str(archive)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    unpack = tmp_path / "unpacked"
    unpack.mkdir()
    with tarfile.open(archive, "r:gz") as handle:
        assert "source-v6-importer/mrs3/source_v6_merge.py" in handle.getnames()
        handle.extractall(unpack)
    bundle = unpack / "source-v6-importer"
    reports = bundle / "reports"
    reports.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (reports / name).write_bytes((FIXTURES / name).read_bytes())
    database = bundle / "source-v6.duckdb"
    surfaces = bundle / "surfaces"
    exports = bundle / "exports"
    runner = bundle / "scripts" / "import_source_v6_debian.py"
    result = subprocess.run(
        [sys.executable, str(runner), "reports", "source-v6.duckdb", "--surface-dir", "surfaces", "--export-dir", "exports"],
        cwd=bundle, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert database.exists() and list(surfaces.glob("*.duckdb"))
    assert (exports / "plateau_report.xlsx").exists()
