#!/usr/bin/env python3
"""Build the minimal Source v6 Debian importer bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import tarfile


REQUIREMENTS = "duckdb==1.5.5\npandas>=2.2,<3\nopenpyxl>=3.1,<4\nlxml>=5,<7\n"
FILES = (
    "scripts/import_source_v6_debian.py",
    "scripts/import-source-v6-debian.sh",
    "src/mrs3/__init__.py",
    "src/mrs3/performance.py",
    "src/mrs3/source_v6.py",
    "src/mrs3/source_v6_importer.py",
    "src/mrs3/source_v6_merge.py",
    "src/mrs3/source_v6_storage.py",
    "src/mrs3/locking.py",
    "src/mrs3/source_v6_stitch.py",
    "src/mrs3/source_v6_coverage.py",
    "src/mrs3/source_v6_surface.py",
    "src/mrs3/source_v6_analysis.py",
    "src/mrs3/models.py",
    "src/mrs3/config.py",
    "src/mrs3/eligibility.py",
    "src/mrs3/duckdb_direct.py",
    "src/mrs3/duckdb_events.py",
    "src/mrs3/duckdb_source_schema.py",
    "src/mrs3/source_packs.py",
    "src/mrs3/analysis_storage.py",
    "src/mrs3/selection.py",
    "src/mrs3/refine.py",
    "src/mrs3/plateau.py",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    staging = args.output.with_suffix("")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for relative in FILES:
        source = root / relative
        target = staging / relative.replace("src/", "")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (staging / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (staging / "README.md").write_text(
        "# Source v6 Debian importer\n\n"
        "Create a fresh database and import reports with `./import-source-v6-debian.sh HTML_DIR source-v6.duckdb`.\n"
        "The runtime uses relative paths, retains no HTML bytes, and emits safe_to_delete=YES only after transactional readback.\n",
        encoding="utf-8",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        archive.add(staging, arcname="source-v6-importer")
    shutil.rmtree(staging)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
