"""Read-only boundary for the shared tester inbox used by Performance v2.

The inbox is owned by the tester workflows.  This module validates and
describes it, but never changes it.  Report parsing is deliberately left to
the v2 HTML parser; this boundary only validates bytes, paths and strategy
identity before a caller starts workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
from typing import Mapping
from uuid import uuid4


class PerformanceV2InputError(ValueError):
    """Raised when a committed Performance v2 inbox is not trustworthy."""


@dataclass(frozen=True, slots=True)
class PreparedOrder:
    order_id: int
    open_ma_len: int
    open_multiplier: Decimal
    shift_bp: int
    lot_x: Decimal
    plateau_id: str = ""
    plateau_point_count: int = 0
    base_point_trades: int = 0
    plateau_total_trades: int = 0

    @property
    def open_ma(self) -> int:
        return self.open_ma_len

    @property
    def multiplier(self) -> Decimal:
        return self.open_multiplier

    @property
    def lot(self) -> Decimal:
        return self.lot_x

    @property
    def id(self) -> int:
        return self.order_id

    @property
    def base_trades(self) -> int:
        return self.base_point_trades


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    strategy_name: str
    symbol: str
    side: str
    timeframe: str
    close_ma_len: int
    orders: tuple[PreparedOrder, ...]

    @property
    def order_count(self) -> int:
        return len(self.orders)

    @property
    def close_ma(self) -> int:
        return self.close_ma_len

    @property
    def common_close_ma(self) -> int:
        return self.close_ma_len

    @property
    def open_ma_lens(self) -> tuple[int, ...]:
        return tuple(order.open_ma_len for order in self.orders)

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def time_frame(self) -> str:
        return self.timeframe


@dataclass(frozen=True, slots=True)
class PlateauFact:
    analysis_run_id: str
    plateau_id: str
    plateau_point_count: int
    plateau_total_trades: int


@dataclass(frozen=True, slots=True)
class PreparedV2Entry:
    strategy_name: str
    strategy_path: Path
    strategy_sha256: str
    report_path: Path
    report_sha256: str
    identity: StrategyIdentity
    analysis_run_id: str
    candidate_identity: str
    wizard_run_id: str
    exchange_name: str

    @property
    def strategy_identity(self) -> StrategyIdentity:
        return self.identity

    @property
    def source_strategy_sha256(self) -> str:
        return self.strategy_sha256

    @property
    def strategy_hash(self) -> str:
        return self.strategy_sha256

    @property
    def source_report_sha256(self) -> str:
        return self.report_sha256

    @property
    def report_hash(self) -> str:
        return self.report_sha256

    @property
    def candidate_id(self) -> str:
        return self.candidate_identity

    @property
    def order_plateau_diagnostics(self) -> tuple[PreparedOrder, ...]:
        return self.identity.orders


@dataclass(frozen=True, slots=True)
class PreparedV2Input:
    inbox_path: Path
    manifest_sha256: str
    inbox_snapshot_sha256: str
    analysis_run_id: str
    entries: tuple[PreparedV2Entry, ...]
    plateaus: tuple[PlateauFact, ...]
    commission_contract: Mapping[str, str]
    commission_contract_id: str
    tester_config_sha256: str
    run_mode: str
    max_html_bytes: int

    @property
    def strategies(self) -> tuple[PreparedV2Entry, ...]:
        return self.entries

    @property
    def records(self) -> tuple[PreparedV2Entry, ...]:
        return self.entries

    @property
    def strategy_entries(self) -> tuple[PreparedV2Entry, ...]:
        return self.entries

    @property
    def source_manifest_sha256(self) -> str:
        return self.manifest_sha256

    @property
    def manifest_hash(self) -> str:
        return self.manifest_sha256

    @property
    def analysis_id(self) -> str:
        return self.analysis_run_id

    @property
    def source_inbox_sha256(self) -> str:
        return self.inbox_snapshot_sha256

    @property
    def snapshot_sha256(self) -> str:
        return self.inbox_snapshot_sha256

    @property
    def inbox_sha256(self) -> str:
        return self.inbox_snapshot_sha256

    @property
    def shared_commission_context(self) -> Mapping[str, object]:
        return {
            "commission_contract": dict(self.commission_contract),
            "commission_contract_id": self.commission_contract_id,
            "tester_config_sha256": self.tester_config_sha256,
        }

    @property
    def shared_commission(self) -> Mapping[str, object]:
        return self.shared_commission_context

    @property
    def commission_context(self) -> Mapping[str, object]:
        return self.shared_commission_context

    @property
    def plateau_facts(self) -> tuple[PlateauFact, ...]:
        return self.plateaus


# The short names are useful to callers without making them depend on a
# private implementation spelling.
PreparedStrategy = PreparedV2Entry
V2Order = PreparedOrder
V2Plateau = PlateauFact

_COMMISSION_FIELDS = (
    "MakerFee",
    "TakerFee",
    "SlippagePercent",
    "FundingRate",
    "FundingIntervalHours",
)
_DEFAULT_MAX_HTML_BYTES = 67_108_864
_HASH_SIZE = 64
_STAGING_MARKER = ".v2-staging-owner"
_STAGING_MARKER_VERSION = "performance-v2-staging-owner-v1"
_STAGING_OWNER_SECRET = secrets.token_bytes(32)


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_strategy_hash(strategy: Mapping[str, object]) -> str:
    # Generated v6 strategies omit the two manifest hashes from the strategy
    # provenance.  Removing them also handles a strategy edited by a caller
    # that has retained those self-referential values.
    value = json.loads(_canonical_json(strategy).decode("utf-8"))
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("strategy_json_sha256", None)
        provenance.pop("generation_manifest_sha256", None)
    return sha256(_canonical_json(value)).hexdigest()


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerformanceV2InputError(f"invalid {field}") from error
    if not result.is_finite():
        raise PerformanceV2InputError(f"invalid {field}: non-finite")
    return result


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise PerformanceV2InputError(f"invalid {field}")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise PerformanceV2InputError(f"invalid {field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerformanceV2InputError(f"missing {field}")
    return value.strip()


def _shift_from_multiplier(multiplier: Decimal, side: str) -> int:
    delta = (Decimal(1) - multiplier) if side == "LONG" else (multiplier - Decimal(1))
    raw = delta * Decimal(10_000)
    shift = raw.to_integral_value()
    if raw != shift or shift < 0:
        raise PerformanceV2InputError("open multiplier does not encode an integer shift_bp")
    return int(shift)


def adapt_strategy_identity(
    strategy: Mapping[str, object],
    *,
    strategy_name: str | None = None,
    order_plateau_diagnostics: Mapping[str, object] | None = None,
) -> StrategyIdentity:
    """Extract only the typed strategy fields needed by v2 storage."""
    if not isinstance(strategy, Mapping):
        raise PerformanceV2InputError("strategy JSON must be an object")
    name = _text(strategy.get("name", strategy_name), "strategy name")
    if strategy_name is not None and name != strategy_name:
        raise PerformanceV2InputError("strategy name mismatch")
    basic = strategy.get("basic")
    mrs3 = strategy.get("mrs3")
    if not isinstance(basic, Mapping) or not isinstance(mrs3, Mapping):
        raise PerformanceV2InputError("strategy basic/mrs3 object is missing")
    symbol = _text(basic.get("symbol"), "strategy symbol")
    timeframe = _text(basic.get("time_frame", basic.get("timeframe")), "strategy timeframe")
    use_long, use_short = basic.get("use_long"), basic.get("use_short")
    if type(use_long) is not bool or type(use_short) is not bool or use_long == use_short:
        explicit_side = basic.get("side")
        if explicit_side not in {"LONG", "SHORT"}:
            raise PerformanceV2InputError("strategy side flags are invalid")
        side = str(explicit_side)
    else:
        side = "LONG" if use_long else "SHORT"
    active_key = "ma_long" if side == "LONG" else "ma_short"
    close_key = "ma_close_long" if side == "LONG" else "ma_close_short"
    raw_orders = mrs3.get(active_key)
    close = mrs3.get(close_key)
    if not isinstance(raw_orders, (list, tuple)) or not 1 <= len(raw_orders) <= 4:
        raise PerformanceV2InputError("strategy order count must be between 1 and 4")
    if not isinstance(close, Mapping):
        raise PerformanceV2InputError("strategy Close MA is missing")
    close_ma_len = _positive_int(close.get("len"), "Close MA length")
    orders: list[PreparedOrder] = []
    for expected_id, raw in enumerate(raw_orders, start=1):
        if not isinstance(raw, Mapping):
            raise PerformanceV2InputError("strategy order is not an object")
        order_id = raw.get("id")
        if type(order_id) is not int or order_id != expected_id:
            raise PerformanceV2InputError("order IDs must be exactly 1..N")
        open_ma_len = _positive_int(raw.get("len"), "open MA length")
        multiplier = _decimal(raw.get("multiplier"), "open multiplier")
        shift_bp = _shift_from_multiplier(multiplier, side)
        lot_x = _decimal(raw.get("lot_x"), "lot_x")
        if lot_x <= 0:
            raise PerformanceV2InputError("lot_x must be positive")
        diagnostic: Mapping[str, object] = {}
        if order_plateau_diagnostics is not None:
            diagnostics_orders = order_plateau_diagnostics.get("orders")
            if not isinstance(diagnostics_orders, (list, tuple)) or len(diagnostics_orders) != len(raw_orders):
                raise PerformanceV2InputError("plateau diagnostics are missing or malformed")
            diagnostic_value = diagnostics_orders[expected_id - 1]
            if not isinstance(diagnostic_value, Mapping):
                raise PerformanceV2InputError("plateau diagnostics are missing or malformed")
            diagnostic = diagnostic_value
        if order_plateau_diagnostics is None:
            plateau_id, point_count, base_trades, total_trades = "", 0, 0, 0
        else:
            plateau_id = _text(diagnostic.get("plateau_id"), "plateau_id")
            diagnostic_id = diagnostic.get("order_id")
            if type(diagnostic_id) is not int or diagnostic_id != expected_id:
                raise PerformanceV2InputError("plateau diagnostic order IDs must be exactly 1..N")
            point_count = _nonnegative_int(diagnostic.get("plateau_point_count"), "plateau_point_count")
            base_trades = _nonnegative_int(diagnostic.get("base_point_trades"), "base_point_trades")
            total_trades = _nonnegative_int(diagnostic.get("plateau_total_trades"), "plateau_total_trades")
        orders.append(
            PreparedOrder(
                expected_id,
                open_ma_len,
                multiplier,
                shift_bp,
                lot_x,
                plateau_id,
                point_count,
                base_trades,
                total_trades,
            )
        )
    return StrategyIdentity(name, symbol, side, timeframe, close_ma_len, tuple(orders))


def _root_from_config(value: object) -> Path | None:
    for attribute in ("report_dir", "tester_report_root", "report_root"):
        candidate = getattr(value, attribute, None)
        if candidate is not None:
            return Path(candidate)
    return None


def _contained_path(raw: object, root: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise PerformanceV2InputError(f"{label} is missing")
    path = Path(raw)
    if ".." in path.parts:
        raise PerformanceV2InputError(f"{label} contains parent traversal")
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    lexical = candidate.absolute()
    try:
        lexical.relative_to(root)
    except ValueError as error:
        raise PerformanceV2InputError(f"{label} is outside its trusted root") from error
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise PerformanceV2InputError(f"{label} is outside its trusted root") from error
    if resolved == root:
        raise PerformanceV2InputError(f"{label} cannot be its trusted root")
    # A symlink/reparse component is rejected even if it currently points back
    # inside the root: otherwise a later replacement can escape this check.
    current = root
    try:
        relative = lexical.relative_to(root)
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise PerformanceV2InputError(f"{label} uses a symlink or reparse point")
    except ValueError as error:
        raise PerformanceV2InputError(f"{label} is outside its trusted root") from error
    return resolved


def _sha256(data: bytes) -> str:
    return sha256(data).hexdigest()


def _snapshot_inbox(root: Path, manifest_bytes: bytes | None = None) -> str:
    root = root.resolve()
    if not root.is_dir() or _is_reparse(root):
        raise PerformanceV2InputError("inbox is not a real directory")
    records: list[bytes] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            if _is_reparse(path):
                target = os.readlink(path)
                records.append(b"L\0" + relative + b"\0" + os.fsencode(target))
                continue
            if path.is_dir():
                continue
            data = (
                manifest_bytes
                if path.name == "inbox_manifest.json" and path.parent == root and manifest_bytes is not None
                else path.read_bytes()
            )
        except (OSError, UnicodeError) as error:
            raise PerformanceV2InputError(f"cannot snapshot inbox file: {relative!r}") from error
        records.append(b"F\0" + relative + b"\0" + len(data).to_bytes(8, "big") + data)
    return _sha256(b"".join(records))


def _manifest_diagnostics(
    manifest: Mapping[str, object], entries: list[Mapping[str, object]], names: set[str]
) -> tuple[dict[str, str], dict[str, Mapping[str, object]]]:
    provenance = manifest.get("v6_provenance")
    if not isinstance(provenance, Mapping):
        provenance = manifest
    candidate_names = provenance.get("candidate_identity_to_strategy_names")
    candidate_diagnostics = provenance.get("candidate_diagnostics")
    result: dict[str, str] = {}
    diagnostics: dict[str, Mapping[str, object]] = {}
    if isinstance(candidate_names, Mapping) and isinstance(candidate_diagnostics, Mapping):
        for candidate, raw_names in candidate_names.items():
            candidate_id = _text(candidate, "candidate identity")
            if not isinstance(raw_names, (list, tuple)) or not raw_names:
                raise PerformanceV2InputError("candidate identity mapping is malformed")
            diagnostic = candidate_diagnostics.get(candidate)
            if not isinstance(diagnostic, Mapping):
                raise PerformanceV2InputError("plateau diagnostics are missing")
            order_count = diagnostic.get("order_count")
            raw_orders = diagnostic.get("orders")
            if type(order_count) is not int or not 1 <= order_count <= 4 or not isinstance(raw_orders, (list, tuple)) or len(raw_orders) != order_count:
                raise PerformanceV2InputError("plateau diagnostics are malformed")
            for name in raw_names:
                name = _text(name, "candidate strategy name")
                if name in result:
                    raise PerformanceV2InputError("strategy has conflicting candidate identities")
                result[name] = candidate_id
                diagnostics[name] = diagnostic
    for entry in entries:
        name = _text(entry.get("strategy_name"), "strategy name")
        if name not in result:
            candidate = entry.get("candidate_identity", entry.get("candidate_id"))
            direct = entry.get("order_plateau_diagnostics", entry.get("plateau_diagnostics"))
            if candidate is None or not isinstance(direct, Mapping):
                raise PerformanceV2InputError("plateau diagnostics are missing")
            result[name] = _text(candidate, "candidate identity")
            diagnostics[name] = direct
    if set(result) != names:
        raise PerformanceV2InputError("candidate identity mapping does not cover inbox")
    return result, diagnostics


def _commission(manifest: Mapping[str, object]) -> tuple[dict[str, str], str, str]:
    value = manifest.get("commission_contract")
    if not isinstance(value, Mapping):
        raise PerformanceV2InputError("shared commission contract is missing")
    contract: dict[str, str] = {}
    for field in _COMMISSION_FIELDS:
        if field not in value:
            raise PerformanceV2InputError(f"shared commission contract is missing {field}")
        contract[field] = str(_decimal(value[field], field))
    contract_id = _text(manifest.get("commission_contract_id"), "commission contract id")
    tester_hash = _text(manifest.get("tester_config_sha256"), "tester config hash")
    if len(tester_hash) != _HASH_SIZE:
        raise PerformanceV2InputError("tester config hash is malformed")
    return contract, contract_id, tester_hash


def read_performance_v2_inbox(
    inbox: Path,
    tester_report_root: Path | object | None = None,
    limits: object | None = None,
    *,
    config: object | None = None,
    max_html_bytes: int | None = None,
    report_root: Path | None = None,
) -> PreparedV2Input:
    """Validate an immutable inbox and return a compact typed description."""
    raw_inbox = Path(inbox)
    if _is_reparse(raw_inbox):
        raise PerformanceV2InputError("inbox cannot be a symlink or reparse point")
    inbox = raw_inbox.resolve()
    if report_root is not None:
        if tester_report_root is not None:
            raise PerformanceV2InputError("tester report root was specified twice")
        tester_report_root = report_root
    if limits is not None:
        if config is not None:
            raise PerformanceV2InputError("v2 limits were specified twice")
        config = limits
    if config is None and tester_report_root is not None and not isinstance(tester_report_root, (str, Path)):
        config = tester_report_root
        tester_report_root = _root_from_config(config)
    if tester_report_root is None and config is not None:
        tester_report_root = _root_from_config(config)
    if tester_report_root is None:
        raise PerformanceV2InputError("configured tester report root is required")
    report_root = Path(tester_report_root).resolve()
    limit = max_html_bytes if max_html_bytes is not None else getattr(config, "max_html_bytes", _DEFAULT_MAX_HTML_BYTES)
    if type(limit) is not int or limit <= 0:
        raise PerformanceV2InputError("max_html_bytes must be a positive integer")
    manifest_path = inbox / "inbox_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise PerformanceV2InputError("inbox manifest is unavailable") from error
    before_snapshot = _snapshot_inbox(inbox, manifest_bytes)
    manifest_hash = _sha256(manifest_bytes)
    prepared: PreparedV2Input | None = None
    failure: Exception | None = None
    try:
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PerformanceV2InputError("inbox manifest is not valid UTF-8 JSON") from error
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise PerformanceV2InputError("invalid inbox manifest schema_version")
        expected = manifest.get("expected_strategy_names")
        raw_entries = manifest.get("entries")
        if not isinstance(expected, list) or not expected or any(not isinstance(name, str) or not name for name in expected) or len(set(expected)) != len(expected):
            raise PerformanceV2InputError("inbox expected strategy names are invalid")
        if not isinstance(raw_entries, list) or len(raw_entries) != len(expected):
            raise PerformanceV2InputError("inbox manifest entries are missing or incomplete")
        contract, contract_id, tester_hash = _commission(manifest)
        run_mode = manifest.get("run_mode", "FAST")
        if run_mode not in {"FAST", "RUNS"}:
            raise PerformanceV2InputError("unsupported inbox run mode")
        raw_entry_mappings: list[Mapping[str, object]] = []
        seen_names: set[str] = set()
        seen_paths: set[Path] = set()
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                raise PerformanceV2InputError("inbox manifest entry is invalid")
            name = _text(raw.get("strategy_name"), "strategy name")
            if name in seen_names:
                raise PerformanceV2InputError("duplicate manifest strategy name")
            seen_names.add(name)
            strategy_path = _contained_path(raw.get("strategy_path"), inbox, "strategy path")
            report_path = _contained_path(raw.get("report_path"), report_root, "report path")
            if strategy_path in seen_paths:
                raise PerformanceV2InputError("duplicate manifest strategy path")
            seen_paths.add(strategy_path)
            required_hashes = ("source_strategy_sha256", "source_report_sha256", "strategy_version_id")
            for field in required_hashes:
                value = raw.get(field)
                if not isinstance(value, str) or len(value) != _HASH_SIZE:
                    raise PerformanceV2InputError(f"manifest {field} is malformed")
            if not strategy_path.is_file() or _is_reparse(strategy_path):
                raise PerformanceV2InputError("strategy path is not a regular file")
            if not report_path.is_file() or _is_reparse(report_path):
                raise PerformanceV2InputError("report path is not a regular file")
            strategy_bytes = strategy_path.read_bytes()
            report_bytes = report_path.read_bytes()
            if len(report_bytes) > limit:
                raise PerformanceV2InputError("HTML report exceeds configured size limit")
            if _sha256(strategy_bytes) != raw["source_strategy_sha256"]:
                raise PerformanceV2InputError("source strategy hash mismatch")
            if _sha256(report_bytes) != raw["source_report_sha256"]:
                raise PerformanceV2InputError("source report hash mismatch")
            try:
                strategy = json.loads(strategy_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PerformanceV2InputError("strategy JSON is invalid") from error
            if not isinstance(strategy, Mapping):
                raise PerformanceV2InputError("strategy JSON must be an object")
            if _canonical_strategy_hash(strategy) != raw["strategy_version_id"]:
                raise PerformanceV2InputError("strategy version hash mismatch")
            if strategy.get("name") != name:
                raise PerformanceV2InputError("strategy name mismatch")
            raw_entry_mappings.append(raw)
        expected_set = set(expected)
        if seen_names != expected_set:
            raise PerformanceV2InputError("manifest names do not match expected names")
        candidate_by_name, diagnostics_by_name = _manifest_diagnostics(manifest, raw_entry_mappings, expected_set)
        provenance = manifest.get("v6_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else manifest
        analysis_run_id = _text(provenance.get("analysis_run_id", manifest.get("analysis_run_id")), "analysis run id")
        provenance_hashes = provenance.get("strategy_json_sha256")
        if provenance_hashes is not None and not isinstance(provenance_hashes, Mapping):
            raise PerformanceV2InputError("strategy JSON provenance hashes are malformed")
        prepared_entries: list[PreparedV2Entry] = []
        plateau_by_key: dict[tuple[str, str], PlateauFact] = {}
        for raw in raw_entry_mappings:
            name = str(raw["strategy_name"])
            strategy_path = _contained_path(raw["strategy_path"], inbox, "strategy path")
            report_path = _contained_path(raw["report_path"], report_root, "report path")
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
            identity = adapt_strategy_identity(strategy, strategy_name=name, order_plateau_diagnostics=diagnostics_by_name[name])
            if identity.order_count != int(diagnostics_by_name[name]["order_count"]):
                raise PerformanceV2InputError("plateau diagnostic order count differs from strategy")
            if isinstance(provenance_hashes, Mapping):
                expected_hash = provenance_hashes.get(strategy_path.name)
                if expected_hash is not None and expected_hash != raw["strategy_version_id"]:
                    raise PerformanceV2InputError("strategy JSON provenance hash mismatch")
            for order in identity.orders:
                key = (analysis_run_id, order.plateau_id)
                fact = PlateauFact(analysis_run_id, order.plateau_id, order.plateau_point_count, order.plateau_total_trades)
                previous = plateau_by_key.get(key)
                if previous is not None and previous != fact:
                    raise PerformanceV2InputError("conflicting facts for shared plateau")
                plateau_by_key[key] = fact
            exchange_name = _text(raw.get("exchange_name", strategy.get("exchange", {}).get("name") if isinstance(strategy.get("exchange"), Mapping) else None), "exchange name")
            prepared_entries.append(
                PreparedV2Entry(
                    name,
                    strategy_path,
                    str(raw["source_strategy_sha256"]),
                    report_path,
                    str(raw["source_report_sha256"]),
                    identity,
                    analysis_run_id,
                    candidate_by_name[name],
                    _text(raw.get("wizard_run_id", raw.get("run_id", "unknown")), "wizard run id"),
                    exchange_name,
                )
            )
        prepared = PreparedV2Input(
            inbox,
            manifest_hash,
            before_snapshot,
            analysis_run_id,
            tuple(prepared_entries),
            tuple(plateau_by_key[key] for key in sorted(plateau_by_key)),
            contract,
            contract_id,
            tester_hash,
            str(run_mode),
            limit,
        )
    except Exception as error:
        failure = error
    try:
        after_snapshot = _snapshot_inbox(inbox)
    except Exception as error:
        raise PerformanceV2InputError("inbox identity could not be verified after reading") from error
    if after_snapshot != before_snapshot:
        raise PerformanceV2InputError("inbox changed while it was being read") from failure
    if failure is not None:
        raise failure
    if prepared is None:
        raise PerformanceV2InputError("inbox preparation failed")
    return prepared


def _staging_root(v2_root: Path | object) -> Path:
    raw_root = Path(getattr(v2_root, "database_root", v2_root))
    if _is_reparse(raw_root):
        raise PerformanceV2InputError("v2 root cannot be a symlink or reparse point")
    root = raw_root.resolve()
    if root.name == ".staging":
        raise PerformanceV2InputError("v2 root cannot be the staging directory")
    staging = root / ".staging"
    if staging.exists() and _is_reparse(staging):
        raise PerformanceV2InputError("v2 staging root is a symlink or reparse point")
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _write_staging_marker(staging: Path, staging_root: Path) -> None:
    token = secrets.token_hex(32)
    payload = "\n".join(
        (_STAGING_MARKER_VERSION, str(staging_root.resolve()), str(staging.resolve()), token)
    )
    signature = hmac.new(_STAGING_OWNER_SECRET, payload.encode("utf-8"), "sha256").hexdigest()
    try:
        (staging / _STAGING_MARKER).write_text(f"{payload}\n{signature}\n", encoding="ascii")
    except OSError as error:
        raise PerformanceV2InputError("could not write v2 staging ownership marker") from error


def _verify_staging_marker(staging: Path, *, expected_staging: Path | None = None) -> None:
    marker = staging / _STAGING_MARKER
    if _is_reparse(marker) or not marker.is_file():
        raise PerformanceV2InputError("v2 staging ownership marker is missing")
    try:
        lines = marker.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise PerformanceV2InputError("v2 staging ownership marker is invalid") from error
    if len(lines) != 5 or lines[0] != _STAGING_MARKER_VERSION:
        raise PerformanceV2InputError("v2 staging ownership marker is invalid")
    payload = "\n".join(lines[:4])
    expected_root = str(staging.parent.resolve())
    expected_staging = str((staging if expected_staging is None else expected_staging).resolve())
    if lines[1] != expected_root or lines[2] != expected_staging or not hmac.compare_digest(
        lines[4], hmac.new(_STAGING_OWNER_SECRET, payload.encode("utf-8"), "sha256").hexdigest()
    ):
        raise PerformanceV2InputError("v2 staging ownership marker does not belong to this process/root")


def _copy_verified(source: Path, target: Path, expected_hash: str, label: str, max_bytes: int | None = None) -> None:
    try:
        data = source.read_bytes()
    except OSError as error:
        raise PerformanceV2InputError(f"{label} disappeared during staging") from error
    if max_bytes is not None and len(data) > max_bytes:
        raise PerformanceV2InputError("HTML report exceeds configured size limit during staging")
    if _sha256(data) != expected_hash:
        raise PerformanceV2InputError(f"{label} changed during staging")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def create_v2_parser_staging(v2_root: Path | object, prepared: PreparedV2Input) -> Path:
    """Copy validated bytes into one fresh v2-owned parser directory."""
    if not isinstance(prepared, PreparedV2Input):
        raise PerformanceV2InputError("prepared v2 input is invalid")
    staging_root = _staging_root(v2_root)
    for _ in range(5):
        staging = staging_root / uuid4().hex
        try:
            staging.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise PerformanceV2InputError("could not allocate fresh v2 staging directory")
    try:
        _write_staging_marker(staging, staging_root)
        for entry in prepared.entries:
            if entry.strategy_path.name != f"{entry.strategy_name}.json":
                raise PerformanceV2InputError("strategy filename does not match strategy name")
            _copy_verified(entry.strategy_path, staging / "strategies" / entry.strategy_path.name, entry.strategy_sha256, "strategy")
            _copy_verified(entry.report_path, staging / "reports" / entry.report_path.name, entry.report_sha256, "HTML report", prepared.max_html_bytes)
        return staging
    except BaseException:
        # Fail closed if the path was replaced while staging: cleanup must not
        # turn an ownership failure into recursive deletion of foreign data.
        try:
            remove_v2_parser_staging(staging)
        except Exception:
            pass
        raise


def _move_staging_to_tombstone(staging: Path) -> Path:
    """Atomically detach one verified staging path before recursive removal."""
    for _ in range(5):
        tombstone = staging.parent / f".v2-cleanup-{uuid4().hex}"
        try:
            staging.rename(tombstone)
            return tombstone
        except FileExistsError:
            continue
        except OSError as error:
            raise PerformanceV2InputError("could not atomically move v2 staging directory") from error
    raise PerformanceV2InputError("could not allocate fresh v2 staging tombstone")


def _before_tombstone_delete(tombstone: Path) -> None:
    """Test seam immediately before handle-bound tombstone deletion."""


def _delete_tombstone_with_fd(tombstone: Path) -> None:
    if not getattr(shutil, "_use_fd_functions", False) or not hasattr(os, "O_DIRECTORY"):
        raise PerformanceV2InputError("handle-bound tombstone cleanup is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY
    parent_fd: int | None = None
    tombstone_fd: int | None = None
    try:
        parent_fd = os.open(tombstone.parent, flags)
        tombstone_fd = os.open(tombstone.name, flags, dir_fd=parent_fd)
        _before_tombstone_delete(tombstone)
        # This removes children through the opened directory handle.  The
        # final attempt to remove ``.`` itself is intentionally ignored.
        shutil.rmtree(".", dir_fd=tombstone_fd, ignore_errors=True)
        if os.listdir(tombstone_fd):
            raise PerformanceV2InputError("v2 staging tombstone cleanup is incomplete")
        try:
            current = os.stat(tombstone.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise PerformanceV2InputError("v2 staging tombstone was replaced") from error
        if not os.path.samestat(current, os.fstat(tombstone_fd)):
            raise PerformanceV2InputError("v2 staging tombstone was replaced")
        try:
            os.rmdir(tombstone.name, dir_fd=parent_fd)
        except OSError as error:
            raise PerformanceV2InputError("could not remove v2 staging tombstone") from error
    finally:
        if tombstone_fd is not None:
            os.close(tombstone_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _delete_tombstone_with_windows_handle(tombstone: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    set_file_info = kernel32.SetFileInformationByHandle
    set_file_info.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    set_file_info.restype = wintypes.BOOL

    handle = create_file(
        str(tombstone),
        0x00010000,  # DELETE
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise PerformanceV2InputError("handle-bound tombstone cleanup is unavailable") from OSError(error, "CreateFileW")
    try:
        _before_tombstone_delete(tombstone)
        cleanup_error: OSError | None = None
        try:
            shutil.rmtree(tombstone)
        except OSError as error:
            cleanup_error = error
        if cleanup_error is not None and not isinstance(cleanup_error, PermissionError):
            raise PerformanceV2InputError("v2 staging tombstone cleanup failed") from cleanup_error
        if tombstone.exists() and any(tombstone.iterdir()):
            raise PerformanceV2InputError("v2 staging tombstone cleanup is incomplete")

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = [("delete_file", wintypes.BOOLEAN)]

        disposition = FileDispositionInfo(1)
        if not set_file_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            error = ctypes.get_last_error()
            raise PerformanceV2InputError("could not remove v2 staging tombstone") from OSError(
                error, "SetFileInformationByHandle"
            )
    finally:
        if not close_handle(handle):
            error = ctypes.get_last_error()
            raise PerformanceV2InputError("could not close v2 staging tombstone handle") from OSError(
                error, "CloseHandle"
            )
    if tombstone.exists():
        raise PerformanceV2InputError("v2 staging tombstone was not removed")


def _delete_tombstone(tombstone: Path) -> None:
    if os.name == "nt":
        _delete_tombstone_with_windows_handle(tombstone)
    else:
        _delete_tombstone_with_fd(tombstone)


def remove_v2_parser_staging(staging: Path) -> None:
    """Remove one owned staging directory; never remove the staging root."""
    path = Path(staging)
    if _is_reparse(path):
        raise PerformanceV2InputError("staging path cannot be a symlink or reparse point")
    resolved = path.resolve()
    if resolved.name == ".staging" or resolved.parent.name != ".staging":
        raise PerformanceV2InputError("staging path is outside the v2 staging directory")
    if not resolved.exists():
        return
    _verify_staging_marker(resolved)
    tombstone = _move_staging_to_tombstone(resolved)
    _verify_staging_marker(tombstone, expected_staging=resolved)
    _delete_tombstone(tombstone)


__all__ = [
    "PerformanceV2InputError",
    "PreparedV2Input",
    "PreparedV2Entry",
    "PreparedStrategy",
    "StrategyIdentity",
    "PreparedOrder",
    "V2Order",
    "PlateauFact",
    "V2Plateau",
    "read_performance_v2_inbox",
    "adapt_strategy_identity",
    "create_v2_parser_staging",
    "remove_v2_parser_staging",
]
