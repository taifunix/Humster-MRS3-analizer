from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]
BUILDER_PATH = ROOT / "scripts" / "build-debian-import-bundle.py"
IMPORT_SOURCE_FILES = {
    Path("src/mrs3/__init__.py"),
    Path("src/mrs3/config.py"),
    Path("src/mrs3/duckdb_import.py"),
    Path("src/mrs3/duckdb_events.py"),
    Path("src/mrs3/duckdb_source_schema.py"),
    Path("src/mrs3/source_packs.py"),
    Path("src/mrs3/locking.py"),
    Path("src/mrs3/models.py"),
}
CODEC_RELATIVE = Path("programs") / "Обработчик HTML-DuckDB" / "mrs3_html_compact_importer_v3.py"


def _builder_module():
    spec = importlib.util.spec_from_file_location("build_debian_import_bundle", BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(tmp_path: Path) -> Path:
    builder = _builder_module()
    destination = tmp_path / "debian-duckdb-importer"
    assert builder.build_bundle(destination, repo_root=ROOT) == destination
    return destination


def test_builder_contains_only_transfer_runtime(tmp_path: Path) -> None:
    bundle = _build(tmp_path)

    required = {
        Path("README.md"),
        Path("IMPORT_INSTRUCTIONS.md"),
        Path("requirements.txt"),
        Path("config.local.json"),
        Path("scripts/import-html-duckdb-debian.sh"),
        Path("scripts/import_html_duckdb_debian.py"),
        *IMPORT_SOURCE_FILES,
        CODEC_RELATIVE,
        Path("data/html/.gitkeep"),
    }
    actual = {
        path.relative_to(bundle)
        for path in bundle.rglob("*")
        if path.is_file()
    }
    assert actual == required

    forbidden_names = {
        "__pycache__",
        ".venv",
        "config.example.json",
        "start_panel.bat",
        "mrs3_html_parallel_compact_importer_v4.py",
        "panel.py",
        "analysis_storage.py",
        "duckdb_direct.py",
        "selection.py",
        "pipeline.py",
        "strategy_json.py",
        "published_surface.py",
        "runner",
    }
    assert not any(part in forbidden_names for path in bundle.rglob("*") for part in path.parts)
    assert not list(bundle.rglob("*.pyc"))
    assert not list(bundle.rglob("*.duckdb"))
    assert not list(bundle.rglob("*.html"))
    assert not (bundle / "tests").exists()
    assert "IMPORT_INSTRUCTIONS.md" in (bundle / "README.md").read_text(encoding="utf-8")

    requirements = (bundle / "requirements.txt").read_text(encoding="utf-8")
    assert requirements == "duckdb>=1.5,<2\npandas>=2.2,<3\nlxml>=5,<7\n"

    instructions = (bundle / "IMPORT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    for phrase in (
        "python3 -m venv .venv",
        "chmod +x scripts/import-html-duckdb-debian.sh",
        '"event":"summary"',
        "safe_to_delete=YES",
        "No space left on device",
        "surface materialization",
        "coverage filtering",
    ):
        assert phrase in instructions


def test_bundle_config_is_relative_and_carries_canonical_contract(tmp_path: Path) -> None:
    bundle = _build(tmp_path)
    config = json.loads((bundle / "config.local.json").read_text(encoding="utf-8"))

    importer = config["duckdb_import"]
    assert importer == {
        "source_duckdb_path": "data/mrs3_source_v5.duckdb",
        "analysis_duckdb_path": None,
        "default_html_root": "data/html",
        "audit_root": "data/import_audit",
        "workers": 4,
        "transaction_batch_size": 250,
    }
    assert config["canonical_shifts_bp"] == [
        30, 40, 50, 60, 70, 90, 110, 140, 170, 200,
        230, 270, 310, 350, 390, 430, 470, 510, 550,
    ]
    assert config["shift_domain"] == {"min_bp": 30, "max_bp": 550}
    assert config["direct_materialization"] == {
        "workers": 15,
        "fetch_batch_size": 256,
        "worker_chunk_size": 16,
        "max_in_flight_chunks": 30,
    }
    assert config["close_support"] == {"core_min": 0.9, "supported_min": 0.6}
    assert config["canonical_metadata_note"] == (
        "Recorded metadata only; raw HTML import does not perform surface "
        "materialization or coverage filtering."
    )
    for value in (
        importer["source_duckdb_path"],
        importer["default_html_root"],
        importer["audit_root"],
    ):
        assert not Path(value).is_absolute()


def test_bundled_runner_help_uses_current_interpreter(tmp_path: Path) -> None:
    bundle = _build(tmp_path)
    result = subprocess.run(
        [sys.executable, str(bundle / "scripts/import_html_duckdb_debian.py"), "--help"],
        cwd=bundle,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--html-root" in result.stdout


def test_builder_replaces_explicit_destination_idempotently(tmp_path: Path) -> None:
    builder = _builder_module()
    destination = tmp_path / "debian-duckdb-importer"
    builder.build_bundle(destination, repo_root=ROOT)
    (destination / "unrelated.txt").write_text("old bundle", encoding="utf-8")
    builder.build_bundle(destination, repo_root=ROOT)
    assert not (destination / "unrelated.txt").exists()
    assert not list(tmp_path.glob(f".{destination.name}.tmp-*"))
