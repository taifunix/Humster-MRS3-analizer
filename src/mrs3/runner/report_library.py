from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from .config import RunnerConfig
from .results import WizardResult, reconcile_results


@dataclass(frozen=True)
class LibraryEntry:
    strategy_name: str
    target_path: Path
    source_path: Path
    source_sha256: str
    safe_to_delete: bool
    status: str


@dataclass(frozen=True)
class ReportLibraryAudit:
    entries: tuple[LibraryEntry, ...]
    accepted_count: int
    duplicate_count: int
    conflict_count: int
    quarantine_count: int
    manifest_path: Path | None


def _manifest_entry(entry: LibraryEntry) -> dict[str, object]:
    return {
        "strategy_name": entry.strategy_name,
        "target_path": str(entry.target_path),
        "source_path": str(entry.source_path),
        "source_sha256": entry.source_sha256,
        "safe_to_delete": entry.safe_to_delete,
        "status": entry.status,
    }


def publish_verified_reports(
    config: RunnerConfig,
    expected_names: Sequence[str],
    results: Sequence[WizardResult],
    report_paths: Mapping[str, Path],
    symbol: str,
    *,
    apply: bool = False,
) -> ReportLibraryAudit:
    """Publish only reports that pass the existing complete reconciliation."""
    reconcile_results(
        tuple(expected_names),
        tuple(results),
        config.report_dir,
        config.metric_tolerance,
        report_paths=dict(report_paths),
    )
    library_dir = config.report_dir.parent / f"{symbol}_reports"
    entries: list[LibraryEntry] = []
    accepted = duplicates = conflicts = 0
    for name in expected_names:
        source = report_paths[name]
        source_bytes = source.read_bytes()
        source_hash = sha256(source_bytes).hexdigest()
        target = library_dir / f"{name}.html"
        if target.is_file():
            if target.read_bytes() == source_bytes:
                duplicates += 1
                if apply:
                    source.unlink()
                entries.append(LibraryEntry(name, target, source, source_hash, True, "DUPLICATE"))
            else:
                conflicts += 1
                entries.append(LibraryEntry(name, target, source, source_hash, False, "CONFLICT"))
            continue
        if apply:
            library_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(source_bytes)
            temporary.replace(target)
        accepted += 1
        entries.append(LibraryEntry(name, target, source, source_hash, apply, "ACCEPTED"))

    manifest_path = library_dir / "report_library_manifest.json" if apply else None
    if manifest_path is not None:
        manifest_path.write_text(
            json.dumps(
                {"symbol": symbol, "entries": [_manifest_entry(entry) for entry in entries]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return ReportLibraryAudit(
        tuple(entries), accepted, duplicates, conflicts, 0, manifest_path
    )
