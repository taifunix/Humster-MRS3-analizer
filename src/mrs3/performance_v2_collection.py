"""Build immutable, explicit collections of heterogeneous SINGLE_MODE inboxes."""

from __future__ import annotations

from hashlib import sha256
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Sequence

from .performance_v2_input import PerformanceV2InputError, read_performance_v2_inbox


_COLLECTION_SCHEMA_VERSION = 2
_COLLECTION_MANIFEST_VERSION = 1
_COLLECTION_RUN_MODE = "SINGLE_MODE_COLLECTION"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_collection_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in {".", ".."}:
        raise PerformanceV2InputError("collection ID is unsafe")
    path = Path(value)
    if (
        path.name != value
        or path.is_absolute()
        or ":" in value
        or "/" in value
        or "\\" in value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise PerformanceV2InputError("collection ID is unsafe")
    return value


def _manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads((path / "inbox_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PerformanceV2InputError("member inbox manifest is unavailable") from error
    if not isinstance(value, dict):
        raise PerformanceV2InputError("member inbox manifest is invalid")
    return value


def _candidate_diagnostic(manifest: dict[str, object], candidate: str) -> object:
    provenance = manifest.get("v6_provenance")
    if not isinstance(provenance, dict):
        provenance = manifest
    diagnostics = provenance.get("candidate_diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get(candidate), dict):
        raise PerformanceV2InputError("member candidate diagnostics are missing")
    return diagnostics[candidate]


def _entry_diagnostics(prepared_entry: object, manifest: dict[str, object]) -> object:
    candidate = getattr(prepared_entry, "candidate_identity", None)
    if not isinstance(candidate, str) or not candidate:
        raise PerformanceV2InputError("member candidate identity is missing")
    return _candidate_diagnostic(manifest, candidate)


def _claim_collection(root: Path, collection_id: str) -> Path:
    """Claim one collection ID with an exclusive directory create."""
    claim = root / f".{collection_id}.claim"
    try:
        claim.mkdir()
    except FileExistsError as error:
        raise PerformanceV2InputError("collection publication claim is busy") from error
    except OSError as error:
        raise PerformanceV2InputError("could not claim collection publication") from error
    return claim


def _rename_noreplace_linux(source: Path, target: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE), failing closed if unavailable."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as error:
        raise PerformanceV2InputError("atomic no-replace publication is unavailable") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100, os.fsencode(source), -100, os.fsencode(target), 1  # AT_FDCWD, RENAME_NOREPLACE
    )
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise PerformanceV2InputError("collection inbox already exists")
    if code in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
        raise PerformanceV2InputError("atomic no-replace publication is unavailable")
    raise PerformanceV2InputError("atomic no-replace publication failed") from OSError(code, os.strerror(code))


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename a complete directory without replacing a target."""
    source = Path(source)
    target = Path(target)
    if sys.platform.startswith("linux"):
        _rename_noreplace_linux(source, target)
        return
    if sys.platform == "win32" and os.name == "nt":
        # On Windows os.rename maps to MoveFileEx without
        # MOVEFILE_REPLACE_EXISTING; an existing destination raises.
        try:
            os.rename(source, target)
        except FileExistsError as error:
            raise PerformanceV2InputError("collection inbox already exists") from error
        except OSError as error:
            raise PerformanceV2InputError("atomic no-replace publication failed") from error
        return
    raise PerformanceV2InputError("atomic no-replace publication is unavailable")


def build_single_mode_collection_inbox(
    inbox_root: Path,
    collection_id: str,
    member_inboxes: Sequence[Path],
    *,
    report_root: Path,
    trusted_strategy_root: Path,
) -> Path:
    """Validate and atomically publish one explicit collection manifest.

    The resulting directory contains metadata only.  Strategy JSON and HTML
    remain at their original trusted paths and are re-hashed by the reader.
    """
    collection_id = _safe_collection_id(collection_id)
    if not member_inboxes or isinstance(member_inboxes, (str, bytes)):
        raise PerformanceV2InputError("collection must contain at least one member")
    root = Path(inbox_root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PerformanceV2InputError("collection inbox root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    claim = _claim_collection(root, collection_id)

    seen_members: set[Path] = set()
    seen_names: set[str] = set()
    seen_reports: set[str] = set()
    entries: list[dict[str, object]] = []
    members: list[dict[str, object]] = []
    try:
        target = root / collection_id
        if target.exists():
            raise PerformanceV2InputError("collection inbox already exists")
        for raw_member in member_inboxes:
            member = Path(raw_member)
            if member.is_symlink() or not member.is_dir():
                raise PerformanceV2InputError("member inbox is not a real directory")
            member = member.resolve()
            if member in seen_members:
                raise PerformanceV2InputError("duplicate member inbox path")
            seen_members.add(member)
            prepared = read_performance_v2_inbox(
                member, report_root, strategy_root=trusted_strategy_root
            )
            if prepared.run_mode != "SINGLE_MODE":
                raise PerformanceV2InputError("collection members must be SINGLE_MODE inboxes")
            manifest = _manifest(member)
            raw_entries = manifest.get("entries")
            if not isinstance(raw_entries, list):
                raise PerformanceV2InputError("member inbox entries are missing")
            raw_by_name = {
                item.get("strategy_name"): item
                for item in raw_entries
                if isinstance(item, dict) and isinstance(item.get("strategy_name"), str)
            }
            if len(raw_by_name) != len(raw_entries):
                raise PerformanceV2InputError("member inbox entries are malformed")
            member_entry_names: list[str] = []
            for prepared_entry in prepared.entries:
                name = prepared_entry.strategy_name
                try:
                    current_report_hash = sha256(prepared_entry.report_path.read_bytes()).hexdigest()
                except OSError as error:
                    raise PerformanceV2InputError("member report artifact is unavailable") from error
                if current_report_hash != prepared_entry.report_sha256:
                    raise PerformanceV2InputError("member report artifact hash mismatch")
                name_key = name.casefold()
                if name_key in seen_names:
                    raise PerformanceV2InputError("duplicate strategy name in collection")
                report_key = prepared_entry.report_path.name.casefold()
                if report_key in seen_reports:
                    raise PerformanceV2InputError("duplicate report basename in collection")
                raw_entry = raw_by_name.get(name)
                if raw_entry is None:
                    raise PerformanceV2InputError("member entry is missing from manifest")
                seen_names.add(name_key)
                seen_reports.add(report_key)
                member_entry_names.append(name)
                entry = dict(raw_entry)
                entry.update({
                    "test_start": prepared_entry.test_start,
                    "test_end": prepared_entry.test_end,
                    "tester_config_sha256": prepared_entry.tester_config_sha256,
                    "commission_contract": dict(prepared_entry.commission_contract),
                    "commission_contract_id": prepared_entry.commission_contract_id,
                    "analysis_run_id": prepared_entry.analysis_run_id,
                    "candidate_identity": prepared_entry.candidate_identity,
                    "order_plateau_diagnostics": _entry_diagnostics(prepared_entry, manifest),
                    "member_inbox_path": str(member),
                    "member_manifest_sha256": prepared.manifest_sha256,
                })
                entries.append(entry)
            members.append({
                "inbox_path": str(member),
                "manifest_sha256": prepared.manifest_sha256,
                "snapshot_sha256": prepared.inbox_snapshot_sha256,
                "batch_id": manifest.get("batch_id"),
                "strategy_names": member_entry_names,
            })
        digest_payload = {"members": members, "entries": entries}
        collection_digest = sha256(_canonical_json(digest_payload)).hexdigest()
        document = {
            "schema_version": _COLLECTION_SCHEMA_VERSION,
            "collection_manifest_version": _COLLECTION_MANIFEST_VERSION,
            "collection_id": collection_id,
            "collection_digest": collection_digest,
            "run_mode": _COLLECTION_RUN_MODE,
            "source_mode": "metadata_only",
            "expected_strategy_names": [entry["strategy_name"] for entry in entries],
            "members": members,
            "entries": entries,
        }
        staging = Path(tempfile.mkdtemp(prefix=f".{collection_id}.", dir=root))
        (staging / "inbox_manifest.json").write_bytes(_canonical_json(document))
        _rename_noreplace(staging, target)
        return target
    except BaseException:
        # A failed validation must never leave a partially published inbox.
        if "staging" in locals() and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        try:
            claim.rmdir()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise PerformanceV2InputError("could not release collection publication claim") from error


__all__ = ["build_single_mode_collection_inbox"]
