"""Safe, local-only settings exposed by the static control panel."""

from __future__ import annotations

from collections.abc import Mapping
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
from typing import Any

from .runner.config import RunnerConfig


INVALID_SETTINGS = "invalid settings"
SETTINGS_UNAVAILABLE = "settings unavailable"
_DEFAULT_ROOTS = frozenset({"static", "legacy"})
_PATH_KEYS = frozenset(
    {
        "local_bot_root",
        "local_runner_root",
        "local_working_root",
        "local_work_root",
        "local_reports_root",
        "local_report_root",
        "local_reports_archive_root",
        "local_archive_root",
        "local_output_root",
        "local_source_db_root",
        "local_surface_root",
        "local_analysis_db_root",
        "remote_bot_root",
        "remote_runner_root",
        "remote_working_root",
        "remote_work_root",
        "remote_reports_root",
        "remote_report_root",
        "remote_reports_archive_root",
        "remote_archive_root",
        "remote_output_root",
        "remote_source_db_root",
        "remote_surface_root",
        "remote_analysis_db_root",
        "source_db_root",
        "surface_root",
        "analysis_db_root",
        "output_root",
        "listing_dates_path",
        "algorithm_config_path",
        "strategy_template_path",
        "runner_root",
        "reports_root",
        "reports_archive_root",
    }
)
_PATH_SEPARATOR = re.compile(r"[\\/]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_CONFIG_PATH_FIELDS = {
    "bot_root": "bot_root",
    "debian_runner_root": "runner_root",
    "runner_root": "runner_root",
    "working_root": "runner_root",
    "work_root": "runner_root",
    "working_dir": "runner_root",
    "work_dir": "runner_root",
    "strategy_dir": "runner_root",
    "report_root": "reports_root",
    "reports_root": "reports_root",
    "report_dir": "reports_root",
    "reports_dir": "reports_root",
    "reports_archive_root": "reports_archive_root",
    "inbox_root": "output_root",
    "output_dir": "output_root",
    "output_root": "output_root",
    "source_db_root": "source_db_root",
    "source_db_dir": "source_db_root",
    "source_duckdb_path": "source_db_root",
    "surface_root": "surface_root",
    "surface_dir": "surface_root",
    "source_v6_surface_dir": "surface_root",
    "analysis_db_root": "analysis_db_root",
    "analysis_db_dir": "analysis_db_root",
    "analysis_duckdb_path": "analysis_db_root",
    "listing_dates_path": "listing_dates_path",
    "audit_root": "output_root",
    "algorithm_config_path": "algorithm_config_path",
    "strategy_template_path": "strategy_template_path",
}
_OPERATIONAL_KEYS = frozenset({
    "local_bot_root", "remote_runner_root", "source_db_path", "output_root",
    "listing_dates_path", "algorithm_version", "import_workers", "transaction_batch_size",
})


class PanelSettingsError(ValueError):
    """A client-safe settings error with no local diagnostic details."""


def _error() -> PanelSettingsError:
    return PanelSettingsError(INVALID_SETTINGS)


def _read_document(path: Path, *, missing_ok: bool) -> tuple[dict[str, Any], str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return {}, None
        raise PanelSettingsError(SETTINGS_UNAVAILABLE) from None
    except (OSError, UnicodeError):
        raise PanelSettingsError(SETTINGS_UNAVAILABLE) from None
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raise PanelSettingsError(SETTINGS_UNAVAILABLE) from None
    if not isinstance(document, dict):
        raise PanelSettingsError(SETTINGS_UNAVAILABLE)
    return document, text


def _panel_document(document: Mapping[str, Any]) -> dict[str, Any]:
    panel = document.get("panel", {})
    if panel is None:
        return {}
    if not isinstance(panel, dict):
        raise _error()
    return dict(panel)


def _section_path_defaults(section: object, prefix: str) -> dict[str, str]:
    if not isinstance(section, Mapping):
        return {}
    paths: dict[str, str] = {}
    sources = [section]
    nested = section.get("paths")
    if isinstance(nested, Mapping):
        sources.append(nested)
    for source in sources:
        for source_key, target_key in _CONFIG_PATH_FIELDS.items():
            value = source.get(source_key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                paths[f"{prefix}_{target_key}"] = _safe_path(value)
            except PanelSettingsError:
                continue
    return paths


def _config_path_defaults(document: Mapping[str, Any]) -> dict[str, str]:
    paths = _section_path_defaults(document.get("tester_runner"), "local")
    paths.update(_section_path_defaults(document.get("duckdb_import"), "local"))
    for key in ("remote", "remote_runner", "remote_testing"):
        paths.update(_section_path_defaults(document.get(key), "remote"))
    workflow = document.get("panel_workflow")
    if isinstance(workflow, Mapping):
        for key in ("listing_dates_path", "algorithm_config_path", "strategy_template_path"):
            value = workflow.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    paths[key] = _safe_path(value)
                except PanelSettingsError:
                    continue
    return paths


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error()
    value = value.strip()
    if "\x00" in value or len(value) > 4096:
        raise _error()
    # UNC/device roots are intentionally excluded: remote defaults are SSH
    # target paths, not SMB shares or local device namespaces.
    if value.startswith(("~", "\\\\", "//", "//?/", "//./")) or value.casefold().startswith(("\\\\.\\", "\\\\?\\")):
        raise _error()
    absolute = value.startswith("/") or bool(_WINDOWS_ABSOLUTE.match(value))
    if not absolute and ":" in value:
        raise _error()
    if absolute:
        return ntpath.normpath(value) if _WINDOWS_ABSOLUTE.match(value) else posixpath.normpath(value)
    parts = tuple(part for part in _PATH_SEPARATOR.split(value) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise _error()
    return "/".join(parts)


def _normalise_panel(raw: Mapping[str, Any], root: Path) -> dict[str, Any]:
    if set(raw) - {"default_root", "path_defaults"}:
        raise _error()
    default_root = raw.get("default_root", "legacy")
    if not isinstance(default_root, str) or default_root not in _DEFAULT_ROOTS:
        raise _error()
    raw_paths = raw.get("path_defaults", {})
    if raw_paths is None:
        raw_paths = {}
    if not isinstance(raw_paths, dict) or set(raw_paths) - _PATH_KEYS:
        raise _error()
    paths: dict[str, str] = {}
    for key, value in raw_paths.items():
        if value is None:
            continue
        paths[key] = _safe_path(value)
    result: dict[str, Any] = {"default_root": default_root}
    if paths:
        result["path_defaults"] = paths
    else:
        result["path_defaults"] = {}
    return result


def _payload_panel(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise _error()
    if "panel" in payload:
        if set(payload) - {"panel", "operational"} or not isinstance(payload["panel"], Mapping):
            raise _error()
        return dict(payload["panel"])
    if set(payload) - {"default_root", "path_defaults"}:
        raise _error()
    return dict(payload)


def _operational_defaults(document: Mapping[str, Any]) -> dict[str, Any]:
    local = document.get("tester_runner") if isinstance(document.get("tester_runner"), Mapping) else {}
    remote = document.get("remote_runner") if isinstance(document.get("remote_runner"), Mapping) else {}
    importer = document.get("duckdb_import") if isinstance(document.get("duckdb_import"), Mapping) else {}
    workflow = document.get("panel_workflow") if isinstance(document.get("panel_workflow"), Mapping) else {}
    result = {
        "local_bot_root": local.get("bot_root", ""), "remote_runner_root": remote.get("debian_runner_root", ""),
        "source_db_path": importer.get("source_duckdb_path", ""), "output_root": local.get("inbox_root", ""),
        "listing_dates_path": workflow.get("listing_dates_path", ""), "algorithm_version": workflow.get("algorithm_version", "0.7-canonical-phase1"),
        "import_workers": importer.get("workers", 1), "transaction_batch_size": importer.get("transaction_batch_size", 5000),
    }
    for key in ("local_bot_root", "remote_runner_root", "source_db_path", "output_root", "listing_dates_path"):
        if result[key]: result[key] = _safe_path(result[key])
    for key in ("import_workers", "transaction_batch_size"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 1: result[key] = 1
    if not isinstance(result["algorithm_version"], str) or not result["algorithm_version"].strip() or len(result["algorithm_version"]) > 128:
        result["algorithm_version"] = "0.7-canonical-phase1"
    return result


def _operational_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    value = payload.get("operational")
    if value is None: return None
    if not isinstance(value, Mapping) or set(value) - _OPERATIONAL_KEYS: raise _error()
    current = dict(value)
    for key in ("local_bot_root", "remote_runner_root", "source_db_path", "output_root", "listing_dates_path"):
        if key in current: current[key] = _safe_path(current[key])
    for key in ("import_workers", "transaction_batch_size"):
        number = current.get(key)
        if key in current and (isinstance(number, bool) or not isinstance(number, int) or number < 1): raise _error()
    version = current.get("algorithm_version")
    if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 128): raise _error()
    return current


def _current_panel(path: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    document, text = _read_document(path, missing_ok=True)
    panel = _panel_document(document)
    try:
        normalised = _normalise_panel(panel, root)
    except PanelSettingsError:
        raise PanelSettingsError(SETTINGS_UNAVAILABLE) from None
    normalised["path_defaults"] = {
        **_config_path_defaults(document),
        **dict(normalised.get("path_defaults", {})),
    }
    return document, normalised, text


def _merge_panel(path: Path, root: Path, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    document, current, text = _current_panel(path, root)
    submitted = _payload_panel(payload)
    merged = dict(current)
    merged.update(submitted)
    merged["path_defaults"] = {
        **dict(current.get("path_defaults", {})),
        **dict(submitted.get("path_defaults", {})),
    }
    panel = _normalise_panel(merged, root)
    result = dict(document)
    result["panel"] = panel
    operational = _operational_payload(payload)
    if operational is not None:
        local = dict(result.get("tester_runner", {})); remote = dict(result.get("remote_runner", {}))
        importer = dict(result.get("duckdb_import", {})); workflow = dict(result.get("panel_workflow", {}))
        local_map = {"local_bot_root": "bot_root", "output_root": "inbox_root"}
        for source, target in local_map.items():
            if source in operational: local[target] = operational[source]
        if "remote_runner_root" in operational: remote["debian_runner_root"] = operational["remote_runner_root"]
        if "source_db_path" in operational: importer["source_duckdb_path"] = operational["source_db_path"]
        for source, target in (("import_workers", "workers"), ("transaction_batch_size", "transaction_batch_size"), ("listing_dates_path", "listing_dates_path"), ("algorithm_version", "algorithm_version")):
            if source in operational: (importer if target in {"workers", "transaction_batch_size"} else workflow).__setitem__(target, operational[source])
        result["tester_runner"] = local; result["remote_runner"] = remote
        result["duckdb_import"] = importer; result["panel_workflow"] = workflow
    return result, panel, text


def _format_document(document: Mapping[str, Any], previous: str | None) -> str:
    if previous is None:
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    pretty = "\n" in previous
    if pretty:
        match = re.search(r"\n([ \t]+)\"", previous)
        indent: int | str = match.group(1) if match else 2
        rendered = json.dumps(document, ensure_ascii=False, indent=indent)
    else:
        rendered = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if "\r\n" in previous:
        rendered = rendered.replace("\n", "\r\n")
    return rendered + ("\r\n" if previous.endswith("\r\n") else "\n" if previous.endswith("\n") else "")


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _atomic_write(path: Path, document: Mapping[str, Any], previous: str | None) -> None:
    # Stage and fsync both files before replacing the live config; no target
    # path is touched while JSON validation or staging can still fail.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _format_document(document, previous).encode("utf-8")
    temporary_path: Path | None = None
    backup_temporary: Path | None = None
    backup: Path | None = _backup_path(path) if path.is_file() else None
    prior_backup: bytes | None = None
    prior_backup_exists = False
    if backup is not None:
        try:
            prior_backup = backup.read_bytes()
            prior_backup_exists = True
        except FileNotFoundError:
            pass
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        if backup is not None:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
                backup_temporary = Path(handle.name)
            os.replace(backup_temporary, backup)
            backup_temporary = None
        try:
            os.replace(temporary_path, path)
        except OSError:
            if backup is not None:
                if prior_backup_exists and prior_backup is not None:
                    backup.write_bytes(prior_backup)
                else:
                    backup.unlink(missing_ok=True)
            raise
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if backup_temporary is not None:
            backup_temporary.unlink(missing_ok=True)


def reload_settings(path: Path, root: Path) -> dict[str, Any]:
    document, panel, _ = _current_panel(path, root)
    return {"valid": True, "settings": {"panel": panel, "operational": _operational_defaults(document)}}


def validate_settings(path: Path, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    document, panel, _ = _merge_panel(path, root, payload)
    return {"valid": True, "settings": {"panel": panel, "operational": _operational_defaults(document)}}


def save_settings(path: Path, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    document, panel, previous = _merge_panel(path, root, payload)
    try:
        _atomic_write(path, document, previous)
    except OSError:
        raise PanelSettingsError(INVALID_SETTINGS) from None
    return {"valid": True, "saved": True, "settings": {"panel": panel, "operational": _operational_defaults(document)}}


def _remote_configured(document: Mapping[str, Any]) -> bool:
    for key in ("remote", "remote_runner", "remote_testing", "ssh"):
        value = document.get(key)
        if isinstance(value, Mapping):
            enabled = value.get("enabled")
            if isinstance(enabled, bool):
                return enabled
            return bool(value)
    return False


def bootstrap(path: Path, root: Path) -> dict[str, Any]:
    try:
        document, _ = _read_document(path, missing_ok=False)
        panel = _normalise_panel(_panel_document(document), root)
    except PanelSettingsError:
        document = {}
        panel = {"default_root": "legacy", "path_defaults": {}}
    try:
        RunnerConfig.from_json(path)
    except Exception:
        runner_configured = False
    else:
        runner_configured = True
        panel["path_defaults"] = {
            **_config_path_defaults(document),
            **dict(panel.get("path_defaults", {})),
        }
    return {
        "version": "panel-ui-v2",
        "defaults": {
            "panel": panel,
            "operational": _operational_defaults(document) if runner_configured else {},
            "runner": {"configured": runner_configured},
            "remote": {"configured": _remote_configured(document)},
        },
        "capabilities": {"settings": True, "portfolio": False},
    }
