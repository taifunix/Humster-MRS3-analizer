"""Performance databases and exchange profiles of `config.local.json` (P7 spec §3)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

EVALUATION_PROTOCOLS = ("v1", "v2")
# The repository templates are the Bybit profile, so Bybit needs no profile (P7 spec §3).
PROFILE_FREE_EXCHANGE = "bybit"
STRATEGY_EXCHANGE_KEYS = ("account", "subaccount_name", "account_type", "bybit_transfer_profit")
TESTER_KEYS = ("MakerFee", "TakerFee", "FundingRate", "FundingIntervalHours", "SlippagePercent")
_NON_NEGATIVE_TESTER_KEYS = frozenset({"MakerFee", "TakerFee", "SlippagePercent"})
_DATABASE_KEYS = frozenset({"database_root", "evaluation_protocol", "exchange"})
_PROFILE_KEYS = frozenset({"strategy_exchange", "tester", "listing_dates"})
_LISTING_KEYS = frozenset({"path", "sheet"})
# Renders and launches of one Panel process may write the same effective template at once.
_TEMPLATE_WRITE_LOCK = threading.Lock()


class _CodedError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class PerformanceDatabaseError(_CodedError):
    """The `performance_databases` section is invalid or names no entry."""


class ExchangeProfileError(_CodedError):
    """The exchange profile of the active database is missing or invalid."""


@dataclass(frozen=True, slots=True)
class PerformanceDatabaseEntry:
    name: str
    database_root: Path
    evaluation_protocol: str
    exchange: str | None


@dataclass(frozen=True, slots=True)
class PerformanceDatabases:
    """Registered databases; `active` None means the database of `config.performance.json`."""

    active: str | None
    items: tuple[PerformanceDatabaseEntry, ...]

    def active_entry(self) -> PerformanceDatabaseEntry | None:
        return next((item for item in self.items if item.name == self.active), None)


@dataclass(frozen=True, slots=True)
class ListingDatesSource:
    path: Path
    sheet: str | None


@dataclass(frozen=True, slots=True)
class ExchangeProfile:
    """Fields one exchange changes in the rendered strategy and tester config."""

    exchange: str
    strategy_exchange: tuple[tuple[str, str], ...] = ()
    tester: tuple[tuple[str, int | float], ...] = ()
    listing_dates: ListingDatesSource | None = None

    def digest(self) -> str:
        """SHA-256 of the fields a render depends on (P7 spec §4: job marker, finalist digest)."""
        document = {
            "strategy_exchange": dict(self.strategy_exchange),
            "tester": {key: format(Decimal(str(value)).normalize(), "f") for key, value in self.tester},
            "listing_dates": None if self.listing_dates is None else [
                self.listing_dates.path.as_posix(), self.listing_dates.sheet,
            ],
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _local_config(path: Path) -> dict[str, object]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid config.local.json") from error
    if not isinstance(raw, dict):
        raise ValueError("config.local.json must be an object")
    return raw


def relative_project_path(value: object, name: str) -> Path:
    """A path relative to the project, without drive, `.` or `..` parts."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a relative path")
    relative = Path(value.strip().replace("\\", "/"))
    if relative.is_absolute() or ":" in relative.parts[0] or "." in relative.parts or ".." in relative.parts:
        raise ValueError(f"{name} must be a relative path")
    return relative


def inside_project(root: Path, relative: Path, name: str) -> Path:
    """Resolve a relative path and require it to stay inside the project (junctions included)."""
    project = Path(root).resolve()
    resolved = (project / relative).resolve()
    try:
        resolved.relative_to(project)
    except ValueError:
        raise ValueError(f"{name} must stay inside the project") from None
    return resolved


def load_performance_databases(local_config: Path) -> PerformanceDatabases:
    section = _local_config(local_config).get("performance_databases")
    if section is None:
        return PerformanceDatabases(None, ())
    if not isinstance(section, dict) or set(section).difference({"active", "items"}):
        raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "performance_databases has unknown keys")
    raw_items = section.get("items", {})
    if not isinstance(raw_items, dict):
        raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "performance_databases.items must be an object")
    project = Path(local_config).parent
    items: list[PerformanceDatabaseEntry] = []
    resolved_roots: list[tuple[str, Path]] = []
    for name, raw in raw_items.items():
        prefix = f"performance_databases.items.{name}"
        if not name.strip() or name != name.strip():
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", f"{prefix}: name must be trimmed and non-empty")
        if not isinstance(raw, dict) or set(raw).difference(_DATABASE_KEYS):
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", f"{prefix} has unknown keys")
        protocol = raw.get("evaluation_protocol", "v2")
        if protocol not in EVALUATION_PROTOCOLS:
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", f"{prefix}.evaluation_protocol must be v1 or v2")
        exchange = raw.get("exchange")
        if exchange is not None and (not isinstance(exchange, str) or not exchange.strip()):
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", f"{prefix}.exchange must be a non-empty string")
        if protocol == "v2" and exchange is None:
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", f"{prefix}.exchange is required for protocol v2")
        try:
            root = relative_project_path(raw.get("database_root"), f"{prefix}.database_root")
            resolved = inside_project(project, root, f"{prefix}.database_root")
        except ValueError as error:
            raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", str(error)) from None
        for other, other_root in resolved_roots:
            if resolved == other_root or other_root in resolved.parents or resolved in other_root.parents:
                raise PerformanceDatabaseError(
                    "PERFORMANCE_DATABASES_INVALID", f"{prefix}.database_root overlaps the root of {other}",
                )
        resolved_roots.append((name, resolved))
        items.append(PerformanceDatabaseEntry(name, root, protocol, None if exchange is None else exchange.strip()))
    active = section.get("active")
    if active is not None and (not isinstance(active, str) or active not in raw_items):
        raise PerformanceDatabaseError("PERFORMANCE_DATABASE_UNKNOWN", "performance_databases.active names no entry")
    return PerformanceDatabases(active, tuple(items))


def _tester_value(key: str, value: object) -> int | float:
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", f"tester.{key} must be a finite number")
    if key in _NON_NEGATIVE_TESTER_KEYS and value < 0:
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", f"tester.{key} must not be negative")
    if key == "FundingIntervalHours" and value <= 0:
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "tester.FundingIntervalHours must be positive")
    return value


def _profile(exchange: str, raw: object) -> ExchangeProfile:
    if not isinstance(raw, dict) or set(raw).difference(_PROFILE_KEYS):
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", f"profile of {exchange} has unknown keys")
    strategy = raw.get("strategy_exchange", {})
    if not isinstance(strategy, dict) or set(strategy).difference(STRATEGY_EXCHANGE_KEYS):
        raise ExchangeProfileError(
            "EXCHANGE_PROFILE_INVALID", "strategy_exchange allows only " + ", ".join(STRATEGY_EXCHANGE_KEYS),
        )
    if any(not isinstance(value, str) for value in strategy.values()):
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "strategy_exchange values must be strings")
    tester = raw.get("tester", {})
    if not isinstance(tester, dict) or set(tester).difference(TESTER_KEYS):
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "tester allows only " + ", ".join(TESTER_KEYS))
    listing = raw.get("listing_dates")
    source = None
    if listing is not None:
        if not isinstance(listing, dict) or set(listing).difference(_LISTING_KEYS):
            raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "listing_dates allows only path and sheet")
        try:
            path = relative_project_path(listing.get("path"), "listing_dates.path")
        except ValueError as error:
            raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", str(error)) from None
        sheet = listing.get("sheet")
        if sheet is not None and (not isinstance(sheet, str) or not sheet.strip()):
            raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "listing_dates.sheet must be a non-empty string")
        source = ListingDatesSource(path, None if sheet is None else sheet.strip())
    return ExchangeProfile(
        exchange,
        tuple((key, strategy[key]) for key in STRATEGY_EXCHANGE_KEYS if key in strategy),
        tuple((key, _tester_value(key, tester[key])) for key in TESTER_KEYS if key in tester),
        source,
    )


def load_exchange_profile(local_config: Path, exchange: str) -> ExchangeProfile:
    """Profile of one exchange; Bybit falls back to the templates, another exchange needs one."""
    profiles = _local_config(local_config).get("exchange_profiles", {})
    if not isinstance(profiles, dict):
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "exchange_profiles must be an object")
    wanted = exchange.strip().casefold()
    matches = [key for key in profiles if isinstance(key, str) and key.strip().casefold() == wanted]
    if len(matches) > 1:
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", f"more than one profile for {exchange}")
    if matches:
        return _profile(exchange.strip(), profiles[matches[0]])
    if wanted == PROFILE_FREE_EXCHANGE:
        return ExchangeProfile(exchange.strip())
    raise ExchangeProfileError(
        "EXCHANGE_PROFILE_MISSING", f"exchange_profiles.{exchange} is required to test on {exchange}",
    )


def materialize_tester_template(
    template: Path, tester: tuple[tuple[str, int | float], ...] | Mapping[str, int | float], directory: Path,
) -> Path:
    """The tester template with profile fields: the template itself, or an immutable content-addressed copy."""
    fields = dict(tester)
    if not fields:
        return Path(template)
    try:
        document = json.loads(Path(template).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "MRS3 tester template is unreadable") from error
    if not isinstance(document, dict):
        raise ExchangeProfileError("EXCHANGE_PROFILE_INVALID", "MRS3 tester template must be an object")
    document.update(fields)
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target = Path(directory) / f"mrs3-tester-{hashlib.sha256(payload).hexdigest()[:16]}.json"
    with _TEMPLATE_WRITE_LOCK:
        if _holds(target, payload):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        except OSError:
            # Another process may hold the target on Windows; equal bytes are success.
            if not _holds(target, payload):
                raise
        finally:
            temporary.unlink(missing_ok=True)
    return target


def _holds(path: Path, payload: bytes) -> bool:
    try:
        return path.is_file() and path.read_bytes() == payload
    except OSError:
        return False


__all__ = [
    "ExchangeProfile",
    "ExchangeProfileError",
    "ListingDatesSource",
    "PerformanceDatabaseEntry",
    "PerformanceDatabaseError",
    "PerformanceDatabases",
    "STRATEGY_EXCHANGE_KEYS",
    "TESTER_KEYS",
    "inside_project",
    "load_exchange_profile",
    "load_performance_databases",
    "materialize_tester_template",
    "relative_project_path",
]
