"""Small, local-only Panel adapter for the Portfolio Optimizer stage 1 flow.

The adapter owns HTTP-facing validation and lifecycle plumbing.  Portfolio
algorithms remain injected (and, by default, are the package primitives).
There is deliberately no tester or runtime integration in this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd

from .audit import normalize_xlsx_workbook, write_audit_workbook
from .panel_jobs import PanelJobError, PanelJobRegistry
from .portfolio.config import POLICY_VERSION, SCHEMA_VERSION, PortfolioConfigError, load_portfolio_config
from .portfolio.input import apply_finalist_cutoff, read_current_finalists


STAGES = (
    "VALIDATE_SNAPSHOT",
    "LOAD_FINALISTS",
    "SELECT_CANDIDATES",
    "GENERATE_VARIANTS",
    "VALIDATE_VARIANTS",
    "BUILD_WORKBOOK",
    "PUBLISH_RESULTS",
)
_TERMINAL = frozenset({"COMMITTED", "CANCELLED", "FAILED"})
_SECRET = re.compile(r"(?:password|passwd|secret|token|credential|api[_-]?key|private[_-]?key)", re.I)
_PATH = re.compile(
    r"(?<![\w])[A-Za-z]:[\\/][^\s,;)]*"
    r"|(?<![\w])\\\\[^\s,;)]*"
    r"|(?<![\w/:])/(?!/)[^\s,;)]*"
    r"|(?<![\w])(?:[^\\/\s,;)]+[\\/])+\.\.(?:[\\/][^\s,;)]*)?"
    r"|(?<![\w])\.\.(?:[\\/][^\s,;)]*)+"
)
SUMMARY_HEADERS = ("Key", "Value")
FINALIST_HEADERS = (
    "Campaign ID", "Strategy ID", "Result ID", "Pair", "Direction", "User Status", "User Rank",
    "Effective Maximum", "Selection Status", "Selection Reason",
)
PORTFOLIO_HEADERS = (
    "Campaign ID", "Candidate ID", "Profile", "Scheduling Position", "Scheduling Key ID",
    "Scheduling Score (Individual/Margin; Not Portfolio PnL)", "Member Count", "Pair Count", "Limiter",
    "Maximum Individual DD %", "Minimum Free Margin Reserve %", "Maximum Account MM Load %", "Gate Result",
    "Blocking Reasons", "User Decision", "User Test Priority", "User Comment",
)
MEMBER_HEADERS = (
    "Campaign ID", "Candidate ID", "Profile", "Member Ordinal", "Strategy ID", "Result ID", "Pair",
    "Direction", "User Rank", "Scalar %", "Quantity", "Leverage", "Notional USDT", "Estimated Individual DD USDT",
    "Estimated Individual DD %", "Liquidity Scalar Ceiling %", "Calculated Initial Margin USDT", "Gate Result", "Reasons",
)
EXCLUDED_HEADERS = (
    "Campaign ID", "Scope", "Object ID", "Pair", "Direction", "Profile", "Stage", "Gate Result",
    "Portfolio Reason", "Message",
)
METADATA_HEADERS = ("Key", "Value")


class PortfolioPanelError(ValueError):
    """A typed, client-safe Panel error."""

    def __init__(self, code: str, message: str | None = None, *, status: int = 400, field_errors: Sequence[Mapping[str, str]] = ()) -> None:
        self.code = code
        self.status = status
        self.field_errors = [dict(item) for item in field_errors]
        super().__init__(message or code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _invoke(callback: Callable[..., Any], *values: Any) -> Any:
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return callback(values[0])
    if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters):
        return callback(*values)
    count = len(tuple(parameter for parameter in parameters if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)))
    return callback(*values[:count])


def _decimal(value: Any, field: str, *, positive: bool = True) -> str:
    if isinstance(value, bool) or value is None:
        raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": field, "code": "INVALID_NUMBER", "message": "must be a finite decimal"}])
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        result = Decimal("NaN")
    if not result.is_finite() or (positive and result <= 0):
        raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": field, "code": "INVALID_NUMBER", "message": "must be a finite positive decimal"}])
    digits = list(result.as_tuple().digits)
    exponent = result.as_tuple().exponent
    while digits and digits[-1] == 0 and exponent < 0:
        digits.pop()
        exponent += 1
    if max(-exponent, 0) > 12 or max(len(digits) + exponent, 0) > 26:
        raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": field, "code": "INVALID_NUMBER", "message": "must fit DECIMAL(38,12)"}])
    text = format(result, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _integer(value: Any, field: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (value < 0 if nonnegative else value <= 0):
        raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": field, "code": "INVALID_INTEGER", "message": "must be a valid integer"}])
    return value


def _redact_text(value: Any) -> str:
    text = str(value)
    text = _SECRET.sub("<redacted-secret>", text)
    text = _PATH.sub("<redacted-path>", text)
    return text[:2048]


def _safe_cell(value: Any, *, key: str = "") -> Any:
    if value is None:
        return None
    if _SECRET.search(key) or key.casefold().endswith(("_path", "_root", "_dir")):
        return "<redacted-secret>" if _SECRET.search(key) else "<redacted-path>"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return _redact_text(_json({str(name): _safe_cell(item, key=str(name)) for name, item in value.items()}))
    if isinstance(value, (tuple, list)):
        return _redact_text(_json([_safe_cell(item, key=key) for item in value]))
    if isinstance(value, str):
        text = "<redacted-path>" if _PATH.search(value) else _redact_text(value)
        # Excel treats several leading characters as formula input.  Keep all
        # source text as literal cell content, including untrusted messages.
        if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
            return "'" + text
        return text
    return _redact_text(value)


class PortfolioPanelService:
    """Server-owned stage 1 service with an injected package boundary."""

    def __init__(
        self,
        root: str | Path,
        config_path: str | Path | None = None,
        *,
        registry: PanelJobRegistry | None = None,
        finalists_reader: Callable[..., Any] = read_current_finalists,
        finalists_loader: Callable[..., Any] | None = None,
        cutoff_selector: Callable[..., Any] = apply_finalist_cutoff,
        variant_generator: Callable[..., Any] | None = None,
        variant_validator: Callable[..., Any] | None = None,
        workbook_builder: Callable[..., Any] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        raw_config = Path(config_path) if config_path is not None else self.root / "portfolio_optimizer.local.json"
        self.config_path = raw_config if raw_config.is_absolute() else self.root / raw_config
        self.config_path = self.config_path.resolve()
        self.registry = registry or PanelJobRegistry(self.root / ".panel-jobs.json")
        self.finalists_reader = finalists_loader or finalists_reader
        self.cutoff_selector = cutoff_selector
        self.variant_generator = variant_generator
        self.variant_validator = variant_validator
        self.workbook_builder = workbook_builder
        self._lock = lock or threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._results_root = self.root / ".portfolio-results"
        self._staging_root = self.root / ".portfolio-staging"

    def _settings_raw(self, *, strict: bool = False) -> tuple[str, dict[str, Any] | None, bytes | None]:
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return "MISSING", None, None
        except OSError as error:
            if strict:
                raise PortfolioPanelError(
                    "PORTFOLIO_SETTINGS_UNAVAILABLE",
                    "portfolio settings are unavailable",
                    status=500,
                ) from error
            return "INVALID", None, None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if strict:
                raise PortfolioPanelError(
                    "PORTFOLIO_SETTINGS_UNAVAILABLE",
                    "portfolio settings are unavailable",
                    status=500,
                ) from error
            return "INVALID", None, raw
        if not isinstance(document, dict):
            return "INVALID", None, raw
        schema = document.get("schema_version")
        policy = document.get("policy_version")
        if schema != SCHEMA_VERSION or policy != POLICY_VERSION:
            return "UNSUPPORTED_SCHEMA", document, raw
        try:
            load_portfolio_config(self.config_path)
        except PortfolioConfigError as error:
            if strict:
                raise PortfolioPanelError(
                    "PORTFOLIO_SETTINGS_UNAVAILABLE",
                    "portfolio settings are unavailable",
                    status=500,
                ) from error
            return "INVALID", document, raw
        return "READY", document, raw

    def settings_get(self) -> dict[str, Any]:
        with self._lock:
            try:
                return self._settings_get()
            except PortfolioPanelError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError, PortfolioConfigError) as error:
                raise PortfolioPanelError(
                    "PORTFOLIO_SETTINGS_UNAVAILABLE",
                    "portfolio settings are unavailable",
                    status=500,
                ) from error

    def _settings_get(self) -> dict[str, Any]:
        state, document, raw = self._settings_raw(strict=True)
        return {
            "state": state,
            "document": document,
            "digest": _digest(raw) if raw is not None and document is not None else None,
            "schema_version": document.get("schema_version") if document else None,
            "policy_version": document.get("policy_version") if document else None,
        }

    @staticmethod
    def _encode_document(document: Mapping[str, Any]) -> bytes:
        try:
            return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PortfolioPanelError("CONFIG_INVALID", "portfolio settings are invalid", status=422) from error

    @staticmethod
    def _replace_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def settings_put(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._settings_put(payload)

    def _settings_put(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {"expected_digest", "document"} or not isinstance(payload.get("document"), Mapping):
            raise PortfolioPanelError("CONFIG_INVALID", "expected_digest and full document are required", status=422)
        state, current, raw = self._settings_raw()
        if state != "READY":
            code = "CONFIG_UNSUPPORTED_SCHEMA" if state == "UNSUPPORTED_SCHEMA" else "CONFIG_INVALID"
            raise PortfolioPanelError(code, "portfolio settings are read-only", status=422)
        expected = payload.get("expected_digest")
        current_digest = _digest(raw) if raw is not None and current is not None else None
        if expected != current_digest:
            raise PortfolioPanelError("CONFIG_CHANGED", "portfolio settings changed", status=409)
        document = dict(payload["document"])
        encoded = self._encode_document(document)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=self.config_path.parent, delete=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            try:
                load_portfolio_config(temporary)
            except PortfolioConfigError as error:
                raise PortfolioPanelError("CONFIG_INVALID", "portfolio settings are invalid", status=422) from error
            previous = raw
            os.replace(temporary, self.config_path)
            temporary = None
            try:
                readback = self.config_path.read_bytes()
                if _digest(readback) != _digest(encoded):
                    raise OSError("settings readback digest mismatch")
                load_portfolio_config(self.config_path)
            except Exception as error:
                if previous is not None:
                    self._replace_bytes(self.config_path, previous)
                else:
                    self.config_path.unlink(missing_ok=True)
                raise PortfolioPanelError("CONFIG_WRITE_FAILED", "portfolio settings could not be verified") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self._settings_get()

    def _config(self) -> tuple[Any, bytes, dict[str, Any]]:
        state, document, raw = self._settings_raw()
        if state != "READY" or document is None or raw is None:
            code = "CONFIG_UNSUPPORTED_SCHEMA" if state == "UNSUPPORTED_SCHEMA" else "CONFIG_INVALID"
            raise PortfolioPanelError(code, "portfolio settings are not ready", status=422)
        try:
            config = load_portfolio_config(self.config_path)
            if self.config_path.read_bytes() != raw:
                raise PortfolioPanelError("CONFIG_CHANGED", "portfolio settings changed", status=409)
            return config, raw, document
        except PortfolioConfigError as error:
            raise PortfolioPanelError("CONFIG_INVALID", "portfolio settings are invalid", status=422) from error

    @staticmethod
    def _path_from_config(root: Path, value: Any) -> Path:
        path = Path(str(value))
        return (root / path).resolve() if not path.is_absolute() else path.resolve()

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            try:
                return self._readiness()
            except PortfolioPanelError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError, PortfolioConfigError) as error:
                raise PortfolioPanelError(
                    "PORTFOLIO_SETTINGS_UNAVAILABLE",
                    "portfolio settings are unavailable",
                    status=500,
                ) from error

    def _readiness(self) -> dict[str, Any]:
        state, document, raw = self._settings_raw(strict=True)
        config_digest = _digest(raw) if raw is not None and document is not None else None
        stage1: list[str] = []
        pairs: list[str] = []
        finalists: dict[str, int] = {}
        if state != "READY":
            stage1.append(f"SETTINGS_{state}")
        if state == "READY" and document is not None:
            inputs = document.get("inputs") if isinstance(document.get("inputs"), Mapping) else {}
            database = self._path_from_config(self.root, inputs.get("performance_db", ""))
            if not database.is_file():
                stage1.append("PERFORMANCE_DB_UNAVAILABLE")
            else:
                try:
                    with duckdb.connect(str(database), read_only=True) as connection:
                        pair_rows = connection.execute("select distinct symbol, side from selection_runs order by symbol, side").fetchall()
                    pair_keys = tuple((str(symbol), str(side).upper()) for symbol, side in pair_rows if str(side).upper() in {"LONG", "SHORT"})
                    rows = _invoke(self.finalists_reader, database, pair_keys)
                    for row in rows:
                        pair = f"{row.get('symbol')}|{row.get('side')}"
                        finalists[pair] = finalists.get(pair, 0) + 1
                    pairs = sorted(finalists)
                except Exception:
                    pairs = []
                    finalists = {}
                    stage1.append("FINALISTS_UNAVAILABLE")
        if not pairs and state == "READY" and "PERFORMANCE_DB_UNAVAILABLE" not in stage1:
            stage1.append("NO_FINALISTS")
        active = self.active_job()
        if active is not None:
            stage1.append("PORTFOLIO_JOB_ACTIVE")
        stage1 = list(dict.fromkeys(stage1))
        return {
            "stage1": {"enabled": not stage1, "blockers": stage1},
            "stage2": {"enabled": False, "blockers": ["PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED"]},
            "settings_state": state,
            "schema_version": document.get("schema_version") if document else None,
            "policy_version": document.get("policy_version") if document else None,
            "config_digest": config_digest,
            "search": {
                "total_test_budget": (
                    document.get("search", {}).get("total_test_budget")
                    if isinstance(document, Mapping) and isinstance(document.get("search"), Mapping)
                    else None
                ),
            },
            "available_pairs": pairs,
            "current_finalists": finalists,
        }

    def _snapshot_finalists(self, document: Mapping[str, Any], launch: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        inputs = document.get("inputs") if isinstance(document.get("inputs"), Mapping) else {}
        database = self._path_from_config(self.root, inputs.get("performance_db", ""))
        pairs = tuple((pair["pair"], side) for pair in launch["pairs"] for side in ("LONG", "SHORT"))
        try:
            loaded = _invoke(self.finalists_reader, database, pairs)
            finalists = tuple(_plain(dict(row)) for row in (loaded or ()) if isinstance(row, Mapping))
            json.dumps(finalists, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except PortfolioPanelError:
            raise
        except Exception as error:
            raise PortfolioPanelError("PORTFOLIO_FINALISTS_UNAVAILABLE", "portfolio finalists are unavailable", status=422) from error
        return finalists

    @staticmethod
    def _package_variant_generator(selected: Sequence[Mapping[str, Any]], campaign: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Run the package search boundary without inventing missing evidence."""
        from .portfolio.search import search_portfolios

        variants: list[Any] = []
        blockers: list[str] = []
        excluded: list[dict[str, Any]] = []
        for profile in profiles:
            profile_id = str(profile.get("profile_id", "<unknown-profile>"))
            try:
                options: dict[str, Any] = {"profile": profile}
                max_candidates = profile.get("max_candidates")
                if isinstance(max_candidates, int) and not isinstance(max_candidates, bool):
                    options["max_pretest_variant_count"] = max_candidates
                result = search_portfolios(selected, **options)
                raw_status = getattr(result, "status", None)
                status = str(raw_status) if isinstance(raw_status, str) else ""
                reason = str(getattr(result, "reason", status))
                detail = getattr(result, "detail", None)
                if status == "PASS":
                    variants.extend(
                        PortfolioPanelService._profile_variant(variant, profile_id)
                        for variant in (getattr(result, "passing", ()) or ())
                    )
                    for rejected in getattr(result, "excluded", ()) or ():
                        rejected_reason = str(_get(rejected, "reason", "UNKNOWN"))
                        rejected_detail = str(_get(rejected, "detail", ""))
                        excluded.append(
                            {
                                "profile": profile_id,
                                "stage": "GENERATE_VARIANTS",
                                "selection_reason": rejected_reason,
                                "message": f"{profile_id}:{rejected_reason}:{rejected_detail}".rstrip(":"),
                                "symbol": _get(rejected, "symbol"),
                            }
                        )
                        if rejected_detail == "MAX_PRETEST_VARIANT_COUNT":
                            blockers.append(f"{profile_id}:MAX_CANDIDATES")
                elif status in {"FAIL", "UNKNOWN", "OPEN_POLICY"}:
                    message = f"{profile_id}:{reason}"
                    if detail:
                        message += f":{_redact_text(detail)}"
                    blockers.append(message)
                    excluded.append({"profile": profile_id, "selection_reason": reason, "message": message})
                else:
                    raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "portfolio search returned an invalid status", status=500)
            except PortfolioPanelError:
                raise
            except Exception as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "portfolio search failed", status=500) from error
        return {"variants": tuple(variants), "blockers": blockers, "excluded": tuple(excluded)}

    @staticmethod
    def _profile_variant(variant: Any, profile_id: str) -> Any:
        if _get(variant, "profile") is not None:
            return variant
        if isinstance(variant, Mapping):
            return {**variant, "profile": profile_id}
        attributes = getattr(variant, "__dict__", None)
        if isinstance(attributes, dict):
            return SimpleNamespace(**{key: value for key, value in attributes.items() if key != "profile"}, profile=profile_id)
        return {"variant": variant, "profile": profile_id}

    @staticmethod
    def _normalise_campaign(payload: Mapping[str, Any], config: Any, config_digest: str) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422)
        allowed = {"pairs", "profiles", "expected_config_digest"}
        if set(payload) != allowed:
            raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422)
        if payload.get("expected_config_digest") != config_digest:
            raise PortfolioPanelError("CONFIG_CHANGED", "portfolio settings changed", status=409)
        raw_pairs = payload.get("pairs")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_pairs, list) or not raw_pairs or not isinstance(raw_profiles, list) or not raw_profiles:
            raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "pairs and profiles are required", status=422)
        pairs: list[dict[str, Any]] = []
        seen_pairs: set[str] = set()
        for index, value in enumerate(raw_pairs):
            if not isinstance(value, Mapping) or set(value) != {"pair", "max_finalist_long", "max_finalist_short"}:
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": f"pairs[{index}]", "code": "INVALID_PAIR", "message": "pair fields are invalid"}])
            symbol = value.get("pair")
            if not isinstance(symbol, str) or not symbol.strip() or symbol in seen_pairs:
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422, field_errors=[{"field": f"pairs[{index}].pair", "code": "INVALID_PAIR", "message": "pair must be unique"}])
            seen_pairs.add(symbol)
            pairs.append({"pair": symbol, "max_finalist_long": _integer(value["max_finalist_long"], f"pairs[{index}].max_finalist_long", nonnegative=True), "max_finalist_short": _integer(value["max_finalist_short"], f"pairs[{index}].max_finalist_short", nonnegative=True)})
        profiles: list[dict[str, Any]] = []
        seen_profiles: set[str] = set()
        configured = getattr(config, "profiles", {})
        total_budget = int(config.search["total_test_budget"])
        candidate_total = 0
        for index, value in enumerate(raw_profiles):
            if not isinstance(value, Mapping):
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422)
            keys = set(value)
            required = {"profile_id", "equity_usdt", "max_candidates"}
            allowed = required | {"max_balance_usdt"}
            if not required.issubset(keys) or not keys.issubset(allowed):
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422)
            profile_id = value.get("profile_id")
            if not isinstance(profile_id, str) or profile_id not in configured or profile_id in seen_profiles:
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign profile is invalid", status=422, field_errors=[{"field": f"profiles[{index}].profile_id", "code": "UNKNOWN_PROFILE", "message": "profile is unknown"}])
            seen_profiles.add(profile_id)
            max_balance = value.get("max_balance_usdt") if "max_balance_usdt" in value else None
            if "max_balance_usdt" in value and max_balance is None:
                raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_INVALID", "campaign fields are invalid", status=422)
            max_candidates = _integer(value.get("max_candidates"), f"profiles[{index}].max_candidates")
            candidate_total += max_candidates
            profiles.append({"profile_id": profile_id, "equity_usdt": _decimal(value.get("equity_usdt"), f"profiles[{index}].equity_usdt"), "max_balance_usdt": _decimal(max_balance, f"profiles[{index}].max_balance_usdt") if max_balance is not None else None, "max_candidates": max_candidates})
            if max_candidates > total_budget or candidate_total > total_budget:
                raise PortfolioPanelError(
                    "PORTFOLIO_CAMPAIGN_INVALID",
                    "profile candidate budgets exceed configured total_test_budget",
                    status=422,
                    field_errors=[
                        {
                            "field": f"profiles[{index}].max_candidates",
                            "code": "TOTAL_TEST_BUDGET_EXCEEDED",
                            "message": "profile candidate budgets exceed configured total_test_budget",
                        }
                    ],
                )
        launch = {"pairs": pairs, "profiles": profiles}
        launch["selected_pairs"] = [[pair["pair"], side] for pair in pairs for side in ("LONG", "SHORT")]
        launch["maximums"] = {f"{pair['pair']}|LONG": pair["max_finalist_long"] for pair in pairs} | {f"{pair['pair']}|SHORT": pair["max_finalist_short"] for pair in pairs}
        return launch

    def _campaign_by_id(self, campaign_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            jobs = self.registry.list()
        except (PanelJobError, KeyError, OSError, TypeError, ValueError) as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        for saved in jobs:
            if saved.get("kind") != "portfolio.stage1":
                continue
            try:
                runtime = self.registry.runtime(saved["job_id"])
            except (PanelJobError, KeyError, OSError, TypeError, ValueError) as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
            if not isinstance(runtime, Mapping):
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500)
            campaign = runtime.get("campaign")
            if isinstance(campaign, Mapping) and campaign.get("campaign_id") == campaign_id:
                return saved, runtime
        raise PortfolioPanelError("PORTFOLIO_CAMPAIGN_NOT_FOUND", "campaign is not available", status=404)

    def _project_orphan(self, saved: Mapping[str, Any]) -> dict[str, Any]:
        """Release a persisted nonterminal job after its worker has disappeared."""
        job_id = saved.get("job_id")
        if saved.get("kind") != "portfolio.stage1" or not isinstance(job_id, str) or saved.get("state") not in {"QUEUED", "RUNNING", "CANCELLING"} or job_id in self._threads:
            return dict(saved)
        projected = {**saved, "state": "FAILED", "phase": "FAILED", "error": {"code": "INTERRUPTED"}}
        try:
            self.registry.sync(job_id, {"state": "FAILED", "phase": "FAILED", "error": {"code": "INTERRUPTED"}})
        except Exception:
            jobs = getattr(self.registry, "jobs", None)
            lock = getattr(self.registry, "lock", None)
            if isinstance(jobs, dict) and lock is not None:
                with lock:
                    current = jobs.get(job_id)
                    if isinstance(current, dict) and current.get("state") not in _TERMINAL:
                        current.update(state="FAILED", phase="FAILED", error={"code": "INTERRUPTED"})
                        try:
                            self.registry._save()
                        except Exception:
                            pass
        return projected

    @staticmethod
    def _project_state(saved: Mapping[str, Any]) -> str:
        state = saved.get("state")
        if state == "COMMITTED":
            return "SUCCEEDED"
        if state == "CANCELLING":
            return "CANCEL_REQUESTED"
        if state == "FAILED" and isinstance(saved.get("error"), Mapping) and saved["error"].get("code") == "INTERRUPTED":
            return "INTERRUPTED"
        return str(state)

    def _public_job(self, saved: Mapping[str, Any], runtime: Mapping[str, Any] | None = None) -> dict[str, Any]:
        runtime = runtime or {}
        campaign = runtime.get("campaign") if isinstance(runtime.get("campaign"), Mapping) else {}
        state = self._project_state(saved)
        completed = int(runtime.get("completed_stages", 0) or 0)
        if state == "SUCCEEDED":
            completed = len(STAGES)
        stage: dict[str, Any] = {"index": min(completed, len(STAGES) - 1), "name": STAGES[min(completed, len(STAGES) - 1)], "status": "SUCCEEDED" if state == "SUCCEEDED" else ("RUNNING" if state in {"RUNNING", "CANCEL_REQUESTED"} else state), "completed": int(runtime.get("stage_completed", 0) or 0)}
        if state == "SUCCEEDED":
            stage.update(completed=1, total=1, percent=100)
        elif isinstance(runtime.get("stage_total"), int) and runtime["stage_total"] >= 0:
            stage["total"] = runtime["stage_total"]
        if isinstance(runtime.get("stage_percent"), int):
            stage["percent"] = runtime["stage_percent"]
        overall = 100 if state == "SUCCEEDED" else min(99, (completed * 100) // len(STAGES))
        if state in {"CANCELLED", "FAILED", "INTERRUPTED"}:
            overall = (completed * 100) // len(STAGES)
        diagnostics = saved.get("diagnostics") if isinstance(saved.get("diagnostics"), list) else runtime.get("diagnostics", [])
        diagnostics = [
            {"severity": str(item.get("severity", "ERROR")), "code": str(item.get("code", "PORTFOLIO_JOB_FAILED")), "message": _redact_text(item.get("message", ""))}
            for item in diagnostics if isinstance(item, Mapping)
        ]
        journal = runtime.get("journal", [])
        journal = [
            {
                "timestamp_utc": str(item.get("timestamp_utc", "")),
                "stage": str(item.get("stage", "")),
                "severity": str(item.get("severity", "INFO")),
                "code": str(item.get("code", "PORTFOLIO_JOB_FAILED")),
                "text": _redact_text(item.get("text", "")),
                "counters": _plain(item.get("counters", {})),
            }
            for item in journal if isinstance(item, Mapping)
        ]
        current_digest = None
        try:
            if self.config_path.is_file():
                current_digest = _digest(self.config_path.read_bytes())
        except OSError:
            pass
        frozen_digest = campaign.get("config_digest")
        result = {"job_id": saved.get("job_id"), "campaign_id": campaign.get("campaign_id"), "kind": "STAGE1_CALCULATION", "status": state, "stage": stage, "overall_percent": overall, "counters": _plain(runtime.get("counters", {})), "diagnostics": diagnostics, "journal": journal, "input_digest": campaign.get("input_digest"), "config_digest": frozen_digest, "settings_changed_since_freeze": bool(frozen_digest and current_digest != frozen_digest), "created_at": saved.get("created_at_utc"), "started_at": runtime.get("started_at"), "finished_at": runtime.get("finished_at")}
        return result

    def job(self, job_id: str) -> dict[str, Any]:
        try:
            saved = self.registry.get(job_id)
        except PanelJobError as error:
            if error.code == "NOT_FOUND":
                raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404) from error
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        except KeyError as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404) from error
        except (OSError, TypeError, ValueError) as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        if not isinstance(saved, Mapping):
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500)
        saved = self._project_orphan(saved)
        if saved.get("kind") != "portfolio.stage1":
            raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404)
        try:
            runtime = self.registry.runtime(job_id)
        except (PanelJobError, KeyError, OSError, TypeError, ValueError) as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        if not isinstance(runtime, Mapping):
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500)
        return self._public_job(saved, runtime)

    def active_job(self) -> dict[str, Any] | None:
        try:
            jobs = self.registry.list()
        except (PanelJobError, KeyError, OSError, TypeError, ValueError) as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        for saved in jobs:
            if saved.get("kind") != "portfolio.stage1":
                continue
            saved = self._project_orphan(saved)
            if saved.get("state") in _TERMINAL:
                continue
            try:
                runtime = self.registry.runtime(saved["job_id"])
            except (PanelJobError, KeyError, OSError, TypeError, ValueError) as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
            if not isinstance(runtime, Mapping):
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500)
            return self._public_job(saved, runtime)
        return None

    def submit_campaign(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self.registry.lock:
            config, raw, document = self._config()
            config_digest = _digest(raw)
            launch = self._normalise_campaign(payload, config, config_digest)
            input_digest = _digest(_json(launch))
            active_jobs: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
            for saved in self.registry.list():
                if saved.get("kind") != "portfolio.stage1":
                    continue
                saved = self._project_orphan(saved)
                if saved.get("state") in _TERMINAL:
                    continue
                runtime = self.registry.runtime(saved["job_id"])
                frozen = runtime.get("campaign") if isinstance(runtime.get("campaign"), Mapping) else {}
                active_jobs.append((saved, frozen))
                if frozen.get("input_digest") == input_digest and frozen.get("config_digest") == config_digest:
                    raise PortfolioPanelError("PORTFOLIO_JOB_ACTIVE_DUPLICATE", "an identical campaign is active", status=409)
            if active_jobs:
                raise PortfolioPanelError("PORTFOLIO_JOB_BUSY", "portfolio optimizer is busy", status=409)
            finalists = self._snapshot_finalists(document, launch)
            campaign_id = f"campaign-{uuid4().hex}"
            campaign = {"campaign_id": campaign_id, "created_at_utc": _now(), "input_digest": input_digest, "config_digest": config_digest, "config_bytes": base64.b64encode(raw).decode("ascii"), "config_document": _plain(document), "launch": _plain(launch), "finalists": finalists, "versions": {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "algorithm_versions": _plain(document.get("algorithm_versions", {}))}}
            saved = None
            submission_key = f"portfolio:{campaign_id}"

            def discard_created_job() -> None:
                candidates: list[str] = []
                if isinstance(saved, Mapping) and isinstance(saved.get("job_id"), str):
                    candidates.append(saved["job_id"])
                else:
                    try:
                        candidates.extend(
                            str(item["job_id"])
                            for item in self.registry.list()
                            if item.get("idempotency_key") == submission_key and item.get("state") == "QUEUED"
                        )
                    except Exception:
                        return
                for candidate in candidates:
                    try:
                        self.registry.discard_queued(candidate)
                    except Exception:
                        pass

            try:
                saved = self.registry.submit("portfolio.stage1", {"campaign_id": campaign_id, "input_digest": input_digest, "config_digest": config_digest}, submission_key, ("portfolio_optimizer",))
                self.registry.reserve_runtime(saved["job_id"], "campaign", campaign)
            except PanelJobError as error:
                discard_created_job()
                code = "PORTFOLIO_JOB_BUSY" if error.code in {"RESOURCE_BUSY", "JOB_CAPACITY_EXHAUSTED"} else error.code
                raise PortfolioPanelError(code, "portfolio optimizer is busy" if code == "PORTFOLIO_JOB_BUSY" else code, status=409 if code == "PORTFOLIO_JOB_BUSY" else 400) from error
            except Exception as error:
                discard_created_job()
                raise PortfolioPanelError("PORTFOLIO_JOB_START_FAILED", "portfolio job could not start", status=503) from error
            event = threading.Event()
            self._cancel_events[saved["job_id"]] = event
            try:
                worker = threading.Thread(target=self._run, args=(saved["job_id"],), name="mrs3-portfolio-stage1", daemon=True)
                self._threads[saved["job_id"]] = worker
                worker.start()
            except BaseException as error:
                self._threads.pop(saved["job_id"], None)
                self._cancel_events.pop(saved["job_id"], None)
                self.registry.discard_queued(saved["job_id"])
                raise PortfolioPanelError("PORTFOLIO_JOB_START_FAILED", "portfolio job could not start", status=503) from error
        return {"campaign_id": campaign_id, "job_id": saved["job_id"], "status": "QUEUED", "input_digest": input_digest, "config_digest": config_digest}

    def _sync_runtime(self, job_id: str, **values: Any) -> None:
        runtime = self.registry.runtime(job_id)
        runtime.update(values)
        self.registry.sync(job_id, {"state": self.registry.get(job_id)["state"]}, runtime=runtime)

    def _append_journal(self, job_id: str, *, stage: str, severity: str, code: str, text: Any) -> None:
        code = code if code.startswith("PORTFOLIO_JOB_") else f"PORTFOLIO_JOB_{code}"
        runtime = self.registry.runtime(job_id)
        entries = runtime.get("journal") if isinstance(runtime.get("journal"), list) else []
        entries.append({"timestamp_utc": _now(), "stage": stage, "severity": severity, "code": code, "text": _redact_text(text), "counters": _plain(runtime.get("counters", {}))})
        runtime["journal"] = entries[-200:]
        self.registry.sync(job_id, {"state": self.registry.get(job_id)["state"]}, runtime=runtime)

    def _set_progress(self, job_id: str, *, stage_index: int, completed: int, stage_total: int | None = None, stage_percent: int | None = None, stage_completed: int = 0) -> None:
        runtime = self.registry.runtime(job_id)
        runtime.update({"stage_index": stage_index, "completed_stages": completed, "stage_completed": stage_completed})
        if stage_total is not None:
            runtime["stage_total"] = stage_total
        else:
            runtime.pop("stage_total", None)
        if stage_percent is not None:
            runtime["stage_percent"] = stage_percent
        else:
            runtime.pop("stage_percent", None)
        self.registry.sync(job_id, {"state": self.registry.get(job_id)["state"], "progress": {"current": completed, "total": len(STAGES), "unit": "stages"}}, runtime=runtime)

    def _cancelled(self, job_id: str) -> bool:
        event = self._cancel_events.get(job_id)
        try:
            state = self.registry.get(job_id)["state"]
        except PanelJobError:
            return True
        return bool(event and event.is_set()) or state == "CANCELLING"

    def _cancel_finish(self, job_id: str, published: Path | None = None, previous: bytes | None = None) -> None:
        if published is not None:
            self._rollback_result(published, previous)
        runtime = self.registry.runtime(job_id)
        candidate = runtime.get("staged_workbook_path")
        if isinstance(candidate, str):
            try:
                self._staging_path(Path(candidate), runtime["campaign"]["campaign_id"]).unlink(missing_ok=True)
            except (KeyError, OSError, PortfolioPanelError):
                pass
        stage_index = runtime.get("stage_index")
        stage = STAGES[stage_index] if isinstance(stage_index, int) and 0 <= stage_index < len(STAGES) else "CANCELLED"
        self._append_journal(job_id, stage=stage, severity="INFO", code="CANCELLED", text="portfolio calculation cancelled")
        try:
            state = self.registry.get(job_id)["state"]
            if state in {"QUEUED", "RUNNING"}:
                state = self.registry.cancel(job_id)["state"]
            if state == "CANCELLING":
                self.registry.transition(job_id, "CANCELLED", phase="CANCELLED")
        except PanelJobError:
            pass
        self._sync_runtime(job_id, finished_at=_now())

    def _staging_path(self, path: Path, campaign_id: str) -> Path:
        expected_parent = self._staging_root / campaign_id
        if not path.is_absolute() or path.name != "stage1.xlsx" or path.parent != expected_parent:
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staged workbook is outside the server staging root", status=500)
        if path.is_symlink() or expected_parent.is_symlink() or self._staging_root.is_symlink():
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staged workbook is outside the server staging root", status=500)
        try:
            resolved = path.resolve(strict=True)
            if resolved.parent != expected_parent.resolve(strict=True):
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staged workbook is outside the server staging root", status=500)
        except FileNotFoundError as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staged workbook is missing", status=500) from error
        return resolved

    def _staging_target(self, campaign_id: str) -> Path:
        """Create the server-owned staging directory without following links."""
        root = self._staging_root
        if root.exists() and root.is_symlink():
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staging root is unsafe", status=500)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if root.resolve(strict=True) != root:
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staging root is unsafe", status=500)
            directory = root / campaign_id
            if directory.exists() and directory.is_symlink():
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staging root is unsafe", status=500)
            directory.mkdir(exist_ok=True)
            if directory.resolve(strict=True) != directory:
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staging root is unsafe", status=500)
        except OSError as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "staging root is unsafe", status=500) from error
        return directory / "stage1.xlsx"

    def _result_path(self, campaign_id: str) -> Path:
        if self._results_root.exists() and self._results_root.is_symlink():
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "result root is unsafe", status=500)
        directory = self._results_root / campaign_id
        if directory.exists() and directory.is_symlink():
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "result root is unsafe", status=500)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve(strict=True) != directory:
            raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "result root is unsafe", status=500)
        return directory / "stage1.xlsx"

    @staticmethod
    def _verify_workbook(path: Path) -> None:
        from openpyxl import load_workbook

        workbook = load_workbook(path, data_only=False)
        try:
            if workbook.sheetnames != ["Summary", "Finalists", "Portfolios", "Members", "Excluded", "Metadata"]:
                raise ValueError("workbook sheets are invalid")
            if getattr(workbook, "_external_links", ()):
                raise ValueError("workbook external links are not allowed")
            for worksheet in workbook.worksheets:
                for row in worksheet.iter_rows():
                    for cell in row:
                        if cell.data_type == "f":
                            raise ValueError("workbook formulas are not allowed")
                        if getattr(cell, "hyperlink", None) is not None:
                            raise ValueError("workbook links are not allowed")
        finally:
            workbook.close()

    def _rollback_result(self, path: Path, previous: bytes | None) -> None:
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                self._replace_bytes(path, previous)
        except OSError:
            pass

    def _run(self, job_id: str) -> None:
        published: Path | None = None
        previous: bytes | None = None
        try:
            self.registry.transition(job_id, "RUNNING", phase=STAGES[0])
            saved = self.registry.get(job_id)
            runtime = self.registry.runtime(job_id)
            campaign = runtime["campaign"]
            launch = campaign["launch"]
            try:
                frozen_raw = base64.b64decode(campaign["config_bytes"], validate=True)
                if _digest(frozen_raw) != campaign["config_digest"] or json.loads(frozen_raw.decode("utf-8")) != campaign["config_document"]:
                    raise ValueError("frozen config digest mismatch")
            except (ValueError, TypeError, UnicodeDecodeError):
                raise PortfolioPanelError("CONFIG_INVALID", "frozen portfolio settings are invalid", status=422) from None
            self._sync_runtime(job_id, started_at=_now())
            finalists = tuple(dict(row) for row in campaign.get("finalists", ()) if isinstance(row, Mapping))
            selected: tuple[dict[str, Any], ...] = ()
            variants: tuple[Any, ...] = ()
            excluded: tuple[dict[str, Any], ...] = ()
            optimizer_excluded: tuple[dict[str, Any], ...] = ()
            optimizer_blockers: list[str] = []
            for index, stage in enumerate(STAGES):
                if self._cancelled(job_id):
                    self._cancel_finish(job_id, published, previous)
                    published = None
                    return
                self._append_journal(job_id, stage=stage, severity="INFO", code="STAGE_STARTED", text=stage)
                known_total = {
                    "LOAD_FINALISTS": len(finalists),
                    "SELECT_CANDIDATES": len(finalists),
                    "GENERATE_VARIANTS": len(launch["profiles"]),
                    "VALIDATE_VARIANTS": len(variants),
                }.get(stage)
                known_total = known_total if known_total and known_total > 0 else None
                self._set_progress(
                    job_id,
                    stage_index=index,
                    completed=index,
                    stage_total=known_total,
                    stage_percent=0 if known_total is not None else None,
                )
                if stage == "VALIDATE_SNAPSHOT":
                    continue
                if stage == "LOAD_FINALISTS":
                    self._sync_runtime(job_id, counters={"finalists_read": len(finalists)})
                elif stage == "SELECT_CANDIDATES":
                    selected_pairs = tuple((item[0], item[1]) for item in launch["selected_pairs"])
                    maximums = {tuple(key.rsplit("|", 1)): value for key, value in launch["maximums"].items()}
                    rows = self.cutoff_selector(finalists, selected_pairs=selected_pairs, maximums=maximums)
                    selected = tuple(dict(row) for row in (rows or ()) if row.get("selection_status") == "SELECTED")
                    excluded = tuple(dict(row) for row in (rows or ()) if row.get("selection_status") != "SELECTED")
                    if not selected:
                        raise PortfolioPanelError("PORTFOLIO_JOB_NO_ELIGIBLE_CANDIDATES", "no eligible candidates", status=422)
                elif stage == "GENERATE_VARIANTS":
                    generator = self.variant_generator or self._package_variant_generator
                    generated = _invoke(generator, selected, campaign, launch["profiles"])
                    if isinstance(generated, Mapping) and "variants" in generated:
                        variants = tuple(generated.get("variants") or ())
                        optimizer_blockers = [str(item) for item in generated.get("blockers", ()) if isinstance(item, str)]
                        generated_excluded = generated.get("excluded", ())
                        optimizer_excluded = tuple(dict(item) for item in generated_excluded if isinstance(item, Mapping))
                    else:
                        variants = tuple(generated or ())
                    if not variants and not optimizer_blockers:
                        optimizer_blockers = ["PORTFOLIO_JOB_VARIANTS_NOT_READY"]
                    variants, capped, cap_blockers = self._cap_variants(variants, launch["profiles"])
                    optimizer_excluded = tuple((*optimizer_excluded, *capped))
                    optimizer_blockers.extend(cap_blockers)
                    if not variants:
                        raise PortfolioPanelError(
                            "PORTFOLIO_JOB_VARIANTS_NOT_READY",
                            "portfolio variant generation produced no variants",
                            status=422,
                        )
                elif stage == "VALIDATE_VARIANTS":
                    if self.variant_validator is not None:
                        checked = _invoke(self.variant_validator, variants, campaign)
                        if isinstance(checked, Mapping) and "variants" in checked:
                            variants = tuple(checked.get("variants") or ())
                            optimizer_blockers.extend(str(item) for item in checked.get("blockers", ()) if isinstance(item, str))
                            optimizer_excluded = tuple((*optimizer_excluded, *(dict(item) for item in checked.get("excluded", ()) if isinstance(item, Mapping))))
                        else:
                            variants = tuple(checked or ())
                    variants, capped, cap_blockers = self._cap_variants(variants, launch["profiles"])
                    optimizer_excluded = tuple((*optimizer_excluded, *capped))
                    optimizer_blockers.extend(cap_blockers)
                    if not variants:
                        raise PortfolioPanelError(
                            "PORTFOLIO_JOB_VARIANTS_NOT_READY",
                            "portfolio variant validation produced no variants",
                            status=422,
                        )
                elif stage == "BUILD_WORKBOOK":
                    final_workbook = self._results_root / campaign["campaign_id"] / "stage1.xlsx"
                    workbook = self._staging_target(campaign["campaign_id"])
                    if self.workbook_builder is not None:
                        built = _invoke(self.workbook_builder, workbook, campaign, finalists, selected, variants, excluded)
                        workbook = Path(built) if built is not None else workbook
                    else:
                        self._write_workbook(workbook, campaign, finalists, selected, variants, excluded, optimizer_excluded=optimizer_excluded, blockers=optimizer_blockers)
                    self._staging_path(workbook, campaign["campaign_id"])
                    summary = self._summary(campaign, finalists, selected, variants, excluded, optimizer_excluded=optimizer_excluded, blockers=optimizer_blockers)
                    runtime_values = {"staged_workbook_path": str(workbook), "final_workbook_path": str(final_workbook), "summary": summary, "counters": {"finalists_read": len(finalists), "candidates_selected": len(selected), "variants_created": len(variants)}}
                    self._sync_runtime(job_id, **runtime_values)
                elif stage == "PUBLISH_RESULTS":
                    pass
                self._set_progress(job_id, stage_index=index + 1 if index + 1 < len(STAGES) else index, completed=index + 1)
            if self._cancelled(job_id):
                self._cancel_finish(job_id, published, previous)
                published = None
                return
            runtime = self.registry.runtime(job_id)
            staged = self._staging_path(Path(runtime["staged_workbook_path"]), campaign["campaign_id"])
            final = self._result_path(campaign["campaign_id"])
            if final.is_symlink():
                raise PortfolioPanelError("PORTFOLIO_JOB_FAILED", "result workbook is unsafe", status=500)
            if final.is_file():
                previous = final.read_bytes()
            os.replace(staged, final)
            published = final
            self._verify_workbook(final)
            if self._cancelled(job_id):
                self._cancel_finish(job_id, published, previous)
                published = None
                return
            self._append_journal(job_id, stage="PUBLISH_RESULTS", severity="INFO", code="COMPLETED", text="stage1 workbook published")
            runtime = self.registry.runtime(job_id)
            committed_runtime = {**runtime, "workbook_path": str(final), "finished_at": _now()}
            try:
                self.registry.sync(job_id, {"state": "COMMITTED", "phase": "COMMITTED", "result": runtime.get("summary", {})}, runtime=committed_runtime)
            except BaseException:
                self._rollback_result(final, previous)
                published = None
                raise
            return
        except BaseException as error:
            if self._cancelled(job_id):
                try:
                    self._cancel_finish(job_id, published, previous)
                except PanelJobError:
                    pass
                return
            if published is not None:
                self._rollback_result(published, previous)
            try:
                failed_runtime = self.registry.runtime(job_id)
                candidate = failed_runtime.get("staged_workbook_path")
                if not isinstance(candidate, str):
                    campaign = failed_runtime.get("campaign")
                    campaign_id = campaign.get("campaign_id") if isinstance(campaign, Mapping) else None
                    if isinstance(campaign_id, str):
                        fallback = self._staging_root / campaign_id / "stage1.xlsx"
                        candidate = str(fallback) if fallback.exists() or fallback.is_symlink() else None
                if isinstance(candidate, str):
                    try:
                        self._staging_path(Path(candidate), failed_runtime["campaign"]["campaign_id"]).unlink(missing_ok=True)
                    except (KeyError, OSError, PortfolioPanelError):
                        pass
            except PanelJobError:
                pass
            try:
                current = self.registry.get(job_id)
                if current["state"] not in _TERMINAL:
                    self.registry.transition(job_id, "FAILED", phase="FAILED")
                code = error.code if isinstance(error, PortfolioPanelError) else "PORTFOLIO_JOB_FAILED"
                message = str(error) if isinstance(error, PortfolioPanelError) else "portfolio calculation failed"
                diagnostics = [{"severity": "ERROR", "code": code, "message": _redact_text(message)}]
                failed_runtime = self.registry.runtime(job_id)
                failed_runtime.pop("workbook_path", None)
                entries = failed_runtime.get("journal") if isinstance(failed_runtime.get("journal"), list) else []
                stage_index = failed_runtime.get("stage_index")
                stage = STAGES[stage_index] if isinstance(stage_index, int) and 0 <= stage_index < len(STAGES) else "FAILED"
                entries.append({"timestamp_utc": _now(), "stage": stage, "severity": "ERROR", "code": code if code.startswith("PORTFOLIO_JOB_") else f"PORTFOLIO_JOB_{code}", "text": _redact_text(message), "counters": _plain(failed_runtime.get("counters", {}))})
                failed_runtime["journal"] = entries[-200:]
                self.registry.sync(job_id, {"state": "FAILED", "phase": "FAILED", "error": {"code": code, "message": _redact_text(message)}, "result": {}}, runtime={**failed_runtime, "diagnostics": diagnostics, "finished_at": _now()})
            except PanelJobError:
                pass
        finally:
            self._cancel_events.pop(job_id, None)
            self._threads.pop(job_id, None)

    @staticmethod
    def _summary(campaign: Mapping[str, Any], finalists: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], variants: Sequence[Any], excluded: Sequence[Mapping[str, Any]], *, optimizer_excluded: Sequence[Mapping[str, Any]] = (), blockers: Sequence[str] = ()) -> dict[str, Any]:
        blockers = list(dict.fromkeys(str(item) for item in blockers if item))
        finalist_reasons: dict[str, int] = {}
        for item in excluded:
            reason = str(item.get("selection_reason") or "UNKNOWN")
            finalist_reasons[reason] = finalist_reasons.get(reason, 0) + 1
        optimizer_reasons: dict[str, int] = {}
        for item in optimizer_excluded:
            reason = str(item.get("selection_reason") or item.get("reason") or "UNKNOWN")
            optimizer_reasons[reason] = optimizer_reasons.get(reason, 0) + 1
        variants_by_profile: dict[str, int] = {}
        for item in variants:
            profile = str(_get(item, "profile", "UNASSIGNED") or "UNASSIGNED")
            variants_by_profile[profile] = variants_by_profile.get(profile, 0) + 1
        prepared_by_profile = {
            str(profile["profile_id"]): min(
                variants_by_profile.get(str(profile["profile_id"]), 0),
                int(profile["max_candidates"]),
            )
            for profile in campaign.get("launch", {}).get("profiles", ())
            if isinstance(profile, Mapping)
        }
        return {
            "finalists_read": len(finalists),
            "candidates_selected": len(selected),
            "variants_created": len(variants),
            "variants_by_profile": dict(sorted(variants_by_profile.items())),
            "prepared": sum(prepared_by_profile.values()),
            "prepared_by_profile": dict(sorted(prepared_by_profile.items())),
            "excluded": len(excluded),
            "finalist_exclusions_by_reason": dict(sorted(finalist_reasons.items())),
            "optimizer_excluded": len(optimizer_excluded),
            "optimizer_exclusions_by_reason": dict(sorted(optimizer_reasons.items())),
            "blockers": blockers,
            "optimizer_blockers": blockers,
            "campaign_id": campaign["campaign_id"],
        }

    @staticmethod
    def _cap_variants(variants: Sequence[Any], profiles: Sequence[Mapping[str, Any]]) -> tuple[tuple[Any, ...], tuple[dict[str, Any], ...], tuple[str, ...]]:
        limits = {
            str(profile["profile_id"]): int(profile["max_candidates"])
            for profile in profiles
            if isinstance(profile, Mapping) and isinstance(profile.get("profile_id"), str) and isinstance(profile.get("max_candidates"), int)
        }
        counts: dict[str, int] = {}
        kept: list[Any] = []
        excluded: list[dict[str, Any]] = []
        blockers: list[str] = []
        for variant in variants:
            profile = _get(variant, "profile")
            profile_id = str(profile) if profile is not None else ""
            limit = limits.get(profile_id)
            if limit is None:
                blockers.append("UNKNOWN_PROFILE")
                excluded.append(
                    {
                        "profile": profile_id,
                        "stage": "GENERATE_VARIANTS",
                        "selection_reason": "UNKNOWN_PROFILE",
                        "message": "UNKNOWN_PROFILE",
                        "candidate_id": next((_get(variant, key) for key in ("candidate_id", "strategy_id", "id") if _get(variant, key) is not None), ""),
                    }
                )
                continue
            if counts.get(profile_id, 0) < limit:
                kept.append(variant)
                counts[profile_id] = counts.get(profile_id, 0) + 1
                continue
            blockers.append(f"{profile_id}:MAX_CANDIDATES")
            excluded.append(
                {
                    "profile": profile_id,
                    "stage": "GENERATE_VARIANTS",
                    "selection_reason": "MAX_CANDIDATES",
                    "message": f"{profile_id}:MAX_CANDIDATES",
                    "candidate_id": next((_get(variant, key) for key in ("candidate_id", "strategy_id", "id") if _get(variant, key) is not None), ""),
                }
            )
        return tuple(kept), tuple(excluded), tuple(dict.fromkeys(blockers))

    def _write_workbook(self, path: Path, campaign: Mapping[str, Any], finalists: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]], variants: Sequence[Any], excluded: Sequence[Mapping[str, Any]], *, optimizer_excluded: Sequence[Mapping[str, Any]] = (), blockers: Sequence[str] = ()) -> Path:
        def val(item: Any, *keys: str, default: Any = None) -> Any:
            for key in keys:
                found = _get(item, key, None)
                if found is not None:
                    return found
            return default

        finalist_rows = []
        for row in tuple(selected) + tuple(excluded):
            if not any(val(row, key) is not None for key in ("strategy_id", "strategyId", "result_id", "resultId", "symbol", "pair")):
                continue
            finalist_rows.append([campaign["campaign_id"], val(row, "strategy_id", "strategyId"), val(row, "result_id", "resultId"), val(row, "symbol", "pair"), val(row, "side", "direction"), val(row, "user_status", "status"), val(row, "user_rank"), val(row, "effective_maximum"), val(row, "selection_status"), val(row, "selection_reason")])
        portfolio_rows = []
        member_rows = []
        for index, variant in enumerate(variants):
            candidate_id = val(variant, "candidate_id", "strategy_id", "id", default=f"candidate-{index + 1}")
            profile = val(variant, "profile", default="")
            directions = val(variant, "directions")
            member_count = len(directions) if isinstance(directions, Mapping) else val(variant, "member_count")
            pair_count = val(variant, "pair_count")
            if pair_count is None:
                symbols = {
                    str(symbol)
                    for symbol in (
                        [val(details, "symbol", "pair") for details in directions.values()]
                        if isinstance(directions, Mapping)
                        else []
                    )
                    if symbol is not None
                }
                if not symbols:
                    symbol = val(variant, "symbol", "pair", default=val(val(variant, "slot"), "symbol"))
                    if symbol is not None:
                        symbols.add(str(symbol))
                pair_count = len(symbols) if symbols else None
            portfolio_rows.append([campaign["campaign_id"], candidate_id, profile, index + 1, val(variant, "scheduling_key_id", default=val(val(variant, "scheduling_key", default={}), "identity")), val(variant, "scheduling_score", "score"), member_count, pair_count, val(variant, "limiter"), val(variant, "maximum_individual_dd_pct"), val(variant, "minimum_free_margin_reserve_pct"), val(variant, "maximum_account_mm_load_pct"), val(variant, "gate", "gate_result", default="UNKNOWN"), val(variant, "blocking_reasons", "reasons", default=""), "", None, ""])
            if isinstance(directions, Mapping) and directions:
                for ordinal, (direction, details) in enumerate(directions.items(), 1):
                    member_rows.append([campaign["campaign_id"], candidate_id, profile, ordinal, val(details, "strategy_id", "strategyId"), val(details, "result_id", "resultId"), val(details, "symbol", "pair", default=val(variant, "symbol")), direction, val(details, "user_rank"), val(details, "scalar", "scalar_pct"), val(details, "quantity", "rounded_quantity"), val(details, "leverage"), val(details, "notional_usdt"), val(details, "estimated_individual_dd_usdt"), val(details, "estimated_individual_dd_pct"), val(details, "liquidity_scalar_ceiling_pct"), val(details, "calculated_initial_margin_usdt"), val(details, "gate", "gate_result", default="UNKNOWN"), val(details, "reasons", default="")])
        excluded_rows = []
        for is_optimizer, rows in ((False, excluded), (True, optimizer_excluded)):
            excluded_rows.extend(
                [campaign["campaign_id"], "PORTFOLIO" if is_optimizer else "FINALIST", val(row, "strategy_id", "result_id", default=""), val(row, "symbol", "pair"), val(row, "side", "direction"), val(row, "profile"), val(row, "stage", default="GENERATE_VARIANTS" if is_optimizer else "SELECT_CANDIDATES"), "BLOCKED" if is_optimizer else "EXCLUDED", val(row, "selection_reason"), _redact_text(val(row, "message", default=val(row, "selection_reason", default="")))]
                for row in rows
            )
        summary = self._summary(campaign, finalists, selected, variants, excluded, optimizer_excluded=optimizer_excluded, blockers=blockers)
        metadata = {"schema_version": "portfolio_panel_stage1_v1", "campaign_id": campaign["campaign_id"], "created_at_utc": campaign.get("created_at_utc", "2000-01-01T00:00:00Z"), "input_digest": campaign["input_digest"], "config_digest": campaign["config_digest"], "policy_version": campaign["versions"]["policy_version"], "algorithm_versions": _json(campaign["versions"])}
        def frame(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> pd.DataFrame:
            return pd.DataFrame([[ _safe_cell(value, key=header) for value, header in zip(row, headers)] for row in rows], columns=headers)

        tables = {
            "Summary": frame([[key, value] for key, value in summary.items()], SUMMARY_HEADERS),
            "Finalists": frame(finalist_rows, FINALIST_HEADERS),
            "Portfolios": frame(portfolio_rows, PORTFOLIO_HEADERS),
            "Members": frame(member_rows, MEMBER_HEADERS),
            "Excluded": frame(excluded_rows, EXCLUDED_HEADERS),
            "Metadata": frame([[key, value] for key, value in metadata.items()], METADATA_HEADERS),
        }
        write_audit_workbook(tables, path)
        from openpyxl import load_workbook
        workbook = load_workbook(path)
        temporary: Path | None = None
        try:
            workbook["Metadata"].sheet_state = "hidden"
            with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
            workbook.save(temporary)
            workbook.close()
            workbook = None
            normalize_xlsx_workbook(temporary)
            os.replace(temporary, path)
            temporary = None
        finally:
            if workbook is not None:
                workbook.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path

    def cancel(self, job_id: str) -> dict[str, Any]:
        try:
            saved = self.registry.get(job_id)
        except PanelJobError as error:
            code = "PORTFOLIO_JOB_NOT_FOUND" if error.code == "NOT_FOUND" else "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
            raise PortfolioPanelError(code, "job is not available" if code == "PORTFOLIO_JOB_NOT_FOUND" else "portfolio job runtime is unavailable", status=404 if code == "PORTFOLIO_JOB_NOT_FOUND" else 500) from error
        except (OSError, TypeError, ValueError) as error:
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        if saved.get("kind") != "portfolio.stage1":
            raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404)
        if saved["state"] in _TERMINAL:
            raise PortfolioPanelError("PORTFOLIO_JOB_TERMINAL", "job is terminal", status=409)
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        if saved["state"] == "QUEUED":
            try:
                saved = self.registry.cancel(job_id)
            except PanelJobError as error:
                if error.code == "NOT_FOUND":
                    raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404) from error
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
            except (OSError, TypeError, ValueError) as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        elif saved["state"] == "RUNNING":
            try:
                saved = self.registry.cancel(job_id)
            except PanelJobError as error:
                if error.code == "NOT_FOUND":
                    raise PortfolioPanelError("PORTFOLIO_JOB_NOT_FOUND", "job is not available", status=404) from error
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
            except (OSError, TypeError, ValueError) as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        else:
            try:
                saved = self.registry.get(job_id)
            except PanelJobError as error:
                code = "PORTFOLIO_JOB_NOT_FOUND" if error.code == "NOT_FOUND" else "PORTFOLIO_JOB_RUNTIME_UNAVAILABLE"
                raise PortfolioPanelError(code, "job is not available" if code == "PORTFOLIO_JOB_NOT_FOUND" else "portfolio job runtime is unavailable", status=404 if code == "PORTFOLIO_JOB_NOT_FOUND" else 500) from error
            except (OSError, TypeError, ValueError) as error:
                raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500) from error
        return {"job_id": job_id, "status": self._project_state(saved)}

    def active_or_job(self) -> dict[str, Any] | None:
        return self.active_job()

    def results(self, campaign_id: str) -> dict[str, Any]:
        saved, runtime = self._campaign_by_id(campaign_id)
        if self._project_state(saved) != "SUCCEEDED":
            raise PortfolioPanelError("PORTFOLIO_JOB_RESULTS_UNAVAILABLE", "results are not available", status=409)
        campaign = runtime.get("campaign")
        if not isinstance(campaign, Mapping) or not isinstance(campaign.get("input_digest"), str) or not isinstance(campaign.get("config_digest"), str):
            raise PortfolioPanelError("PORTFOLIO_JOB_RUNTIME_UNAVAILABLE", "portfolio job runtime is unavailable", status=500)
        summary = runtime.get("summary") if isinstance(runtime.get("summary"), Mapping) else {}
        try:
            workbook = Path(runtime.get("workbook_path", ""))
            expected = self._results_root / campaign_id / "stage1.xlsx"
            if workbook != expected or workbook.is_symlink() or workbook.parent.is_symlink() or self._results_root.is_symlink() or workbook.resolve(strict=True) != expected:
                raise OSError
        except (OSError, TypeError, ValueError, FileNotFoundError):
            raise PortfolioPanelError("PORTFOLIO_JOB_RESULTS_UNAVAILABLE", "results are not available", status=409) from None
        return {"campaign_id": campaign_id, "input_digest": campaign["input_digest"], "config_digest": campaign["config_digest"], "summary": summary, "blockers": summary.get("blockers", []), "workbook_available": workbook.is_file()}

    def workbook(self, campaign_id: str) -> bytes:
        saved, runtime = self._campaign_by_id(campaign_id)
        if self._project_state(saved) != "SUCCEEDED":
            raise PortfolioPanelError("PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE", "workbook is not available", status=409)
        raw = runtime.get("workbook_path")
        if not isinstance(raw, str):
            raise PortfolioPanelError("PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE", "workbook is not available", status=409)
        path = Path(raw)
        try:
            expected = self._results_root / campaign_id / "stage1.xlsx"
            if path != expected or not path.is_absolute() or path.is_symlink() or path.parent.is_symlink() or self._results_root.is_symlink():
                raise OSError
            resolved = path.resolve(strict=True)
            if resolved != expected or not resolved.is_file():
                raise OSError
            return path.read_bytes()
        except (OSError, ValueError, FileNotFoundError):
            raise PortfolioPanelError("PORTFOLIO_JOB_WORKBOOK_UNAVAILABLE", "workbook is not available", status=409) from None

    def submit_tester_submission(self, campaign_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise PortfolioPanelError("PORTFOLIO_JOB_STAGE2_NOT_AUTHORIZED", "stage 2 is not authorized", status=409)


__all__ = ["PortfolioPanelError", "PortfolioPanelService", "STAGES"]
