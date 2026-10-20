from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import csv
from dataclasses import asdict, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from hashlib import sha256
import inspect
import logging
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from time import perf_counter
from typing import BinaryIO, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
import uuid
import webbrowser

_PANEL_WEB = Path(__file__).with_name("panel_web")
_PERFORMANCE_V2_MAINTENANCE_PREVIEW_TTL_SECONDS = 15 * 60
_PERFORMANCE_V2_MAINTENANCE_RECOVERY_TTL_SECONDS = 15 * 60
_PERFORMANCE_V2_MAINTENANCE_MAX_PREVIEWS = 32
_BULK_RETEST_EQUITY_FILTER_LEASE_SECONDS = 20 * 60
_BULK_RETEST_EQUITY_FILTER_MAX_ATTEMPTS = 8
_LOGGER = logging.getLogger(__name__)
_RETEST_INBOX_PATH_UNAVAILABLE = "committed RETEST inbox path is unavailable"
_RETEST_INBOX_MANIFEST_UNAVAILABLE = "committed RETEST inbox manifest is unavailable"
_RETEST_INBOX_UNAVAILABLE = frozenset({_RETEST_INBOX_PATH_UNAVAILABLE, _RETEST_INBOX_MANIFEST_UNAVAILABLE})


def _open_regular_artifact(path: Path) -> tuple[BinaryIO, int]:
    """Open one already-authorized artifact without following POSIX symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        fd = os.open(path, flags | nofollow)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("artifact is not a regular file")
            return os.fdopen(fd, "rb"), info.st_size
        except BaseException:
            os.close(fd)
            raise

    # Windows has no O_NOFOLLOW; bind the response to this opened handle and
    # verify the path did not become a reparse point or another file while it
    # was being opened.
    expected_path = path.resolve(strict=True)
    expected_parent = expected_path.parent.resolve(strict=True)
    expected_info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(expected_info.st_mode):
        raise OSError("artifact is not a regular file")
    handle = path.open("rb")
    try:
        info = os.fstat(handle.fileno())
        resolved = path.resolve(strict=True)
        if (
            not stat.S_ISREG(info.st_mode)
            or not os.path.samestat(expected_info, info)
            or resolved != expected_path
            or resolved.parent.resolve(strict=True) != expected_parent
        ):
            raise OSError("artifact is not a regular file")
        return handle, info.st_size
    except BaseException:
        handle.close()
        raise

import duckdb
import pandas as pd

from .config import load_panel_path_settings
from .config import (
    DuckDBImportSettings,
    load_duckdb_import_settings,
)
from .exchange_profiles import (
    ExchangeProfile,
    ExchangeProfileError,
    PerformanceDatabaseError,
    exchange_template_names,
    inside_project,
    load_exchange_profile,
    load_performance_databases,
    parse_performance_databases,
    relative_project_path,
    require_apart_from_default,
    save_performance_databases,
)
from .loader import load_listing_dates
from .duckdb_direct import (
    V2_AUDIT_SCHEMA_VERSION,
)
from .models import Side
from .performance_import import _canonical, _canonical_contract, _sha256
from .panel_settings import (
    PanelSettingsError,
    bootstrap as panel_bootstrap,
    reload_settings as reload_panel_settings,
    save_settings as save_panel_settings,
    validate_settings as validate_panel_settings,
)
from .panel_jobs import TERMINAL, PanelJobError, PanelJobRegistry
from .panel_report_collection import PanelReportCollection
from .panel_portfolio import PortfolioPanelError, PortfolioPanelService
from .panel_testing import (
    LocalTestingService,
    PanelTestingError,
    mrs3_tester_config_template,
)
from .error_sanitization import redact_local_paths as _redact_local_paths
from .pair_registry.store import RegistryError
from .panel_main import MainScreenError, MainScreenService
from .trade_store import TradeStoreError
from .panel_tasks import TaskRunner
from .store_ready_tick import StoreReadyError, list_ready_folders, prepare_tick_batch, resolve_ready_folder
from .evaluation_protocol import (
    EvaluationProtocolError,
    EvaluationTarget,
    evaluation_target,
    require_protocol_v2_strategy,
    require_protocol_v2_tester_config,
)
from .panel_strategy_batch import LocalStrategyBatchService, StrategyBatchValidationError, validate_strategy_manifest
from .panel_tester_runs import LocalRunsBatchService
from .panel_fast_strategy_test import (
    LocalSingleModeStrategyTestService,
    parse_initial_balance,
)
from .panel_performance_v2 import (
    LocalPerformanceV2Jobs,
    PerformanceV2ApiError,
    PerformanceV2PanelRequest,
    _is_duckdb_lock_error,
)
from .performance_v2_store import (
    PerformanceV2Config,
    PerformanceV2StoreError,
    PerformanceV2WriterLock,
    database_identity,
    initialize_performance_v2,
    load_performance_v2_config,
    performance_v2_database_path,
    require_performance_v2,
    require_performance_v2_readable,
    require_database_identity,
    PerformanceV2IdentityError,
)
from .performance_v2_maintenance import (
    PerformanceV2MaintenanceError,
    PerformanceV2MaintenanceSchemaError,
    apply_preview as apply_performance_v2_maintenance_preview,
    catalog_symbols as performance_v2_maintenance_catalog,
    create_preview as create_performance_v2_maintenance_preview,
    public_preview as public_performance_v2_maintenance_preview,
    set_query_workers as set_performance_v2_maintenance_workers,
)
from .performance_v2_equity_cache import EquitySourceChangedError
from .performance_v2_selection import (
    EquityCacheSchemaInvalidError,
    EquitySchemaUpgradeRequiredError,
    PerformanceV2SelectionError,
    SelectionRequest,
    SelectionStage,
    SelectionConfig,
    load_selection_candidates,
    load_selection_config,
    parse_selection_request,
    prepare_selection_window_cache,
    run_selection,
    selection_cache_missing_strategy_ids,
    selection_cache_status,
    write_selection_workbook,
    retest_cohort_request,
)
from .performance_v2_equity_regime import (
    ALGORITHM_VERSION as _EQUITY_REGIME_ALGORITHM_VERSION,
    equity_regime_policy_snapshot,
)
from .performance_v2_selection_review import (
    SelectionReviewError,
    apply_prior_rejected,
    _current_equity_revisions,
    import_retest_tags,
    persist_selection_snapshots,
)
from .performance_v2_retest import build_retest_manifest, retest_status
from .performance_v2_finalist_retest import (
    FinalistRetestError,
    build_finalist_retest_manifest,
    canonical_digest,
    freeze_finalist_cohort,
    validate_combined_control_workbook,
    combined_control_workbook_bytes,
    import_combined_control_workbook,
    current_effective_finalist_members,
    finalist_retest_config_digest,
    _json_value,
)
from .runner.config import RunnerConfig
from .runner.inbox import capture_verified_inbox
from .runner.workflow import BatchPlan, _load_saved_result_evidence, _load_saved_results, plan_batch, run_batch


def _control_score(value: object) -> object:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return value
    if not number.is_finite():
        return None
    return int(number) if number == number.to_integral_value() else float(number)




class PanelController:
    def __init__(
        self,
        root: Path,
        default_config: Path,
    ) -> None:
        self.root = root.resolve()
        self.default_config = self._path(default_config)
        self._lock = threading.RLock()
        self._performance_v2_writer_lock = threading.RLock()
        self._bulk_retest_import_lock = threading.RLock()
        self._bulk_retest_equity_filter_lock = threading.RLock()
        self._performance_v2_maintenance_state_lock = threading.RLock()
        self._performance_v2_maintenance_active_job: str | None = None
        self._performance_v2_maintenance_previews: dict[str, tuple[float, dict[str, object]]] = {}
        self._performance_v2_maintenance_job: dict[str, object] | None = None
        self._performance_v2_maintenance_recovery_preview: tuple[float, dict[str, object]] | None = None
        self._performance_v2_recalculate_progress: dict[str, object] = {
            "status": "IDLE", "total_pairs": 0, "ready_pairs": 0,
            "planned_pairs": 0, "completed_pairs": 0,
            "total_strategies": 0, "completed_strategies": 0,
            "current_pair": None, "error": None,
        }
        self._performance_v2_schema_ready: set[tuple[str, int, int, int, int, int]] = set()
        self._panel_jobs = PanelJobRegistry(self.root / ".panel-jobs.json", recover_on_load=False)
        self._report_collection_service: PanelReportCollection | None = None
        self._portfolio_service = PortfolioPanelService(
            self.root,
            self.root / "portfolio_optimizer.local.json",
            registry=self._panel_jobs,
            lock=self._lock,
            local_testing_service_provider=lambda: self._local_testing_service(),
        )
        self._portfolio_service.startup_recover()
        self._local_testing_service_instance: LocalTestingService | None = None
        self._fresh_strategy_manifests: dict[str, Path] = {}
        self._strategy_batch_service: LocalStrategyBatchService | None = None
        self._runs_batch_service: LocalRunsBatchService | None = None
        self._single_mode_strategy_test_service: LocalSingleModeStrategyTestService | None = None
        self._strategy_batch_inboxes: dict[str, Path] = {}
        self._performance_v2_jobs: LocalPerformanceV2Jobs | None = None
        self._collection_import_claim_ids: set[str] = set()
        self._reconcile_interrupted_tester_jobs()

    def _path(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def _panel_path(self, name: str) -> Path:
        """Resolve one server-owned fixed root; requests cannot override it."""
        settings = load_panel_path_settings(self.default_config)
        try:
            value = getattr(settings, name)
        except AttributeError:
            raise ValueError("panel path is unavailable") from None
        return self._path(value)

    def _workflow_defaults(self) -> dict[str, object]:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"listing_dates_path": "", "strategy_templates": {}}
        raw = document.get("panel_workflow", {})
        if not isinstance(raw, dict):
            return {"listing_dates_path": "", "strategy_templates": {}}
        dates = raw.get("listing_dates_path", "")
        templates = raw.get("strategy_templates", {})
        resolved_templates = {
            side: str(self._path(path))
            for side, path in templates.items()
            if side in {"LONG", "SHORT"} and isinstance(path, str) and path.strip()
        } if isinstance(templates, dict) else {}
        return {
            "listing_dates_path": str(self._path(dates)) if isinstance(dates, str) and dates.strip() else "",
            "strategy_templates": resolved_templates,
        }

    @staticmethod
    def _required(payload: Mapping[str, object], name: str) -> str:
        value = str(payload.get(name, "")).strip()
        if not value:
            raise ValueError(f"required field is empty: {name}")
        return value

    @staticmethod
    def _is_hash(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True

    @staticmethod
    def _validate_performance_inbox(inbox: Path) -> None:
        manifest_path = inbox / "inbox_manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("inbox is incomplete: invalid manifest") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("inbox is incomplete: schema_version must be 1")
        for field in ("batch_id", "tester_config_sha256"):
            if not isinstance(document.get(field), str) or not document[field].strip():
                raise ValueError(f"inbox is incomplete: {field} is missing")
        if not PanelController._is_hash(document["tester_config_sha256"]):
            raise ValueError("inbox is incomplete: tester_config_sha256 is invalid")
        expected_names = document.get("expected_strategy_names")
        entries = document.get("entries")
        if not isinstance(expected_names, list) or not all(isinstance(name, str) and name for name in expected_names):
            raise ValueError("inbox is incomplete: expected_strategy_names is invalid")
        if not isinstance(entries, list) or not entries:
            raise ValueError("inbox is incomplete: manifest has no entries")
        try:
            _canonical_contract(document)
        except Exception as error:
            raise ValueError(f"inbox is incomplete: commission contract: {error}") from error
        names: list[str] = []
        required = ("manifest_entry_id", "strategy_name", "strategy_version_id", "strategy_path", "report_path", "wizard_run_id", "exchange_name", "source_strategy_sha256", "source_report_sha256")
        for entry in entries:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(field), str) or not entry[field].strip() for field in required):
                raise ValueError("inbox is incomplete: mandatory entry field is missing")
            names.append(entry["strategy_name"])
            for field in ("strategy_version_id", "source_strategy_sha256", "source_report_sha256"):
                if not PanelController._is_hash(entry[field]):
                    raise ValueError(f"inbox is incomplete: {field} is invalid")
            paths: dict[str, Path] = {}
            for field in ("strategy_path", "report_path"):
                value = Path(entry[field])
                if ".." in value.parts:
                    raise ValueError("inbox is incomplete: path is outside inbox")
                candidate = value.resolve() if value.is_absolute() else (inbox / value).resolve()
                try:
                    if not value.is_absolute():
                        candidate.relative_to(inbox.resolve())
                except ValueError as error:
                    raise ValueError("inbox is incomplete: path is outside inbox") from error
                if not candidate.is_file():
                    raise ValueError(f"inbox is incomplete: missing {field}")
                paths[field] = candidate
            if _sha256(paths["report_path"].read_bytes()) != entry["source_report_sha256"]:
                raise ValueError("inbox is incomplete: report hash mismatch")
            try:
                strategy = json.loads(paths["strategy_path"].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("inbox is incomplete: invalid strategy JSON") from error
            if not isinstance(strategy, dict):
                raise ValueError("inbox is incomplete: strategy JSON is not an object")
            if _sha256(paths["strategy_path"].read_bytes()) != entry["source_strategy_sha256"]:
                raise ValueError("inbox is incomplete: strategy hash mismatch")
            if _sha256(_canonical(strategy)) != entry["strategy_version_id"]:
                raise ValueError("inbox is incomplete: strategy version hash mismatch")
        if sorted(names) != sorted(expected_names):
            raise ValueError("inbox is incomplete: strategy names do not match")

    def _validate_metadata_inbox(self, inbox: Path) -> None:
        """Perform only the handoff check; v2 importer owns source validation."""
        config = RunnerConfig.from_json(self.default_config)
        output_root = self._output_strategy_root()
        strategy_roots = [output_root]
        try:
            document = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("metadata inbox is incomplete: invalid manifest") from error
        if not isinstance(document, dict) or document.get("schema_version") not in {1, 2}:
            raise ValueError("metadata inbox is incomplete: unsupported schema version")
        if document.get("schema_version") == 2:
            if (
                document.get("collection_manifest_version") != 1
                or document.get("run_mode") != "SINGLE_MODE_COLLECTION"
                or not isinstance(document.get("collection_id"), str)
                or not document.get("collection_id")
            ):
                raise ValueError("metadata inbox is incomplete: invalid collection manifest")
        artifact_roots = {
            "strategy_path": tuple(dict.fromkeys(strategy_roots)),
            "report_path": (config.report_dir.resolve(),),
        }
        expected_mode = "SINGLE_MODE_COLLECTION" if document.get("schema_version") == 2 else "SINGLE_MODE"
        if document.get("run_mode") != expected_mode or document.get("source_mode") != "metadata_only":
            raise ValueError("metadata inbox is incomplete: unsupported handoff mode")
        expected = document.get("expected_strategy_names")
        entries = document.get("entries")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(name, str) or not name for name in expected)
            or len(set(expected)) != len(expected)
            or not isinstance(entries, list)
            or len(entries) != len(expected)
        ):
            raise ValueError("metadata inbox is incomplete: strategy names are invalid")
        if document.get("inbox_ready") is not True or (inbox / "strategies").exists() or (inbox / "strategies").is_symlink():
            raise ValueError("metadata inbox is incomplete: inbox is not ready")
        names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("metadata inbox is incomplete: entry is invalid")
            name = entry.get("strategy_name")
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("metadata inbox is incomplete: strategy names do not match")
            names.add(name)
            for field in ("strategy_path", "report_path"):
                value = entry.get(field)
                path = Path(value) if isinstance(value, str) and value else None
                roots = artifact_roots[field]
                if path is None:
                    raise ValueError(f"metadata inbox is incomplete: {field} is missing")
                candidates = (
                    ((path, roots),)
                    if path.is_absolute()
                    else tuple((root / path, (root,)) for root in roots)
                )
                outside = False
                for candidate_path, trusted_roots in candidates:
                    if candidate_path.is_symlink():
                        continue
                    candidate = candidate_path.resolve()
                    if not any(candidate == trusted or trusted in candidate.parents for trusted in trusted_roots):
                        outside = True
                        continue
                    if candidate.is_file():
                        break
                else:
                    if outside:
                        raise ValueError(f"metadata inbox is incomplete: {field} is outside configured directory")
                    raise ValueError(f"metadata inbox is incomplete: {field} is missing")
        if names != set(expected):
            raise ValueError("metadata inbox is incomplete: strategy names do not match")

    def _owned_single_mode_inbox(self, inbox_root: Path, job_id: str) -> Path | None:
        inbox = (inbox_root / job_id).resolve()
        if inbox.parent != inbox_root:
            return None
        manifest_path = inbox / "inbox_manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, Mapping) or manifest.get("batch_id") != job_id:
            raise ValueError("existing tester inbox belongs to another job")
        try:
            self._validate_metadata_inbox(inbox)
        except ValueError:
            return None
        return inbox

    def panel_bootstrap(self) -> dict[str, object]:
        """Return only validated, non-sensitive v2 panel capabilities."""
        with self._lock:
            return panel_bootstrap(self.default_config, self.root)

    def panel_settings_reload(self) -> dict[str, object]:
        with self._lock:
            return reload_panel_settings(self.default_config, self.root)

    def panel_settings_validate(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            return validate_panel_settings(self.default_config, self.root, payload)

    def panel_settings_save(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            return save_panel_settings(self.default_config, self.root, payload)


    _PERFORMANCE_DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

    def _local_config_path(self) -> Path:
        return self.default_config.with_name("config.local.json")

    def _performance_database_busy(self) -> bool:
        """Unfinished jobs, open report collections or pending imports block a database switch (P7 spec §5)."""
        for job in self._panel_jobs.list():
            if job.get("state") not in TERMINAL:
                return True
            if job.get("kind") != "strategies.tester.collection":
                continue
            try:
                runtime = self._panel_jobs.runtime(str(job.get("job_id")))
            except PanelJobError:
                continue
            state = runtime.get("collection_state")
            members = runtime.get("members")
            current = [
                member for member in members if isinstance(member, Mapping) and member.get("superseded") is not True
            ] if isinstance(members, list) else []
            if state == "VERIFIED" or (state == "OPEN" and current):
                return True
        return False

    def _performance_database_row(
        self, name: str | None, root: Path, protocol: str, exchange: str | None, active: bool, busy: bool = False,
    ) -> dict[str, object]:
        resolved = Path(root).resolve()
        try:
            relative = resolved.relative_to(self._local_config_path().parent.resolve()).as_posix()
        except ValueError:
            relative = resolved.as_posix()
        database = resolved / "strategy_performance.duckdb"
        row: dict[str, object] = {
            "name": name, "database_root": relative, "evaluation_protocol": protocol, "exchange": exchange,
            "active": active, "exists": database.is_file(), "size_bytes": None, "markers": None,
        }
        if not database.is_file():
            return row
        if busy or not self._performance_v2_writer_lock.acquire(blocking=False):
            # A job or a Panel writer may be connecting: a second connection could make its connect fail.
            try:
                row["size_bytes"] = database.stat().st_size
            except OSError:
                pass
            return row
        try:
            row["size_bytes"] = database.stat().st_size
            if row["size_bytes"]:
                with duckdb.connect(str(database), read_only=True) as connection:
                    marker_protocol, marker_exchange = database_identity(connection)
                    version = connection.execute(
                        "select value from schema_info where key = 'schema_version'"
                    ).fetchone()
                row["markers"] = {
                    "evaluation_protocol": marker_protocol, "exchange": marker_exchange,
                    "schema_version": None if version is None else str(version[0]),
                }
        except (OSError, duckdb.Error, PerformanceV2StoreError):
            # An open writer or a foreign file: the list stays readable without markers.
            row["markers"] = None
        finally:
            self._performance_v2_writer_lock.release()
        return row

    def performance_databases_get(self) -> dict[str, object]:
        """Registered PerformanceDBs and the default one (P7 spec §5)."""
        local = self._local_config_path()
        databases = load_performance_databases(local)
        default = self._default_performance_v2_config()
        busy = self._performance_database_busy()
        rows = [self._performance_database_row(
            None, default.database_root, default.evaluation_protocol, default.exchange, databases.active is None, busy,
        )]
        rows.extend(
            self._performance_database_row(
                item.name, local.parent / item.database_root, item.evaluation_protocol, item.exchange,
                databases.active == item.name, busy,
            )
            for item in databases.items
        )
        return {
            "active": databases.active, "items": rows, "exchanges": list(exchange_template_names(self.root)),
            "busy": busy,
        }

    def _performance_databases_section(self) -> dict[str, object]:
        local = self._local_config_path()
        try:
            document = json.loads(local.read_text(encoding="utf-8")) if local.is_file() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "config.local.json is unreadable") from error
        section = document.get("performance_databases") if isinstance(document, dict) else None
        if section is None:
            return {"active": None, "items": {}}
        if not isinstance(section, dict) or not isinstance(section.get("items", {}), dict):
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "performance_databases is invalid")
        return {"active": section.get("active"), "items": dict(section.get("items", {}))}

    def performance_databases_activate(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Make an entry (or the default database, name None) active; only when nothing runs (P7 spec §5)."""
        if not isinstance(payload, Mapping) or set(payload) != {"name"}:
            raise PerformanceDatabaseError("INVALID_REQUEST", "activate needs exactly the field name")
        name = payload["name"]
        if name is not None and not isinstance(name, str):
            raise PerformanceDatabaseError("INVALID_REQUEST", "name must be a string or null")
        with self._lock, self._panel_jobs.lock:
            section = self._performance_databases_section()
            if name is not None and name not in section["items"]:  # type: ignore[operator]
                raise PerformanceDatabaseError("PERFORMANCE_DATABASE_UNKNOWN", f"no database named {name}")
            if self._performance_database_busy():
                raise PerformanceDatabaseError("RESOURCE_BUSY", "finish or clear running jobs and report collections first")
            section["active"] = name
            save_performance_databases(self._local_config_path(), section)
        return self.performance_databases_get()

    def performance_databases_create(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Create an empty protocol v2 database for an exchange and register it (P7 spec §5)."""
        if not isinstance(payload, Mapping) or set(payload) != {"name", "database_root", "exchange"}:
            raise PerformanceDatabaseError("INVALID_REQUEST", "create needs name, database_root and exchange")
        name, root_value, exchange = payload["name"], payload["database_root"], payload["exchange"]
        if not isinstance(name, str) or not self._PERFORMANCE_DATABASE_NAME.fullmatch(name):
            raise PerformanceDatabaseError("INVALID_REQUEST", "name allows letters, digits, '.', '_' and '-' (1-64)")
        if not isinstance(exchange, str) or not exchange.strip():
            raise PerformanceDatabaseError("INVALID_REQUEST", "exchange must be a non-empty string")
        exchange = exchange.strip()
        local = self._local_config_path()
        try:
            root = relative_project_path(root_value, "database_root")
        except ValueError as error:
            raise PerformanceDatabaseError("INVALID_REQUEST", str(error)) from None
        if len(root.parts) < 2 or root.parts[0].casefold() != "data":
            raise PerformanceDatabaseError("INVALID_REQUEST", "database_root must be a folder inside data/")
        try:
            # The marker takes the spelling of the template folder: it is the exchange.name of the bot.
            exchange = load_exchange_profile(self.root, exchange).exchange
        except ExchangeProfileError as error:
            raise PerformanceDatabaseError(error.code, error.message) from error
        with self._lock:
            section = self._performance_databases_section()
            if any(str(key).casefold() == name.casefold() for key in section["items"]):  # type: ignore[union-attr]
                raise PerformanceDatabaseError("PERFORMANCE_DATABASE_EXISTS", f"a database named {name} already exists")
            section["items"][name] = {  # type: ignore[index]
                "database_root": root.as_posix(), "evaluation_protocol": "v2", "exchange": exchange,
            }
            default = self._default_performance_v2_config()
            # Validate the new section (paths, overlaps, default root) before anything is written.
            require_apart_from_default(parse_performance_databases(section, local.parent), local.parent, default.database_root)
            try:
                target = inside_project(local.parent, root, "database_root")
                data_root = inside_project(local.parent, Path("data"), "data")
            except ValueError as error:
                raise PerformanceDatabaseError("INVALID_REQUEST", str(error)) from None
            if data_root not in target.parents:
                raise PerformanceDatabaseError("INVALID_REQUEST", "database_root must be a folder inside data/")
            if target == default.database_root.resolve():
                raise PerformanceDatabaseError("INVALID_REQUEST", "database_root is the default database folder")
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise PerformanceDatabaseError("INVALID_REQUEST", "database_root must be a new or empty folder")
            created = not target.exists()
            try:
                self._initialize_missing_performance_v2_target(
                    replace(default, database_root=target, evaluation_protocol="v2", exchange=exchange),
                )
                save_performance_databases(local, section)
            except (OSError, duckdb.Error, ValueError) as error:
                # Leave no half-created database behind: a retry must find a new or empty folder again.
                shutil.rmtree(target, ignore_errors=True)
                if not created:
                    target.mkdir(parents=True, exist_ok=True)
                if isinstance(error, ValueError) and not isinstance(error, PerformanceV2StoreError):
                    # For example the new root overlaps the v1 database root.
                    raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", str(error)) from error
                raise PerformanceDatabaseError(
                    "PERFORMANCE_DATABASE_CREATE_FAILED", "the database or config.local.json could not be written",
                ) from error
        return self.performance_databases_get()

    def _default_performance_v2_config(self) -> PerformanceV2Config:
        try:
            return load_performance_v2_config(
                self.default_config.with_name("config.performance.json"),
                v1_database_root=self._panel_path("performance_db_root"), use_active_entry=False,
            )
        except PerformanceDatabaseError:
            raise
        except ValueError as error:
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", str(error)) from error

    def panel_jobs(self) -> list[dict]:
        return self._panel_jobs.public_list()

    def strategies_tester_report_collection(self) -> dict[str, object]:
        return self._report_collection().status()

    def strategies_tester_report_collection_clear(self, payload: Mapping[str, object]) -> dict[str, object]:
        if set(payload) != {"collection_id"} or not isinstance(payload.get("collection_id"), str) or not payload["collection_id"].strip():
            raise ValueError("collection clear requires collection_id")
        return self._report_collection().clear(payload["collection_id"])

    def portfolio_readiness(self) -> dict[str, object]:
        return self._portfolio_service.readiness()

    def portfolio_settings_get(self) -> dict[str, object]:
        return self._portfolio_service.settings_get()

    def portfolio_settings_put(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._portfolio_service.settings_put(payload)

    def portfolio_submit_campaign(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._portfolio_service.submit_campaign(payload)

    def portfolio_prepare_finalist_inputs(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._portfolio_service.prepare_finalist_inputs(payload)

    def portfolio_active_job(self) -> dict[str, object] | None:
        return self._portfolio_service.active_or_job()

    def portfolio_job(self, job_id: str) -> dict[str, object]:
        return self._portfolio_service.job(job_id)

    def portfolio_cancel(self, job_id: str) -> dict[str, object]:
        return self._portfolio_service.cancel(job_id)

    def portfolio_results(self, campaign_id: str) -> dict[str, object]:
        return self._portfolio_service.results(campaign_id)

    def portfolio_workbook(self, campaign_id: str) -> bytes:
        return self._portfolio_service.workbook(campaign_id)

    def portfolio_tester_submission(self, campaign_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        return self._portfolio_service.submit_tester_submission(campaign_id, payload)

    def has_active_panel_jobs(self) -> bool:
        if any(job["state"] not in {"COMMITTED", "CANCELLED", "FAILED"} for job in self.panel_jobs()):
            return True
        if self._main_task_running():
            return True
        return (
            self._strategy_batch_service is not None and self._strategy_batch_service.has_active_job()
        ) or (
            self._runs_batch_service is not None and self._runs_batch_service.has_active_job()
        ) or (
            self._single_mode_strategy_test_service is not None and self._single_mode_strategy_test_service.has_active_job()
        )

    def panel_job_submit(self, payload: Mapping[str, object]) -> dict:
        kind = payload.get("kind")
        request = payload.get("request")
        if kind == "strategies.tester.start" and isinstance(request, Mapping):
            return self.strategies_tester_start(request)
        if kind == "strategies.tester.retry" and isinstance(request, Mapping):
            return self.strategies_tester_retry(request)
        if kind == "strategies.tester.runs" and isinstance(request, Mapping):
            return self.strategies_tester_runs_start(request)
        if isinstance(kind, str) and kind.startswith("strategies.tester.") and ".fast." in kind:
            raise PanelJobError("UNSUPPORTED_ROUTE")
        if kind == "strategies.tester.cancel" and isinstance(request, Mapping):
            return self.strategies_tester_cancel(self._required(request, "job_id"))
        if kind == "strategies.performance.v2.import" and isinstance(request, Mapping):
            return self.strategies_performance_v2_import(request)
        if kind == "strategies.performance.v2.retest.start" and isinstance(request, Mapping):
            return self.strategies_performance_v2_retest_start(request)
        if kind == "strategies.performance.v2.retest.import" and isinstance(request, Mapping):
            return self.strategies_performance_v2_retest_import(request)
        if kind == "strategies.performance.v2.finalist-retest.start" and isinstance(request, Mapping):
            return self.strategies_performance_v2_finalist_retest_start(request)
        if kind == "strategies.performance.v2.finalist-retest.import" and isinstance(request, Mapping):
            return self.strategies_performance_v2_finalist_retest_import(request)
        idempotency_key = payload.get("idempotency_key")
        resource_keys = payload.get("resource_keys", [])
        if not isinstance(kind, str) or not isinstance(request, dict) or not isinstance(idempotency_key, str) or not isinstance(resource_keys, list):
            raise PanelJobError("INVALID_REQUEST")
        return self._panel_jobs.submit(kind, request, idempotency_key, tuple(resource_keys))

    def _start_tracked_panel_job(
        self,
        kind: str,
        request: dict[str, object],
        resource_keys: tuple[str, ...],
        start: Callable[[str], dict[str, object]],
        runtime: dict[str, object] | None = None,
        before_start: Callable[[str], None] | None = None,
        job_id: str | None = None,
    ) -> dict[str, object]:
        job_id = uuid.uuid4().hex if job_id is None else job_id
        # The runtime is persisted with the job, so a journal failure leaves no half-created job.
        self._panel_jobs.submit(kind, request, f"panel:{job_id}", resource_keys, job_id=job_id, runtime=runtime or None)
        self._panel_jobs.transition(job_id, "RUNNING")
        try:
            if before_start is not None:
                before_start(job_id)
            return self._sync_tracked_panel_job(start(job_id))
        except BaseException:
            self._panel_jobs.transition(job_id, "FAILED")
            raise

    @staticmethod
    def _performance_v2_result_snapshot(document: Mapping[str, object]) -> dict[str, object]:
        result = document.get("result")
        if not isinstance(result, Mapping):
            return {}
        keys = (
            "import_id", "status", "imported_count", "skipped_count", "rejected_count",
            "strategy_count", "order_count",
            "plateau_count", "result_count", "window_count",
            "cleanup_warning",
        )
        snapshot = {key: result[key] for key in keys if key in result}
        if "failure_report_path" in result:
            snapshot["failure_report_available"] = bool(result["failure_report_path"])
            job_id = document.get("job_id")
            if isinstance(job_id, str) and job_id:
                snapshot["failure_report_token"] = f"performance-v2-failure-report:{job_id}"
        return snapshot

    def _record_special_job(self, document: dict[str, object]) -> None:
        """Persist worker completion without exposing controller-only artifact paths."""
        job_id = document.get("job_id")
        if not isinstance(job_id, str):
            return
        runtime = {}
        inbox = document.get("inbox_path")
        if isinstance(inbox, str) and inbox:
            runtime["inbox_path"] = inbox
            if document.get("mode") == "SINGLE_MODE":
                runtime["mode"] = "SINGLE_MODE"
        public = {key: value for key, value in document.items() if key in {"state", "phase", "progress", "error", "evidence"}}
        if document.get("state") == "COMMITTED" and document.get("inbox_ready") is True:
            public["inbox_ready"] = True
        publication_error = document.get("progress_publication_error")
        if (
            isinstance(publication_error, str)
            and publication_error
            and public.get("state") not in {"COMMITTED", "FAILED", "CANCELLED"}
        ):
            progress = dict(public.get("progress")) if isinstance(public.get("progress"), Mapping) else {}
            progress["publication_error"] = publication_error
            public["progress"] = progress
        try:
            tracked = self._panel_jobs._peek(job_id)
        except PanelJobError:
            return
        if runtime:
            # Worker callbacks must not erase controller-owned RETEST handoff
            # metadata (dates, manifest and listing path) when adding inbox_path.
            try:
                saved_runtime = self._panel_jobs.runtime(job_id)
            except PanelJobError:
                saved_runtime = {}
            saved_runtime.update(runtime)
            runtime = saved_runtime
        callback_runtime = document.get("runtime")
        if isinstance(callback_runtime, Mapping):
            if not runtime:
                try:
                    runtime = self._panel_jobs.runtime(job_id)
                except PanelJobError:
                    runtime = {}
            runtime.update(callback_runtime)
        if (
            tracked.get("kind") in {"strategies.tester.runs", "strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry", "strategies.performance.v2.finalist-retest"}
            and document.get("state") == "COMMITTED"
            and isinstance(inbox, str)
            and inbox
        ):
            public["inbox_ready"] = True
        if (
            tracked.get("kind") in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}
            and document.get("state") == tracked.get("state")
            and document.get("phase") == tracked.get("phase")
            and document.get("error") == tracked.get("error")
            and document.get("evidence", tracked.get("evidence")) == tracked.get("evidence")
            and not runtime
            and "inbox_ready" not in public
        ):
            try:
                self._panel_jobs.volatile_sync(job_id, public, expected=tracked)
            except PanelJobError:
                pass
            return
        if (
            tracked.get("kind") == "strategies.performance.v2.finalist-retest"
            and document.get("state") == "RUNNING"
        ):
            try:
                self._panel_jobs.volatile_sync(
                    job_id,
                    public,
                    expected=tracked,
                    runtime=runtime if runtime else None,
                )
            except PanelJobError:
                pass
            return
        if tracked.get("kind") == "strategies.performance.v2.import" and document.get("state") in {"COMMITTED", "FAILED", "CANCELLED"}:
            result = self._performance_v2_result_snapshot(document)
            if result:
                public["result"] = result
            raw_report = document.get("result", {}).get("failure_report_path") if isinstance(document.get("result"), Mapping) else None
            raw_result = document.get("result")
            if isinstance(raw_result, Mapping):
                for key in ("successful_replacements", "failures"):
                    if isinstance(raw_result.get(key), list):
                        runtime[key] = raw_result[key]
            if isinstance(raw_report, str) and raw_report:
                runtime["failure_report_path"] = raw_report
            if document.get("state") == "COMMITTED" and runtime:
                public["inbox_ready"] = True
            request = tracked.get("request")
            tester_job_id = request.get("tester_job_id") if isinstance(request, Mapping) else None
            if not isinstance(tester_job_id, str):
                tracked_runtime = tracked.get("runtime")
                tester_job_id = tracked_runtime.get("report_collection_id") if isinstance(tracked_runtime, Mapping) else None
            if isinstance(tester_job_id, str) and tester_job_id:
                try:
                    tester_snapshot = self._panel_jobs.get(tester_job_id)
                    is_collection = tester_snapshot.get("kind") == "strategies.tester.collection"
                    if is_collection:
                        if self._panel_jobs.runtime(tester_job_id).get("import_in_progress") == job_id:
                            collection_service = self._report_collection_service
                            if collection_service is None:
                                collection_service = self._report_collection()
                            collection_service.finish_import(
                                tester_job_id,
                                job_id,
                                committed=document.get("state") == "COMMITTED",
                            )
                    else:
                        tester_runtime = self._panel_jobs.runtime(tester_job_id)
                        if tester_runtime.get("performance_v2_import_verified") is not False:
                            tester_runtime["performance_v2_import_verified"] = False
                            self._panel_jobs.sync(
                                tester_job_id, tester_snapshot, runtime=tester_runtime,
                                skip_save_if_unchanged=True,
                            )
                except PanelJobError:
                    pass
                except Exception:
                    _LOGGER.exception("collection import completion failed for job_id=%s", job_id)
            elif self._report_collection_service is not None:
                try:
                    self._report_collection_service.finish_import_job(
                        job_id,
                        committed=document.get("state") == "COMMITTED",
                    )
                except PanelJobError:
                    pass
                except Exception:
                    _LOGGER.exception("collection import completion failed for job_id=%s", job_id)
            with self._lock:
                self._collection_import_claim_ids.discard(job_id)
        try:
            performance_import = tracked.get("kind") == "strategies.performance.v2.import"
            if (
                performance_import
                and document.get("state") == "RUNNING"
                and not runtime
                and "inbox_ready" not in public
            ):
                self._panel_jobs.volatile_sync(job_id, public, expected=tracked)
                return
            terminal_performance = (
                performance_import
                and document.get("state") in {"COMMITTED", "FAILED"}
                and tracked.get("state") not in {"COMMITTED", "FAILED"}
            )
            sync_started: float | None = None
            if terminal_performance:
                try:
                    started = float(perf_counter())
                    sync_started = started if math.isfinite(started) else None
                except BaseException:
                    sync_started = None
            self._panel_jobs.sync(
                job_id,
                public,
                runtime=runtime or None,
                skip_save_if_unchanged=performance_import,
            )
            if terminal_performance and sync_started is not None:
                try:
                    finished = float(perf_counter())
                    if math.isfinite(finished):
                        _LOGGER.info(
                            "PANEL_TERMINAL_SYNC job_id=%s duration_seconds=%.6f",
                            job_id,
                            max(0.0, finished - sync_started),
                        )
                except Exception:
                    pass
        except PanelJobError:
            pass

    def _sync_tracked_panel_job(self, document: dict[str, object]) -> dict[str, object]:
        job_id, state = document.get("job_id"), document.get("state")
        if not isinstance(job_id, str) or not isinstance(state, str):
            return document
        try:
            tracked = self._panel_jobs.get(job_id)
        except PanelJobError:
            return document
        target = state if state in {"COMMITTED", "CANCELLED", "FAILED", "CANCELLING"} else "RUNNING"
        if tracked["state"] != target:
            try:
                self._panel_jobs.transition(job_id, target, phase=str(document.get("phase") or target))
            except PanelJobError:
                pass
        return document

    def _tracked_job_or_interrupted(
        self, job_id: str, status: Callable[[str], dict[str, object]]
    ) -> dict[str, object]:
        try:
            document = status(job_id)
            self._record_special_job(document)
            return self._sync_tracked_panel_job(document)
        except (KeyError, ValueError):
            return self._panel_jobs.get(job_id)

    def _local_testing_service(self) -> LocalTestingService:
        if self._local_testing_service_instance is not None:
            return self._local_testing_service_instance
        try:
            config = RunnerConfig.from_json(self.default_config)
        except Exception:
            raise PanelTestingError("invalid testing request") from None
        self._local_testing_service_instance = LocalTestingService(config, Path(__file__).resolve().parents[2])
        return self._local_testing_service_instance

    # -- BingX mode (docs/specs/2026-10-10-bingx-data-downloader.md, stage 2):
    # «Подготовить файлы» first tops up BingX data in a background thread, then
    # fills the tester with the API symbols resolved by that completed top-up.

    # -- «Пары и стратегии» main screen (docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract»):
    # actions run as one task of a TaskRunner at a time; views and the pair card read the pair registry.

    _main_screen_lock = threading.Lock()

    def main_screen(self) -> MainScreenService:
        with self._main_screen_lock:           # one service per controller: one main-screen task at a time
            service = getattr(self, "_main_screen_service", None)
            if service is None:
                repo = Path(__file__).resolve().parents[2]
                service = MainScreenService(repo, self.default_config, TaskRunner(repo), controller=self)
                self._main_screen_service = service
        return service

    def _main_task_running(self) -> bool:
        service = getattr(self, "_main_screen_service", None)
        return service is not None and service.task()["task"].get("state") == "running"

    def _registry_finalist_decisions(self, connection: duckdb.DuckDBPyConnection) -> dict[int, tuple] | None:
        """The pair-registry marks as finalist decisions of this PerformanceDB (ADR-0080): strategy_id -> (status,
        rank, None), matched by strategy name; None without a registry (the selection reviews decide)."""
        from .pair_registry.records import database_exchange, finalist_marks
        from .pair_registry.store import DATABASE, RegistryError, load_registry_settings

        from .pair_registry.records import registry_decides

        try:
            registry = load_registry_settings(self.default_config).root / DATABASE
        except RegistryError:
            return None
        if not registry.is_file():
            return None
        exchange = database_exchange(connection)
        try:
            with duckdb.connect(str(registry), read_only=True) as con:
                if not registry_decides(con, exchange):
                    return None                  # before the one-time seed the reviews still decide
                marks = finalist_marks(con, exchange)
        except (duckdb.IOException, duckdb.ConnectionException) as error:
            raise FinalistRetestError("REGISTRY_BUSY", "the pair registry is busy: retry") from error
        except duckdb.Error as error:
            raise FinalistRetestError("REGISTRY_UNAVAILABLE", "the pair registry cannot be read") from error
        names = dict(connection.execute(
            "select strategy_name, strategy_id from strategies where lifecycle_status = 'ACTIVE'").fetchall())
        return {int(names[name]): (mark["status"], mark["user_rank"], None)
                for name, mark in marks.items() if name in names}

    def _registry_decides_for(self, target: Path) -> bool:
        """True when the pair registry decides the finalists of this PerformanceDB's exchange (ADR-0080): then the
        old review imports that set user decisions are refused, the marks are set in the «Стратегии» view."""
        if not Path(target).is_file():
            return False
        try:
            with duckdb.connect(str(target), read_only=True) as connection:
                return self._registry_finalist_decisions(connection) is not None
        except FinalistRetestError:
            return True                          # registry busy or unreadable: refuse rather than write reviews
        except duckdb.Error:
            return False

    def _trade_store_root(self) -> Path:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise StoreReadyError("конфиг не читается") from None
        root = (document.get("trade_store") or {}).get("root") if isinstance(document, dict) else None
        if not isinstance(root, str) or not root.strip():
            raise StoreReadyError("в config не задан trade_store.root")
        return Path(root)

    # -- MRS3 tick test from a store READY folder (docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 1b
    # contract»): the folder is re-rendered for the active PerformanceDB and published as a generation manifest
    # under Output/store-ready/, which the unchanged SINGLE_MODE tick test runs.

    def store_ready_catalog(self) -> dict[str, object]:
        return {"folders": list_ready_folders(self._trade_store_root())}

    _store_ready_lock = threading.Lock()

    def store_ready_prepare(self, payload: Mapping[str, object], *, strategies: Sequence[str] | None = None) -> dict[str, object]:
        if set(payload) != {"ready"}:
            raise StoreReadyError("запрос: только поле ready")
        folder = resolve_ready_folder(self._trade_store_root(), payload["ready"])
        exchange = self._ready_exchange(folder)
        target = self._evaluation_target(exchange)       # the folder's exchange (Panel stage 5e-3a)
        api_symbols = self._bingx_api_symbols() if exchange != "bybit" else None
        with self._store_ready_lock:                 # one preparation at a time: no race on the batch folder
            result = prepare_tick_batch(folder, self.root / "Output" / "store-ready",
                                        self._ready_strategy_template(target, "LONG"), target, strategies,
                                        api_symbols=api_symbols)
        self._fresh_strategy_manifests[str(result["analysis_run_id"])] = Path(str(result["manifest_path"])).resolve()
        return {"ready": folder.name, **{key: result[key] for key in
                                         ("analysis_run_id", "strategy_count", "pairs", "ready_pairs", "reused")}}

    @staticmethod
    def _ready_exchange(folder: Path) -> str:
        try:
            document = json.loads((Path(folder) / "ready_manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise StoreReadyError("ready_manifest.json повреждён") from None
        return str(document.get("exchange") or "bybit").lower() if isinstance(document, dict) else "bybit"

    def _bingx_api_symbols(self) -> dict[str, str]:
        """The BingX API symbol of every BingX pair of the registry (read-only), for the tick batch (5e-3a)."""
        from .pair_registry.history import _registry_connection

        con = _registry_connection(self.default_config, None)
        if con is None:
            raise StoreReadyError("реестр пар недоступен: нужен для API-символов BingX")
        with con:
            return {str(symbol): str(api) for symbol, api in con.execute(
                "select symbol, api_symbol from pairs where exchange = 'bingx' and api_symbol is not null").fetchall()}

    def _bingx_data_preparer(self, manifest: Path, start_date: str, end_date: str, tester_template: Path):
        """The BingX minute days of every strategy of the batch over warm-up and window, ensured and verified before
        the tester lock (the tester does not download BingX data); returns the problem text or None (5e-3a)."""
        from .pair_registry.bingx import human_ticker
        from .pair_registry.minute_days import ensure_bingx_days_shared
        from .pair_registry.store import RegistryError, load_registry_settings

        validated = validate_strategy_manifest(Path(manifest))
        symbols: set[str] = set()
        for path in Path(validated.strategy_source).glob("*.json"):
            basic = json.loads(path.read_text(encoding="utf-8")).get("basic") or {}
            if isinstance(basic.get("symbol"), str):
                symbols.add(basic["symbol"])
        warmup = int(json.loads(Path(tester_template).read_text(encoding="utf-8")).get("WarmupDays", 0) or 0)
        first = date.fromisoformat(start_date) - timedelta(days=warmup)
        last = date.fromisoformat(end_date)
        listed: dict[str, date] = {}                    # no candles exist before the listing day: not required
        con = None
        try:
            from .pair_registry.history import _registry_connection

            con = _registry_connection(self.default_config, None)
        except Exception:
            con = None
        if con is not None:
            with con:
                for api_symbol, listing_ms in con.execute(
                        "select api_symbol, listing_ms from pairs where exchange = 'bingx' and listing_ms is not null"
                ).fetchall():
                    listed[str(api_symbol)] = datetime.fromtimestamp(int(listing_ms) / 1000, timezone.utc).date()

        def prepare() -> str | None:
            try:
                settings = load_registry_settings(self.default_config)
            except RegistryError:
                return "MISSING_DAYS: the pair registry settings are unreadable"
            for api_symbol in sorted(symbols):
                symbol = human_ticker(api_symbol)
                if symbol is None:
                    return f"MISSING_DAYS: {api_symbol} is not a BingX API symbol"
                try:
                    days = ensure_bingx_days_shared(settings, symbol, first, last, api_symbol=api_symbol)
                except (RegistryError, OSError, duckdb.Error) as error:
                    return f"MISSING_DAYS: {symbol}: {type(error).__name__}"
                accepted = set(days.accepted)
                start = max(first, listed.get(api_symbol, first))
                absent = [start + timedelta(days=offset) for offset in range((last - start).days + 1)
                          if start + timedelta(days=offset) not in accepted]
                if absent:
                    return f"MISSING_DAYS: {symbol} {[day.isoformat() for day in absent][:10]}"
            return None

        return prepare

    def _workflow_default(self, name: str, *, side: str | None = None) -> Path:
        try:
            document = json.loads(self.default_config.read_text(encoding="utf-8"))
            workflow = document.get("panel_workflow", {})
            value = workflow.get(name)
            if name == "strategy_templates" and isinstance(value, Mapping) and side is not None:
                value = value.get(side.upper())
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            return self._path(value)
        except Exception:
            raise ValueError("panel workflow default is unavailable") from None

    def _reconcile_interrupted_tester_jobs(self) -> None:
        """Restore a batch only from its durable completed inbox, never its progress counter."""
        try:
            config = RunnerConfig.from_json(self.default_config)
            inbox_root = Path(config.inbox_root).resolve()
        except Exception:
            return
        for job in self._panel_jobs.list():
            if job.get("kind") == "strategies.performance.v2.finalist-retest":
                job_id = job.get("job_id")
                if isinstance(job_id, str) and (
                    job.get("state") not in {"COMMITTED", "CANCELLED", "FAILED"}
                    or (
                        job.get("error") == {"code": "INTERRUPTED"}
                        and job.get("phase") != "FAILED"
                    )
                ):
                    try:
                        self._panel_jobs.sync(
                            job_id,
                            {"state": "FAILED", "phase": "FAILED", "error": {"code": "INTERRUPTED"}},
                        )
                    except PanelJobError:
                        pass
                continue
            if job.get("kind") not in {"strategies.tester", "strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry", "strategies.tester.runs"}:
                continue
            job_id = job.get("job_id")
            if not isinstance(job_id, str):
                continue
            if job.get("state") == "COMMITTED" and job.get("inbox_ready") is not True:
                try:
                    runtime = self._panel_jobs.runtime(job_id)
                    inbox = Path(runtime["inbox_path"]).resolve()
                    inbox.relative_to(inbox_root)
                    is_single_mode = job.get("kind") in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}
                    if is_single_mode:
                        self._validate_metadata_inbox(inbox)
                    else:
                        self._validate_performance_inbox(inbox)
                    self._panel_jobs.sync(
                        job_id,
                        {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
                        runtime={"inbox_path": str(inbox)},
                    )
                except (KeyError, OSError, TypeError, ValueError, PanelJobError):
                    pass
                continue
            if job.get("state") != "FAILED":
                continue
            state_path = inbox_root / f"{job_id}.state.json"
            if (
                job.get("kind") in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}
                and job.get("error") == {"code": "INTERRUPTED"}
                and not state_path.exists()
            ):
                inbox = (inbox_root / job_id).resolve()
                try:
                    inbox.relative_to(inbox_root)
                    self._validate_metadata_inbox(inbox)
                    self._panel_jobs.recover_committed(job_id, runtime={"inbox_path": str(inbox)})
                    self._panel_jobs.sync(
                        job_id,
                        {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
                        runtime={"inbox_path": str(inbox)},
                    )
                    continue
                except (OSError, TypeError, ValueError, PanelJobError):
                    pass
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                inbox = Path(state["inbox_path"]).resolve()
                manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
                expected = state["expected_names"]
                entries = manifest["entries"]
                if (
                    state.get("state") != "COMPLETED"
                    or not isinstance(expected, list)
                    or not isinstance(entries, list)
                    or {entry.get("strategy_name") for entry in entries if isinstance(entry, dict)} != set(expected)
                    or len(entries) != len(expected)
                    or manifest.get("schema_version") != 1
                ):
                    continue
                inbox.relative_to(inbox_root)
                self._validate_performance_inbox(inbox)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            try:
                self._panel_jobs.recover_committed(job_id, runtime={"inbox_path": str(inbox)})
            except PanelJobError:
                pass
            continue
        self._resume_interrupted_tester_finalization()

    def _resume_interrupted_tester_finalization(self) -> None:
        """Finish only an already verified batch; never resubmit a strategy after restart."""
        try:
            config = RunnerConfig.from_json(self.default_config)
        except Exception:
            return
        for job in self._panel_jobs.list():
            if job.get("kind") not in {"strategies.tester", "strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"} or job.get("error") != {"code": "INTERRUPTED"}:
                continue
            job_id = job.get("job_id")
            if not isinstance(job_id, str):
                continue
            state_path = Path(config.inbox_root) / f"{job_id}.state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                source = Path(state["strategy_source"])
                output = Path(state["output_csv"])
                provenance = state["v6_provenance"]
                retry_incomplete_capture = (
                    state.get("state") == "FAILED"
                    and isinstance(state.get("error"), str)
                    and state["error"].startswith("InboxCaptureError: inbox already exists:")
                )
                if state.get("state") != "STOPPED_FOR_CLEANUP" and not retry_incomplete_capture:
                    continue
                if not isinstance(provenance, Mapping):
                    continue
                if plan_batch(config, source, output, hydrate_resume=True).resume_remaining_names:
                    continue
                inbox_root = Path(config.inbox_root).resolve()
                incomplete_inbox = (inbox_root / job_id).resolve()
                incomplete_inbox.relative_to(inbox_root)
                if incomplete_inbox.exists():
                    if (incomplete_inbox / "inbox_manifest.json").is_file():
                        continue
                    shutil.rmtree(incomplete_inbox)
                self._panel_jobs.recover_running(job_id)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, PanelJobError):
                continue
            threading.Thread(
                target=self._finish_interrupted_tester_inbox,
                args=(job_id, config, source, output, dict(provenance)),
                name="mrs3-panel-tester-inbox-recovery",
                daemon=True,
            ).start()

    def _finish_interrupted_tester_inbox(
        self, job_id: str, config: RunnerConfig, source: Path, output: Path, provenance: dict[str, object]
    ) -> None:
        try:
            result = run_batch(config, source, output, provenance=provenance)
            inbox = Path(result.inbox_path).resolve()
            self._panel_jobs.recover_committed(job_id, runtime={"inbox_path": str(inbox)})
        except Exception:
            try:
                self._panel_jobs.sync(job_id, {"state": "FAILED", "phase": "FAILED", "error": {"code": "INBOX_RECOVERY_FAILED"}})
            except PanelJobError:
                pass

    def _fresh_strategy_manifest(self, analysis_id: str | None = None) -> Path:
        candidates = []
        if analysis_id and analysis_id in self._fresh_strategy_manifests:
            candidates.append(self._fresh_strategy_manifests[analysis_id])
        generated_root = self.root / "Output" / "fresh-shortlist-v2"
        try:
            generated = [
                path for path in generated_root.glob("*/*/strategy_manifest.json")
                if path.is_file() and not path.is_symlink()
                and not path.parent.is_symlink()
                and re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{32}", path.parent.name)
                and (analysis_id is None or path.parent.parent.name == analysis_id)
            ]
            store_ready = [
                path for path in (self.root / "Output" / "store-ready").glob("*/strategy_manifest.json")
                if path.is_file() and not path.is_symlink() and not path.parent.is_symlink()
                and re.fullmatch(r"[0-9a-f]{64}", path.parent.name)
                and (analysis_id is None or path.parent.name == analysis_id)
            ]
            candidates.extend(sorted([*generated, *store_ready], key=lambda path: path.stat().st_mtime_ns, reverse=True))
        except OSError:
            pass
        candidates.extend((
            self.root / "Output" / "strategy_manifest.json",
            self.root / "Output" / "strategies" / "strategy_manifest.json",
        ))
        for candidate in candidates:
            try:
                validated = validate_strategy_manifest(candidate)
            except StrategyBatchValidationError:
                continue
            if analysis_id is not None and validated.analysis_run_id != analysis_id:
                continue
            self._fresh_strategy_manifests[validated.analysis_run_id] = validated.manifest_path
            return validated.manifest_path
        raise ValueError("fresh strategy batch is not available")

    def strategies_fresh_batch(self) -> dict[str, object]:
        manifest = self._fresh_strategy_manifest()
        validated = validate_strategy_manifest(manifest)
        hashes = validated.provenance.get("strategy_json_sha256", {})
        return {
            "phase": "COMMITTED",
            "analysis_run_id": validated.analysis_run_id,
            "strategy_count": len(hashes) if isinstance(hashes, Mapping) else 0,
        }

    def _strategy_batch(self) -> LocalStrategyBatchService:
        if self._strategy_batch_service is None:
            self._strategy_batch_service = LocalStrategyBatchService(
                RunnerConfig.from_json(self.default_config),
                on_update=self._record_special_job,
                tester_config_template=mrs3_tester_config_template(self.root),
            )
        return self._strategy_batch_service

    def _ready_strategy_template(self, target: EvaluationTarget, side: str) -> Path:
        if target.strategy_template is not None:
            return Path(target.strategy_template)
        return self._workflow_default("strategy_templates", side=side)

    def _evaluation_target(self, exchange: str | None = None) -> EvaluationTarget:
        """Protocol v2 target of the active PerformanceDB; refuses before any MRS3 render or launch (§7a).

        `exchange` (Panel stage 5e-3a, ADR-0083: one database holds both exchanges) takes that exchange's profile
        instead of the configured one; None keeps the configuration's exchange."""
        config = self._performance_v2_config()
        configured = str(getattr(config, "exchange", None) or "").casefold()
        if exchange is not None and configured and exchange.casefold() != configured \
                and getattr(config, "evaluation_protocol", "v1") == "v2":
            try:
                chosen = load_exchange_profile(self.root, exchange).exchange
            except ExchangeProfileError as error:
                raise EvaluationProtocolError(error.code, error.message) from error
            config = replace(config, exchange=chosen)
        profile = self._exchange_profile(config)
        return evaluation_target(
            config, profile.mrs3_tester if profile is not None else mrs3_tester_config_template(self.root), profile,
            project_root=self.root,
        )

    def _exchange_profile(self, config: object) -> ExchangeProfile | None:
        """Templates of the exchange of the active protocol v2 database (P7 spec §3); None for a v1 database."""
        exchange = getattr(config, "exchange", None)
        if getattr(config, "evaluation_protocol", "v1") != "v2" or not exchange:
            return None
        try:
            return load_exchange_profile(self.root, exchange)
        except ExchangeProfileError as error:
            raise EvaluationProtocolError(error.code, error.message) from error

    def _mrs3_tester_template(self, target: EvaluationTarget) -> Path:
        """MRS3 tester template of the exchange of `target` (P7 spec §4)."""
        return target.tester_template or mrs3_tester_config_template(self.root)

    def _mrs3_strategy_templates(self, target: EvaluationTarget) -> Mapping[str, object]:
        """MRS3 strategy template of the exchange for both sides (P7 spec §4).

        A target without exchange templates (tests) keeps `panel_workflow.strategy_templates`.
        """
        if target.strategy_template is not None:
            return {"LONG": str(target.strategy_template), "SHORT": str(target.strategy_template)}
        templates = self._workflow_defaults().get("strategy_templates", {})
        return templates if isinstance(templates, Mapping) else {}

    def _listing_dates_source(self, exchange: str | None = None) -> tuple[Path, str | None]:
        """Listing dates: the file of `panel_workflow`, the sheet of the exchange (P7 spec §4) — of `exchange` when
        given (a BingX tick job, Panel stage 5e-3a), else of the active database's exchange."""
        configured = str(getattr(self._performance_v2_config(), "exchange", None) or "").casefold()
        if exchange is not None and configured and exchange.casefold() != configured:
            try:
                sheet = load_exchange_profile(self.root, exchange).listing_dates_sheet
            except ExchangeProfileError as error:
                raise EvaluationProtocolError(error.code, error.message) from error
            return self._workflow_default("listing_dates_path"), sheet
        profile = self._exchange_profile(self._performance_v2_config())
        return self._workflow_default("listing_dates_path"), None if profile is None else profile.listing_dates_sheet

    @staticmethod
    def _protocol_v2_runtime(target: EvaluationTarget) -> dict[str, object]:
        """Runtime marker of an MRS3 job rendered under protocol v2 for one base and exchange."""
        return {
            "evaluation_protocol": "v2",
            "evaluation_base": format(target.base.normalize(), "f"),
            "evaluation_exchange": target.exchange,
            # P7 spec §4: a switch of the database or a profile change starts jobs anew.
            "evaluation_database": target.database,
            "evaluation_profile": target.profile_digest or "none",
        }

    @classmethod
    def _matches_protocol_v2(cls, runtime: Mapping[str, object], target: EvaluationTarget) -> bool:
        return all(runtime.get(key) == value for key, value in cls._protocol_v2_runtime(target).items())

    def _require_import_target(self, tester_job_id: str, is_collection_job: bool) -> str | None:
        """Reports rendered for another database or profile do not enter this one (P7 spec §4).

        A collection is checked through every current member; jobs without a v2 marker import as before.
        """
        runtime = self._panel_jobs.runtime(tester_job_id)
        runtimes = [runtime]
        members = runtime.get("members") if is_collection_job else None
        for member in members if isinstance(members, list) else ():
            if not isinstance(member, Mapping) or member.get("superseded") is True:
                continue
            try:
                runtimes.append(self._panel_jobs.runtime(str(member.get("tester_job_id"))))
            except PanelJobError:
                continue
        marked = [item for item in runtimes if item.get("evaluation_protocol") == "v2"]
        if not marked:
            return None
        # ADR-0083: one database holds both exchanges; a job is checked against the target of its own exchange
        exchanges = {str(item.get("evaluation_exchange") or "") for item in marked}
        if len(exchanges) != 1:
            raise EvaluationProtocolError("IMPORT_EXCHANGE_MIXED", "the collection holds jobs of different exchanges")
        exchange = exchanges.pop() or None
        target = self._evaluation_target(exchange)
        if any(not self._matches_protocol_v2(item, target) for item in marked):
            raise EvaluationProtocolError(
                "IMPORT_TARGET_CHANGED", "the tester job was rendered for another PerformanceDB or exchange profile",
            )
        return exchange

    def _require_protocol_v2_tester_template(self, target: EvaluationTarget) -> None:
        try:
            document = json.loads(self._mrs3_tester_template(target).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvaluationProtocolError("PROTOCOL_V2_TESTER_CONFIG_REJECTED", "MRS3 tester template is unreadable") from error
        require_protocol_v2_tester_config(document if isinstance(document, Mapping) else {}, target)

    def _require_protocol_v2_strategy_dir(self, directory: Path, target: EvaluationTarget) -> None:
        for path in sorted(Path(directory).glob("*.json")):
            if path.name == "strategy_manifest.json":
                continue
            try:
                strategy = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise EvaluationProtocolError("PROTOCOL_V2_RENDER_REJECTED", f"strategy {path.name} is unreadable") from error
            require_protocol_v2_strategy(strategy if isinstance(strategy, Mapping) else {}, target)

    def _require_protocol_v2_manifest(self, manifest_path: Path, target: EvaluationTarget) -> None:
        """Every strategy of a READY/RETEST manifest and the tester template meet protocol v2 before launch."""
        self._require_protocol_v2_tester_template(target)
        validated = validate_strategy_manifest(Path(manifest_path))
        self._require_protocol_v2_strategy_dir(validated.strategy_source, target)

    def _require_protocol_v2_runs(self, target: EvaluationTarget) -> None:
        """Published RUNS snapshots (strategy and embedded tester config) meet protocol v2 before launch."""
        runner = RunnerConfig.from_json(self.default_config)
        runs = Path(runner.bot_root) / "tester" / "runs"
        for path in sorted(runs.glob("*.json")) if runs.is_dir() else ():
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                settings = snapshot["settings"]
                strategy, tester_config = settings[0], snapshot["tester_config"]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                raise EvaluationProtocolError("PROTOCOL_V2_RENDER_REJECTED", f"RUNS snapshot {path.name} is unreadable") from error
            require_protocol_v2_strategy(strategy, target)
            require_protocol_v2_tester_config(tester_config, target)

    def _runs_batch(self) -> LocalRunsBatchService:
        if self._runs_batch_service is None:
            self._runs_batch_service = LocalRunsBatchService(RunnerConfig.from_json(self.default_config), on_update=self._record_special_job)
        return self._runs_batch_service

    def _single_mode_strategy_test(self) -> LocalSingleModeStrategyTestService:
        if self._single_mode_strategy_test_service is None:
            self._single_mode_strategy_test_service = LocalSingleModeStrategyTestService(
                RunnerConfig.from_json(self.default_config),
                on_update=self._record_special_job,
                tester_config_template=mrs3_tester_config_template(self.root),
            )
        return self._single_mode_strategy_test_service

    def _report_collection(self, *, live_import_job_ids: Sequence[str] = ()) -> PanelReportCollection:
        with self._lock:
            if self._report_collection_service is None:
                config = RunnerConfig.from_json(self.default_config)
                self._report_collection_service = PanelReportCollection(
                    self._panel_jobs,
                    inbox_root=Path(config.inbox_root),
                    report_root=Path(config.report_dir),
                    trusted_strategy_root=self._output_strategy_root(),
                )
        active_import_job_ids: set[str] = set()
        if self._performance_v2_jobs is not None:
            active_job_ids = getattr(self._performance_v2_jobs, "active_job_ids", None)
            if not callable(active_job_ids):
                _LOGGER.warning("collection reconciliation deferred: active import job probe unavailable")
                return self._report_collection_service
            try:
                active_import_job_ids = set(active_job_ids())
            except Exception:
                _LOGGER.exception("collection reconciliation deferred: active import job probe failed")
                return self._report_collection_service
        with self._lock:
            for job_id in tuple(self._collection_import_claim_ids):
                try:
                    state = self._panel_jobs._peek(job_id).get("state")
                except PanelJobError:
                    state = None
                if state is None or state in TERMINAL:
                    self._collection_import_claim_ids.discard(job_id)
            live = active_import_job_ids.union(self._collection_import_claim_ids, live_import_job_ids)
            self._report_collection_service.reconcile_import_claims(live_import_job_ids=live)
        return self._report_collection_service

    @staticmethod
    def _manifest_strategy_names(manifest: Path) -> tuple[str, ...]:
        validated = validate_strategy_manifest(manifest)
        hashes = validated.provenance.get("strategy_json_sha256")
        if not isinstance(hashes, Mapping):
            raise ValueError("strategy manifest names are unavailable")
        names = tuple(Path(name).stem for name in hashes)
        if not names or len({name.casefold() for name in names}) != len(names):
            raise ValueError("strategy manifest names are invalid")
        return names

    def strategies_tester_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        unexpected = set(payload).difference({"analysis_run_id", "start_date", "end_date", "test_start", "test_end", "collect_reports"})
        if unexpected:
            raise ValueError("tester start request contains unsupported fields")
        collect_reports = payload.get("collect_reports", False)
        if type(collect_reports) is not bool:
            raise ValueError("collect_reports must be a boolean")
        analysis_id = self._required(payload, "analysis_run_id")
        start_date = payload.get("start_date", payload.get("test_start"))
        end_date = payload.get("end_date", payload.get("test_end"))
        if "start_date" in payload and "test_start" in payload and payload["start_date"] != payload["test_start"]:
            raise ValueError("start_date and test_start disagree")
        if "end_date" in payload and "test_end" in payload and payload["end_date"] != payload["test_end"]:
            raise ValueError("end_date and test_end disagree")
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date are required")
        initial_balance = self._requested_initial_balance(payload)
        manifest = self._fresh_strategy_manifest(analysis_id)
        exchange = self._manifest_exchange(manifest)         # the batch's exchange (Panel stage 5e-3a)
        evaluation = self._evaluation_target(exchange)
        self._require_protocol_v2_manifest(Path(manifest), evaluation)
        expected_names = self._manifest_strategy_names(manifest) if collect_reports else ()
        request = {"analysis_run_id": analysis_id, "start_date": start_date, "end_date": end_date, "initial_balance": initial_balance, "mode": "SINGLE_MODE", "collect_reports": collect_reports}
        single_mode_service = self._single_mode_strategy_test()
        tester_template = self._mrs3_tester_template(evaluation)
        reconcile = getattr(single_mode_service, "reconcile_pending_cleanup", None)
        if callable(reconcile):
            reconcile()

        def register(job_id: str) -> None:
            collection_id = self._report_collection().register(job_id, expected_names)
            runtime = self._panel_jobs.runtime(job_id)
            runtime["report_collection_id"] = collection_id
            self._panel_jobs.sync(job_id, {"state": "RUNNING", "phase": "RUNNING"}, runtime=runtime)

        preparer = (self._bingx_data_preparer(Path(manifest), start_date, end_date, tester_template)
                    if exchange == "bingx" else None)
        return self._start_tracked_panel_job(
            "strategies.tester.start", request,
            ("strategies.tester",),
            lambda job_id: single_mode_service.start(
                manifest,
                analysis_run_id=analysis_id,
                start_date=start_date,
                end_date=end_date,
                job_id=job_id,
                tester_config_template=tester_template,
                **({"initial_balance": initial_balance} if initial_balance is not None else {}),
                **({"prepare_data": preparer} if preparer is not None else {}),
            ),
            runtime=self._protocol_v2_runtime(evaluation),
            before_start=register if collect_reports else None,
        )

    @staticmethod
    def _manifest_exchange(manifest: Path) -> str | None:
        """The exchange of a generation manifest (store READY batches name it), lower case; None when absent."""
        try:
            document = json.loads(Path(manifest).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        value = document.get("exchange") if isinstance(document, dict) else None
        return value.lower() if isinstance(value, str) and value.strip() else None

    def strategies_tester_retry(self, payload: Mapping[str, object]) -> dict[str, object]:
        if set(payload) != {"job_id"}:
            raise ValueError("tester retry request contains unsupported fields")
        source_job_id = self._required(payload, "job_id")
        source = self._panel_jobs.get(source_job_id)
        if source.get("kind") not in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}:
            raise PanelJobError("UNSUPPORTED_ROUTE")
        source_runtime = self._panel_jobs.runtime(source_job_id)
        if source.get("retest") is True or source_runtime.get("retest") is True:
            raise PanelJobError("RETEST_RETRY_NOT_ALLOWED")
        if source.get("state") not in {"FAILED", "CANCELLED"}:
            raise PanelJobError("TESTER_JOB_NOT_RECOVERABLE")
        if str(source_runtime.get("evaluation_exchange") or "").casefold() == "bingx":
            # Panel stage 5e-3a: a BingX job needs its minute days ensured first; it is started anew, not retried
            raise PanelJobError("RETRY_BINGX_START_ANEW")
        evaluation = self._evaluation_target()
        if not self._matches_protocol_v2(source_runtime, evaluation):
            # Protocol v2 §7a: a job rendered before P6 or for another base/exchange is started anew.
            raise PanelJobError("RETRY_PRE_PROTOCOL_V2")
        self._require_protocol_v2_tester_template(evaluation)
        tester_template = self._mrs3_tester_template(evaluation)

        def register(job_id: str) -> None:
            runtime = self._panel_jobs.runtime(source_job_id)
            collection_id = runtime.get("report_collection_id")
            if not isinstance(collection_id, str) or not collection_id:
                return
            source_member = self._report_collection()._member(
                self._report_collection()._runtime(self._panel_jobs.get(collection_id)), source_job_id
            )
            if source_member is None:
                raise PanelJobError("COLLECTION_RETRY_SOURCE_NOT_REGISTERED")
            new_collection_id = self._report_collection().register(
                job_id,
                source_member.get("expected_strategy_names", ()),
                replaces_job_id=source_job_id,
            )
            new_runtime = self._panel_jobs.runtime(job_id)
            new_runtime["report_collection_id"] = new_collection_id
            self._panel_jobs.sync(job_id, {"state": "RUNNING", "phase": "RUNNING"}, runtime=new_runtime)

        return self._start_tracked_panel_job(
            "strategies.tester.retry",
            {"source_job_id": source_job_id},
            ("strategies.tester",),
            lambda job_id: self._single_mode_strategy_test().retry(
                source_job_id, job_id=job_id, tester_config_template=tester_template,
            ),
            runtime=self._protocol_v2_runtime(evaluation),
            before_start=register,
        )

    def strategies_tester_runs_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        if payload:
            raise ValueError("tester RUNS request contains unsupported fields")
        self._require_protocol_v2_runs(self._evaluation_target())
        try:
            return self._start_tracked_panel_job(
                "strategies.tester.runs", {}, ("strategies.tester",),
                lambda job_id: self._runs_batch().start(job_id),
            )
        except ValueError as error:
            if str(error) == "RUNS_EMPTY":
                raise PanelJobError("RUNS_EMPTY") from None
            raise

    def strategies_tester_status(self, job_id: str) -> dict[str, object]:
        tracked = self._panel_jobs.get(job_id)
        if tracked.get("kind") in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}:
            status = self._single_mode_strategy_test().status
        elif tracked.get("kind") == "strategies.tester.runs":
            status = self._runs_batch().status
        else:
            status = self._strategy_batch().status
        result = self._tracked_job_or_interrupted(job_id, status)
        if result.get("state") == "COMMITTED" and isinstance(result.get("inbox_path"), str):
            self._strategy_batch_inboxes[job_id] = Path(result["inbox_path"])
            result["inbox_ready"] = True
        is_retest_native = tracked.get("kind") == "strategies.tester.native.start" and self._panel_jobs.runtime(job_id).get("retest") is True
        if is_retest_native:
            runtime = self._panel_jobs.runtime(job_id)
            if runtime.get("retest") is True and isinstance(runtime.get("inbox_path"), str):
                result["inbox_path"] = runtime["inbox_path"]
        return result if is_retest_native else {key: value for key, value in result.items() if key != "inbox_path"}

    def strategies_tester_verify_inbox(self, job_id: str) -> dict[str, object]:
        tracked = self._panel_jobs.get(job_id)
        runtime = self._panel_jobs.runtime(job_id)
        if tracked.get("kind") == "strategies.performance.v2.finalist-retest":
            return self._capture_bulk_finalist_retest_inbox(job_id, tracked=tracked, runtime=runtime)
        config = RunnerConfig.from_json(self.default_config)
        inbox_root = Path(config.inbox_root).resolve()
        if tracked.get("kind") == "strategies.tester.collection":
            return {
                "job_id": job_id,
                "inbox_path": str(self._report_collection().verify(job_id)),
                **self._report_collection().status(),
            }
        if (
            tracked.get("kind") in {"strategies.tester.start", "strategies.tester.retry"}
            and tracked.get("state") == "COMMITTED"
        ):
            inbox = self._owned_single_mode_inbox(inbox_root, job_id)
            if inbox is not None:
                runtime.update({
                    "inbox_path": str(inbox),
                    "mode": "SINGLE_MODE",
                    "performance_v2_import_verified": True,
                })
                self._panel_jobs.sync(
                    job_id,
                    {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
                    runtime=runtime,
                )
                self._strategy_batch_inboxes[job_id] = inbox
                return self._panel_jobs.get(job_id)
        is_retest_native = (
            tracked.get("kind") == "strategies.tester.native.start"
            and (tracked.get("retest") is True or runtime.get("retest") is True)
        )
        if (
            is_retest_native
            and tracked.get("state") == "COMMITTED"
            and (tracked.get("inbox_ready") is True or runtime.get("inbox_ready") is True)
        ):
            raw_inbox = runtime.get("inbox_path")
            try:
                if (
                    tracked.get("inbox_ready") is not True
                    and runtime.get("inbox_ready") is not True
                ):
                    raise ValueError("committed RETEST inbox is not ready")
                if not isinstance(raw_inbox, str) or not raw_inbox.strip():
                    raise ValueError(_RETEST_INBOX_PATH_UNAVAILABLE)
                inbox = Path(raw_inbox).resolve()
                if inbox == inbox_root:
                    raise ValueError(_RETEST_INBOX_PATH_UNAVAILABLE)
                try:
                    inbox.relative_to(inbox_root)
                except ValueError as error:
                    raise ValueError(_RETEST_INBOX_PATH_UNAVAILABLE) from error
                manifest_path = inbox / "inbox_manifest.json"
                if not inbox.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
                    raise ValueError(_RETEST_INBOX_MANIFEST_UNAVAILABLE)
                if manifest_path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("committed RETEST inbox manifest is too large")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entries = manifest.get("entries") if isinstance(manifest, Mapping) else None
                expected = manifest.get("expected_strategy_names") if isinstance(manifest, Mapping) else None
                entry_names = [
                    entry.get("strategy_name")
                    for entry in entries
                    if isinstance(entry, Mapping)
                ] if isinstance(entries, list) else []
                if (
                    not isinstance(manifest, Mapping)
                    or manifest.get("schema_version") != 1
                    or manifest.get("run_mode") != "SINGLE_MODE"
                    or manifest.get("source_mode") != "metadata_only"
                    or manifest.get("inbox_ready") is not True
                    or not isinstance(expected, list)
                    or not expected
                    or not isinstance(entries, list)
                    or len(entries) != len(expected)
                    or any(not isinstance(name, str) or not name for name in expected)
                    or len(set(expected)) != len(expected)
                    or set(entry_names) != set(expected)
                    or len(set(entry_names)) != len(entry_names)
                    or any(
                        not isinstance(entry, Mapping)
                        or any(
                            not isinstance(entry.get(field), str) or not entry[field].strip()
                            for field in ("strategy_path", "report_path")
                        )
                        for entry in entries
                    )
                    or ("batch_id" in manifest and manifest.get("batch_id") != job_id)
                ):
                    raise ValueError("committed RETEST inbox manifest is invalid")
            except (OSError, RuntimeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                if str(error) in _RETEST_INBOX_UNAVAILABLE:
                    raise PerformanceV2ApiError(
                        "RETEST_SOURCE_ARTIFACTS_UNAVAILABLE",
                        status=409,
                        message="committed RETEST inbox source artifacts are unavailable",
                    ) from error
                raise ValueError(str(error)) from None
            try:
                self._validate_metadata_inbox(inbox)
            except ValueError as error:
                if str(error) in {
                    "metadata inbox is incomplete: strategy_path is missing",
                    "metadata inbox is incomplete: report_path is missing",
                }:
                    raise PerformanceV2ApiError(
                        "RETEST_SOURCE_ARTIFACTS_UNAVAILABLE",
                        status=409,
                        message="committed RETEST inbox source artifacts are unavailable",
                    ) from error
                raise
            self._strategy_batch_inboxes[job_id] = inbox
            status = self._panel_jobs.get(job_id)
            status["inbox_path"] = str(inbox)
            status["inbox_ready"] = True
            return status
        is_single_mode = tracked.get("kind") in {"strategies.tester.start", "strategies.tester.native.start", "strategies.tester.retry"}
        if is_single_mode:
            service = self._single_mode_strategy_test()
            inbox = (
                service.capture_inbox(job_id, force_single_mode=True)
                if tracked.get("kind") in {"strategies.tester.native.start", "strategies.tester.retry"}
                else service.capture_inbox(job_id)
            )
            service.mark_inbox_ready(job_id, inbox)
            self._strategy_batch_inboxes[job_id] = inbox
            runtime = {
                "inbox_path": str(inbox),
                "mode": "SINGLE_MODE",
                "performance_v2_import_verified": True,
            }
            state = self._panel_jobs.get(job_id)["state"]
            if state == "FAILED":
                self._panel_jobs.recover_running(job_id)
                state = "RUNNING"
            if state == "RUNNING":
                self._panel_jobs.sync(
                    job_id,
                    {"state": "RUNNING", "phase": "RECOVERING_INBOX", "inbox_ready": True},
                    runtime=runtime,
                )
                self._panel_jobs.recover_committed(job_id, runtime=runtime)
            elif state == "COMMITTED":
                self._panel_jobs.sync(
                    job_id,
                    {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
                    runtime=runtime,
                )
            else:
                raise ValueError("tester job is not ready for inbox verification")
            return self.strategies_tester_status(job_id)
        inbox = (inbox_root / job_id).resolve()
        inbox.relative_to(inbox_root)
        manifest_path = inbox / "inbox_manifest.json"
        provenance: Mapping[str, object] | None = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = manifest["entries"]
            saved_provenance = manifest.get("v6_provenance")
            if isinstance(saved_provenance, Mapping):
                provenance = saved_provenance
            sources_missing = not isinstance(entries, list)
        except (OSError, ValueError, TypeError):
            sources_missing = True
        if sources_missing:
            state = json.loads((inbox_root / f"{job_id}.state.json").read_text(encoding="utf-8"))
            expected = tuple(state["expected_names"])
            source = Path(state["strategy_source"])
            output = Path(state["output_csv"])
            if provenance is None:
                generation = self._fresh_strategy_manifest()
                provenance = validate_strategy_manifest(generation).provenance
            results = _load_saved_results(output)
            evidence = _load_saved_result_evidence(output)
            reports = {name: evidence[name][0] for name in expected}
            if {result.strategy_names[0] for result in results} != set(expected) or not all(path.is_file() for path in reports.values()):
                raise ValueError("verified reports are incomplete")
            if inbox.exists():
                shutil.rmtree(inbox)
            plan = BatchPlan(source, expected, tuple(f"{name}.json" for name in expected), tuple(), tuple(), tuple(), tuple())
            snapshot = (inbox_root / f".{job_id}.tester-config.snapshot").read_bytes()
            capture_verified_inbox(config, output, plan, results, reports, tester_config_bytes=snapshot, provenance=provenance)
        try:
            self._panel_jobs.recover_committed(job_id, runtime={"inbox_path": str(inbox)})
        except PanelJobError:
            if self._panel_jobs.get(job_id).get("state") != "COMMITTED":
                raise
        return self.strategies_tester_status(job_id)

    def _capture_bulk_finalist_retest_inbox(
        self,
        job_id: str,
        *,
        tracked: Mapping[str, object] | None = None,
        runtime: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        tracked = self._panel_jobs.get(job_id) if tracked is None else tracked
        saved_runtime = self._panel_jobs.runtime(job_id) if runtime is None else dict(runtime)
        if tracked.get("kind") != "strategies.performance.v2.finalist-retest" or saved_runtime.get("bulk_retest") is not True:
            raise ValueError("job is not a bulk finalist RETEST")
        recovering_interrupted = (
            tracked.get("state") == "FAILED"
            and tracked.get("error") == {"code": "INTERRUPTED"}
        )
        if tracked.get("state") != "COMMITTED" and not recovering_interrupted:
            raise ValueError("bulk RETEST tester job is not committed")
        config = RunnerConfig.from_json(self.default_config)
        inbox_root = Path(config.inbox_root).resolve()
        raw_inbox = saved_runtime.get("inbox_path")
        if not recovering_interrupted and (tracked.get("inbox_ready") is True or saved_runtime.get("inbox_ready") is True):
            if not isinstance(raw_inbox, str) or not raw_inbox.strip():
                raise ValueError("committed bulk RETEST inbox path is unavailable")
            inbox = Path(raw_inbox).resolve()
        else:
            service = self._single_mode_strategy_test()
            inbox = service.capture_inbox(job_id, force_single_mode=True).resolve()

        if inbox == inbox_root:
            raise ValueError("committed bulk RETEST inbox path is unavailable")
        try:
            inbox.relative_to(inbox_root)
        except ValueError as error:
            raise ValueError("committed bulk RETEST inbox path is outside the configured inbox root") from error
        manifest_path = inbox / "inbox_manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("committed bulk RETEST inbox manifest is unavailable")
        if manifest_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("committed bulk RETEST inbox manifest is too large")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("committed bulk RETEST inbox manifest is invalid") from error
        members = saved_runtime.get("cohort_members")
        cohort_names = {
            str(member.get("strategy_name"))
            for member in members
            if isinstance(member, Mapping) and isinstance(member.get("strategy_name"), str)
        } if isinstance(members, list) else set()
        expected_names = manifest.get("expected_strategy_names") if isinstance(manifest, Mapping) else None
        entries = manifest.get("entries") if isinstance(manifest, Mapping) else None
        entry_names = [entry.get("strategy_name") for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("schema_version") != 1
            or manifest.get("run_mode") != "SINGLE_MODE"
            or manifest.get("source_mode") != "metadata_only"
            or manifest.get("inbox_ready") is not True
            or ("batch_id" in manifest and manifest.get("batch_id") != job_id)
            or not cohort_names
            or not isinstance(expected_names, list)
            or len(expected_names) != len(cohort_names)
            or set(expected_names) != cohort_names
            or not isinstance(entries, list)
            or len(entries) != len(cohort_names)
            or len(entry_names) != len(cohort_names)
            or set(entry_names) != cohort_names
        ):
            raise ValueError("committed bulk RETEST inbox does not match its frozen cohort")
        self._validate_metadata_inbox(inbox)
        service = self._single_mode_strategy_test()
        saved_runtime.update({"inbox_path": str(inbox), "inbox_ready": True, "mode": "SINGLE_MODE"})
        if recovering_interrupted:
            self._panel_jobs.recover_committed(
                job_id,
                runtime=saved_runtime,
                expected_state="FAILED",
                expected_error={"code": "INTERRUPTED"},
            )
        try:
            service.mark_inbox_ready(job_id, inbox)
        except Exception:
            # The registry commit above is durable and authoritative.  The
            # service marker is only an in-memory restart aid, so a stale or
            # unavailable service must not turn a committed recovery into a
            # failed verification.
            _LOGGER.warning("tester service inbox marker unavailable after durable commit", exc_info=True)
        if not recovering_interrupted:
            self._panel_jobs.sync(
                job_id,
                {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
                runtime=saved_runtime,
            )
        self._strategy_batch_inboxes[job_id] = inbox
        return self._panel_jobs.get(job_id)

    def strategies_tester_cancel(self, job_id: str) -> dict[str, object]:
        try:
            tracked = self._panel_jobs.get(job_id)
            if tracked.get("kind") in {"strategies.tester.start", "strategies.tester.retry"}:
                cancel = self._single_mode_strategy_test().cancel
            elif tracked.get("kind") == "strategies.tester.runs":
                cancel = self._runs_batch().cancel
            else:
                cancel = self._strategy_batch().cancel
            result = cancel(job_id)
        except KeyError:
            return self._panel_jobs.get(job_id)
        self._panel_jobs.cancel(job_id)
        return self._sync_tracked_panel_job(result)

    def _tester_inbox(self, job_id: str) -> Path:
        inbox = self._strategy_batch_inboxes.get(job_id)
        if inbox is None:
            try:
                saved = self._panel_jobs.runtime(job_id).get("inbox_path")
                inbox = Path(saved) if isinstance(saved, str) else None
            except PanelJobError:
                inbox = None
        if inbox is None or not inbox.is_dir():
            raise ValueError("committed tester inbox is not available")
        return inbox.resolve()

    @staticmethod
    def _performance_v2_window(payload: Mapping[str, object], name: str) -> tuple[object, object] | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"{name} must contain start and end")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{name} timestamps are invalid")
        return value[0].strip(), value[1].strip()

    def _performance_v2_config(self):
        config = load_performance_v2_config(
            self.default_config.with_name("config.performance.json"),
            v1_database_root=self._panel_path("performance_db_root"),
        )
        return replace(config, workers=max(1, int(self._import_settings().workers)))

    @contextmanager
    def _performance_v2_writer_guard(self, target: Path):
        """Serialize Panel writers before the shared cross-process lock."""
        with self._performance_v2_writer_lock:
            file_lock = PerformanceV2WriterLock(target.parent)
            try:
                file_lock.__enter__()
            except PerformanceV2StoreError as error:
                raise PerformanceV2ApiError(
                    "PERFORMANCE_V2_LOCKED", status=409,
                    message="Performance v2 database writer is busy",
                ) from error
            try:
                yield
            finally:
                file_lock.__exit__(None, None, None)

    @staticmethod
    def _retest_date(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be an ISO date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} must be an ISO date") from None
        if parsed.isoformat() != value:
            raise ValueError(f"{field} must be an ISO date")
        return value

    @staticmethod
    def _requested_initial_balance(payload: Mapping[str, object]) -> float | None:
        return None if "initial_balance" not in payload else parse_initial_balance(payload["initial_balance"])

    @staticmethod
    def _validate_retest_end_date(end: str) -> str:
        if date.fromisoformat(end) > date.today() - timedelta(days=1):
            raise PerformanceV2ApiError(
                "RETEST_END_DATE_INVALID",
                status=422,
                message="RETEST test_end must not be later than yesterday",
            )
        return end

    def _retest_default_dates(self, connection: duckdb.DuckDBPyConnection) -> tuple[str, str]:
        rows = connection.execute(
            """
            select s.strategy_id, r.report_start_utc, r.report_end_utc
              from strategy_tags tags
              join strategies s on s.strategy_id = tags.strategy_id
              join strategy_results r on r.result_id = s.current_result_id
                                        and r.strategy_id = s.strategy_id
             where tags.tag = 'RETEST'
               and s.lifecycle_status = 'ACTIVE'
               and s.current_result_id is not null
             order by s.strategy_id
            """
        ).fetchall()
        if not rows:
            raise ValueError("no active RETEST strategies with current results")

        def as_datetime(value: object) -> datetime:
            if isinstance(value, datetime):
                return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            if isinstance(value, date):
                return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
            raise ValueError("RETEST result dates are invalid")

        candidates: list[tuple[timedelta, int, datetime, datetime]] = []
        for strategy_id, raw_start, raw_end in rows:
            start, end = as_datetime(raw_start), as_datetime(raw_end)
            if end <= start:
                continue
            candidates.append((end - start, int(strategy_id), start, end))
        if not candidates:
            raise ValueError("RETEST result dates are invalid")
        _, _, start, end = max(candidates, key=lambda item: (item[0], -item[1]))
        return start.astimezone(timezone.utc).date().isoformat(), end.astimezone(timezone.utc).date().isoformat()

    def _retest_range(self, payload: Mapping[str, object], connection: duckdb.DuckDBPyConnection) -> tuple[str, str]:
        explicit = self._retest_explicit_range(payload)
        if explicit is not None:
            return explicit[0], self._validate_retest_end_date(explicit[1])
        start, end = self._retest_default_dates(connection)
        if start >= end:
            raise ValueError("RETEST test_start must be before test_end")
        return start, self._validate_retest_end_date(end)

    def _retest_explicit_range(self, payload: Mapping[str, object]) -> tuple[str, str] | None:
        if not isinstance(payload, Mapping):
            raise ValueError("RETEST start request must be an object")
        allowed = {"test_start", "test_end", "start_date", "end_date"}
        if set(payload).difference(allowed):
            raise ValueError("RETEST start request contains unsupported fields")
        pairs = [("test_start", "test_end"), ("start_date", "end_date")]
        present = [(start, end) for start, end in pairs if start in payload or end in payload]
        if len(present) > 1:
            raise ValueError("RETEST dates must use one date contract")
        if not present:
            return None
        start_key, end_key = present[0]
        if start_key not in payload:
            raise ValueError("RETEST test_start and test_end are required together")
        if end_key not in payload:
            raise PerformanceV2ApiError("RETEST_END_DATE_INVALID", status=422, message="RETEST test_end must be an ISO date")
        start = self._retest_date(payload[start_key], start_key)
        try:
            end = self._retest_date(payload[end_key], end_key)
        except ValueError:
            raise PerformanceV2ApiError("RETEST_END_DATE_INVALID", status=422, message="RETEST test_end must be an ISO date") from None
        if start >= end:
            raise ValueError("RETEST test_start must be before test_end")
        return start, end

    def _retest_listing_context(self) -> tuple[Path, Path, str | None]:
        configured, sheet = self._listing_dates_source()
        try:
            relative = configured.resolve().relative_to(self.root.resolve())
        except ValueError:
            raise ValueError("RETEST listing dates path must be inside the project") from None
        if not configured.is_file():
            raise ValueError("RETEST listing dates file is unavailable")
        return configured.resolve(), Path(*relative.parts), sheet

    def _validate_retest_listing(
        self, connection: duckdb.DuckDBPyConnection, path: Path, start: str, sheet: str | None = None,
    ) -> None:
        try:
            listing_dates = load_listing_dates(path) if sheet is None else load_listing_dates(path, sheet)
        except Exception as error:
            raise ValueError("RETEST listing dates file is invalid") from error
        symbols = [row[0] for row in connection.execute(
            """
            select s.symbol
              from strategy_tags tags
              join strategies s on s.strategy_id = tags.strategy_id
             where tags.tag = 'RETEST'
               and s.lifecycle_status = 'ACTIVE'
               and s.current_result_id is not null
             order by s.strategy_id
            """
        ).fetchall()]
        for symbol in symbols:
            listing = listing_dates.get(str(symbol))
            if listing is None:
                raise ValueError(f"RETEST listing date is missing for {symbol}")
            if hasattr(listing, "to_pydatetime"):
                listing = listing.to_pydatetime()
            if isinstance(listing, datetime):
                listed = listing.astimezone(timezone.utc).date() if listing.tzinfo else listing.date()
            elif isinstance(listing, date):
                listed = listing
            else:
                raise ValueError(f"RETEST listing date is invalid for {symbol}")

    def strategies_performance_v2_retest_status(self) -> dict[str, object]:
        config = self._performance_v2_config()
        target = performance_v2_database_path(config)
        if not target.is_file():
            return {"count": 0, "retest_count": 0, "active_count": 0, "phase": "IDLE"}
        try:
            with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                status = retest_status(connection)
                defaults = self._retest_default_dates(connection) if status.active_count else None
        except (duckdb.Error, OSError, PerformanceV2StoreError) as error:
            if not target.exists():
                return {"count": 0, "retest_count": 0, "active_count": 0, "phase": "IDLE"}
            raise ValueError("Performance v2 RETEST status is unavailable") from error
        try:
            listing_path, listing_sheet = self._listing_dates_source()
        except (EvaluationProtocolError, ValueError):
            # Status stays readable; the launch reports the profile error with its code.
            listing_path, listing_sheet = self._workflow_default("listing_dates_path"), None
        return {
            "count": status.active_count,
            "retest_count": status.active_count,
            "active_count": status.active_count,
            "default_start": defaults[0] if defaults else None,
            "default_end": defaults[1] if defaults else None,
            "listing_dates_path": str(listing_path),
            **({"listing_dates_sheet": listing_sheet} if listing_sheet is not None else {}),
            "phase": "READY" if status.active_count else "IDLE",
        }

    def _reusable_retest_job(self, evaluation: EvaluationTarget) -> dict[str, object] | None:
        """Find the newest committed RETEST inbox still backed by tester files.

        New registry records carry a persisted creation time.  Legacy records
        without it remain eligible and use their stable job id as a deterministic
        fallback, rather than relying on JSON dictionary order.
        """
        try:
            inbox_root = Path(RunnerConfig.from_json(self.default_config).inbox_root).resolve()
        except (OSError, TypeError, ValueError):
            return None

        def order(job: dict[str, object]) -> tuple[int, datetime, str]:
            raw_created = job.get("created_at_utc")
            if isinstance(raw_created, str):
                try:
                    created = datetime.fromisoformat(raw_created)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    else:
                        created = created.astimezone(timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    created = None
            else:
                created = None
            if created is None:
                return (0, datetime.min.replace(tzinfo=timezone.utc), str(job.get("job_id", "")))
            return (1, created, str(job.get("job_id", "")))

        jobs = self._panel_jobs.list()
        jobs_by_id = {
            job.get("job_id"): job
            for job in jobs
            if isinstance(job.get("job_id"), str) and job.get("job_id")
        }
        runtimes: dict[str, dict[str, object]] = {}
        for job_id in jobs_by_id:
            try:
                value = self._panel_jobs.runtime(job_id)
            except PanelJobError:
                value = {}
            runtimes[job_id] = value

        def is_retest(job: Mapping[str, object], runtime: Mapping[str, object]) -> bool:
            return job.get("retest") is True or runtime.get("retest") is True

        active_states = {"QUEUED", "RUNNING", "CANCELLING"}
        if any(
            job.get("state") in active_states
            and job.get("kind") in {"strategies.tester", "strategies.tester.start", "strategies.tester.native.start"}
            and is_retest(job, runtimes.get(str(job.get("job_id")), {}))
            for job in jobs
        ):
            return None
        if any(
            job.get("state") in active_states
            and job.get("kind") == "strategies.performance.v2.import"
            and is_retest(job, runtimes.get(str(job.get("job_id")), {}))
            for job in jobs
        ):
            return None

        def references_tester(
            import_job: Mapping[str, object], import_runtime: Mapping[str, object],
            tester_job_id: str, inbox: Path,
        ) -> bool:
            if f"tester:{tester_job_id}" in import_job.get("resource_keys", ()):  # persisted reverse link
                return True
            containers = (import_job, import_runtime)
            for container in containers:
                for key in ("tester_job_id", "retest_tester_job_id", "source_tester_job_id"):
                    if container.get(key) == tester_job_id:
                        return True
                for key in ("inbox_path", "retest_inbox_path"):
                    raw_path = container.get(key)
                    if not isinstance(raw_path, str) or not raw_path.strip():
                        continue
                    path = Path(raw_path)
                    if not path.is_absolute():
                        path = self.root / path
                    try:
                        if path.resolve() == inbox:
                            return True
                    except OSError:
                        continue
            return False

        def has_committed_import(tester_job_id: str, inbox: Path) -> bool:
            for import_job in jobs:
                if (
                    import_job.get("kind") != "strategies.performance.v2.import"
                    or import_job.get("state") != "COMMITTED"
                ):
                    continue
                import_id = import_job.get("job_id")
                import_runtime = runtimes.get(str(import_id), {})
                if references_tester(import_job, import_runtime, tester_job_id, inbox):
                    return True
            return False

        def has_active_import(tester_job_id: str, inbox: Path) -> bool:
            for import_job in jobs:
                if (
                    import_job.get("kind") != "strategies.performance.v2.import"
                    or import_job.get("state") not in active_states
                ):
                    continue
                import_id = import_job.get("job_id")
                import_runtime = runtimes.get(str(import_id), {})
                if is_retest(import_job, import_runtime) or references_tester(
                    import_job, import_runtime, tester_job_id, inbox
                ):
                    return True
            return False

        jobs = sorted(jobs, key=order, reverse=True)
        for job in jobs:
            if (
                job.get("kind") != "strategies.tester.native.start"
                or job.get("state") != "COMMITTED"
            ):
                continue
            job_id = job.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                continue
            try:
                runtime = runtimes.get(job_id, {})
                if job.get("inbox_ready") is not True and runtime.get("inbox_ready") is not True:
                    continue
                if job.get("retest") is not True and runtime.get("retest") is not True:
                    continue
                if not self._matches_protocol_v2(runtime, evaluation):
                    continue
                if "retest_import_job_id" in runtime:
                    previous_import_id = runtime.get("retest_import_job_id")
                    if not isinstance(previous_import_id, str) or not previous_import_id.strip():
                        continue
                    previous_import = self._panel_jobs.get(previous_import_id)
                    if (
                        previous_import.get("kind") != "strategies.performance.v2.import"
                        or previous_import.get("state") not in {"FAILED", "CANCELLED"}
                    ):
                        continue
                raw_inbox = runtime.get("inbox_path")
                if not isinstance(raw_inbox, str) or not raw_inbox.strip():
                    continue
                inbox = Path(raw_inbox).resolve()
                if inbox == inbox_root:
                    continue
                inbox.relative_to(inbox_root)
                self._validate_metadata_inbox(inbox)
                if has_committed_import(job_id, inbox) or has_active_import(job_id, inbox):
                    continue
            except (OSError, TypeError, ValueError, PanelJobError):
                continue
            self._strategy_batch_inboxes[job_id] = inbox
            reusable = dict(job)
            reusable["inbox_path"] = str(inbox)
            reusable["inbox_ready"] = True
            return reusable
        return None

    def strategies_performance_v2_retest_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        config = self._performance_v2_config()
        target = performance_v2_database_path(config)
        if not target.is_file():
            raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404, message="Performance v2 database is unavailable")
        initial_balance = self._requested_initial_balance(payload)
        explicit = self._retest_explicit_range(payload)
        if explicit is not None:
            self._validate_retest_end_date(explicit[1])
        evaluation = self._evaluation_target()
        self._require_protocol_v2_tester_template(evaluation)
        tester_template = self._mrs3_tester_template(evaluation)
        reusable = self._reusable_retest_job(evaluation)
        if reusable is not None:
            return reusable
        listing_path, listing_relative, listing_sheet = self._retest_listing_context()
        templates = self._mrs3_strategy_templates(evaluation)
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            start, end = self._retest_range(payload, connection)
            self._validate_retest_listing(connection, listing_path, start, listing_sheet)
            batch = build_retest_manifest(
                connection,
                templates if isinstance(templates, Mapping) else {},
                config.strategy_root.parent if config.strategy_root is not None else self.root / "Output",
                evaluation=evaluation,
            )
        runtime = {
            "retest": True,
            "mode": "SINGLE_MODE",
            "manifest_path": str(batch.manifest_path),
            "run_id": batch.run_id,
            "strategies_path": str(batch.strategies_path),
            "test_start": start,
            "test_end": end,
            "initial_balance": initial_balance,
            "listing_dates_path": str(listing_relative),
            **({"listing_dates_sheet": listing_sheet} if listing_sheet is not None else {}),
            **self._protocol_v2_runtime(evaluation),
        }
        request = {
            "manifest_path": str(batch.manifest_path),
            "analysis_run_id": batch.run_id,
            "start_date": start,
            "end_date": end,
            "mode": "SINGLE_MODE",
            "retest": True,
            "initial_balance": initial_balance,
        }
        return self._start_tracked_panel_job(
            "strategies.tester.native.start",
            request,
            ("strategies.tester", "performance-v2-retest"),
            lambda job_id: self._single_mode_strategy_test().start(
                batch.manifest_path,
                analysis_run_id=batch.run_id,
                start_date=start,
                end_date=end,
                job_id=job_id,
                tester_config_template=tester_template,
                **({"initial_balance": initial_balance} if initial_balance is not None else {}),
            ),
            runtime=runtime,
        )

    def _retest_mapping(self, tester_job_id: str) -> tuple[dict[str, int], dict[str, object]]:
        job = self._panel_jobs.get(tester_job_id)
        runtime = self._panel_jobs.runtime(tester_job_id)
        if (
            job.get("kind") != "strategies.tester.native.start"
            or (job.get("retest") is not True and runtime.get("retest") is not True)
        ):
            raise ValueError("tester job is not a RETEST run")
        if (
            job.get("state") != "COMMITTED"
            or (job.get("inbox_ready") is not True and runtime.get("inbox_ready") is not True)
        ):
            raise ValueError("RETEST tester inbox is not committed")
        inbox = self._tester_inbox(tester_job_id)
        try:
            manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("committed RETEST inbox manifest is unavailable") from error
        names = manifest.get("expected_strategy_names") if isinstance(manifest, Mapping) else None
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("committed RETEST inbox names are invalid")
        if manifest.get("run_mode") != "SINGLE_MODE":
            raise ValueError("RETEST inbox must be a committed SINGLE_MODE snapshot")
        config = self._performance_v2_config()
        target = performance_v2_database_path(config)
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            rows = connection.execute(
                """
                select s.strategy_name, s.strategy_id
                  from strategy_tags tags
                  join strategies s on s.strategy_id = tags.strategy_id
                 where tags.tag = 'RETEST'
                   and s.lifecycle_status = 'ACTIVE'
                   and s.current_result_id is not null
                 order by s.strategy_id
                """
            ).fetchall()
        mapping = {str(name): int(strategy_id) for name, strategy_id in rows}
        if set(mapping) != set(names):
            raise ValueError("RETEST inbox does not match current RETEST strategies")
        return mapping, runtime

    def strategies_performance_v2_retest_import(self, payload: Mapping[str, object]) -> dict[str, object]:
        if set(payload) != {"tester_job_id"}:
            raise ValueError("RETEST import accepts only tester_job_id; mapping is server-built")
        tester_job_id = self._required(payload, "tester_job_id")
        mapping, runtime = self._retest_mapping(tester_job_id)
        if "retest_import_job_id" in runtime:
            previous_import_id = runtime.get("retest_import_job_id")
            if not isinstance(previous_import_id, str) or not previous_import_id.strip():
                raise ValueError("RETEST import is already started")
            try:
                previous_import = self._panel_jobs.get(previous_import_id)
            except PanelJobError:
                raise ValueError("RETEST import is already started") from None
            if not isinstance(previous_import, Mapping):
                raise ValueError("RETEST import is already started")
            error = previous_import.get("error")
            error_code = error.get("code") if isinstance(error, Mapping) else None
            retryable = previous_import.get("state") == "CANCELLED" or (
                previous_import.get("state") == "FAILED"
                and isinstance(error_code, str)
                and error_code == "PERFORMANCE_V2_IMPORT_FAILED"
            )
            if not retryable:
                raise ValueError("RETEST import is already started")
            self._panel_jobs.clear_runtime(tester_job_id, "retest_import_job_id", value=previous_import_id)
        start, end = runtime.get("test_start"), runtime.get("test_end")
        listing_path = runtime.get("listing_dates_path")
        if not isinstance(start, str) or not isinstance(end, str) or not isinstance(listing_path, str):
            raise ValueError("RETEST run provenance is incomplete")
        pending_import = f"pending:{uuid.uuid4().hex}"
        try:
            self._panel_jobs.reserve_runtime(tester_job_id, "retest_import_job_id", pending_import)
        except PanelJobError as error:
            if error.code == "RUNTIME_BUSY":
                raise ValueError("RETEST import is already started") from None
            raise
        try:
            import_job = self.strategies_performance_v2_import({
                "tester_job_id": tester_job_id,
                "mode": "REPLACE",
                "replacement_strategy_ids": mapping,
                "clear_retest_on_success": True,
                "test_start": start,
                "test_end": end,
                "_retest": True,
            }, _internal=True)
            if not isinstance(import_job, Mapping):
                raise ValueError("RETEST import returned an invalid job")
            inner_job_id = import_job.get("job_id")
            if not isinstance(inner_job_id, str) or not inner_job_id.strip():
                raise ValueError("RETEST import returned an invalid job")
            try:
                inner_job = self._panel_jobs.get(inner_job_id)
            except PanelJobError:
                raise ValueError("RETEST import job was not registered") from None
            if inner_job.get("kind") != "strategies.performance.v2.import":
                raise ValueError("RETEST import returned an invalid job")
        except BaseException:
            try:
                self._panel_jobs.clear_runtime(tester_job_id, "retest_import_job_id", value=pending_import)
            except PanelJobError:
                pass
            raise
        runtime["retest_import_job_id"] = inner_job_id
        self._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)
        return import_job

    def _ensure_performance_v2_schema(self, target: Path) -> None:
        if not target.is_file():
            raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404, message="Performance v2 database is unavailable")
        with self._performance_v2_writer_lock:
            try:
                stat = target.stat()
            except OSError as error:
                raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404, message="Performance v2 database is unavailable") from error
            config = self._performance_v2_config()
            # The configured protocol/exchange is part of the key: a config change
            # without a database change must be checked again (protocol v2 §4.1).
            identity = (
                str(target.resolve()),
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_ctime_ns),
                int(stat.st_mtime_ns),
                int(stat.st_size),
                config.evaluation_protocol,
                config.exchange,
            )
            if identity in self._performance_v2_schema_ready:
                return
            try:
                if stat.st_size == 0:
                    self._initialize_missing_performance_v2_target(config)
                version = None
                schema_repair_required = False
                with duckdb.connect(str(target), read_only=True) as connection:
                    has_schema_info = connection.execute(
                        "select count(*) from information_schema.tables "
                        "where table_schema = 'main' and table_name = 'schema_info'"
                    ).fetchone()[0] == 1
                    if has_schema_info:
                        version_row = connection.execute(
                            "select value from schema_info where key = 'schema_version'"
                        ).fetchone()
                        version = None if version_row is None else str(version_row[0])
                        if version in {"5", "6", "7", "8"}:
                            require_performance_v2_readable(connection)
                            raise PerformanceV2ApiError(
                                "PERFORMANCE_V2_MIGRATION_REQUIRED", status=409,
                                message="Existing Performance v2 database requires an explicit offline migration",
                            )
                        if version in {"2", "3", "4"}:
                            raise PerformanceV2ApiError(
                                "PERFORMANCE_V2_MIGRATION_REQUIRED", status=409,
                                message="Existing Performance v2 database requires an explicit offline migration",
                            )
                        if version in {"9", "10", "11", "12"}:
                            require_performance_v2_readable(connection)
                            # Refuse a mismatching database before any migration writes to it.
                            require_database_identity(connection, config)
                            repairable_columns = {
                                ("window_metrics", "holding_seconds"),
                                ("window_metrics", "time_in_market_pct"),
                                ("strategy_results", "reported_start_utc"),
                                ("strategy_results", "reported_end_utc"),
                                ("strategy_results", "listing_date_utc"),
                                ("strategy_results", "listing_date_raw"),
                                ("strategy_results", "listing_date_source"),
                                ("strategy_results", "effective_start_utc"),
                                ("strategy_results", "effective_end_utc"),
                                ("strategy_results", "warmup_hours"),
                                ("strategy_results", "excluded_trade_count"),
                                ("strategy_results", "exclusion_reason"),
                                ("strategy_results", "optimizer_source_metadata_json"),
                            }
                            existing_columns = set(connection.execute(
                                """select table_name, column_name from information_schema.columns
                                    where table_schema = 'main'
                                      and table_name in ('window_metrics', 'strategy_results')"""
                            ).fetchall())
                            schema_repair_required = not repairable_columns.issubset(existing_columns)
                if version != "12" or schema_repair_required:
                    with self._performance_v2_writer_guard(target):
                        with duckdb.connect(str(target)) as connection:
                            initialize_performance_v2(connection)
                            require_performance_v2(connection)
                with duckdb.connect(str(target), read_only=True) as connection:
                    require_database_identity(connection, config)
            except PerformanceV2ApiError:
                raise
            except PerformanceV2IdentityError as error:
                raise PerformanceV2ApiError(error.code, status=409, message=str(error)) from error
            except PerformanceV2StoreError as error:
                raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=500, message=str(error)) from error
            except duckdb.Error as error:
                if isinstance(error, duckdb.IOException) and _is_duckdb_lock_error(error):
                    raise PerformanceV2ApiError(
                        "PERFORMANCE_V2_LOCKED", status=409, message="Performance v2 database is locked",
                    ) from error
                raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=500, message=str(error)) from error
            stat = target.stat()
            identity = (
                str(target.resolve()), int(stat.st_dev), int(stat.st_ino),
                int(stat.st_ctime_ns), int(stat.st_mtime_ns), int(stat.st_size),
                config.evaluation_protocol, config.exchange,
            )
            self._performance_v2_schema_ready.add(identity)

    @staticmethod
    def _initialize_missing_performance_v2_target(config: PerformanceV2Config) -> None:
        resolved_target = performance_v2_database_path(config)
        target = config.database_root / "strategy_performance.duckdb"
        if target.is_symlink() or target != resolved_target:
            raise PerformanceV2StoreError("Performance v2 target is redirected")
        if target.exists() and target.stat().st_size != 0:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with PerformanceV2WriterLock(target.parent):
                empty_identity = None
                if target.exists():
                    existing = target.stat()
                    if existing.st_size != 0:
                        return
                    empty_identity = (
                        int(existing.st_dev), int(existing.st_ino), int(existing.st_ctime_ns),
                        int(existing.st_mtime_ns), int(existing.st_size),
                    )
                with duckdb.connect(str(staging)) as connection:
                    initialize_performance_v2(
                        connection, evaluation_protocol=config.evaluation_protocol, exchange=config.exchange,
                    )
                    require_performance_v2(connection)
                if empty_identity is not None:
                    current = target.stat()
                    if (
                        int(current.st_dev), int(current.st_ino), int(current.st_ctime_ns),
                        int(current.st_mtime_ns), int(current.st_size),
                    ) != empty_identity:
                        return
                    target.unlink()
                try:
                    os.link(staging, target)
                except FileExistsError:
                    return
        finally:
            staging.unlink(missing_ok=True)

    def _output_strategy_root(self) -> Path:
        output_root = self.root / "Output"
        if output_root.is_symlink() or output_root.is_junction():
            raise ValueError("Output strategy root is redirected")
        resolved = output_root.resolve()
        if resolved != output_root.absolute():
            raise ValueError("Output strategy root is redirected")
        return resolved

    def strategies_performance_v2_import(
        self,
        payload: Mapping[str, object],
        *,
        _internal: bool = False,
        job_id: str | None = None,
    ) -> dict[str, object]:
        allowed = {
            "tester_job_id", "mode", "replacement_strategy_ids", "window_a", "window_b",
            "clear_retest_on_success", "test_start", "test_end", "listing_dates_path", "_retest",
            "_expected_current_result_ids",
        }
        if set(payload).difference(allowed):
            raise ValueError("Performance v2 import request contains unsupported fields")
        if not _internal and any(
            key in payload for key in ("replacement_strategy_ids", "clear_retest_on_success", "_retest", "_expected_current_result_ids")
        ):
            raise ValueError("Performance v2 replacement controls are internal only")
        if job_id is not None and not _internal:
            raise ValueError("Performance v2 import job ID is internal only")
        if not _internal and payload.get("mode") == "REPLACE":
            raise ValueError("Performance v2 REPLACE is internal only")
        tester_job_id = self._required(payload, "tester_job_id")
        tester_job = self._panel_jobs.get(tester_job_id)
        is_collection_job = tester_job.get("kind") == "strategies.tester.collection"
        expected_inbox_manifest_sha256 = None
        expected_collection_id = None
        collection_service = None
        if is_collection_job and not _internal:
            collection_service = self._report_collection()
            try:
                snapshot = collection_service.import_snapshot(tester_job_id)
            except PanelJobError as error:
                raise ValueError("Performance v2 import requires explicit inbox verification") from error
            expected_inbox_manifest_sha256 = snapshot["manifest_sha256"]
            expected_collection_id = tester_job_id
        if tester_job.get("state") != "COMMITTED" or tester_job.get("inbox_ready") is not True:
            raise ValueError("Performance v2 import requires a committed tester inbox")
        job_exchange = self._require_import_target(tester_job_id, is_collection_job)
        if not _internal and not is_collection_job and self._panel_jobs.runtime(tester_job_id).get("performance_v2_import_verified") is not True:
            raise ValueError("Performance v2 import requires explicit inbox verification")
        if not _internal:
            try:
                self._validate_metadata_inbox(self._tester_inbox(tester_job_id))
            except ValueError as error:
                raise PerformanceV2ApiError(
                    "PERFORMANCE_V2_VERIFIED_INBOX_UNAVAILABLE",
                    status=409,
                    message="verified tester inbox is unavailable",
                ) from error
            with self._panel_jobs.lock:
                current = self._panel_jobs.get(tester_job_id)
                runtime = self._panel_jobs.runtime(tester_job_id)
                if (
                    current.get("state") != "COMMITTED"
                    or current.get("inbox_ready") is not True
                    or (not is_collection_job and runtime.get("performance_v2_import_verified") is not True)
                ):
                    raise ValueError("Performance v2 import requires explicit inbox verification")
                if not is_collection_job:
                    runtime["performance_v2_import_verified"] = False
                    self._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)
        mode = payload.get("mode", "ADD")
        if not isinstance(mode, str) or mode not in {"ADD", "REPLACE"}:
            raise ValueError("Performance v2 import mode must be ADD or REPLACE")
        replacement = payload.get("replacement_strategy_ids")
        if replacement is not None and (
            not isinstance(replacement, Mapping)
            or any(
                not isinstance(name, str)
                or not name.strip()
                or isinstance(strategy_id, bool)
                or not isinstance(strategy_id, int)
                for name, strategy_id in replacement.items()
            )
        ):
            raise ValueError("replacement_strategy_ids must be a mapping")
        expected_current = payload.get("_expected_current_result_ids")
        if expected_current is not None and (
            not _internal or not isinstance(expected_current, Mapping)
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in expected_current.values())
        ):
            raise ValueError("expected current Result IDs are internal only")
        if isinstance(expected_current, Mapping) and set(expected_current) != set(replacement or {}):
            raise ValueError("expected current Result IDs must cover the replacement cohort")
        window_a = self._performance_v2_window(payload, "window_a")
        window_b = self._performance_v2_window(payload, "window_b")
        if (window_a is None) != (window_b is None):
            raise ValueError("window_a and window_b must be supplied together")
        clear_retest = payload.get("clear_retest_on_success", False)
        if type(clear_retest) is not bool:
            raise ValueError("clear_retest_on_success must be a boolean")
        test_start, test_end = payload.get("test_start"), payload.get("test_end")
        if (test_start is None) != (test_end is None) or (
            test_start is not None and (
                not isinstance(test_start, str) or not isinstance(test_end, str)
                or date.fromisoformat(test_start).isoformat() != test_start
                or date.fromisoformat(test_end).isoformat() != test_end
                or test_start >= test_end
            )
        ):
            raise ValueError("test_start and test_end must be valid ISO dates")
        if payload.get("listing_dates_path") is not None:
            raise ValueError("listing_dates_path is configured by the server")
        listing_source, listing_dates_sheet = self._listing_dates_source(job_exchange)   # its exchange's sheet
        try:
            listing_dates_path = listing_source.resolve().relative_to(self.root.resolve())
        except ValueError:
            raise ValueError("Performance v2 listing dates file is unavailable") from None
        if self._performance_v2_jobs is None:
            self._performance_v2_jobs = LocalPerformanceV2Jobs(on_update=self._record_special_job)
        performance_config = self._performance_v2_config()
        self._initialize_missing_performance_v2_target(performance_config)
        output_strategy_root = self._output_strategy_root()
        runner_config = RunnerConfig.from_json(self.default_config)
        request = PerformanceV2PanelRequest(
            inbox=self._tester_inbox(tester_job_id),
            report_root=runner_config.report_dir,
            config=performance_config,
            mode=mode,
            replacement_strategy_ids=replacement,
            window_a=window_a,
            window_b=window_b,
            strategy_root=output_strategy_root,
            tester_strategy_root=getattr(runner_config, "strategy_dir", None),
            tester_bot_root=getattr(runner_config, "bot_root", None),
            clear_retest_on_success=clear_retest,
            test_start=test_start if isinstance(test_start, str) else None,
            test_end=test_end if isinstance(test_end, str) else None,
            listing_dates_path=listing_dates_path,
            listing_dates_root=self.root,
            listing_dates_sheet=listing_dates_sheet,
            expected_current_result_ids=expected_current,
            expected_inbox_manifest_sha256=expected_inbox_manifest_sha256,
            expected_collection_id=expected_collection_id,
        )
        job_request = {"tester_job_id": tester_job_id, "mode": mode}
        if _internal:
            job_request["retest"] = True
        claim_started = False

        def claim_collection_import(job_id: str) -> None:
            nonlocal claim_started
            if collection_service is not None:
                with self._lock:
                    self._collection_import_claim_ids.add(job_id)
                try:
                    collection_service.claim_import(
                        tester_job_id,
                        job_id,
                        expected_digest=expected_inbox_manifest_sha256,
                    )
                except BaseException:
                    with self._lock:
                        self._collection_import_claim_ids.discard(job_id)
                    raise
                claim_started = True

        def start_import(job_id: str) -> dict[str, object]:
            nonlocal claim_started
            try:
                return self._performance_v2_jobs.start(request, job_id=job_id)
            except BaseException:
                with self._lock:
                    self._collection_import_claim_ids.discard(job_id)
                if claim_started and collection_service is not None:
                    try:
                        collection_service.release_import_claim(tester_job_id, job_id)
                    except PanelJobError:
                        pass
                    claim_started = False
                raise

        return self._start_tracked_panel_job(
            "strategies.performance.v2.import",
            job_request,
            (f"tester:{tester_job_id}", "performance-v2-db"),
            start_import,
            runtime={"report_collection_id": tester_job_id} if collection_service is not None else None,
            before_start=claim_collection_import if collection_service is not None else None,
            job_id=job_id,
        )

    def strategies_performance_v2_import_status(self, job_id: str) -> dict[str, object]:
        if self._performance_v2_jobs is None:
            return self._panel_jobs.get(job_id)
        self._tracked_job_or_interrupted(job_id, self._performance_v2_jobs.status)
        # The worker status contains local filesystem paths; the persisted
        # panel job is the redacted public snapshot.
        return self._panel_jobs.get(job_id)


    def _performance_v2_maintenance_target(self) -> tuple[Path, int]:
        try:
            config = self._performance_v2_config()
            target = performance_v2_database_path(config)
        except (OSError, TypeError, ValueError) as error:
            raise PerformanceV2ApiError("PERFORMANCE_DB_UNAVAILABLE", status=503, message="PerformanceDB is unavailable") from error
        if not target.is_file():
            raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404, message="PerformanceDB is unavailable")
        return target, min(max(1, int(config.workers)), 16)

    @staticmethod
    def _performance_v2_maintenance_database_error(error: duckdb.Error) -> PerformanceV2ApiError:
        if isinstance(error, duckdb.IOException) and _is_duckdb_lock_error(error):
            return PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message=str(error))
        return PerformanceV2ApiError("PERFORMANCE_V2_DATABASE_ERROR", status=500, message=str(error))

    def _performance_v2_maintenance_prune_previews(self, now: float) -> None:
        expired = [
            token for token, (created_at, _preview) in self._performance_v2_maintenance_previews.items()
            if now - created_at > _PERFORMANCE_V2_MAINTENANCE_PREVIEW_TTL_SECONDS
        ]
        for token in expired:
            del self._performance_v2_maintenance_previews[token]
        while len(self._performance_v2_maintenance_previews) > _PERFORMANCE_V2_MAINTENANCE_MAX_PREVIEWS:
            del self._performance_v2_maintenance_previews[next(iter(self._performance_v2_maintenance_previews))]

    def _performance_v2_maintenance_require_idle(self) -> None:
        with self._performance_v2_maintenance_state_lock:
            if self._performance_v2_maintenance_active_job is not None:
                raise PerformanceV2ApiError(
                    "PERFORMANCE_V2_MAINTENANCE_BUSY", status=409,
                    message="PerformanceDB maintenance is running; retry after its status is terminal",
                )

    @contextmanager
    def _performance_v2_maintenance_read_guard(self):
        if not self._performance_v2_writer_lock.acquire(blocking=False):
            raise PerformanceV2ApiError(
                "PERFORMANCE_V2_LOCKED", status=409,
                message="PerformanceDB has an active Panel writer; retry after it completes",
            )
        try:
            yield
        finally:
            self._performance_v2_writer_lock.release()

    def strategies_performance_v2_maintenance_catalog(self) -> dict[str, object]:
        self._performance_v2_maintenance_require_idle()
        target, workers = self._performance_v2_maintenance_target()
        try:
            with self._performance_v2_maintenance_read_guard(), duckdb.connect(str(target), read_only=True) as connection:
                set_performance_v2_maintenance_workers(connection, workers)
                symbols = performance_v2_maintenance_catalog(connection)
            return {"symbols": symbols, "count": len(symbols)}
        except PerformanceV2MaintenanceSchemaError as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=409, message=str(error)) from error
        except PerformanceV2MaintenanceError as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=409, message=str(error)) from error
        except PerformanceV2ApiError:
            raise
        except duckdb.Error as error:
            raise self._performance_v2_maintenance_database_error(error) from error

    def strategies_performance_v2_maintenance_preview(self, payload: object) -> dict[str, object]:
        self._performance_v2_maintenance_require_idle()
        if not isinstance(payload, Mapping) or set(payload) != {"operation", "symbols"}:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="preview requires exactly operation and symbols")
        recovery_preview: Mapping[str, object] | None = None
        requested_symbols = payload.get("symbols")
        if (
            payload.get("operation") == "full"
            and isinstance(requested_symbols, (list, tuple))
            and all(isinstance(symbol, str) for symbol in requested_symbols)
        ):
            with self._performance_v2_maintenance_state_lock:
                recovery_entry = self._performance_v2_maintenance_recovery_preview
                if recovery_entry is not None:
                    created_at, candidate = recovery_entry
                    if perf_counter() - created_at > _PERFORMANCE_V2_MAINTENANCE_RECOVERY_TTL_SECONDS:
                        self._performance_v2_maintenance_recovery_preview = None
                    elif set(requested_symbols) & set(candidate.get("symbols", ())):
                        recovery_preview = candidate
        target, workers = self._performance_v2_maintenance_target()
        try:
            with self._performance_v2_maintenance_read_guard(), duckdb.connect(str(target), read_only=True) as connection:
                set_performance_v2_maintenance_workers(connection, workers)
                preview = create_performance_v2_maintenance_preview(
                    connection, payload["symbols"], payload["operation"],
                    recovery_preview=recovery_preview,
                )
        except PerformanceV2MaintenanceSchemaError as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=409, message=str(error)) from error
        except PerformanceV2ApiError:
            raise
        except PerformanceV2MaintenanceError as error:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message=str(error)) from error
        except duckdb.Error as error:
            raise self._performance_v2_maintenance_database_error(error) from error
        token = uuid.uuid4().hex
        with self._performance_v2_maintenance_state_lock:
            self._performance_v2_maintenance_prune_previews(perf_counter())
            self._performance_v2_maintenance_previews[token] = (perf_counter(), preview)
            self._performance_v2_maintenance_prune_previews(perf_counter())
        return {**public_performance_v2_maintenance_preview(preview), "token": token}

    def strategies_performance_v2_maintenance_apply(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping) or set(payload) != {"token"} or not isinstance(payload.get("token"), str) or not payload["token"]:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="apply requires exactly one preview token")
        with self._performance_v2_maintenance_state_lock:
            if self._performance_v2_maintenance_active_job is not None:
                raise PerformanceV2ApiError("PERFORMANCE_V2_MAINTENANCE_BUSY", status=409, message="PerformanceDB maintenance is already running")
            token = payload["token"]
            now = perf_counter()
            self._performance_v2_maintenance_prune_previews(now)
            token_entry = self._performance_v2_maintenance_previews.pop(token, None)
            if token_entry is None:
                raise PerformanceV2ApiError("PREVIEW_TOKEN_INVALID", status=409, message="Preview token is unknown, stale, or already used")
            _created_at, preview = token_entry
            job_id = uuid.uuid4().hex
            start = perf_counter()
            public = public_performance_v2_maintenance_preview(preview)
            table_counts = {str(table): 0 for table in public["table_counts"]}
            pair_counts = {str(pair["symbol"]): 0 for pair in public["pairs"]}
            pair_table_counts = {
                str(pair["symbol"]): {str(table): 0 for table in pair["table_counts"]}
                for pair in public["pairs"]
            }
            job: dict[str, object] = {
                "job_id": job_id, "status": "RUNNING", "phase": "waiting_for_writer",
                "operation": str(public["operation"]),
                "current_table": None, "symbols": list(public["symbols"]),
                "strategy_total": sum(int(pair.get("strategy_count", 0)) for pair in public["pairs"]),
                "pair_scoped_total": int(public["pair_scoped_total"]), "pair_scoped_deleted": 0,
                "shared_plateau_total": int(public["shared_plateau_rows"]), "shared_plateau_deleted": 0,
                "table_counts": table_counts, "pair_counts": pair_counts,
                "pair_table_counts": pair_table_counts,
                "global_journal_total": sum(int(value) for value in public["global_counts"].values()),
                "global_journal_counts": {str(table): 0 for table in public["global_counts"]},
                "started_at": start, "elapsed_seconds": 0.0, "error": None,
            }
            self._performance_v2_maintenance_job = job
            self._performance_v2_maintenance_active_job = job_id

        worker = threading.Thread(
            target=self._run_performance_v2_maintenance,
            args=(job_id, preview, start),
            name=f"performance-v2-maintenance-{job_id[:8]}", daemon=True,
        )
        worker.start()
        return {"job_id": job_id, "status": "RUNNING"}

    def _run_performance_v2_maintenance(self, job_id: str, preview: Mapping[str, object], started_at: float) -> None:
        target: Path | None = None

        def phase_changed(phase: str) -> None:
            with self._performance_v2_maintenance_state_lock:
                job = self._performance_v2_maintenance_job
                if job is not None and job.get("job_id") == job_id:
                    job["phase"] = phase
                    job["current_table"] = phase if phase not in {"revalidation", "waiting_for_writer"} else None

        def committed(event: dict[str, object]) -> None:
            with self._performance_v2_maintenance_state_lock:
                job = self._performance_v2_maintenance_job
                if job is None or job.get("job_id") != job_id:
                    return
                table = str(event["table"])
                if event.get("global") is True:
                    counts = job["global_journal_counts"]
                    counts[table] = int(counts.get(table, 0)) + int(event["rows"])
                    return
                table_counts = job["table_counts"]
                table_counts[table] = int(table_counts.get(table, 0)) + int(event["rows"])
                by_symbol = event.get("rows_by_symbol", {})
                for symbol, count in by_symbol.items():
                    symbol = str(symbol)
                    job["pair_counts"][symbol] = int(job["pair_counts"].get(symbol, 0)) + int(count)
                    symbol_tables = job["pair_table_counts"].setdefault(symbol, {})
                    symbol_tables[table] = int(symbol_tables.get(table, 0)) + int(count)
                shared = int(event.get("shared_plateau_rows", 0))
                job["shared_plateau_deleted"] += shared
                job["pair_scoped_deleted"] += int(event["rows"]) - shared

        try:
            target, workers = self._performance_v2_maintenance_target()
            # All PerformanceDB writers take the controller RLock, then this
            # cross-process lock, then open their sole writable connection.
            with self._performance_v2_writer_lock:
                with PerformanceV2WriterLock(target.parent):
                    phase_changed("revalidation")
                    with duckdb.connect(str(target)) as connection:
                        set_performance_v2_maintenance_workers(connection, workers)
                        result = apply_performance_v2_maintenance_preview(
                            connection, preview, on_phase=phase_changed, on_commit=committed,
                        )
            with self._performance_v2_maintenance_state_lock:
                job = self._performance_v2_maintenance_job
                if job is not None and job.get("job_id") == job_id:
                    if preview.get("operation") == "full":
                        self._performance_v2_maintenance_recovery_preview = None
                    job.update({
                        "status": "COMMITTED", "phase": "committed", "current_table": None,
                        "pair_scoped_deleted": int(result["pair_scoped_deleted"]),
                        "global_journal_counts": dict(result["global_deleted"]),
                        "table_counts": dict(result["table_counts"]),
                        "shared_plateau_deleted": int(result["shared_plateau_deleted"]),
                        "elapsed_seconds": max(0.0, perf_counter() - started_at),
                    })
        except Exception as error:
            with self._performance_v2_maintenance_state_lock:
                job = self._performance_v2_maintenance_job
                if job is not None and job.get("job_id") == job_id:
                    job.update({
                        "status": "FAILED", "error": str(error),
                        "elapsed_seconds": max(0.0, perf_counter() - started_at),
                    })
                    if preview.get("operation") == "full":
                        self._performance_v2_maintenance_recovery_preview = (perf_counter(), dict(preview))
                        job["recovery_warning"] = (
                            f"This full-delete recovery map is held in Panel memory for "
                            f"{_PERFORMANCE_V2_MAINTENANCE_RECOVERY_TTL_SECONDS // 60} minutes; a later failed full delete can replace it. "
                            "After a Panel restart or expiry, unattributed plateau rows are preserved because they cannot be safely assigned to selected pairs."
                        )
        finally:
            with self._performance_v2_maintenance_state_lock:
                if self._performance_v2_maintenance_active_job == job_id:
                    self._performance_v2_maintenance_active_job = None

    def strategies_performance_v2_maintenance_status(self, job_id: object) -> dict[str, object]:
        if not isinstance(job_id, str) or not job_id:
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="job_id is required")
        with self._performance_v2_maintenance_state_lock:
            job = self._performance_v2_maintenance_job
            if job is None or job.get("job_id") != job_id:
                raise PerformanceV2ApiError("MAINTENANCE_JOB_NOT_FOUND", status=404, message="maintenance job is unavailable")
            result = deepcopy(job)
            result["elapsed_seconds"] = (
                max(0.0, perf_counter() - float(job["started_at"]))
                if job.get("status") == "RUNNING" else float(job["elapsed_seconds"])
            )
            result.pop("started_at", None)
            return result

    @staticmethod
    def _bulk_retest_range(payload: Mapping[str, object], listing_dates: Mapping[str, object], connection: duckdb.DuckDBPyConnection, include_reserve: bool, decisions: Mapping[int, tuple] | None = None) -> tuple[str, str]:
        allowed = {"include_reserve", "clear_reports", "test_start", "test_end", "start_date", "end_date"}
        if set(payload).difference(allowed):
            raise FinalistRetestError("INVALID_REQUEST", "bulk retest request contains unsupported fields")
        if type(include_reserve) is not bool:
            raise FinalistRetestError("INVALID_REQUEST", "include_reserve must be a boolean")
        start_keys = [key for key in ("test_start", "start_date") if key in payload]
        end_keys = [key for key in ("test_end", "end_date") if key in payload]
        if len(start_keys) > 1 or len(end_keys) > 1 or bool(start_keys) != bool(end_keys):
            raise FinalistRetestError("INVALID_TEST_RANGE", "bulk retest dates must be supplied together")
        if start_keys:
            start = PanelController._retest_date(payload[start_keys[0]], "test_start")
            end = PanelController._retest_date(payload[end_keys[0]], "test_end")
        else:
            end = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
            provisional = freeze_finalist_cohort(
                connection, test_start="1970-01-01", test_end=end,
                include_reserve=include_reserve, listing_dates=listing_dates, decisions=decisions,
            )
            if not provisional.members:
                raise FinalistRetestError("COHORT_EMPTY", "bulk retest cohort has no runnable members")
            starts = [str(member["listing_date_utc"])[:10] for member in provisional.members]
            start = min(starts)
        if start >= end:
            raise FinalistRetestError("INVALID_TEST_RANGE", "test_start must be before test_end")
        return start, end

    def strategies_performance_v2_finalist_retest_start(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise FinalistRetestError("INVALID_REQUEST", "bulk retest request must be an object")
        if set(payload).difference({"include_reserve", "clear_reports", "test_start", "test_end", "start_date", "end_date"}):
            raise FinalistRetestError("INVALID_REQUEST", "bulk retest request contains unsupported fields")
        include_reserve = payload.get("include_reserve", False)
        clear_reports = payload.get("clear_reports", False)
        if type(clear_reports) is not bool:
            raise FinalistRetestError("INVALID_REQUEST", "clear_reports must be a boolean")
        initial_balance = self._requested_initial_balance(payload)
        listing_path, listing_relative, listing_sheet = self._retest_listing_context()
        try:
            listing_dates = (
                load_listing_dates(listing_path) if listing_sheet is None else load_listing_dates(listing_path, listing_sheet)
            )
        except Exception as error:
            raise FinalistRetestError("LISTING_DATE_INVALID", "listing dates are invalid") from error
        config = self._performance_v2_config()
        try:
            selection_config = load_selection_config(self.default_config.with_name("config.performance.json"))
        except PerformanceV2SelectionError:
            # A legacy/test installation may not have the optional panel config;
            # freeze the same library defaults used by the selection pipeline.
            selection_config = SelectionConfig()
        regime_policy = equity_regime_policy_snapshot()
        cache_workers = max(1, min(int(getattr(config, "workers", 16)), 16))
        target = performance_v2_database_path(config)
        if not target.is_file():
            raise FinalistRetestError("PERFORMANCE_V2_NOT_FOUND", "Performance v2 database is unavailable")
        evaluation = self._evaluation_target()
        templates = self._mrs3_strategy_templates(evaluation)
        self._require_protocol_v2_tester_template(evaluation)
        tester_template = self._mrs3_tester_template(evaluation)
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            decisions = self._registry_finalist_decisions(connection)
            start, end = self._bulk_retest_range(payload, listing_dates, connection, include_reserve, decisions)
            current_cohort = freeze_finalist_cohort(
                connection, test_start=start, test_end=end,
                include_reserve=include_reserve, listing_dates=listing_dates, decisions=decisions,
            )
        if not current_cohort.members:
            raise FinalistRetestError("COHORT_EMPTY", "bulk retest cohort has no runnable members")
        current_config_sha256 = finalist_retest_config_digest(
            templates if isinstance(templates, Mapping) else {}, evaluation=evaluation,
        )
        for existing in self._panel_jobs.list():
            if existing.get("kind") != "strategies.performance.v2.finalist-retest":
                continue
            try:
                old_runtime = self._panel_jobs.runtime(str(existing.get("job_id")))
            except PanelJobError:
                continue
            same_request = (
                self._matches_protocol_v2(old_runtime, evaluation)
                and old_runtime.get("scope") == current_cohort.scope
                and old_runtime.get("test_start") == start
                and old_runtime.get("test_end") == end
                and old_runtime.get("cohort_sha256") == current_cohort.cohort_sha256
                and old_runtime.get("config_sha256") == current_config_sha256
                and old_runtime.get("initial_balance") == initial_balance
            )
            if not same_request:
                continue
            if existing.get("state") in {"QUEUED", "RUNNING", "CANCELLING"}:
                raise PanelJobError("RESOURCE_BUSY")
            if existing.get("state") == "COMMITTED" and old_runtime.get("outcomes_finalized") is True and old_runtime.get("successful_replacements"):
                return self._bulk_retest_status_document(str(existing["job_id"]))
            # Terminal all-failure jobs intentionally fall through and create
            # a new frozen run for a fresh retry.
        run_id = f"finalist-retest-{uuid.uuid4().hex}"
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            batch = build_finalist_retest_manifest(
                connection, templates if isinstance(templates, Mapping) else {},
                config.strategy_root.parent if config.strategy_root is not None else self.root / "Output",
                test_start=start, test_end=end, include_reserve=include_reserve,
                listing_dates=listing_dates, job_id=run_id, evaluation=evaluation,
                decisions=decisions,             # the same registry marks as the cohort checked above
            )
        runtime = {
            "bulk_retest": True, "scope": batch.cohort.scope, "cohort_sha256": batch.cohort.cohort_sha256,
            **self._protocol_v2_runtime(evaluation),
            "config_sha256": batch.config_sha256, "manifest_sha256": sha256(batch.manifest_path.read_bytes()).hexdigest(),
            "test_start": start, "test_end": end, "listing_dates_path": str(listing_relative),
            **({"listing_dates_sheet": listing_sheet} if listing_sheet is not None else {}),
            "initial_balance": initial_balance,
            "manifest_path": str(batch.manifest_path), "cohort_members": _json_value([dict(member) for member in batch.cohort.members]),
            "exclusions": [item.as_dict() for item in batch.cohort.exclusions],
            "successful_replacements": [], "failures": [item.as_dict() for item in batch.cohort.exclusions],
            "equity_filter_snapshot": {
                "schema_version": 1,
                "request": {"stages": [{"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"}]},
                "selection_config": _json_value(asdict(selection_config)),
                "selection_config_sha256": canonical_digest(asdict(selection_config)),
                "regime_policy": _json_value(regime_policy),
                "regime_policy_sha256": canonical_digest(regime_policy),
                "algorithm_version": _EQUITY_REGIME_ALGORITHM_VERSION,
                "cache_workers": cache_workers,
            },
        }
        request = {"scope": batch.cohort.scope, "test_start": start, "test_end": end, "retest": True, "clear_reports": clear_reports, "initial_balance": initial_balance}
        return self._start_tracked_panel_job(
            "strategies.performance.v2.finalist-retest", request,
            ("strategies.tester", "performance-v2-finalist-retest"),
            lambda job_id: self._single_mode_strategy_test().start(
                batch.manifest_path, analysis_run_id=batch.run_id, start_date=start, end_date=end, job_id=job_id,
                tester_config_template=tester_template,
                clear_reports=clear_reports,
                **({"initial_balance": initial_balance} if initial_balance is not None else {}),
            ), runtime=runtime,
        )

    def strategies_performance_v2_finalist_retest_preview(self, include_reserve: bool = False) -> dict[str, object]:
        if type(include_reserve) is not bool:
            raise FinalistRetestError("INVALID_REQUEST", "include_reserve must be a boolean")
        listing_path, _, listing_sheet = self._retest_listing_context()
        try:
            listing_dates = (
                load_listing_dates(listing_path) if listing_sheet is None else load_listing_dates(listing_path, listing_sheet)
            )
        except Exception as error:
            raise FinalistRetestError("LISTING_DATE_INVALID", "listing dates are invalid") from error
        target = performance_v2_database_path(self._performance_v2_config())
        if not target.is_file():
            raise FinalistRetestError("PERFORMANCE_V2_NOT_FOUND", "Performance v2 database is unavailable")
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            decisions = self._registry_finalist_decisions(connection)
            start, end = self._bulk_retest_range({}, listing_dates, connection, include_reserve, decisions)
            cohort = freeze_finalist_cohort(
                connection, test_start=start, test_end=end,
                include_reserve=include_reserve, listing_dates=listing_dates, decisions=decisions,
            )
        return {
            "scope": cohort.scope,
            "test_start": start,
            "test_end": end,
            "cohort_count": cohort.member_count,
            "excluded_count": cohort.excluded_count,
        }

    # Short aliases keep the HTTP/controller surface discoverable for clients
    # that call the feature "bulk retest".
    strategies_performance_v2_bulk_retest_start = strategies_performance_v2_finalist_retest_start

    def _reconcile_bulk_retest_import(
        self, parent_job_id: str, runtime: dict[str, object],
    ) -> tuple[str | None, bool, dict[str, object] | None]:
        """Resolve a preallocated pending child ID once its job is durable."""
        marker = runtime.get("bulk_import_job_id")
        if not isinstance(marker, str) or not marker:
            return None, False, None
        pending = marker.startswith("pending:")
        child_id = marker.removeprefix("pending:") if pending else marker
        try:
            child = self._panel_jobs.get(child_id)
        except PanelJobError:
            return (None, True, None) if pending else (marker, False, None)
        if pending:
            runtime["bulk_import_job_id"] = child_id
            parent_state = self._panel_jobs.get(parent_job_id).get("state", "COMMITTED")
            self._panel_jobs.sync(parent_job_id, {"state": parent_state}, runtime=runtime)
        return child_id, False, child

    def _bulk_retest_status_document(self, job_id: str) -> dict[str, object]:
        tracked = self._panel_jobs.get(job_id)
        runtime = self._panel_jobs.runtime(job_id)
        if tracked.get("kind") != "strategies.performance.v2.finalist-retest" or runtime.get("bulk_retest") is not True:
            raise ValueError("job is not a bulk finalist RETEST")
        try:
            status = self._single_mode_strategy_test().status(job_id)
            self._record_special_job(status)
            self._sync_tracked_panel_job(status)
            tracked = self._panel_jobs.get(job_id)
            runtime = self._panel_jobs.runtime(job_id)
        except (KeyError, ValueError):
            pass
        import_job_id, import_pending, import_job = self._reconcile_bulk_retest_import(job_id, runtime)
        self._refresh_bulk_retest_outcomes(job_id)
        tracked = self._panel_jobs.get(job_id)
        runtime = self._panel_jobs.runtime(job_id)
        bulk_error = None
        if runtime.get("outcomes_finalized") is True and not runtime.get("successful_replacements"):
            bulk_error = {"code": "RETEST_COHORT_NO_SUCCESSFUL_MEMBERS", "message": "no finalist cohort member imported successfully"}
            runtime["error_code"] = bulk_error["code"]
        if import_pending:
            import_job_id = None
            import_job = None
        cohort_count = len(runtime.get("cohort_members", [])) if isinstance(runtime.get("cohort_members"), list) else 0
        imported_count = runtime.get("imported_count")
        skipped_count = runtime.get("skipped_count")
        rejected_count = runtime.get("rejected_count")
        has_import_counts = any(type(count) is int and count >= 0 for count in (imported_count, skipped_count, rejected_count))
        expected_count = sum(
            int(count) for count in (imported_count, skipped_count, rejected_count)
            if type(count) is int and count >= 0
        ) if has_import_counts else cohort_count
        document = {
            **tracked,
            "scope": runtime.get("scope"), "cohort_sha256": runtime.get("cohort_sha256"),
            "cohort_count": cohort_count,
            "expected_count": expected_count,
            "imported_count": imported_count if type(imported_count) is int else len(runtime.get("successful_replacements", [])) if isinstance(runtime.get("successful_replacements"), list) else 0,
            "skipped_count": skipped_count if type(skipped_count) is int else 0,
            "rejected_count": rejected_count if type(rejected_count) is int else len(runtime.get("failures", [])) if isinstance(runtime.get("failures"), list) else 0,
            "success_count": len(runtime.get("successful_replacements", [])) if isinstance(runtime.get("successful_replacements"), list) else 0,
            "failure_count": len(runtime.get("failures", [])) if isinstance(runtime.get("failures"), list) else 0,
            "test_start": runtime.get("test_start"), "test_end": runtime.get("test_end"),
            "bulk_retest": True,
            "outcomes_finalized": runtime.get("outcomes_finalized") is True,
            "successful_replacements": runtime.get("successful_replacements", []),
            "failures": runtime.get("failures", []),
            "import_job_id": import_job_id,
            "import_job_state": import_job.get("state") if isinstance(import_job, Mapping) else None,
            "import_job_error": import_job.get("error") if isinstance(import_job, Mapping) else None,
            "import_pending": import_pending,
            "error": bulk_error or tracked.get("error"),
        }
        equity_filter = self._equity_filter_child_status(job_id, runtime)
        document["equity_filter"] = equity_filter
        document["equity_filter_job_id"] = equity_filter.get("job_id") if equity_filter else None
        document["equity_filter_state"] = equity_filter.get("state") if equity_filter else None
        document["equity_filter_phase"] = equity_filter.get("phase") if equity_filter else None
        document["equity_filter_progress"] = equity_filter.get("progress") if equity_filter else None
        document["equity_filter_result"] = equity_filter.get("result") if equity_filter else None
        document["equity_filter_error"] = equity_filter.get("error") if equity_filter else None
        return document

    def _equity_filter_child_status(
        self, parent_job_id: str, runtime: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        current = runtime if isinstance(runtime, Mapping) else self._panel_jobs.runtime(parent_job_id)
        raw_id = current.get("equity_filter_job_id")
        attempt = current.get("equity_filter_attempt")
        attempt = int(attempt) if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else 0
        application_key = current.get("equity_filter_application_key")
        if not isinstance(application_key, str) or not application_key:
            application_key = None
            if (
                current.get("outcomes_finalized") is True
                and isinstance(current.get("successful_replacements"), list)
                and current.get("successful_replacements")
                and isinstance(current.get("equity_filter_source_revision_digest"), str)
                and current.get("equity_filter_source_revision_digest")
            ):
                seed = self._equity_filter_identity_seed(current)
                application_key = sha256(f"bulk-retest-equity-filter-v1:{parent_job_id}:{seed}".encode()).hexdigest()
        eligible = True
        reason: str | None = None
        if current.get("equity_filter_published") is True:
            eligible, reason = False, "ACTION_COMMITTED"
        elif current.get("outcomes_finalized") is not True:
            eligible, reason = False, "IMPORT_NOT_FINALIZED"
        elif not isinstance(current.get("successful_replacements"), list) or not current.get("successful_replacements"):
            eligible, reason = False, "NO_SUCCESSFUL_MEMBERS"
        elif not isinstance(current.get("equity_filter_snapshot"), Mapping):
            eligible, reason = False, "CONFIG_MISSING"
        try:
            parent = self._panel_jobs.get(parent_job_id)
            if parent.get("state") != "COMMITTED":
                eligible, reason = False, "RETEST_COHORT_JOB_NOT_COMMITTED"
        except PanelJobError:
            eligible, reason = False, "RETEST_COHORT_JOB_NOT_FOUND"
        default = {
            "job_id": raw_id if isinstance(raw_id, str) and raw_id else None,
            "application_key": application_key,
            "attempt": attempt,
            "eligible": eligible,
            "ineligible_reason": reason,
            "reason": reason,
            "state": "IDLE",
            "phase": None,
            "progress": {"total_pairs": 0, "completed_pairs": 0, "total_strategies": len(current.get("successful_replacements", [])) if isinstance(current.get("successful_replacements"), list) else 0, "processed_strategies": 0, "ready_cache_count": 0, "warmed_strategy_count": 0, "current_pair": None},
            "result": None,
            "error": None,
        }
        if not isinstance(raw_id, str) or not raw_id:
            return default
        try:
            child = self._panel_jobs.get(raw_id)
        except PanelJobError:
            default.update({"state": "FAILED", "phase": "FAILED", "error": {"code": "RETEST_EQUITY_FILTER_FAILED", "message": "equity filter child job is unavailable"}, "eligible": not bool(current.get("equity_filter_published")), "ineligible_reason": None})
            return default
        state = child.get("state") if isinstance(child.get("state"), str) else "FAILED"
        raw_progress = child.get("progress") if isinstance(child.get("progress"), Mapping) else {}
        if {"total_pairs", "completed_pairs", "total_strategies", "processed_strategies"}.issubset(raw_progress):
            progress = dict(raw_progress)
        else:
            progress = dict(default["progress"])
            unit = raw_progress.get("unit")
            current_units = int(raw_progress.get("current") or 0)
            total = int(raw_progress.get("total") or 0)
            if unit == "strategies":
                progress.update(total_strategies=total, processed_strategies=current_units)
            else:
                progress.update(total_pairs=total, completed_pairs=current_units)
            progress.update(current=current_units, total=total, unit=unit or "pairs")
            if isinstance(raw_progress.get("current_pair"), str):
                progress["current_pair"] = raw_progress["current_pair"]
        default.update({
            "state": state,
            "phase": child.get("phase") if isinstance(child.get("phase"), str) else state,
            "progress": progress,
            "result": child.get("result") if isinstance(child.get("result"), Mapping) else None,
            "error": child.get("error") if isinstance(child.get("error"), Mapping) else None,
        })
        if state in {"QUEUED", "RUNNING", "CANCELLING"}:
            default.update({"eligible": False, "reason": "ACTION_RUNNING", "ineligible_reason": "ACTION_RUNNING"})
        elif state == "COMMITTED":
            default.update({"eligible": False, "reason": "ACTION_COMMITTED", "ineligible_reason": "ACTION_COMMITTED"})
        elif state in {"FAILED", "CANCELLED"} and current.get("equity_filter_published") is not True:
            default.update({"eligible": True, "reason": None, "ineligible_reason": None})
        return default

    def _refresh_bulk_retest_outcomes(self, job_id: str) -> None:
        """Persist redacted per-member outcomes after the native import worker ends."""
        try:
            runtime = self._panel_jobs.runtime(job_id)
        except PanelJobError:
            return
        child_id = runtime.get("bulk_import_job_id")
        if not isinstance(child_id, str) or not child_id or child_id.startswith("pending:"):
            return
        if runtime.get("outcomes_finalized") is True:
            return
        try:
            child = self._panel_jobs.get(child_id)
            child_runtime = self._panel_jobs.runtime(child_id)
        except PanelJobError:
            return
        if child.get("state") not in {"COMMITTED", "FAILED", "CANCELLED"}:
            return
        members = [member for member in runtime.get("cohort_members", []) if isinstance(member, Mapping)]
        failures = [dict(item) for item in runtime.get("exclusions", []) if isinstance(item, Mapping)]
        imported_successes = child_runtime.get("successful_replacements")
        imported_failures = child_runtime.get("failures")
        child_result = child.get("result")
        if isinstance(child_result, Mapping):
            for key in ("imported_count", "skipped_count", "rejected_count"):
                count = child_result.get(key)
                if type(count) is int and count >= 0:
                    runtime[key] = count
        if isinstance(imported_successes, list) and isinstance(imported_failures, list):
            runtime["successful_replacements"] = [dict(item) for item in imported_successes if isinstance(item, Mapping)]
            runtime["failures"] = failures + [dict(item) for item in imported_failures if isinstance(item, Mapping)]
            runtime["outcomes_finalized"] = True
            try:
                self._panel_jobs.sync(job_id, {"state": self._panel_jobs.get(job_id).get("state", "COMMITTED")}, runtime=runtime)
            except PanelJobError:
                pass
            return
        failed_names: set[str] = set()
        report_path = child_runtime.get("failure_report_path")
        if isinstance(report_path, str) and report_path:
            try:
                with Path(report_path).open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        name = row.get("strategy_name", "")
                        if name:
                            failed_names.add(name)
                            failures.append({
                                "strategy_id": next((int(member["strategy_id"]) for member in members if member.get("strategy_name") == name), None),
                                "strategy_name": name, "reason": row.get("reason") or "IMPORT_FAILED",
                            })
            except (OSError, UnicodeDecodeError, csv.Error, TypeError, ValueError):
                pass
        successful: list[dict[str, int]] = []
        expected_by_name = {str(member["strategy_name"]): member for member in members}
        config = self._performance_v2_config()
        target = performance_v2_database_path(config)
        current: dict[int, int | None] = {}
        if child.get("state") == "COMMITTED" and target.is_file():
            try:
                with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                    current = {int(strategy_id): (None if result_id is None else int(result_id)) for strategy_id, result_id in connection.execute("select strategy_id, current_result_id from strategies").fetchall()}
            except duckdb.Error:
                return
        for name, member in expected_by_name.items():
            strategy_id, old_result = int(member["strategy_id"]), int(member["result_id"])
            if name in failed_names:
                continue
            if child.get("state") == "COMMITTED" and current.get(strategy_id) == old_result:
                successful.append({"strategy_id": strategy_id, "old_result_id": old_result, "new_result_id": old_result})
            elif child.get("state") == "COMMITTED":
                failures.append({"strategy_id": strategy_id, "strategy_name": name, "reason": "STALE_RESULT"})
            else:
                failures.append({"strategy_id": strategy_id, "strategy_name": name, "reason": "IMPORT_FAILED"})
        runtime["successful_replacements"] = successful
        runtime["failures"] = failures
        runtime["outcomes_finalized"] = True
        try:
            self._panel_jobs.sync(job_id, {"state": self._panel_jobs.get(job_id).get("state", "COMMITTED")}, runtime=runtime)
        except PanelJobError:
            pass

    def strategies_performance_v2_finalist_retest_status(self, job_id: str) -> dict[str, object]:
        return self._bulk_retest_status_document(self._required({"job_id": job_id}, "job_id"))

    strategies_performance_v2_bulk_retest_status = strategies_performance_v2_finalist_retest_status

    def strategies_performance_v2_finalist_retest_import(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._bulk_retest_import_lock:
            return self._strategies_performance_v2_finalist_retest_import_locked(payload)

    def _strategies_performance_v2_finalist_retest_import_locked(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload, Mapping) or set(payload) != {"tester_job_id"}:
            raise ValueError("bulk RETEST import accepts only tester_job_id")
        tester_job_id = self._required(payload, "tester_job_id")
        tracked = self._panel_jobs.get(tester_job_id)
        runtime = self._panel_jobs.runtime(tester_job_id)
        if tracked.get("kind") != "strategies.performance.v2.finalist-retest" or runtime.get("bulk_retest") is not True:
            raise ValueError("tester job is not a bulk finalist RETEST")
        if tracked.get("state") != "COMMITTED":
            raise ValueError("bulk RETEST tester job is not committed")
        if tracked.get("inbox_ready") is not True and runtime.get("inbox_ready") is not True:
            self.strategies_tester_verify_inbox(tester_job_id)
            tracked = self._panel_jobs.get(tester_job_id)
            runtime = self._panel_jobs.runtime(tester_job_id)
        if tracked.get("inbox_ready") is not True and runtime.get("inbox_ready") is not True:
            raise ValueError("bulk RETEST tester inbox is not committed")
        previous = runtime.get("bulk_import_job_id")
        if isinstance(previous, str) and previous.startswith("pending:"):
            previous, unresolved, _child = self._reconcile_bulk_retest_import(tester_job_id, runtime)
            if unresolved:
                raise PanelJobError("RUNTIME_BUSY")
            runtime = self._panel_jobs.runtime(tester_job_id)
        retrying_import = False
        if isinstance(previous, str) and previous and not previous.startswith("pending:"):
            try:
                previous_job = self._panel_jobs.get(previous)
            except PanelJobError:
                previous_job = None
            if isinstance(previous_job, Mapping) and previous_job.get("state") not in {"FAILED", "CANCELLED"}:
                return previous_job
            if isinstance(previous_job, Mapping):
                self._panel_jobs.clear_runtime(tester_job_id, "bulk_import_job_id", value=previous)
                retrying_import = True
        names = {str(member["strategy_name"]): int(member["strategy_id"]) for member in runtime.get("cohort_members", []) if isinstance(member, Mapping)}
        expected_results = {str(member["strategy_name"]): int(member["result_id"]) for member in runtime.get("cohort_members", []) if isinstance(member, Mapping)}
        start, end = runtime.get("test_start"), runtime.get("test_end")
        if not isinstance(start, str) or not isinstance(end, str) or not names:
            raise ValueError("bulk RETEST provenance is incomplete")
        child_id = uuid.uuid4().hex
        pending = f"pending:{child_id}"
        queued_child_id: str | None = None
        try:
            self._panel_jobs.reserve_runtime(tester_job_id, "bulk_import_job_id", pending)
            if retrying_import:
                runtime = self._panel_jobs.runtime(tester_job_id)
                runtime["outcomes_finalized"] = False
                runtime["successful_replacements"] = []
                runtime["failures"] = [
                    dict(item) for item in runtime.get("exclusions", []) if isinstance(item, Mapping)
                ]
                self._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)
            result = self.strategies_performance_v2_import({
                "tester_job_id": tester_job_id, "mode": "REPLACE", "replacement_strategy_ids": names,
                "clear_retest_on_success": False, "test_start": start, "test_end": end, "_retest": True,
                "_expected_current_result_ids": expected_results,
            }, _internal=True, job_id=child_id)
            queued_child_id = result.get("job_id") if isinstance(result, Mapping) else None
            if queued_child_id != child_id:
                raise ValueError("bulk RETEST import returned an invalid job")
            runtime = self._panel_jobs.runtime(tester_job_id)
            runtime["bulk_import_job_id"] = child_id
            self._panel_jobs.sync(tester_job_id, {"state": "COMMITTED"}, runtime=runtime)
            return result
        except BaseException:
            try:
                self._panel_jobs.get(child_id)
                child_exists = True
            except PanelJobError:
                child_exists = False
            if not child_exists and queued_child_id is None:
                try:
                    self._panel_jobs.clear_runtime(tester_job_id, "bulk_import_job_id", value=pending)
                except PanelJobError:
                    pass
            raise

    strategies_performance_v2_bulk_retest_import = strategies_performance_v2_finalist_retest_import

    @staticmethod
    def _equity_filter_positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FinalistRetestError("RETEST_COHORT_INVALID", f"{field} must be a positive integer")
        return value

    @staticmethod
    def _equity_filter_snapshot_config(raw: Mapping[str, object]) -> object:
        if raw.get("schema_version") != 1:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_UNSUPPORTED", "frozen equity configuration schema is unsupported")
        expected_request = {"stages": [{"id": "filter_equity_regime", "enabled": True, "scope": "pair_side"}]}
        if raw.get("request") != expected_request:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "frozen equity request is invalid")
        if raw.get("algorithm_version") != _EQUITY_REGIME_ALGORITHM_VERSION:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_UNSUPPORTED", "frozen equity algorithm is unsupported")
        policy = raw.get("regime_policy")
        if not isinstance(policy, Mapping) or raw.get("regime_policy_sha256") != canonical_digest(policy):
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "frozen equity policy digest is invalid")
        if dict(policy) != equity_regime_policy_snapshot():
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_UNSUPPORTED", "frozen equity policy is unsupported")
        snapshot = raw.get("selection_config")
        if not isinstance(snapshot, Mapping):
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "bulk retest has no frozen equity configuration")
        if raw.get("selection_config_sha256") != canonical_digest(snapshot):
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "frozen selection configuration digest is invalid")
        defaults = SelectionConfig()
        values: dict[str, object] = {}
        for name, default in asdict(defaults).items():
            value = snapshot.get(name, default)
            if isinstance(default, Decimal):
                try:
                    value = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise FinalistRetestError("EQUITY_SCHEMA_UPGRADE_REQUIRED", "frozen equity configuration is invalid") from error
            elif isinstance(default, bool):
                if type(value) is not bool:
                    raise FinalistRetestError("EQUITY_SCHEMA_UPGRADE_REQUIRED", "frozen equity configuration is invalid")
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise FinalistRetestError("EQUITY_SCHEMA_UPGRADE_REQUIRED", "frozen equity configuration is invalid")
            values[name] = value
        try:
            return SelectionConfig(**values)
        except (TypeError, ValueError) as error:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "frozen equity configuration is invalid") from error

    def _equity_filter_validate_cohort(
        self, bulk_job_id: str, runtime: Mapping[str, object], target: Path,
    ) -> tuple[dict[tuple[str, str], dict[int, dict[str, object]]], dict[int, tuple[int, str]], str]:
        if runtime.get("outcomes_finalized") is not True:
            raise FinalistRetestError("RETEST_IMPORT_NOT_FINALIZED", "bulk RETEST import outcomes are not finalized")
        raw_members = runtime.get("cohort_members")
        raw_successes = runtime.get("successful_replacements")
        if not isinstance(raw_members, list) or not isinstance(raw_successes, list):
            raise FinalistRetestError("RETEST_COHORT_INVALID", "bulk RETEST cohort provenance is invalid")
        members: dict[int, dict[str, object]] = {}
        for raw in raw_members:
            if not isinstance(raw, Mapping):
                raise FinalistRetestError("RETEST_COHORT_INVALID", "cohort member is invalid")
            strategy_id = self._equity_filter_positive_int(raw.get("strategy_id"), "strategy_id")
            result_id = self._equity_filter_positive_int(raw.get("result_id"), "result_id")
            symbol, side = raw.get("symbol"), raw.get("side")
            if not isinstance(symbol, str) or not symbol or side not in {"LONG", "SHORT"}:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "cohort member pair is invalid")
            if strategy_id in members:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "cohort contains duplicate strategies")
            members[strategy_id] = {**dict(raw), "strategy_id": strategy_id, "result_id": result_id, "symbol": symbol, "side": side}
        if not members:
            raise FinalistRetestError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS", "bulk RETEST cohort is empty")
        successes: dict[int, dict[str, object]] = {}
        for raw in raw_successes:
            if not isinstance(raw, Mapping):
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacement is invalid")
            strategy_id = self._equity_filter_positive_int(raw.get("strategy_id"), "strategy_id")
            new_result_id = self._equity_filter_positive_int(raw.get("new_result_id", raw.get("result_id")), "new_result_id")
            member = members.get(strategy_id)
            if member is None:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacement is outside the frozen cohort")
            if strategy_id in successes:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacements contain duplicate strategies")
            old_result_id = raw.get("old_result_id")
            if old_result_id is not None and (
                isinstance(old_result_id, bool) or not isinstance(old_result_id, int) or old_result_id != member["result_id"]
            ):
                raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", "successful replacement has a stale old result")
            successes[strategy_id] = {**dict(raw), "strategy_id": strategy_id, "new_result_id": new_result_id}
        if not successes:
            raise FinalistRetestError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS", "bulk RETEST has no successful replacements")
        grouped: dict[tuple[str, str], dict[int, dict[str, object]]] = {}
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            require_performance_v2(connection)
            ids = sorted(successes)
            current_rows = connection.execute(
                "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[])) and lifecycle_status = 'ACTIVE'",
                [ids],
            ).fetchall()
            current = {int(row[0]): (None if row[1] is None else int(row[1])) for row in current_rows}
            if set(current) != set(ids):
                raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", "successful strategy is missing or inactive")
            result_rows = connection.execute(
                "select result_id, strategy_id from strategy_results where result_id in (select unnest(?::bigint[]))",
                [[int(item["new_result_id"]) for item in successes.values()]],
            ).fetchall()
            result_by_id = {int(row[0]): int(row[1]) for row in result_rows}
            for strategy_id, outcome in successes.items():
                result_id = int(outcome["new_result_id"])
                if result_by_id.get(result_id) != strategy_id or current[strategy_id] != result_id:
                    raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", "current strategy result does not match the imported replacement")
                member = members[strategy_id]
                grouped.setdefault((str(member["symbol"]), str(member["side"])), {})[strategy_id] = member
            try:
                source_revisions = _current_equity_revisions(connection, ids)
            except (SelectionReviewError, EquitySourceChangedError, duckdb.Error) as error:
                raise FinalistRetestError("EQUITY_SOURCE_CHANGED", "equity source revision is unreadable") from error
            if set(source_revisions) != set(ids):
                raise FinalistRetestError("EQUITY_SOURCE_CHANGED", "equity source revision is unavailable")
        identity = canonical_digest({
            "bulk_job_id": bulk_job_id,
            "scope": runtime.get("scope"),
            "cohort_sha256": runtime.get("cohort_sha256"),
            "pairs": [[strategy_id, int(successes[strategy_id]["new_result_id"])] for strategy_id in sorted(successes)],
            "source_revisions": {str(strategy_id): list(source_revisions[strategy_id]) for strategy_id in sorted(source_revisions)},
        })
        return grouped, source_revisions, identity

    def _equity_filter_assert_owner(
        self, bulk_job_id: str, child_id: str, *, warm_deadline: float | None = None,
    ) -> None:
        with self._bulk_retest_equity_filter_lock:
            runtime = self._panel_jobs.runtime(bulk_job_id)
            if runtime.get("equity_filter_lock_holder") != child_id:
                raise FinalistRetestError("RETEST_EQUITY_ACTION_SUPERSEDED", "equity filter action was superseded")
            raw_expiry = runtime.get("equity_filter_lock_expires_at")
            try:
                expired = not isinstance(raw_expiry, str) or datetime.fromisoformat(raw_expiry).timestamp() <= datetime.now(timezone.utc).timestamp()
            except ValueError:
                expired = True
            if expired:
                raise FinalistRetestError("RETEST_EQUITY_ACTION_SUPERSEDED", "equity filter action lease expired")
            if warm_deadline is not None and perf_counter() > warm_deadline:
                raise FinalistRetestError("RETEST_EQUITY_CACHE_WARM_TIMEOUT", "equity cache warming exceeded its deadline")
            heartbeat = datetime.now(timezone.utc)
            runtime["equity_filter_heartbeat_at"] = heartbeat.isoformat()
            runtime["equity_filter_lock_expires_at"] = (heartbeat + timedelta(seconds=_BULK_RETEST_EQUITY_FILTER_LEASE_SECONDS)).isoformat()
            parent = self._panel_jobs.get(bulk_job_id)
            if runtime.get("equity_filter_lock_holder") != child_id:
                raise FinalistRetestError("RETEST_EQUITY_ACTION_SUPERSEDED", "equity filter action was superseded")
            self._panel_jobs.sync(
                bulk_job_id,
                {"state": parent.get("state", "COMMITTED"), "phase": parent.get("phase", "COMMITTED")},
                runtime=runtime,
            )

    def _run_bulk_retest_equity_filter(self, child_id: str, bulk_job_id: str) -> dict[str, object]:
        parent = self._panel_jobs.get(bulk_job_id)
        runtime = self._panel_jobs.runtime(bulk_job_id)
        if runtime.get("scope") not in {"FINALIST", "FINALIST_RESERVE"}:
            raise FinalistRetestError("RETEST_COHORT_INVALID", "bulk RETEST scope is not a finalist cohort")
        snapshot = runtime.get("equity_filter_snapshot")
        if not isinstance(snapshot, Mapping):
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_MISSING", "bulk RETEST has no frozen equity snapshot")
        selection_config = self._equity_filter_snapshot_config(snapshot)
        workers = snapshot.get("cache_workers")
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "frozen cache worker limit is invalid")
        target = performance_v2_database_path(self._performance_v2_config())
        if not target.is_file():
            raise FinalistRetestError("PERFORMANCE_V2_NOT_FOUND", "Performance v2 database is unavailable")
        self._equity_filter_assert_owner(bulk_job_id, child_id)
        self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "VALIDATING_COHORT", "progress": {"current": 0, "total": 0, "unit": "pairs"}})
        grouped, source_revisions, identity = self._equity_filter_validate_cohort(bulk_job_id, runtime, target)
        current_source_digest = canonical_digest({str(strategy_id): list(source_revisions[strategy_id]) for strategy_id in sorted(source_revisions)})
        if runtime.get("equity_filter_source_revision_digest") != current_source_digest:
            raise FinalistRetestError("EQUITY_SOURCE_CHANGED", "equity source revision changed after action was queued")
        expected_application_key = sha256(
            f"bulk-retest-equity-filter-v1:{bulk_job_id}:{self._equity_filter_identity_seed(runtime)}".encode()
        ).hexdigest()
        if runtime.get("equity_filter_application_key") != expected_application_key:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "equity filter application identity is invalid")
        pair_total = len(grouped)
        strategy_total = sum(len(group) for group in grouped.values())
        request_stage = SelectionStage("filter_equity_regime", True, "pair_side")
        successful_by_id: dict[int, Mapping[str, object]] = {}
        for raw_item in runtime.get("successful_replacements", []):
            if not isinstance(raw_item, Mapping):
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacement is invalid")
            raw_strategy_id = raw_item.get("strategy_id")
            if isinstance(raw_strategy_id, bool) or not isinstance(raw_strategy_id, int) or raw_strategy_id <= 0:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacement strategy id is invalid")
            if raw_strategy_id in successful_by_id:
                raise FinalistRetestError("RETEST_COHORT_INVALID", "successful replacements contain duplicate strategies")
            successful_by_id[raw_strategy_id] = raw_item

        grouped_strategy_ids = {strategy_id for members in grouped.values() for strategy_id in members}
        replacement_result_ids: dict[int, int] = {}
        for strategy_id in sorted(grouped_strategy_ids):
            item = successful_by_id.get(strategy_id)
            if item is None:
                raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", "successful replacement is missing from the frozen cohort")
            raw_result_id = item.get("new_result_id", item.get("result_id"))
            if isinstance(raw_result_id, bool) or not isinstance(raw_result_id, int) or raw_result_id <= 0:
                raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", "successful replacement result id is invalid")
            replacement_result_ids[strategy_id] = raw_result_id
        requests: dict[tuple[str, str], SelectionRequest] = {
            key: retest_cohort_request(
                SelectionRequest(key[0], key[1], (request_stage,)), bulk_job_id,
                {strategy_id: replacement_result_ids[strategy_id] for strategy_id in members},
            )
            for key, members in grouped.items()
        }
        self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "SCANNING_CACHE", "progress": {"current": 0, "total": pair_total, "unit": "pairs"}})
        cache_info: dict[tuple[str, str], tuple[dict[str, object], tuple[int, ...]]] = {}
        ready_cache_count = 0
        warmed = 0
        for index, key in enumerate(sorted(requests), start=1):
            self._equity_filter_assert_owner(bulk_job_id, child_id)
            request = requests[key]
            with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                status = selection_cache_status(connection, request, selection_config, include_readiness_breakdown=True)
                missing = selection_cache_missing_strategy_ids(connection, request, selection_config, include_equity_regime=True)
            cache_info[key] = (status, missing)
            ready_cache_count += max(0, len(grouped[key]) - len(missing))
            self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "SCANNING_CACHE", "progress": {"current": index, "total": pair_total, "unit": "pairs", "current_pair": f"{key[0]}/{key[1]}"}})
        self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "WARMING_CACHE", "progress": {"current": 0, "total": strategy_total, "unit": "strategies"}})
        warmed_done = 0
        warm_deadline = perf_counter() + 15 * 60
        for key in sorted(requests):
            missing = cache_info[key][1]
            if not missing:
                continue
            try:
                self._equity_filter_assert_owner(bulk_job_id, child_id, warm_deadline=warm_deadline)
                def on_cache_batch(count: int, *, base: int = warmed_done, pair: tuple[str, str] = key) -> None:
                    self._equity_filter_assert_owner(bulk_job_id, child_id, warm_deadline=warm_deadline)
                    self._panel_jobs.sync(
                        child_id,
                        {"state": "RUNNING", "phase": "WARMING_CACHE", "progress": {"current": min(strategy_total, base + count), "total": strategy_total, "unit": "strategies", "current_pair": f"{pair[0]}/{pair[1]}"}},
                    )
                with self._performance_v2_writer_guard(target):
                    prepare_selection_window_cache(
                        target, requests[key], selection_config, workers, missing,
                        include_equity_regime=True,
                        deadline_monotonic=warm_deadline,
                        on_batch_complete=on_cache_batch,
                    )
                warmed += len(missing)
                warmed_done += len(missing)
                self._equity_filter_assert_owner(bulk_job_id, child_id, warm_deadline=warm_deadline)
            except TimeoutError as error:
                raise FinalistRetestError("RETEST_EQUITY_CACHE_WARM_TIMEOUT", "equity cache warming exceeded its deadline") from error
            except EquitySourceChangedError as error:
                raise FinalistRetestError("EQUITY_SOURCE_CHANGED", str(error)) from error
            except (EquitySchemaUpgradeRequiredError, EquityCacheSchemaInvalidError):
                raise
            except (OSError, duckdb.Error) as error:
                raise FinalistRetestError("RETEST_EQUITY_CACHE_WARM_FAILED", "equity cache warming failed") from error
        run_specs: list[dict[str, object]] = []
        counts = {"PASS": 0, "DROP": 0, "RESERVED": 0, "NOT_EVALUATED": 0}
        self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "APPLYING_FILTER", "progress": {"current": 0, "total": pair_total, "unit": "pairs"}})
        for index, key in enumerate(sorted(requests), start=1):
            self._equity_filter_assert_owner(bulk_job_id, child_id)
            request = requests[key]
            with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                status = selection_cache_status(connection, request, selection_config, include_readiness_breakdown=True)
                if not status.get("ready"):
                    raise FinalistRetestError("EQUITY_CACHE_INCOMPLETE", "equity cache remains incomplete after warming")
                result = run_selection(
                    apply_prior_rejected(connection, load_selection_candidates(connection, request, selection_config, cache_only=True)),
                    request, selection_config,
                )
                result.attrs["source_revisions"] = _current_equity_revisions(connection, [int(row["strategy_id"]) for row in result.to_dict(orient="records")])
                rows: list[dict[str, object]] = []
                for raw in result.to_dict(orient="records"):
                    strategy_id = int(raw["strategy_id"])
                    member = grouped[key][strategy_id]
                    raw.update({
                        "symbol": key[0], "side": key[1], "user_status": member.get("effective_status"),
                        "user_rank": None, "retest": None, "comment": None,
                        "user_analog_of_strategy_id": None,
                        "auto_status": raw.get("auto_status"), "auto_rank": _control_score(raw.get("final_rank")),
                        "auto_reason": raw.get("elimination_reason"), "score": _control_score(raw.get("final_score")),
                        "final_score": _control_score(raw.get("final_score")), "finalist": bool(raw.get("finalist")),
                        "effective_start": member.get("effective_start"), "effective_end": member.get("effective_end"),
                    })
                    decision = str(raw.get("equity_regime_decision") or "NOT_EVALUATED")
                    state = str(raw.get("equity_regime_state") or "")
                    if decision == "DROP": counts["DROP"] += 1
                    elif state == "STALLED": counts["RESERVED"] += 1
                    elif decision == "NOT_EVALUATED": counts["NOT_EVALUATED"] += 1
                    elif decision == "PASS": counts["PASS"] += 1
                    else:
                        raise FinalistRetestError("RETEST_EQUITY_FILTER_FAILED", "equity filter returned an unknown disposition")
                    rows.append(raw)
                if len(rows) != len(grouped[key]):
                    raise FinalistRetestError("RETEST_EQUITY_GROUP_EMPTY", "equity filter returned an incomplete pair group")
                run_specs.append({"symbol": key[0], "side": key[1], "rows": rows, "result": result, "request": request, "run_id": str(uuid.uuid4())})
            self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "APPLYING_FILTER", "progress": {"current": index, "total": pair_total, "unit": "pairs", "current_pair": f"{key[0]}/{key[1]}"}})
        candidates = [row for spec in run_specs for row in spec["rows"]]
        if not run_specs or not candidates:
            raise FinalistRetestError("RETEST_EQUITY_GROUP_EMPTY", "equity filter produced no publishable cohort members")
        if sum(counts.values()) != strategy_total or ready_cache_count + warmed != strategy_total:
            raise FinalistRetestError("RETEST_EQUITY_FILTER_FAILED", "equity filter count invariant failed")
        group_run_ids = {f"{spec['symbol']}|{spec['side']}": spec["run_id"] for spec in run_specs}
        exact_rowsets = {
            f"{spec['symbol']}|{spec['side']}": [
                {name: row.get(source) for name, source in {
                    "Pair": "symbol", "Direction": "side", "Strategy ID": "strategy_id", "Result ID": "result_id",
                    "Auto Status": "auto_status", "Auto Rank": "auto_rank", "Auto Analog Of ID": "auto_analog_of_strategy_id",
                    "Auto Reason": "auto_reason", "Score": "score",
                }.items()}
                for row in sorted(spec["rows"], key=lambda item: int(item["strategy_id"]))
            ] for spec in run_specs
        }
        groups = [{
            "symbol": spec["symbol"], "side": spec["side"], "frozen_count": len(spec["rows"]),
            "success_count": len(spec["rows"]), "failure_count": 0,
            "auto_status_count": len({str(row.get("auto_status") or "") for row in spec["rows"]}),
        } for spec in run_specs]
        immutable = [row for key in sorted(exact_rowsets) for row in exact_rowsets[key]]
        metadata: dict[str, object] = {
            "database_instance_id": None, "publication_kind": "BULK_RETEST_EQUITY_FILTER", "publication_version": 1,
            "application_key": str(runtime.get("equity_filter_application_key") or ""),
            "equity_filter_job_id": child_id, "identity_digest": identity,
            "ranking_scope": "RETEST_COHORT", "bulk_retest_job_id": bulk_job_id,
            "scope": runtime.get("scope"), "cohort_sha256": runtime.get("cohort_sha256"),
            "manifest_sha256": runtime.get("manifest_sha256"), "config_sha256": runtime.get("config_sha256"),
            "selection_config_sha256": snapshot.get("selection_config_sha256"),
            "group_run_ids_json": group_run_ids, "exact_rowsets_json": exact_rowsets,
            "exact_rowsets_sha256": canonical_digest(exact_rowsets), "immutable_content_sha256": canonical_digest(immutable),
            "groups_sha256": canonical_digest(groups), "failures_sha256": canonical_digest([]),
            "source_revisions_sha256": canonical_digest(source_revisions),
        }
        if not metadata["application_key"]:
            raise FinalistRetestError("RETEST_EQUITY_CONFIG_INVALID", "equity filter application identity is missing")
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            metadata["database_instance_id"] = connection.execute("select value from schema_info where key='database_instance_id'").fetchone()[0]
        self._panel_jobs.sync(child_id, {"state": "RUNNING", "phase": "PERSISTING", "progress": {"current": 0, "total": pair_total, "unit": "pairs"}})
        self._equity_filter_assert_owner(bulk_job_id, child_id)
        candidate_workbook = self._finalist_control_candidate_workbook(candidates, SelectionRequest(str(candidates[0]["symbol"]), str(candidates[0]["side"]), (request_stage,)))
        workbook = combined_control_workbook_bytes(candidates, groups, (), metadata, candidate_workbook=candidate_workbook)
        snapshots = [{
            "request": spec["request"], "config": selection_config, "result": spec["result"],
            "metadata": {"selection_run_id": spec["run_id"], "database_instance_id": metadata["database_instance_id"], "selection_contract_version": "performance-v2-selection-review-v1"},
            "request_json_extra": {
                "publication_kind": "BULK_RETEST_EQUITY_FILTER", "publication_version": 1, "application_key": metadata["application_key"],
                "bulk_retest_job_id": bulk_job_id, "equity_filter_job_id": child_id, "identity_digest": identity,
                "ranking_scope": "RETEST_COHORT", "cohort_sha256": runtime.get("cohort_sha256"),
                "manifest_sha256": runtime.get("manifest_sha256"), "config_sha256": runtime.get("config_sha256"),
                "selection_config_sha256": snapshot.get("selection_config_sha256"), "equity_filter_snapshot_sha256": canonical_digest(snapshot),
            },
        } for spec in run_specs]
        replayed = False
        try:
            with self._performance_v2_writer_guard(target), duckdb.connect(str(target)) as connection:
                self._equity_filter_assert_owner(bulk_job_id, child_id)
                try:
                    latest_revisions = _current_equity_revisions(connection, sorted(grouped_strategy_ids))
                except (SelectionReviewError, EquitySourceChangedError, duckdb.Error) as error:
                    raise FinalistRetestError("EQUITY_SOURCE_CHANGED", "equity source revision is unreadable before publication") from error
                latest_source_digest = canonical_digest({
                    str(strategy_id): list(latest_revisions[strategy_id]) for strategy_id in sorted(latest_revisions)
                })
                if set(latest_revisions) != grouped_strategy_ids or latest_source_digest != runtime.get("equity_filter_source_revision_digest"):
                    raise FinalistRetestError("EQUITY_SOURCE_CHANGED", "equity source revision changed before publication")
                existing_rows = connection.execute(
                    """select selection_run_id, symbol, side
                         from selection_runs
                        where json_extract_string(request_json, '$.publication_kind') = 'BULK_RETEST_EQUITY_FILTER'
                          and json_extract_string(request_json, '$.application_key') = ?""",
                    [metadata["application_key"]],
                ).fetchall()
                expected_groups = {(str(spec["symbol"]), str(spec["side"])) for spec in run_specs}
                existing_groups = {(str(row[1]), str(row[2])) for row in existing_rows}
                if existing_rows:
                    if existing_groups != expected_groups or len(existing_rows) != len(expected_groups):
                        raise FinalistRetestError("RETEST_EQUITY_PUBLICATION_INVALID", "existing equity filter publication is incomplete")
                    run_ids = tuple(str(row[0]) for row in existing_rows)
                    replayed = True
                else:
                    run_ids = persist_selection_snapshots(connection, snapshots, workbook_bytes=workbook)
        except SelectionReviewError as error:
            if error.code in {"SELECTION_REVIEW_STALE_RESULTS", "SELECTION_REVIEW_SCHEMA_MISMATCH"}:
                raise FinalistRetestError("RETEST_COHORT_STALE_RESULTS", str(error), details=error.details) from error
            raise FinalistRetestError(error.code, str(error), details=error.details) from error
        result = {
            "pairs": pair_total, "strategies": strategy_total, "ready_cache_count": ready_cache_count,
            "warmed_strategies": warmed, "PASS": counts["PASS"], "DROP": counts["DROP"], "RESERVED": counts["RESERVED"],
            "NOT_EVALUATED": counts["NOT_EVALUATED"], "run_ids": list(run_ids),
            "application_key": metadata["application_key"], "identity_digest": identity, "replayed": replayed,
        }
        return result

    def strategies_performance_v2_finalist_retest_equity_filter(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload, Mapping) or set(payload) != {"job_id"} or not isinstance(payload.get("job_id"), str) or not payload["job_id"].strip():
            raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="equity filter accepts exactly a nonempty job_id")
        bulk_job_id = str(payload["job_id"]).strip()
        with self._bulk_retest_equity_filter_lock:
            try:
                parent = self._panel_jobs.get(bulk_job_id)
                runtime = self._panel_jobs.runtime(bulk_job_id)
            except PanelJobError as error:
                raise PerformanceV2ApiError("RETEST_COHORT_JOB_NOT_FOUND", status=404, message="bulk RETEST job was not found") from error
            if parent.get("kind") != "strategies.performance.v2.finalist-retest" or runtime.get("bulk_retest") is not True:
                raise PerformanceV2ApiError("RETEST_COHORT_INVALID", status=409, message="job is not a bulk finalist RETEST")
            if runtime.get("scope") not in {"FINALIST", "FINALIST_RESERVE"}:
                raise PerformanceV2ApiError("RETEST_COHORT_INVALID", status=409, message="bulk RETEST scope is not a finalist cohort")
            if parent.get("state") != "COMMITTED":
                raise PerformanceV2ApiError("RETEST_COHORT_JOB_NOT_COMMITTED", status=409, message="bulk RETEST is not committed")
            if runtime.get("outcomes_finalized") is not True:
                raise PerformanceV2ApiError("RETEST_IMPORT_NOT_FINALIZED", status=409, message="bulk RETEST import outcomes are not finalized")
            if not isinstance(runtime.get("successful_replacements"), list) or not runtime.get("successful_replacements"):
                raise PerformanceV2ApiError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS", status=409, message="bulk RETEST has no successful replacements")
            raw_successes = runtime.get("successful_replacements")
            ids = sorted(
                int(item["strategy_id"])
                for item in raw_successes
                if isinstance(item, Mapping) and isinstance(item.get("strategy_id"), int) and not isinstance(item.get("strategy_id"), bool)
            ) if isinstance(raw_successes, list) else []
            target = performance_v2_database_path(self._performance_v2_config())
            try:
                with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                    require_performance_v2(connection)
                    revisions = _current_equity_revisions(connection, ids)
            except (OSError, duckdb.Error, SelectionReviewError) as error:
                raise PerformanceV2ApiError("EQUITY_SOURCE_CHANGED", status=409, message="equity source revision is unavailable") from error
            if set(revisions) != set(ids):
                raise PerformanceV2ApiError("EQUITY_SOURCE_CHANGED", status=409, message="equity source revision is unavailable")
            source_digest = canonical_digest({str(strategy_id): list(revisions[strategy_id]) for strategy_id in sorted(revisions)})
            runtime = dict(runtime)
            runtime["equity_filter_source_revision_digest"] = source_digest
            identity_runtime = dict(runtime)
            identity_runtime["equity_filter_source_revision_digest"] = source_digest
            identity_seed = self._equity_filter_identity_seed(identity_runtime)
            application_key = sha256(f"bulk-retest-equity-filter-v1:{bulk_job_id}:{identity_seed}".encode()).hexdigest()
            current_attempt = runtime.get("equity_filter_attempt")
            attempt = current_attempt if isinstance(current_attempt, int) and not isinstance(current_attempt, bool) and current_attempt >= 0 else 0
            existing_id = runtime.get("equity_filter_job_id")
            same_identity = runtime.get("equity_filter_application_key") == application_key
            if isinstance(existing_id, str) and existing_id:
                try:
                    existing = self._panel_jobs.get(existing_id)
                except PanelJobError:
                    existing = None
                if existing is not None:
                    if existing.get("state") == "COMMITTED":
                        if same_identity:
                            return {"job_id": existing_id, "job": existing, "application_key": application_key, "replayed": True}
                    if existing.get("state") in {"QUEUED", "RUNNING", "CANCELLING"}:
                        lease_expired = False
                        raw_expiry = runtime.get("equity_filter_lock_expires_at")
                        if isinstance(raw_expiry, str):
                            try:
                                lease_expired = datetime.fromisoformat(raw_expiry).timestamp() <= datetime.now(timezone.utc).timestamp()
                            except ValueError:
                                lease_expired = True
                        if not lease_expired:
                            if same_identity:
                                return {"job_id": existing_id, "job": existing, "application_key": application_key, "replayed": True}
                            raise PerformanceV2ApiError(
                                "RETEST_EQUITY_ACTION_ACTIVE", status=409,
                                message="equity filter action is already running for this RETEST cohort",
                            )
                        # A crashed/stalled worker may leave a live child behind. Reclaim
                        # its durable lease before submitting the replacement child.
                        try:
                            self._panel_jobs.sync(existing_id, {
                                "state": "FAILED", "phase": "FAILED",
                                "error": {"code": "PERFORMANCE_V2_LOCKED", "message": "equity filter lease expired and was reclaimed"},
                            })
                        except PanelJobError:
                            pass
            try:
                while attempt < _BULK_RETEST_EQUITY_FILTER_MAX_ATTEMPTS:
                    attempt += 1
                    child_id = uuid.uuid4().hex
                    registry_key = f"{application_key}:attempt:{attempt}"
                    child = self._panel_jobs.submit(
                        "strategies.performance.v2.finalist-retest.equity-filter",
                        {"bulk_job_id": bulk_job_id, "application_key": application_key, "identity_seed_digest": identity_seed}, registry_key,
                        ("performance-v2-equity-filter",), job_id=child_id,
                    )
                    actual_child_id = str(child.get("job_id") or child_id)
                    if actual_child_id != child_id:
                        existing_state = child.get("state")
                        if existing_state in {"QUEUED", "RUNNING", "CANCELLING", "COMMITTED"}:
                            runtime = dict(runtime)
                            runtime.update({"equity_filter_job_id": actual_child_id, "equity_filter_application_key": application_key, "equity_filter_attempt": attempt})
                            self._panel_jobs.sync(bulk_job_id, {"state": "COMMITTED", "phase": parent.get("phase", "COMMITTED")}, runtime=runtime)
                            return {"job_id": actual_child_id, "job": child, "application_key": application_key, "replayed": True}
                        continue
                    runtime = dict(runtime)
                    runtime.update({
                        "equity_filter_job_id": child_id, "equity_filter_application_key": application_key,
                        "equity_filter_idempotency_key": application_key, "equity_filter_registry_key": registry_key,
                        "equity_filter_identity_seed_digest": identity_seed,
                        "equity_filter_attempt": attempt,
                        "equity_filter_lock_holder": child_id,
                        "equity_filter_lock_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=_BULK_RETEST_EQUITY_FILTER_LEASE_SECONDS)).isoformat(),
                        "equity_filter_published": False,
                        "equity_filter_source_revision_digest": source_digest,
                    })
                    self._panel_jobs.sync(bulk_job_id, {"state": "COMMITTED", "phase": parent.get("phase", "COMMITTED")}, runtime=runtime)
                    break
                else:
                    raise PerformanceV2ApiError(
                        "RETEST_EQUITY_ACTION_RETRY_EXHAUSTED",
                        status=409,
                        message="equity filter retry allocation exhausted",
                    )
            except PanelJobError as error:
                raise PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message="equity filter is already running") from error
            threading.Thread(target=self._run_bulk_retest_equity_filter_job, args=(bulk_job_id, child_id), daemon=True, name="mrs3-panel-equity-filter").start()
            return {"job_id": child_id, "job": child, "application_key": application_key}

    def _run_bulk_retest_equity_filter_job(self, bulk_job_id: str, child_id: str) -> None:
        succeeded = False
        failure_code = "RETEST_EQUITY_FILTER_FAILED"
        failure_message = "bulk RETEST equity filter failed"
        try:
            with self._bulk_retest_equity_filter_lock:
                runtime = self._panel_jobs.runtime(bulk_job_id)
                if runtime.get("equity_filter_lock_holder") != child_id:
                    return
            result = self._run_bulk_retest_equity_filter(child_id, bulk_job_id)
            with self._bulk_retest_equity_filter_lock:
                runtime = self._panel_jobs.runtime(bulk_job_id)
                if runtime.get("equity_filter_lock_holder") != child_id:
                    return
                committed_result = {
                    "scope": runtime.get("scope"), "pair_count": result["pairs"], "processed_count": result["strategies"],
                    "ready_cache_count": result.get("ready_cache_count", 0), "warmed_strategy_count": result.get("warmed_strategies", 0),
                    "pass_count": result.get("PASS", 0), "drop_count": result.get("DROP", 0),
                    "reserved_count": result.get("RESERVED", 0), "not_evaluated_count": result.get("NOT_EVALUATED", 0),
                    "selection_run_ids": result.get("run_ids", []), "application_key": result.get("application_key"),
                }
                self._panel_jobs.sync(child_id, {"state": "COMMITTED", "phase": "COMMITTED", "progress": {"current": result["pairs"], "total": result["pairs"], "unit": "pairs", "total_pairs": result["pairs"], "completed_pairs": result["pairs"], "total_strategies": result["strategies"], "processed_strategies": result["strategies"], "ready_cache_count": result.get("ready_cache_count", 0), "warmed_strategy_count": result.get("warmed_strategies", 0), "current_pair": None}, "result": committed_result})
                runtime.update({"equity_filter_published": True, "equity_filter_identity_digest": result.get("identity_digest"), "equity_filter_lock_holder": None, "equity_filter_lock_expires_at": None})
                self._panel_jobs.sync(bulk_job_id, {"state": "COMMITTED", "phase": self._panel_jobs.get(bulk_job_id).get("phase", "COMMITTED")}, runtime=runtime)
                succeeded = True
        except Exception as error:
            if isinstance(error, FinalistRetestError):
                failure_code, failure_message = error.code, str(error)
            elif isinstance(error, (EquitySchemaUpgradeRequiredError, EquityCacheSchemaInvalidError)):
                failure_code, failure_message = getattr(error, "code", "PERFORMANCE_V2_SCHEMA_INVALID"), str(error)
            else:
                _LOGGER.exception("bulk finalist retest equity filter failed")
        finally:
            try:
                with self._bulk_retest_equity_filter_lock:
                    runtime = self._panel_jobs.runtime(bulk_job_id)
                    if runtime.get("equity_filter_lock_holder") == child_id and not succeeded:
                        try:
                            child_state = self._panel_jobs.get(child_id).get("state")
                            if child_state not in {"FAILED", "CANCELLED", "COMMITTED"}:
                                self._panel_jobs.sync(child_id, {"state": "FAILED", "phase": "FAILED", "error": {"code": failure_code, "message": failure_message}})
                        except BaseException:
                            _LOGGER.exception("failed to publish bulk equity filter child failure")
                        runtime.update({"equity_filter_lock_holder": None, "equity_filter_lock_expires_at": None})
                        try:
                            self._panel_jobs.sync(bulk_job_id, {"state": "COMMITTED", "phase": self._panel_jobs.get(bulk_job_id).get("phase", "COMMITTED")}, runtime=runtime)
                        except BaseException:
                            _LOGGER.exception("failed to clear bulk equity filter lease")
            except BaseException:
                _LOGGER.exception("failed to publish bulk equity filter failure")

    @staticmethod
    def _equity_filter_identity_seed(runtime: Mapping[str, object]) -> str:
        """Stable request identity used before the async worker reads the DB."""
        snapshot = runtime.get("equity_filter_snapshot")
        members = runtime.get("cohort_members")
        successes = runtime.get("successful_replacements")
        return canonical_digest({
            "scope": runtime.get("scope"),
            "cohort_sha256": runtime.get("cohort_sha256"),
            "members": members if isinstance(members, list) else [],
            "successful_replacements": successes if isinstance(successes, list) else [],
            "equity_filter_snapshot_sha256": canonical_digest(snapshot) if isinstance(snapshot, Mapping) else None,
            "source_revision_digest": runtime.get("equity_filter_source_revision_digest"),
        })

    def _finalist_control_candidate_workbook(
        self,
        rows: Sequence[Mapping[str, object]],
        request: SelectionRequest,
    ) -> bytes:
        normalized_rows = []
        for source in rows:
            row = dict(source)
            for field in ("user_rank", "auto_rank", "final_rank"):
                row[field] = _control_score(row.get(field))
            normalized_rows.append(row)
        reviews = {
            int(row["strategy_id"]): {
                "user_status": row.get("user_status"),
                "user_rank": row.get("user_rank"),
                "user_analog_of_strategy_id": row.get("user_analog_of_strategy_id"),
                "comment": row.get("comment"),
            }
            for row in normalized_rows
            if row.get("strategy_id") is not None
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_selection_workbook(
                pd.DataFrame(normalized_rows), Path(directory) / "candidates.xlsx", request,
                {"workbook_schema_version": "2"}, reviews,
            )
            return path.read_bytes()

    def _export_current_effective_finalist_control(self, include_reserve: bool) -> tuple[str, bytes]:
        if type(include_reserve) is not bool:
            raise FinalistRetestError("INVALID_REQUEST", "include_reserve must be a boolean")
        selection_config = load_selection_config(self.default_config.with_name("config.performance.json"))
        performance_config = self._performance_v2_config()
        target = performance_v2_database_path(performance_config)
        if not target.is_file():
            raise FinalistRetestError("PERFORMANCE_V2_NOT_FOUND")
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            current_members = current_effective_finalist_members(
                connection, include_reserve=include_reserve, decisions=self._registry_finalist_decisions(connection),
            )
            if not current_members:
                raise FinalistRetestError("COHORT_EMPTY", "there are no current effective finalists")
            instance = connection.execute("select value from schema_info where key='database_instance_id'").fetchone()[0]
            grouped: dict[tuple[str, str], list[Mapping[str, object]]] = {}
            for member in current_members:
                grouped.setdefault((str(member["symbol"]), str(member["side"])), []).append(member)
            specs: list[dict[str, object]] = []
            for (symbol, side), group_members in sorted(grouped.items()):
                origin_runs = {str(member["selection_run_id"]) for member in group_members
                               if member["selection_run_id"] is not None}       # registry marks have no origin run
                request = retest_cohort_request(
                    SelectionRequest(symbol, side, ()), "current-effective-control",
                    {int(member["strategy_id"]): int(member["result_id"]) for member in group_members},
                )
                if not selection_cache_status(connection, request, selection_config).get("ready"):
                    raise FinalistRetestError(
                        "SELECTION_CACHE_INCOMPLETE",
                        "selection cache is not ready; prepare or recalculate it before exporting the control workbook",
                    )
                loaded = load_selection_candidates(connection, request, selection_config, cache_only=True)
                canonical_by_strategy = {
                    int(item["strategy_id"]): dict(item)
                    for item in loaded.to_dict(orient="records")
                    if item.get("strategy_id") is not None
                }
                for member in group_members:
                    canonical = canonical_by_strategy.get(int(member["strategy_id"]))
                    try:
                        canonical_result_id = int(canonical["result_id"]) if canonical is not None else None
                    except (TypeError, ValueError, OverflowError):
                        canonical_result_id = None
                    if canonical_result_id != int(member["result_id"]):
                        raise FinalistRetestError(
                            "CONTROL_CANONICAL_ROW_MISMATCH",
                            f"selection cache does not match the current result for {symbol}/{side}; "
                            "recalculate the selection cache before exporting the control workbook",
                        )
                snapshots: dict[tuple[str, int], tuple[object, ...]] = {}
                reviewed: dict[tuple[str, int], tuple[object, object, object, object]] = {}
                for origin_run_id in origin_runs:
                    snapshots.update({
                        (origin_run_id, int(row[0])): row
                        for row in connection.execute(
                            """select strategy_id, result_id_at_selection, auto_status, auto_score,
                                      auto_rank, auto_reason, auto_analog_of_strategy_id
                                 from selection_results where selection_run_id = ?""", [origin_run_id]
                        ).fetchall()
                    })
                    review = connection.execute(
                        """select review_import_id from selection_review_imports
                           where selection_run_id = ? order by imported_at_utc desc, review_import_id desc limit 1""", [origin_run_id]
                    ).fetchone()
                    if review:
                        reviewed.update({
                            (origin_run_id, int(row[0])): (row[1], row[2], row[3], row[4])
                            for row in connection.execute(
                                "select strategy_id, user_status, user_rank, user_analog_of_strategy_id, comment from selection_review_rows where review_import_id = ?", [review[0]]
                            ).fetchall()
                        })
                records: list[dict[str, object]] = []
                workbook_rows: list[dict[str, object]] = []
                for member in group_members:
                    strategy_id = int(member["strategy_id"])
                    origin_key = (str(member["selection_run_id"]), strategy_id)
                    snap = snapshots.get(origin_key)
                    reviewed_row = reviewed.get(origin_key)
                    auto_status = snap[2] if snap else member["effective_status"]
                    auto_score = snap[3] if snap else None
                    auto_rank = _control_score(snap[4]) if snap else None
                    auto_reason = snap[5] if snap else None
                    auto_analog = snap[6] if snap else None
                    user_status = reviewed_row[0] if reviewed_row else member["effective_status"]
                    user_rank = _control_score(reviewed_row[1] if reviewed_row else member.get("effective_rank"))
                    user_analog = reviewed_row[2] if reviewed_row else None
                    comment = reviewed_row[3] if reviewed_row else None
                    records.append({
                        "strategy_id": strategy_id, "result_id": int(member["result_id"]), "auto_status": str(auto_status),
                        "final_score": auto_score, "final_rank": auto_rank, "elimination_reason": auto_reason,
                        "auto_analog_of_strategy_id": auto_analog, "prior_rejected": False,
                    })
                    canonical = canonical_by_strategy.get(strategy_id, {}).copy()
                    canonical.update({
                        "symbol": symbol, "side": side, "strategy_id": strategy_id, "result_id": int(member["result_id"]),
                        "user_status": user_status, "user_rank": user_rank, "retest": None, "comment": comment,
                        "user_analog_of_strategy_id": user_analog,
                        "auto_status": auto_status, "auto_rank": auto_rank, "auto_analog_of_strategy_id": auto_analog,
                        "auto_reason": auto_reason, "effective_start": _json_value(member.get("effective_start")),
                        "effective_end": _json_value(member.get("effective_end")),
                        "final_score": _control_score(auto_score), "final_rank": auto_rank,
                        "score": _control_score(auto_score),
                        "elimination_reason": auto_reason, "finalist": auto_status in {"FINALIST", "RESERVE"},
                    })
                    workbook_rows.append(canonical)
                specs.append({
                    "symbol": symbol, "side": side, "rows": workbook_rows, "result": pd.DataFrame(records),
                    "request": SelectionRequest(symbol, side, ()), "run_id": str(uuid.uuid4()),
                })
        candidates = [row for spec in specs for row in spec["rows"]]
        if not specs or not candidates:
            raise FinalistRetestError("COHORT_EMPTY", "there are no current effective finalists")
        groups = [{
            "symbol": spec["symbol"], "side": spec["side"], "frozen_count": len(spec["rows"]),
            "success_count": len(spec["rows"]), "failure_count": 0,
            "auto_status_count": len({str(row.get("auto_status") or "") for row in spec["rows"]}),
        } for spec in specs]
        group_run_ids = {f"{spec['symbol']}|{spec['side']}": spec["run_id"] for spec in specs}
        workbook_names = {
            "Pair": "symbol", "Direction": "side", "Strategy ID": "strategy_id", "Result ID": "result_id",
            "Auto Status": "auto_status", "Auto Rank": "auto_rank", "Auto Analog Of ID": "auto_analog_of_strategy_id",
            "Auto Reason": "auto_reason", "Score": "score",
        }
        exact_rowsets = {
            f"{spec['symbol']}|{spec['side']}": [
                {name: row.get(source) for name, source in workbook_names.items()}
                for row in sorted(spec["rows"], key=lambda item: int(item["strategy_id"]))
            ] for spec in specs
        }
        immutable = [row for key in sorted(exact_rowsets) for row in exact_rowsets[key]]
        group_digest_rows = [{
            "Pair": row["symbol"], "Direction": row["side"], "Frozen Count": row["frozen_count"],
            "Success Count": row["success_count"], "Failure Count": row["failure_count"], "Auto Status Count": row["auto_status_count"],
        } for row in groups]
        metadata: dict[str, object] = {
            "database_instance_id": instance, "control_mode": "CURRENT_EFFECTIVE", "ranking_scope": "CURRENT_EFFECTIVE",
            "scope": "FINALIST_RESERVE" if include_reserve else "FINALIST",
            "group_run_ids_json": group_run_ids, "exact_rowsets_json": exact_rowsets,
            "exact_rowsets_sha256": canonical_digest(exact_rowsets), "immutable_content_sha256": canonical_digest(immutable),
            "groups_sha256": canonical_digest(group_digest_rows), "failures_sha256": canonical_digest([]),
            "selection_config_sha256": canonical_digest(asdict(selection_config)),
        }
        candidate_workbook = self._finalist_control_candidate_workbook(
            candidates, SelectionRequest(str(candidates[0]["symbol"]), str(candidates[0]["side"]), ()),
        )
        data = combined_control_workbook_bytes(candidates, groups, (), metadata, candidate_workbook=candidate_workbook)
        snapshots = [{
            "request": spec["request"], "config": selection_config, "result": spec["result"],
            "metadata": {"selection_run_id": spec["run_id"], "database_instance_id": instance, "selection_contract_version": "performance-v2-selection-review-v1"},
            "request_json_extra": {"control_mode": "CURRENT_EFFECTIVE", "ranking_scope": "CURRENT_EFFECTIVE"},
        } for spec in specs]
        try:
            with self._performance_v2_writer_guard(target), duckdb.connect(str(target)) as connection:
                persist_selection_snapshots(connection, snapshots, workbook_bytes=data)
        except SelectionReviewError as error:
            raise FinalistRetestError(error.code, details=error.details) from error
        return f"performance-v2-current-finalists{'-with-reserve' if include_reserve else ''}.xlsx", data

    def strategies_performance_v2_finalist_retest_export(self, payload: Mapping[str, object]) -> tuple[str, bytes]:
        if isinstance(payload, Mapping) and set(payload) == {"include_reserve"}:
            return self._export_current_effective_finalist_control(payload["include_reserve"])
        if not isinstance(payload, Mapping) or set(payload) != {"job_id"}:
            raise ValueError("bulk RETEST export accepts only job_id")
        job_id = self._required(payload, "job_id")
        document = self._bulk_retest_status_document(job_id)
        if document.get("state") != "COMMITTED":
            raise ValueError("bulk RETEST is not committed")
        if isinstance(document.get("error"), Mapping) and document["error"].get("code") == "RETEST_COHORT_NO_SUCCESSFUL_MEMBERS":
            raise FinalistRetestError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS")
        runtime = self._panel_jobs.runtime(job_id)
        successes = runtime.get("successful_replacements")
        if not isinstance(successes, list) or not successes:
            raise ValueError("bulk RETEST has no successful members")
        members = {int(member["strategy_id"]): member for member in runtime.get("cohort_members", []) if isinstance(member, Mapping)}
        audit_members: dict[int, Mapping[str, object]] = dict(members)
        for exclusion in runtime.get("exclusions", []):
            if not isinstance(exclusion, Mapping):
                continue
            raw_id, symbol, side = exclusion.get("strategy_id"), exclusion.get("symbol"), exclusion.get("side")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and isinstance(symbol, str) and isinstance(side, str) and side in {"LONG", "SHORT"}:
                audit_members.setdefault(raw_id, {
                    "strategy_id": raw_id, "strategy_name": exclusion.get("strategy_name"),
                    "symbol": symbol, "side": side, "excluded": True,
                })
        success_members = {
            int(item["strategy_id"]): item for item in successes
            if isinstance(item, Mapping) and int(item.get("strategy_id", 0)) in members
        }
        if not success_members:
            raise FinalistRetestError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS")
        selection_config = load_selection_config(self.default_config.with_name("config.performance.json"))
        performance_config = self._performance_v2_config()
        target = performance_v2_database_path(performance_config)
        if not target.is_file():
            raise FinalistRetestError("PERFORMANCE_V2_NOT_FOUND")

        # Every Pair+Side is ranked independently.  The only IDs passed to the
        # selection/cache helpers come from the server-owned successful outcome
        # list above; browser payloads never participate in this path.
        by_group: dict[tuple[str, str], dict[int, Mapping[str, object]]] = {}
        all_group_members: dict[tuple[str, str], dict[int, Mapping[str, object]]] = {}
        for strategy_id, member in audit_members.items():
            all_group_members.setdefault((str(member["symbol"]), str(member["side"])), {})[strategy_id] = member
        for strategy_id, outcome in success_members.items():
            member = members[strategy_id]
            by_group.setdefault((str(member["symbol"]), str(member["side"])), {})[strategy_id] = member
        run_specs: list[dict[str, object]] = []
        try:
            with self._performance_v2_writer_lock:
                for (symbol, side), group_members in sorted(by_group.items()):
                    base = SelectionRequest(symbol, side, ())
                    # Reuse the last server-recorded stage order for this pair;
                    # the browser still supplies no ranking controls for a
                    # cohort export.  A legacy run may have no decodable stage
                    # payload, in which case the pipeline's ordinary empty
                    # stage behavior remains the safe fallback.
                    with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                        latest_request = connection.execute(
                            "select request_json from selection_runs where symbol = ? and side = ? order by created_at_utc desc, selection_run_id desc limit 1",
                            [symbol, side],
                        ).fetchone()
                    if latest_request:
                        try:
                            decoded = json.loads(str(latest_request[0]))
                            if isinstance(decoded, Mapping) and isinstance(decoded.get("stages"), list):
                                base = parse_selection_request({"symbol": symbol, "side": side, "stages": decoded["stages"]})
                        except (TypeError, ValueError, PerformanceV2SelectionError):
                            pass
                    cohort_request = retest_cohort_request(
                        base, job_id,
                        {strategy_id: int(success_members[strategy_id].get("new_result_id", member["result_id"])) for strategy_id, member in group_members.items()},
                    )
                    with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                        require_performance_v2(connection)
                        cache = selection_cache_status(connection, cohort_request, selection_config)
                    if not cache.get("ready"):
                        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                            missing = selection_cache_missing_strategy_ids(connection, cohort_request, selection_config)
                        with self._performance_v2_writer_guard(target):
                            prepare_selection_window_cache(target, cohort_request, selection_config, performance_config.workers, missing)
                    with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
                        if not selection_cache_status(connection, cohort_request, selection_config).get("ready"):
                            raise FinalistRetestError("SELECTION_CACHE_INCOMPLETE")
                        result = run_selection(
                            apply_prior_rejected(connection, load_selection_candidates(connection, cohort_request, selection_config, cache_only=True)),
                            cohort_request,
                            selection_config,
                        )
                    run_id = str(uuid.uuid4())
                    rows: list[dict[str, object]] = []
                    for raw in result.to_dict(orient="records"):
                        strategy_id = int(raw["strategy_id"])
                        member = group_members.get(strategy_id)
                        if member is None:
                            continue
                        rows.append({
                            **raw,
                            "symbol": symbol, "side": side, "strategy_id": strategy_id,
                            "result_id": int(raw["result_id"]), "user_status": member.get("effective_status"),
                            "user_rank": None, "retest": None, "comment": None,
                            "user_analog_of_strategy_id": None,
                            "auto_status": raw.get("auto_status"), "auto_rank": _control_score(raw.get("final_rank")),
                            "auto_analog_of_strategy_id": raw.get("auto_analog_of_strategy_id"),
                            "auto_reason": raw.get("elimination_reason"), "score": _control_score(raw.get("final_score")),
                            "final_score": _control_score(raw.get("final_score")), "finalist": bool(raw.get("finalist")),
                            "effective_start": member.get("effective_start"), "effective_end": member.get("effective_end"),
                        })
                    run_specs.append({
                        "symbol": symbol, "side": side, "rows": rows, "result": result, "request": cohort_request,
                        "run_id": run_id,
                        "request_json_extra": {
                            "bulk_retest_job_id": job_id, "ranking_scope": "RETEST_COHORT",
                            "cohort_sha256": runtime.get("cohort_sha256"), "manifest_sha256": runtime.get("manifest_sha256"),
                            "config_sha256": runtime.get("config_sha256"),
                            "selection_config_sha256": canonical_digest(asdict(selection_config)),
                        },
                    })
        except PerformanceV2SelectionError as error:
            raise FinalistRetestError(str(error)) from error

        candidates = [row for spec in run_specs for row in spec["rows"]]
        if not run_specs or not candidates:
            raise FinalistRetestError("RETEST_COHORT_NO_SUCCESSFUL_MEMBERS")
        groups: list[dict[str, object]] = []
        all_failures: list[dict[str, object]] = []
        failures_by_group: dict[tuple[str, str], list[dict[str, object]]] = {}
        for failure in runtime.get("failures", []):
            if not isinstance(failure, Mapping):
                continue
            raw_strategy_id = failure.get("strategy_id")
            member = audit_members.get(int(raw_strategy_id)) if isinstance(raw_strategy_id, int) and not isinstance(raw_strategy_id, bool) else None
            if member is None:
                name = failure.get("strategy_name")
                member = next((candidate for candidate in audit_members.values() if candidate.get("strategy_name") == name), None)
            if member is None:
                continue
            group_key = (str(member["symbol"]), str(member["side"]))
            item = {"symbol": group_key[0], "side": group_key[1], **dict(failure)}
            failures_by_group.setdefault(group_key, []).append(item)
            all_failures.append(item)
        for (symbol, side), group_members in sorted(all_group_members.items()):
            key = f"{symbol}|{side}"
            group_rows = [row for row in candidates if row["symbol"] == symbol and row["side"] == side]
            status_counts: dict[str, int] = {}
            for row in group_rows:
                status = str(row.get("auto_status") or "")
                status_counts[status] = status_counts.get(status, 0) + 1
            groups.append({
                "symbol": symbol, "side": side, "frozen_count": len(group_members),
                "success_count": len(group_rows), "failure_count": len(failures_by_group.get((symbol, side), [])),
                "auto_status_count": len(status_counts),
            })
        group_run_ids = {f"{spec['symbol']}|{spec['side']}": spec["run_id"] for spec in run_specs}
        workbook_names = {
            "Pair": "symbol", "Direction": "side", "Strategy ID": "strategy_id", "Result ID": "result_id",
            "Auto Status": "auto_status", "Auto Rank": "auto_rank", "Auto Analog Of ID": "auto_analog_of_strategy_id",
            "Auto Reason": "auto_reason", "Score": "score",
        }
        exact_rowsets = {
            key: [
                {name: row.get(source) for name, source in workbook_names.items()}
                for row in sorted(spec["rows"], key=lambda item: int(item["strategy_id"]))
            ]
            for key, spec in ((f"{spec['symbol']}|{spec['side']}", spec) for spec in run_specs)
        }
        immutable = [row for key in sorted(exact_rowsets) for row in exact_rowsets[key]]
        group_digest_rows = [{
            "Pair": row["symbol"], "Direction": row["side"], "Frozen Count": row["frozen_count"],
            "Success Count": row["success_count"], "Failure Count": row["failure_count"], "Auto Status Count": row["auto_status_count"],
        } for row in groups]
        failure_digest_rows = [{
            "Pair": row.get("symbol"), "Direction": row.get("side"), "Strategy ID": row.get("strategy_id"),
            "Strategy": row.get("strategy_name"), "Result ID": row.get("result_id"), "Reason": row.get("reason"),
        } for row in all_failures]
        metadata: dict[str, object] = {
            "database_instance_id": None, "ranking_scope": "RETEST_COHORT", "bulk_retest_job_id": job_id,
            "scope": runtime.get("scope"), "cohort_sha256": runtime.get("cohort_sha256"),
            "manifest_sha256": runtime.get("manifest_sha256"), "config_sha256": runtime.get("config_sha256"),
            "selection_config_sha256": canonical_digest(asdict(selection_config)),
            "test_start": runtime.get("test_start"), "test_end": runtime.get("test_end"),
            "group_run_ids_json": group_run_ids,
            "exact_rowsets_json": exact_rowsets,
            "exact_rowsets_sha256": canonical_digest(exact_rowsets),
            "immutable_content_sha256": canonical_digest(immutable),
            "groups_sha256": canonical_digest(group_digest_rows), "failures_sha256": canonical_digest(failure_digest_rows),
        }
        with self._performance_v2_writer_lock, duckdb.connect(str(target), read_only=True) as connection:
            metadata["database_instance_id"] = connection.execute("select value from schema_info where key='database_instance_id'").fetchone()[0]
        candidate_workbook = self._finalist_control_candidate_workbook(
            candidates,
            SelectionRequest(str(candidates[0]["symbol"]), str(candidates[0]["side"]), ()),
        )
        data = combined_control_workbook_bytes(
            candidates, groups, all_failures, metadata, candidate_workbook=candidate_workbook,
        )
        snapshots = []
        for spec in run_specs:
            snapshot_metadata = {
                "selection_run_id": spec["run_id"], "database_instance_id": metadata["database_instance_id"],
                "selection_contract_version": "performance-v2-selection-review-v1",
            }
            snapshots.append({"request": spec["request"], "config": selection_config, "result": spec["result"], "metadata": snapshot_metadata, "request_json_extra": spec["request_json_extra"]})
        try:
            with self._performance_v2_writer_guard(target), duckdb.connect(str(target)) as connection:
                persist_selection_snapshots(connection, snapshots, workbook_bytes=data)
        except SelectionReviewError as error:
            raise FinalistRetestError(error.code, details=error.details) from error
        return f"performance-v2-finalist-retest-{job_id}.xlsx", data

    strategies_performance_v2_bulk_retest_export = strategies_performance_v2_finalist_retest_export

    def strategies_performance_v2_finalist_retest_control_import(self, data: bytes) -> dict[str, object]:
        if not isinstance(data, bytes):
            raise FinalistRetestError("CONTROL_INVALID_FILE")
        target = performance_v2_database_path(self._performance_v2_config())
        self._ensure_performance_v2_schema(target)
        try:
            with self._performance_v2_writer_guard(target), duckdb.connect(str(target)) as connection:
                return import_combined_control_workbook(connection, data)
        except FinalistRetestError:
            raise
        except duckdb.Error as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message="Performance v2 database is locked") from error

    strategies_performance_v2_bulk_retest_control_import = strategies_performance_v2_finalist_retest_control_import

    def strategies_performance_v2_retest_tags_import(self, data: bytes) -> dict[str, object]:
        target = performance_v2_database_path(self._performance_v2_config())
        if not target.is_file():
            raise PerformanceV2ApiError("PERFORMANCE_V2_NOT_FOUND", status=404)
        self._ensure_performance_v2_schema(target)
        try:
            with self._performance_v2_writer_guard(target), duckdb.connect(str(target)) as connection:
                return import_retest_tags(connection, data)
        except SelectionReviewError as error:
            details = f": {error.details}" if error.details else ""
            raise PerformanceV2ApiError(error.code, status=400, message=f"{error}{details}") from error
        except PerformanceV2StoreError as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_SCHEMA_INVALID", status=500, message=str(error)) from error
        except duckdb.Error as error:
            raise PerformanceV2ApiError("PERFORMANCE_V2_LOCKED", status=409, message="Performance v2 database is locked") from error

    def strategies_performance_v2_recalculate_all(self) -> dict[str, object]:
        with self._lock:
            if self._performance_v2_recalculate_progress["status"] in {"SCANNING", "RUNNING"}:
                raise PerformanceV2ApiError(
                    "PERFORMANCE_V2_RECALCULATE_IN_PROGRESS", status=409,
                    message="Performance v2 recalculation is already running",
                )
            self._performance_v2_recalculate_progress = {
                "status": "SCANNING", "total_pairs": 0, "ready_pairs": 0,
                "planned_pairs": 0, "completed_pairs": 0,
                "total_strategies": 0, "completed_strategies": 0,
                "current_pair": None, "error": None,
            }
        try:
            config = load_selection_config(self.default_config.with_name("config.performance.json"))
            performance_config = self._performance_v2_config()
            target = performance_v2_database_path(performance_config)
            self._ensure_performance_v2_schema(target)
            with self._performance_v2_writer_lock:
                with duckdb.connect(str(target), read_only=True) as connection:
                    require_performance_v2(connection)
                    pairs = connection.execute(
                        """select s.symbol, s.side from strategies s
                             join strategy_results r on r.result_id = s.current_result_id and r.strategy_id = s.strategy_id
                            where s.lifecycle_status = 'ACTIVE'
                            group by s.symbol, s.side order by s.symbol, s.side"""
                    ).fetchall()
                    pending = []
                    for symbol, side in pairs:
                        request = SelectionRequest(str(symbol), str(side), ())
                        missing_strategy_ids = selection_cache_missing_strategy_ids(
                            connection, request, config, include_equity_regime=True,
                        )
                        if missing_strategy_ids:
                            pending.append((request, missing_strategy_ids))
                with self._lock:
                    self._performance_v2_recalculate_progress.update({
                        "status": "RUNNING", "total_pairs": len(pairs),
                        "ready_pairs": len(pairs) - len(pending),
                        "planned_pairs": len(pending),
                        "total_strategies": sum(len(ids) for _, ids in pending),
                    })

                def after_batch(count: int) -> None:
                    with self._lock:
                        self._performance_v2_recalculate_progress["completed_strategies"] += count

                for request, missing_strategy_ids in pending:
                    with self._lock:
                        self._performance_v2_recalculate_progress["current_pair"] = f"{request.symbol}/{request.side}"
                    with self._performance_v2_writer_guard(target):
                        prepare_selection_window_cache(
                            target, request, config, performance_config.workers, missing_strategy_ids,
                            include_equity_regime=True, on_batch_complete=after_batch,
                        )
                    with self._lock:
                        self._performance_v2_recalculate_progress["completed_pairs"] += 1
            with self._lock:
                self._performance_v2_recalculate_progress.update({"status": "READY", "current_pair": None})
            return {
                "status": "READY", "total_pairs": len(pairs), "recalculated_pairs": len(pending),
                "ready_pairs": len(pairs) - len(pending),
            }
        except Exception as error:
            with self._lock:
                self._performance_v2_recalculate_progress.update({"status": "FAILED", "error": str(error)})
            if isinstance(error, EquitySourceChangedError):
                raise PerformanceV2ApiError("EQUITY_SOURCE_CHANGED", status=409, message=str(error)) from error
            if isinstance(error, EquitySchemaUpgradeRequiredError):
                raise PerformanceV2ApiError(error.code, status=409, message=str(error)) from error
            if isinstance(error, EquityCacheSchemaInvalidError):
                raise PerformanceV2ApiError(error.code, status=500, message=str(error)) from error
            if isinstance(error, (PerformanceV2SelectionError, OSError, duckdb.Error)):
                raise PerformanceV2ApiError("PERFORMANCE_V2_RECALCULATE_FAILED", status=400, message=str(error)) from error
            raise

    def strategies_performance_v2_recalculate_all_progress(self) -> dict[str, object]:
        with self._lock:
            return dict(self._performance_v2_recalculate_progress)

    def _import_settings(self, payload: Mapping[str, object] | None = None) -> DuckDBImportSettings:
        if payload is None:
            return load_duckdb_import_settings(self.default_config)
        previous = load_duckdb_import_settings(self.default_config)
        paths = {name: payload.get(name, getattr(previous, name)) for name in ("source_duckdb_path", "analysis_duckdb_path", "source_v6_surface_dir", "default_html_root", "audit_root")}
        def path(name: str) -> Path | None:
            value = paths[name]
            if value is None or value == "": return None
            if isinstance(value, Path): return value
            if not isinstance(value, str): raise ValueError(f"{name} must be a string or null")
            return self._path(value)
        def number(name: str) -> int:
            value = payload.get(name, getattr(previous, name))
            if isinstance(value, bool): raise ValueError(f"{name} must be a positive integer")
            try: result = int(value)
            except (TypeError, ValueError): raise ValueError(f"{name} must be a positive integer") from None
            if result < 1: raise ValueError(f"{name} must be a positive integer")
            return result
        return DuckDBImportSettings(**{name: path(name) for name in paths}, workers=number("workers"), transaction_batch_size=number("transaction_batch_size"))

    def artifact(self, name: str) -> Path | tuple[str, bytes] | None:
        if name.startswith("performance-v2-failure-report:"):
            return self.performance_v2_failure_report(name.partition(":")[2])
        return None

    def performance_v2_failure_report(self, job_id: str) -> Path:
        try:
            job = self._panel_jobs.get(job_id)
            if job.get("kind") != "strategies.performance.v2.import" or job.get("state") not in {"COMMITTED", "FAILED"}:
                raise ValueError("failure report is not available")
            runtime = self._panel_jobs.runtime(job_id)
            result = job.get("result")
            value = runtime.get("failure_report_path")
            if not isinstance(value, str) or not value:
                value = result.get("failure_report_path") if isinstance(result, dict) else None
            if self._performance_v2_jobs is not None:
                try:
                    private = self._performance_v2_jobs.status(job_id)
                except KeyError:
                    private = {}
                private_result = private.get("result") if isinstance(private, dict) else None
                if isinstance(private_result, dict):
                    private_value = private_result.get("failure_report_path")
                    if isinstance(private_value, str) and private_value:
                        value = private_value
            if not isinstance(value, str) or not value:
                raise ValueError("failure report is not available")
            raw_path = Path(value)
            root = performance_v2_database_path(self._performance_v2_config()).parent.resolve()
            if not root.is_dir() or not raw_path.is_absolute():
                raise ValueError("failure report is not available")
            if raw_path.is_symlink() or os.path.islink(raw_path):
                raise ValueError("failure report is not available")
            os.lstat(raw_path)
            parent = raw_path.parent
            if parent.is_symlink() or os.path.islink(parent) or parent.resolve(strict=True) != root:
                raise ValueError("failure report is not available")
            path = raw_path.resolve(strict=True)
            if path.is_symlink() or not path.is_file():
                raise ValueError("failure report is not available")
            return path
        except (PanelJobError, OSError, ValueError) as error:
            raise ValueError("failure report is not available") from error

class _PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        controller: PanelController,
        *,
        restart_launcher: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self._restart_launcher = restart_launcher or self._launch_restart_helper
        super().__init__(address, _PanelHandler)

    def _launch_restart_helper(self) -> None:
        script = self.controller.root / "scripts" / "restart_new_panel.bat"
        if not script.is_file():
            raise PanelJobError("RESTART_UNAVAILABLE")
        try:
            subprocess.Popen(
                ("cmd.exe", "/c", str(script), str(self.server_port)),
                cwd=self.controller.root,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise PanelJobError("RESTART_UNAVAILABLE") from None

    def restart_panel(self) -> dict[str, bool]:
        if self.controller.has_active_panel_jobs():
            raise PanelJobError("RESTART_BLOCKED")
        self._restart_launcher()
        stop = threading.Timer(0.15, self.shutdown)
        stop.daemon = True
        stop.start()
        return {"restarting": True}










class _PanelHandler(BaseHTTPRequestHandler):
    server: _PanelServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(
        self, status: int, content_type: str, length: int,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
        )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(
        self, status: int, value: object, *, headers: Mapping[str, str] | None = None,
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload), headers)
        self.wfile.write(payload)

    def _empty_json(self, status: int, value: object) -> None:
        """HEAD: the JSON headers without a body."""
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))

    def _portfolio_error(self, error: PortfolioPanelError) -> None:
        self._json(error.status, {"error": {"code": error.code, "message": str(error), "field_errors": error.field_errors}})

    def _has_local_host(self) -> bool:
        host = self.headers.get("Host", "").casefold()
        port = self.server.server_port
        return host in {
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "localhost",
            f"localhost:{port}",
            "[::1]",
            f"[::1]:{port}",
        }

    def do_GET(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        parsed = urlparse(self.path)
        static_paths = {
            "/": "index.html",
            "/panel-web/app.css": "app.css",
            "/panel-web/retest_recovery.js": "retest_recovery.js",
            "/panel-web/app.js": "app.js",
        }
        if parsed.path in static_paths:
            name = static_paths[parsed.path]
            try:
                payload = (_PANEL_WEB / name).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            content_type = {"index.html": "text/html", "app.css": "text/css", "retest_recovery.js": "text/javascript", "app.js": "text/javascript"}[name]
            self._headers(200, f"{content_type}; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if parsed.path.startswith("/api/v2/main/"):
            self._main_get(parsed)
            return
        if parsed.path == "/api/v2/strategies/store-ready/catalog":
            try:
                self._json(200, self.server.controller.store_ready_catalog())
            except StoreReadyError as error:
                self._json(400, {"error": str(error)})
            return
        if parsed.path == "/api/v2/bootstrap":
            self._json(200, self.server.controller.panel_bootstrap())
            return
        if parsed.path == "/api/v2/portfolio/readiness":
            try:
                self._json(200, self.server.controller.portfolio_readiness())
            except PortfolioPanelError as error:
                self._portfolio_error(error)
            return
        if parsed.path == "/api/v2/portfolio/settings":
            try:
                self._json(200, self.server.controller.portfolio_settings_get())
            except PortfolioPanelError as error:
                self._portfolio_error(error)
            return
        if parsed.path == "/api/v2/portfolio/jobs/active":
            try:
                self._json(200, {"job": self.server.controller.portfolio_active_job()})
            except PortfolioPanelError as error:
                self._portfolio_error(error)
            return
        portfolio_job_match = re.fullmatch(r"/api/v2/portfolio/jobs/([^/]+)", parsed.path)
        if portfolio_job_match:
            try:
                self._json(200, {"job": self.server.controller.portfolio_job(portfolio_job_match.group(1))})
            except PortfolioPanelError as error:
                self._portfolio_error(error)
            return
        portfolio_result_match = re.fullmatch(r"/api/v2/portfolio/campaigns/([^/]+)/results", parsed.path)
        if portfolio_result_match:
            try:
                self._json(200, self.server.controller.portfolio_results(portfolio_result_match.group(1)))
            except PortfolioPanelError as error:
                self._portfolio_error(error)
            return
        portfolio_workbook_match = re.fullmatch(r"/api/v2/portfolio/campaigns/([^/]+)/stage1\.xlsx", parsed.path)
        if portfolio_workbook_match:
            try:
                data = self.server.controller.portfolio_workbook(portfolio_workbook_match.group(1))
            except PortfolioPanelError as error:
                self._portfolio_error(error)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="stage1.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/v2/settings/reload":
            try:
                result = self.server.controller.panel_settings_reload()
            except PanelSettingsError as error:
                self._json(400, {"error": str(error)})
                return
            self._json(200, result)
            return
        if parsed.path == "/api/v2/settings/performance-databases":
            try:
                result = self.server.controller.performance_databases_get()
            except PerformanceDatabaseError as error:
                self._json(400, {"error": {"code": error.code, "message": error.message}})
                return
            except (OSError, ValueError):
                self._json(400, {"error": {"code": "PERFORMANCE_DATABASES_INVALID", "message": "PerformanceDB settings are unavailable"}})
                return
            self._json(200, result)
            return
        if parsed.path == "/api/v2/jobs":
            self._json(200, {"jobs": self.server.controller.panel_jobs()})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/maintenance/catalog":
            try:
                self._json(200, self.server.controller.strategies_performance_v2_maintenance_catalog())
            except PerformanceV2ApiError as error:
                self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/maintenance/status":
            try:
                query = parse_qs(parsed.query)
                if set(query) != {"job_id"} or len(query["job_id"]) != 1:
                    raise PerformanceV2ApiError("INVALID_REQUEST", status=400, message="status requires exactly one job_id")
                self._json(200, self.server.controller.strategies_performance_v2_maintenance_status(query["job_id"][0]))
            except PerformanceV2ApiError as error:
                self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
            return
        if parsed.path == "/api/v2/strategies/fresh/batch":
            try:
                self._json(200, self.server.controller.strategies_fresh_batch())
            except ValueError:
                self._json(404, {"error": "fresh strategy batch is not available"})
            return
        if parsed.path == "/api/v2/strategies/tester/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.strategies_tester_status(job_id))
            except (KeyError, ValueError):
                self._json(400, {"error": "invalid strategy batch request"})
            return
        if parsed.path == "/api/v2/strategies/tester/report-collection":
            try:
                self._json(200, self.server.controller.strategies_tester_report_collection())
            except (KeyError, ValueError, PanelJobError):
                self._json(400, {"error": "invalid report collection request"})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/retest/status":
            try:
                self._json(200, self.server.controller.strategies_performance_v2_retest_status())
            except ValueError:
                self._json(200, {"count": 0, "retest_count": 0, "active_count": 0, "phase": "UNAVAILABLE"})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/recalculate-all/progress":
            self._json(200, self.server.controller.strategies_performance_v2_recalculate_all_progress())
            return
        if parsed.path == "/api/v2/strategies/performance-v2/finalist-retest/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.strategies_performance_v2_finalist_retest_status(job_id))
            except (KeyError, ValueError):
                self._json(400, {"error": {"code": "INVALID_REQUEST", "message": "invalid bulk RETEST request"}})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/finalist-retest/equity-filter":
            self._json(405, {"error": {"code": "INVALID_REQUEST", "message": "equity filter requires POST"}})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/finalist-retest/preview":
            raw = parse_qs(parsed.query).get("include_reserve", ["false"])[0].casefold()
            if raw not in {"true", "false"}:
                self._json(400, {"error": {"code": "INVALID_REQUEST", "message": "include_reserve must be true or false"}})
                return
            try:
                self._json(200, self.server.controller.strategies_performance_v2_finalist_retest_preview(raw == "true"))
            except FinalistRetestError as error:
                self._json(409, {"error": {"code": error.code, "message": str(error)}})
            return
        if parsed.path == "/api/v2/strategies/performance-v2/finalist-retest/export":
            try:
                query = parse_qs(parsed.query)
                if "job_id" in query:
                    if set(query) != {"job_id"}:
                        raise ValueError("unsupported export fields")
                    filename, data = self.server.controller.strategies_performance_v2_finalist_retest_export({"job_id": query["job_id"][0]})
                else:
                    if set(query) - {"include_reserve"}:
                        raise ValueError("unsupported export fields")
                    raw = query.get("include_reserve", ["false"])[0].casefold()
                    if raw not in {"true", "false"}:
                        raise ValueError("include_reserve must be true or false")
                    filename, data = self.server.controller.strategies_performance_v2_finalist_retest_export({"include_reserve": raw == "true"})
            except FinalistRetestError as error:
                safe_messages = {
                    "SELECTION_CACHE_INCOMPLETE": "selection cache is not ready; prepare or recalculate it before exporting the control workbook",
                    "CONTROL_CANONICAL_ROW_MISMATCH": "selection cache does not match current results; recalculate the selection cache before exporting the control workbook",
                }
                message = safe_messages.get(error.code)
                if message is None:
                    self._json(409, {"error": {"code": "CONTROL_EXPORT_UNAVAILABLE", "message": "bulk control workbook is unavailable"}})
                else:
                    self._json(409, {"error": {"code": error.code, "message": message}})
                return
            except (KeyError, ValueError):
                self._json(409, {"error": {"code": "CONTROL_EXPORT_UNAVAILABLE", "message": "bulk control workbook is unavailable"}})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/v2/strategies/performance-v2/import/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self._json(200, self.server.controller.strategies_performance_v2_import_status(job_id))
            except (KeyError, ValueError):
                self._json(400, {"error": "invalid Performance v2 import request"})
            return
        if parsed.path == "/api/artifact":
            name = parse_qs(parsed.query).get("name", [""])[0]
            try:
                artifact = self.server.controller.artifact(name)
            except (OSError, ValueError):
                self._json(404, {"error": "artifact is not available"})
                return
            if artifact is None:
                self._json(404, {"error": "artifact is not available"})
                return
            artifact_file: BinaryIO | None = None
            if isinstance(artifact, Path):
                try:
                    filename, data = artifact.name, None
                    artifact_file, size = _open_regular_artifact(artifact)
                except OSError:
                    self._json(404, {"error": "artifact is not available"})
                    return
            else:
                filename, data = artifact
                size = len(data)
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            safe_filename = "".join(char for char in filename if char >= " " and char != "\x7f").replace(chr(34), "") or "artifact"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{safe_filename}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                if data is not None:
                    self.wfile.write(data)
                elif artifact_file is not None:
                    with artifact_file:
                        shutil.copyfileobj(artifact_file, self.wfile, length=1024 * 1024)
            finally:
                if artifact_file is not None and not artifact_file.closed:
                    artifact_file.close()
            return
        self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        if not self._has_local_host():
            self._empty_json(403, {"error": "local Host header required"})
            return
        self._empty_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        if urlparse(self.path).path != "/api/v2/portfolio/settings":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").partition(";")[0].strip().casefold() != "application/json":
            self._portfolio_error(PortfolioPanelError("CONFIG_INVALID", "Content-Type must be application/json", status=415))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._portfolio_error(PortfolioPanelError("CONFIG_INVALID", "invalid JSON body length", status=400))
            return
        try:
            document = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("JSON body must be an object")
            result = self.server.controller.portfolio_settings_put(document)
        except PortfolioPanelError as error:
            self._portfolio_error(error)
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._portfolio_error(PortfolioPanelError("CONFIG_INVALID", "portfolio settings are invalid", status=422))
            return
        self._json(200, result)

    def _json_request(self) -> bool:
        """Early POST routes accept only a JSON body (a cross-site form or text/plain POST is refused)."""
        if self.headers.get("Content-Type", "").partition(";")[0].strip().casefold() != "application/json":
            self._json(415, {"error": "Content-Type: application/json"})
            return False
        return True

    # -- «Пары и стратегии» main screen routes (docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3»)

    _MAIN_POST = ("/api/v2/main/view-settings", "/api/v2/main/decision", "/api/v2/main/setting",
                  "/api/v2/main/plan", "/api/v2/main/run", "/api/v2/main/stop", "/api/v2/main/finalist")
    _MAIN_MARKS_UPLOAD = "/api/v2/main/finalists.xlsx"
    _XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _main_error(self, error: Exception) -> None:
        if isinstance(error, EvaluationProtocolError):
            self._json(409, {"error": {"code": error.code, "message": str(error)}})
        elif isinstance(error, PanelJobError):
            self._json(409 if error.code in {"RESOURCE_BUSY", "JOB_CAPACITY_EXHAUSTED", "IDEMPOTENCY_CONFLICT",
                                             "RESTART_BLOCKED"} else 400, {"error": error.code})
        elif isinstance(error, FinalistRetestError):
            self._json(400 if error.code in {"INVALID_REQUEST", "INVALID_TEST_RANGE"} else 409,
                       {"error": {"code": error.code, "message": str(error)}})
        elif isinstance(error, StrategyBatchValidationError):
            self._json(500, {"error": f"пакет не прошёл самопроверку: {error}"})
        elif isinstance(error, PanelTestingError):
            self._json(400, {"error": "настройки тик-теста не приняты"})
        elif isinstance(error, duckdb.Error):     # the registry or the store catalog is held by a task
            self._json(409, {"error": "реестр или хранилище заняты задачей: повторите"})
        elif isinstance(error, (MainScreenError, StoreReadyError, RegistryError, TradeStoreError)):
            self._json(400, {"error": _redact_local_paths(str(error))})
        elif isinstance(error, RuntimeError):     # a tester service: busy or unavailable
            self._json(409, {"error": "тестер занят или недоступен"})
        else:
            _LOGGER.exception("Main screen request failed")
            self._json(500, {"error": "внутренняя ошибка панели: подробности в журнале"})

    def _main_get(self, parsed) -> None:
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        service = self.server.controller.main_screen()
        workbook = None
        try:
            options = {key: query[key] for key in ("hard", "top5", "hide") if key in query}
            if parsed.path == "/api/v2/main/view":
                result = service.view(query.get("name", ""), query.get("exchange", ""), options)
            elif parsed.path == "/api/v2/main/view.xlsx":
                columns = [key for key in query.get("columns", "").split(",") if key] or None
                workbook = service.view_xlsx(query.get("name", ""), query.get("exchange", ""), columns,
                                             query.get("text", ""), query.get("sort") or None,
                                             query.get("desc") in ("1", "true"), options)
            elif parsed.path == "/api/v2/main/view-settings":
                result = service.view_settings()
            elif parsed.path == "/api/v2/main/pair":
                result = service.pair(query.get("exchange", ""), query.get("symbol", ""))
            elif parsed.path == "/api/v2/main/task":
                result = service.task()
            else:
                self._json(404, {"error": "not found"})
                return
        except Exception as error:                 # every failure answers JSON: the connection is never dropped
            self._main_error(error)
            return
        if workbook is not None:
            filename, data = workbook
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(200, result)

    def _main_marks_upload(self) -> None:
        """«Загрузить отметки»: the workbook is the body (Content-Type xlsx, at most 20 MB); the exchange in the query."""
        if self.headers.get("Content-Type", "").partition(";")[0].strip().casefold() != self._XLSX_TYPE:
            self._json(415, {"error": f"Content-Type: {self._XLSX_TYPE}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 20 * 1024 * 1024:
            self._json(400, {"error": "книга: от 1 байта до 20 МБ"})
            return
        data = self.rfile.read(length)
        exchange = parse_qs(urlparse(self.path).query).get("exchange", [""])[-1]
        try:
            result = self.server.controller.main_screen().import_marks(exchange, data)
        except Exception as error:
            self._main_error(error)
            return
        self._json(200, result)

    def _main_post(self, endpoint: str) -> None:
        if not self._json_request():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            document = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 65_536 else {}
            if not isinstance(document, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "тело запроса: JSON-объект"})
            return
        service = self.server.controller.main_screen()
        try:
            if endpoint == "/api/v2/main/view-settings":
                result = service.save_view_settings(document)
            elif endpoint == "/api/v2/main/decision":
                result = service.decision(document)
            elif endpoint == "/api/v2/main/setting":
                result = service.setting(document)
            elif endpoint == "/api/v2/main/finalist":
                result = service.finalist(document)
            elif endpoint == "/api/v2/main/plan":
                result = service.plan(str(document.get("action", "")), str(document.get("exchange", "")))
            elif endpoint == "/api/v2/main/run":
                options = {} if document.get("options") is None else document["options"]
                if not isinstance(options, dict):
                    raise MainScreenError("options: JSON-объект")
                result = service.run(str(document.get("action", "")), str(document.get("exchange", "")),
                                     document.get("items", []), options)
            else:
                result = service.stop()
        except OSError:
            self._json(409, {"error": "файлы заняты: повторите"})
            return
        except Exception as error:                 # every failure answers JSON: the connection is never dropped
            self._main_error(error)
            return
        self._json(200, result)

    def _store_ready_post(self) -> None:
        if not self._json_request():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            document = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 65_536 else {}
            if not isinstance(document, dict):
                raise StoreReadyError("тело запроса: JSON-объект")
            result = self.server.controller.store_ready_prepare(document)
        except EvaluationProtocolError as error:
            self._json(409, {"error": {"code": error.code, "message": str(error)}})
            return
        except StoreReadyError as error:
            self._json(400, {"error": str(error)})
            return
        except StrategyBatchValidationError as error:
            self._json(500, {"error": f"пакет не прошёл самопроверку: {error}"})
            return
        except OSError:
            self._json(409, {"error": "файлы пакета заняты: повторите подготовку"})
            return
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "тело запроса: JSON-объект"})
            return
        self._json(200, result)

    def do_POST(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        endpoint = urlparse(self.path).path
        if endpoint in self._MAIN_POST:
            self._main_post(endpoint)
            return
        if endpoint == self._MAIN_MARKS_UPLOAD:
            self._main_marks_upload()
            return
        if endpoint == "/api/v2/strategies/store-ready/prepare":
            self._store_ready_post()
            return
        direct_retest_endpoint = endpoint if endpoint in {
            "/api/v2/strategies/performance-v2/retest/start",
            "/api/v2/strategies/performance-v2/retest/import",
        } else None
        bulk_retest_endpoint = endpoint if endpoint in {
            "/api/v2/strategies/performance-v2/finalist-retest/start",
            "/api/v2/strategies/performance-v2/finalist-retest/import",
        } else None
        equity_filter_endpoint = endpoint == "/api/v2/strategies/performance-v2/finalist-retest/equity-filter"
        if bulk_retest_endpoint is not None:
            endpoint = "/api/v2/strategies/performance-v2/retest/start" if bulk_retest_endpoint.endswith("/start") else "/api/v2/strategies/performance-v2/retest/import"
        bulk_control_import_endpoint = urlparse(self.path).path == "/api/v2/strategies/performance-v2/finalist-retest/control-import"
        if bulk_control_import_endpoint:
            endpoint = "/api/v2/strategies/performance-v2/selection-review-import"
        performance_v2_maintenance_apply_endpoint = endpoint == "/api/v2/strategies/performance-v2/maintenance/apply"
        performance_v2_maintenance_endpoint = endpoint in {
            "/api/v2/strategies/performance-v2/maintenance/preview",
            "/api/v2/strategies/performance-v2/maintenance/apply",
        }
        portfolio_preparation_route = endpoint == "/api/v2/portfolio/finalist-inputs"
        portfolio_route = portfolio_preparation_route or endpoint == "/api/v2/portfolio/campaigns" or bool(re.fullmatch(r"/api/v2/portfolio/jobs/[^/]+/cancel", endpoint)) or bool(re.fullmatch(r"/api/v2/portfolio/campaigns/[^/]+/tester-submissions", endpoint))
        portfolio_cancel_route = bool(re.fullmatch(r"/api/v2/portfolio/jobs/[^/]+/cancel", endpoint))
        portfolio_submission_route = bool(re.fullmatch(r"/api/v2/portfolio/campaigns/[^/]+/tester-submissions", endpoint))
        if equity_filter_endpoint:
            pass
        elif bulk_retest_endpoint is None and not bulk_control_import_endpoint and endpoint not in {"/api/v2/panel/restart", "/api/v2/settings/validate", "/api/v2/settings/save", "/api/v2/settings/performance-databases/activate", "/api/v2/settings/performance-databases/create", "/api/v2/jobs", "/api/v2/strategies/tester/verify-inbox", "/api/v2/strategies/tester/report-collection/clear", "/api/v2/strategies/performance-v2/recalculate-all", "/api/v2/strategies/performance-v2/retest-tags-import", "/api/v2/strategies/performance-v2/retest/start", "/api/v2/strategies/performance-v2/retest/import"} and not portfolio_route and not performance_v2_maintenance_endpoint:
            self._json(404, {"error": "not found"})
            return
        if endpoint in {"/api/v2/strategies/performance-v2/selection-review-import", "/api/v2/strategies/performance-v2/retest-tags-import"}:
            error_code = "RETEST_TAG_IMPORT_INVALID_FILE" if endpoint.endswith("retest-tags-import") else "SELECTION_REVIEW_INVALID_FILE"
            if self.headers.get("Content-Type", "").partition(";")[0].strip().casefold() != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                self._json(415, {"error": {"code": error_code, "message": "XLSX Content-Type required"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 20 * 1024 * 1024:
                self._json(400, {"error": {"code": error_code, "message": "XLSX must be between 1 byte and 20 MiB"}})
                return
            if not endpoint.endswith("retest-tags-import") and self.server.controller._registry_decides_for(
                    performance_v2_database_path(self.server.controller._performance_v2_config())):
                self._json(409, {"error": {"code": "MARKS_IN_REGISTRY", "message":
                                           "отметки финалистов ставятся в виде «Стратегии» (ADR-0080)"}})
                return
            try:
                if bulk_control_import_endpoint:
                    result = self.server.controller.strategies_performance_v2_finalist_retest_control_import(self.rfile.read(length))
                else:
                    result = self.server.controller.strategies_performance_v2_retest_tags_import(self.rfile.read(length))
            except PerformanceV2ApiError as error:
                self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
                return
            except FinalistRetestError as error:
                self._json(409, {"error": {"code": error.code, "message": str(error)}})
                return
            self._json(200, result)
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0]
        if content_type.strip().casefold() != "application/json":
            if portfolio_route:
                code = "PORTFOLIO_JOB_CANCEL_INVALID" if portfolio_cancel_route else "PORTFOLIO_CAMPAIGN_INVALID"
                self._portfolio_error(PortfolioPanelError(code, "portfolio request is invalid", status=415))
                return
            self._json(415, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "Content-Type must be application/json"})
            return
        max_body_length = 65_536
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > max_body_length:
            if portfolio_route:
                code = "PORTFOLIO_JOB_CANCEL_INVALID" if portfolio_cancel_route else "PORTFOLIO_CAMPAIGN_INVALID"
                self._portfolio_error(PortfolioPanelError(code, "portfolio request is invalid", status=400))
                return
            self._json(400, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": "JSON body must be between 1 and 65536 bytes"})
            return
        try:
            document = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("JSON body must be an object")
            portfolio_preparation_match = endpoint == "/api/v2/portfolio/finalist-inputs"
            portfolio_campaign_match = endpoint == "/api/v2/portfolio/campaigns"
            portfolio_cancel_match = re.fullmatch(r"/api/v2/portfolio/jobs/([^/]+)/cancel", endpoint)
            portfolio_submission_match = re.fullmatch(r"/api/v2/portfolio/campaigns/([^/]+)/tester-submissions", endpoint)
            if bulk_retest_endpoint is not None:
                endpoint = bulk_retest_endpoint
                if endpoint.endswith("/start"):
                    result = self.server.controller.strategies_performance_v2_finalist_retest_start(document)
                else:
                    result = self.server.controller.strategies_performance_v2_finalist_retest_import(document)
                self._json(200, result if isinstance(result, Mapping) else {"result": result})
                return
            if equity_filter_endpoint:
                result = self.server.controller.strategies_performance_v2_finalist_retest_equity_filter(document)
                self._json(202, result)
                return
            if direct_retest_endpoint is not None:
                document = {
                    "kind": "strategies.performance.v2.retest.start"
                    if direct_retest_endpoint.endswith("/start")
                    else "strategies.performance.v2.retest.import",
                    "request": document,
                }
            if portfolio_preparation_match:
                result = self.server.controller.portfolio_prepare_finalist_inputs(document)
            elif portfolio_campaign_match:
                result = self.server.controller.portfolio_submit_campaign(document)
            elif portfolio_cancel_match:
                result = self.server.controller.portfolio_cancel(portfolio_cancel_match.group(1))
            elif portfolio_submission_match:
                result = self.server.controller.portfolio_tester_submission(portfolio_submission_match.group(1), document)
            elif endpoint == "/api/v2/panel/restart":
                result = self.server.restart_panel()
            elif endpoint == "/api/v2/strategies/tester/verify-inbox":
                result = self.server.controller.strategies_tester_verify_inbox(self.server.controller._required(document, "job_id"))
            elif endpoint == "/api/v2/strategies/tester/report-collection/clear":
                result = self.server.controller.strategies_tester_report_collection_clear(document)
            elif endpoint == "/api/v2/strategies/performance-v2/maintenance/preview":
                result = self.server.controller.strategies_performance_v2_maintenance_preview(document)
            elif endpoint == "/api/v2/strategies/performance-v2/maintenance/apply":
                result = self.server.controller.strategies_performance_v2_maintenance_apply(document)
            elif endpoint == "/api/v2/strategies/performance-v2/recalculate-all":
                result = self.server.controller.strategies_performance_v2_recalculate_all()
            elif endpoint == "/api/v2/strategies/performance-v2/retest/start":
                result = {"job": self.server.controller.panel_job_submit(document)}
            elif endpoint == "/api/v2/strategies/performance-v2/retest/import":
                result = {"job": self.server.controller.panel_job_submit(document)}
            elif endpoint == "/api/v2/jobs":
                result = {"job": self.server.controller.panel_job_submit(document)}
            elif endpoint == "/api/v2/settings/validate":
                result = self.server.controller.panel_settings_validate(document)
            elif endpoint == "/api/v2/settings/save":
                result = self.server.controller.panel_settings_save(document)
            elif endpoint == "/api/v2/settings/performance-databases/activate":
                result = self.server.controller.performance_databases_activate(document)
            elif endpoint == "/api/v2/settings/performance-databases/create":
                result = self.server.controller.performance_databases_create(document)
            else:
                self._json(404, {"error": "not found"})
                return
        except PortfolioPanelError as error:
            self._portfolio_error(error)
            return
        except PanelJobError as error:
            if error.code == "JOB_PERSISTENCE_FAILED":
                _LOGGER.exception("Panel job submission failed while persisting the job journal")
                self._json(
                    503,
                    {"error": {
                        "code": error.code,
                        "message": "Panel could not save the job journal.",
                    }},
                )
                return
            self._json(409 if error.code in {"RESOURCE_BUSY", "JOB_CAPACITY_EXHAUSTED", "IDEMPOTENCY_CONFLICT", "RESTART_BLOCKED"} else 400, {"error": error.code})
            return
        except EvaluationProtocolError as error:
            self._json(409, {"error": {"code": error.code, "message": str(error)}})
            return
        except PerformanceDatabaseError as error:
            status = 409 if error.code in {"RESOURCE_BUSY", "PERFORMANCE_DATABASE_EXISTS"} else 500 if error.code == "PERFORMANCE_DATABASE_CREATE_FAILED" else 400
            self._json(status, {"error": {"code": error.code, "message": error.message}})
            return
        except FinalistRetestError as error:
            status = 400 if error.code in {"INVALID_REQUEST", "INVALID_TEST_RANGE"} else 409
            self._json(status, {"error": {"code": error.code, "message": str(error)}})
            return
        except PanelTestingError:
            self._json(400, {"error": "invalid settings"})
            return
        except PerformanceV2ApiError as error:
            self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
            return
        except RuntimeError as error:
            self._json(409, {"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": str(error)})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as error:
            if portfolio_route:
                code = "PORTFOLIO_JOB_CANCEL_INVALID" if portfolio_cancel_route else "PORTFOLIO_CAMPAIGN_INVALID"
                self._portfolio_error(PortfolioPanelError(code, "portfolio request is invalid", status=422))
            elif direct_retest_endpoint is not None:
                self._json(400, {"error": str(error)})
            else:
                self._json(400, {"error": str(error)} if endpoint == "/api/v2/strategies/tester/verify-inbox" else ({"error": "invalid settings"} if endpoint.startswith("/api/v2/") else {"error": str(error)}))
            return
        accepted = portfolio_preparation_route or portfolio_cancel_route or portfolio_submission_route or performance_v2_maintenance_apply_endpoint or endpoint in {"/api/v2/jobs", "/api/v2/strategies/performance-v2/retest/start", "/api/v2/strategies/performance-v2/retest/import", "/api/v2/strategies/performance-v2/finalist-retest/start", "/api/v2/strategies/performance-v2/finalist-retest/import", "/api/v2/portfolio/campaigns"}
        self._json(202 if accepted else 200, result)

    def do_PATCH(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._has_local_host():
            self._json(403, {"error": "local Host header required"})
            return
        self._json(404, {"error": "not found"})



def create_panel_server(
    host: str,
    port: int,
    controller: PanelController,
    *,
    restart_launcher: Callable[[], None] | None = None,
) -> ThreadingHTTPServer:
    if host.casefold() not in {"127.0.0.1", "localhost"}:
        raise ValueError("control panel must bind to a loopback host")
    if not 0 <= port <= 65535:
        raise ValueError("panel port must be between 0 and 65535")
    return _PanelServer((host, port), controller, restart_launcher=restart_launcher)


def serve_panel(
    host: str = "127.0.0.1",
    port: int = 8765,
    default_config: Path = Path("config.example.json"),
    *,
    open_browser: bool = True,
) -> None:
    root = Path.cwd().resolve()
    controller = PanelController(root, default_config)
    server = create_panel_server(host, port, controller)
    shown_host = "127.0.0.1" if host == "localhost" else host
    url = f"http://{shown_host}:{server.server_port}/"
    print(f"MRS3 Control Panel: {url}", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
