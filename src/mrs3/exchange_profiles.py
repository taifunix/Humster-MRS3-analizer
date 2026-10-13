"""Performance databases of `config.local.json` and exchange template folders (P7 spec §3)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

EVALUATION_PROTOCOLS = ("v1", "v2")
EXCHANGE_TEMPLATES = Path("templates") / "exchanges"
# Fields every launch check compares with the exchange templates; account_type is a Bybit-only notion.
CHECKED_STRATEGY_FIELDS = ("account",)
CHECKED_TESTER_FIELDS = ("MakerFee", "TakerFee", "FundingRate", "FundingIntervalHours")
_NON_NEGATIVE_TESTER_KEYS = frozenset({"MakerFee", "TakerFee"})
_DATABASE_KEYS = frozenset({"database_root", "evaluation_protocol", "exchange"})
_EXCHANGE_KEYS = frozenset({"exchange", "listing_dates_sheet"})


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
class ExchangeProfile:
    """Templates of one exchange in `templates/exchanges/<exchange>/` (P7 spec §3)."""

    exchange: str
    folder: Path
    mrs3_strategy: Path
    mrs3_tester: Path
    portfolio_strategy: Path | None
    portfolio_tester: Path | None
    listing_dates_sheet: str | None
    strategy_exchange: tuple[tuple[str, str], ...]
    tester: tuple[tuple[str, int | float], ...]
    digest: str


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
    return parse_performance_databases(_local_config(local_config).get("performance_databases"), Path(local_config).parent)


def parse_performance_databases(section: object, project: Path) -> PerformanceDatabases:
    """Validate a `performance_databases` section of a project (P7 spec §3)."""
    if section is None:
        return PerformanceDatabases(None, ())
    if not isinstance(section, dict) or set(section).difference({"active", "items"}):
        raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "performance_databases has unknown keys")
    raw_items = section.get("items", {})
    if not isinstance(raw_items, dict):
        raise PerformanceDatabaseError("PERFORMANCE_DATABASES_INVALID", "performance_databases.items must be an object")
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


def require_apart_from_default(databases: PerformanceDatabases, project: Path, default_root: Path) -> None:
    """An entry may register the default database but not nest with its root (P7 spec §3)."""
    default = Path(default_root).resolve()
    for item in databases.items:
        root = (Path(project) / item.database_root).resolve()
        if root != default and (root in default.parents or default in root.parents):
            raise PerformanceDatabaseError(
                "PERFORMANCE_DATABASES_INVALID",
                f"performance_databases.items.{item.name}.database_root overlaps the default database root",
            )


def save_performance_databases(local_config: Path, section: Mapping[str, object]) -> PerformanceDatabases:
    """Validate and atomically write the `performance_databases` section; other sections are kept."""
    path = Path(local_config)
    document = _local_config(path)
    parsed = parse_performance_databases(dict(section), path.parent)
    document["performance_databases"] = json.loads(json.dumps(section))
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return parsed


def _tester_value(key: str, value: object) -> int | float:
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"mrs3-tester.json {key} must be a finite number")
    if key in _NON_NEGATIVE_TESTER_KEYS and value < 0:
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"mrs3-tester.json {key} must not be negative")
    if key == "FundingIntervalHours" and value <= 0:
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", "mrs3-tester.json FundingIntervalHours must be positive")
    return value


def _template_document(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"{path.parent.name}/{path.name} is unreadable") from error
    if not isinstance(document, dict):
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"{path.parent.name}/{path.name} must be an object")
    return document, payload


def exchange_template_names(root: Path) -> tuple[str, ...]:
    """Exchanges with a valid template folder: the exchanges a database can be created for."""
    base = Path(root) / EXCHANGE_TEMPLATES
    if not base.is_dir():
        return ()
    names = []
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        try:
            names.append(load_exchange_profile(root, folder.name).exchange)
        except ExchangeProfileError:
            continue
    return tuple(sorted(dict.fromkeys(names), key=str.casefold))


def load_exchange_profile(root: Path, exchange: str) -> ExchangeProfile:
    """Templates of the exchange of the active database; another exchange needs its own folder."""
    wanted = exchange.strip().casefold()
    base = Path(root) / EXCHANGE_TEMPLATES
    folders = [folder for folder in base.iterdir() if folder.is_dir() and folder.name.casefold() == wanted] if base.is_dir() else []
    if not folders:
        raise ExchangeProfileError(
            "EXCHANGE_TEMPLATES_MISSING", f"templates/exchanges/{exchange.strip()} is required to test on {exchange.strip()}",
        )
    if len(folders) > 1:
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"more than one template folder for {exchange.strip()}")
    folder = folders[0]
    meta, _ = _template_document(folder / "exchange.json")
    if set(meta) != _EXCHANGE_KEYS or meta["exchange"] != folder.name:
        raise ExchangeProfileError(
            "EXCHANGE_TEMPLATES_INVALID", f"{folder.name}/exchange.json needs exchange={folder.name} and listing_dates_sheet",
        )
    sheet = meta["listing_dates_sheet"]
    if sheet is not None and (not isinstance(sheet, str) or not sheet.strip()):
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"{folder.name}/exchange.json listing_dates_sheet is invalid")
    strategy, _ = _template_document(folder / "mrs3-strategy.json")
    block = strategy.get("exchange")
    if not isinstance(block, dict) or block.get("name") != folder.name:
        raise ExchangeProfileError(
            "EXCHANGE_TEMPLATES_INVALID", f"{folder.name}/mrs3-strategy.json exchange.name must be {folder.name}",
        )
    if any(not isinstance(block.get(key), str) for key in CHECKED_STRATEGY_FIELDS):
        raise ExchangeProfileError("EXCHANGE_TEMPLATES_INVALID", f"{folder.name}/mrs3-strategy.json needs exchange.account")
    tester, _ = _template_document(folder / "mrs3-tester.json")
    fields = tuple((key, _tester_value(key, tester.get(key))) for key in CHECKED_TESTER_FIELDS)
    # Canonical JSON, not file bytes: a line-ending change in a checkout is not a template change.
    canonical = json.dumps(
        {"strategy": strategy, "tester": tester, "listing_dates_sheet": sheet}, sort_keys=True, ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8"))
    portfolio_strategy, portfolio_tester = folder / "portfolio-strategy.json", folder / "portfolio-tester.json"
    return ExchangeProfile(
        exchange=folder.name,
        folder=folder,
        mrs3_strategy=folder / "mrs3-strategy.json",
        mrs3_tester=folder / "mrs3-tester.json",
        portfolio_strategy=portfolio_strategy if portfolio_strategy.is_file() else None,
        portfolio_tester=portfolio_tester if portfolio_tester.is_file() else None,
        listing_dates_sheet=None if sheet is None else sheet.strip(),
        strategy_exchange=tuple((key, block[key]) for key in CHECKED_STRATEGY_FIELDS),
        tester=fields,
        digest=digest.hexdigest(),
    )


__all__ = [
    "CHECKED_STRATEGY_FIELDS",
    "CHECKED_TESTER_FIELDS",
    "EXCHANGE_TEMPLATES",
    "ExchangeProfile",
    "ExchangeProfileError",
    "PerformanceDatabaseEntry",
    "PerformanceDatabaseError",
    "PerformanceDatabases",
    "exchange_template_names",
    "inside_project",
    "load_exchange_profile",
    "load_performance_databases",
    "parse_performance_databases",
    "relative_project_path",
    "require_apart_from_default",
    "save_performance_databases",
]
