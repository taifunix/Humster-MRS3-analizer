"""Small, append-only SQLite store for fixture live-account evidence.

This module deliberately has no exchange client.  It stores facts supplied by a
fixture (or by a future read-only adapter) and keeps the tested Portfolio DB out
of the live history.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from uuid import uuid4


class LiveStoreError(RuntimeError):
    """A fail-closed live-store error."""


class WriteOutcome(str, Enum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    INCONSISTENT = "INCONSISTENT"


StoreOutcome = WriteOutcome


_SECRET_KEY = re.compile(
    r"^(?:api[_-]?(?:key|token)|secret|token|(?:client|session|refresh|secret|token)[_-]?"
    r"(?:secret|token|key|value|path)|password|passwd|cookie|credential|"
    r"private[_-]?(?:key|key_pem)|access[_-]?(?:key|token)|auth[_-]?(?:header|token|key)|"
    r"passphrase|signature|hmac|secret[_-]?id)$",
    re.IGNORECASE,
)
_LOCAL_PATH_KEY = re.compile(
    r"(?:^|_)(?:path|paths|file|files|dir|directory|root)$|"
    r"(?:^|_)(?:local|fixture|secret|credential|credentials|database|db)_(?:path|file|dir|root)$",
    re.IGNORECASE,
)
_LOCAL_PATH_VALUE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\|file://)")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_READ_PERMISSIONS = frozenset({"account.read", "wallet.read", "positions.read", "orders.read", "executions.read"})
_APPEND_ONLY = (
    "deployment_manifests",
    "account_snapshots",
    "position_snapshots",
    "order_snapshots",
    "stream_events",
    "execution_events",
    "cashflow_events",
    "reconciliations",
    "stream_checkpoints",
    "watchdog_findings",
)


def _reject_secrets(value: Any, path: str = "manifest") -> None:
    if isinstance(value, str):
        if _LOCAL_PATH_VALUE.match(value.strip()):
            raise ValueError(f"local path value: {path}")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized_key = re.sub(r"[_-]", "", key.strip()).casefold()
            if _SECRET_KEY.fullmatch(key.strip()) or normalized_key in {"apikey", "apitoken", "apisecret", "secret", "token", "password", "passwd", "cookie", "credential", "credentials", "privatekey", "privatekeypem", "accesskey", "accesstoken", "auth", "authorization", "authheader", "authtoken", "authkey", "clientsecret", "clienttoken", "clientkey", "clientvalue", "sessionsecret", "sessiontoken", "sessionkey", "sessionvalue", "refreshsecret", "refreshtoken", "refreshkey", "passphrase", "apipassphrase", "signature", "hmac", "secretid"}:
                raise ValueError(f"secret-like manifest field: {path}.{key}")
            if _LOCAL_PATH_KEY.search(key.strip()):
                raise ValueError(f"local path field: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def canonical_decimal(value: Decimal) -> str:
    """Serialize a finite Decimal without exponent or insignificant zeroes."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return "0"
    try:
        normalized = value.normalize()
    except InvalidOperation as error:
        raise ValueError("decimal must be finite") from error
    result = format(normalized, "f")
    if result.startswith("-0") and Decimal(result) == 0:
        return "0"
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mapping keys must be strings")
        return {key: _jsonable(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        raise TypeError("bare floats are not allowed in canonical live facts")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported live fact value: {type(value).__name__}")


def canonical_json(payload: Mapping[str, Any] | Any) -> str:
    """Return deterministic JSON used for all duplicate/conflict decisions."""

    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_bytes(payload: Mapping[str, Any] | Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def canonical_digest(payload: Mapping[str, Any] | Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def canonical_source_order(payload: Mapping[str, Any]) -> tuple[str, str, str, str, int, str]:
    """Return the contract's deterministic source ordering tuple."""

    if not isinstance(payload, Mapping):
        raise TypeError("source fact must be a mapping")
    effective = payload.get("effective_at_utc", payload.get("effective_at"))
    observed = payload.get("observed_at_utc", payload.get("observed_at"))
    source_kind = payload.get("source_kind")
    source_id = payload.get("source_id")
    sequence = payload.get("source_sequence", payload.get("sequence"))
    if effective is None or observed is None or source_kind is None or source_id is None or sequence is None:
        raise ValueError("source ordering fields are incomplete")
    if not all(isinstance(item, str) for item in (effective, observed, source_kind, source_id)):
        raise ValueError("source ordering fields must be strings")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise ValueError("source sequence must be an integer")
    return (effective, observed, source_kind, source_id, sequence, canonical_digest(payload))


source_order = canonical_source_order


_EXECUTION_ALIASES = {
    "pnl": "realized_pnl",
    "realised_pnl": "realized_pnl",
    "exec_qty": "qty",
    "quantity": "qty",
    "execution_price": "price",
    "trade_price": "price",
    "commission": "fee",
}
_EXECUTION_ENVELOPE = {
    "deployment_id", "account_id", "execution_id", "id", "event_id", "channel", "sequence",
    "seq", "kind", "event_type", "source_kind", "observed_at", "timestamp_received",
}


def _execution_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip REST/WS envelope differences before execution deduplication."""

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _EXECUTION_ENVELOPE:
            continue
        result[_EXECUTION_ALIASES.get(key, key)] = value
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


class DeploymentManifest:
    """Secret-free immutable deployment identity and live settings.

    A mapping is accepted so fixtures can mirror the JSON contract directly.
    Keyword construction is supported for small callers and tests.
    """

    def __init__(self, data: Mapping[str, Any] | None = None, **fields: Any) -> None:
        if data is not None and not isinstance(data, Mapping):
            raise TypeError("manifest must be a mapping")
        payload = dict(data or {})
        payload.update(fields)
        if "manifest_version" in payload and "version" not in payload:
            payload["version"] = payload["manifest_version"]
        if "id" in payload and "manifest_id" not in payload:
            payload["manifest_id"] = payload["id"]
        account = payload.get("account")
        if isinstance(account, Mapping):
            payload.setdefault("account_alias", account.get("alias"))
            payload.setdefault("account_id", account.get("id"))
        for canonical, aliases in (("limiter_settings", ("limiter_settings", "limiter")), ("watchdog_settings", ("watchdog_settings", "watchdog"))):
            if canonical not in payload:
                for alias in aliases:
                    if alias in payload:
                        payload[canonical] = payload[alias]
                        break
        if "member_composition" not in payload:
            for alias in ("members", "strategies"):
                if alias in payload:
                    payload["member_composition"] = payload[alias]
                    break
        if "source" not in payload and "source_kind" in payload:
            payload["source"] = payload["source_kind"]
        _reject_secrets(payload)
        for key in ("deployment_id", "manifest_id"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"manifest requires {key}")
        if "version" not in payload:
            raise ValueError("manifest requires version")
        if isinstance(payload["version"], bool) or not isinstance(payload["version"], (int, str)):
            raise ValueError("manifest version must be an integer or string")
        if isinstance(payload["version"], str) and not payload["version"].strip():
            raise ValueError("manifest version must be non-empty")
        if not all(isinstance(payload.get(key), str) and payload[key].strip() for key in ("account_alias", "account_id")):
            raise ValueError("manifest requires public account alias and id")
        if "manifest_schema_version" not in payload:
            raise ValueError("manifest requires manifest_schema_version")
        if type(payload.get("manifest_schema_version")) is not int or payload.get("manifest_schema_version") != 1:
            raise ValueError("unsupported manifest schema version")
        required = ("portfolio_set", "evaluation", "run_id", "attempt_id", "series_version", "metrics_version", "member_composition", "source")
        for key in required:
            if key not in payload or payload[key] in (None, "", (), []):
                raise ValueError(f"manifest requires {key}")
        if payload.get("source") != "fixture":
            raise ValueError("live monitor currently accepts source=fixture only")
        if not isinstance(payload.get("semantic_digest"), str) or not _HEX64.fullmatch(payload["semantic_digest"]):
            raise ValueError("semantic_digest must be a 64-hex digest")
        for key in ("series_version", "metrics_version"):
            if not isinstance(payload[key], str) or not payload[key].strip():
                raise ValueError(f"manifest {key} must be non-empty")
        composition = payload["member_composition"]
        if not isinstance(composition, (list, tuple)) or not composition:
            raise ValueError("manifest member_composition must be non-empty")
        strategy_ids: set[str] = set()
        for member in composition:
            if not isinstance(member, Mapping):
                raise ValueError("manifest members must be mappings")
            strategy_id = member.get("strategy_id")
            symbol = member.get("symbol")
            side = str(member.get("side", "")).upper()
            timeframe = member.get("timeframe")
            priority = member.get("priority")
            counted = member.get("counted")
            if isinstance(strategy_id, str):
                strategy_id = strategy_id.strip()
            if not isinstance(strategy_id, str) or not strategy_id or strategy_id in strategy_ids:
                raise ValueError("manifest strategy_id must be unique and non-empty")
            if not isinstance(symbol, str) or not symbol.strip() or side not in {"LONG", "SHORT"}:
                raise ValueError("manifest member symbol/side is invalid")
            if not isinstance(timeframe, str) or not timeframe.strip():
                raise ValueError("manifest member timeframe is required")
            if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
                raise ValueError("manifest member priority is invalid")
            if not isinstance(counted, bool):
                raise ValueError("manifest member counted flag is required")
            strategy_ids.add(strategy_id)
        for key in ("limiter_settings", "watchdog_settings"):
            settings = payload.get(key)
            if not isinstance(settings, Mapping):
                raise ValueError(f"manifest requires {key}")
            if not isinstance(settings.get("settings_version"), str) or not settings["settings_version"].strip():
                raise ValueError(f"{key}.settings_version is required")
            limit = settings.get("limit", settings.get("L"))
            grace = settings.get("grace_seconds", settings.get("grace"))
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError(f"{key}.limit must be a non-negative integer")
            if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
                raise ValueError(f"{key}.grace_seconds must be a non-negative integer")
        permissions = payload.get("read_only_permissions")
        if not isinstance(permissions, (list, tuple)) or not permissions or any(item not in _ALLOWED_READ_PERMISSIONS for item in permissions):
            raise ValueError("manifest permissions must be read-only")
        try:
            # Validate once at the boundary; storage never receives a secret.
            canonical_json(payload)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid deployment manifest: {error}") from error
        self._payload = _freeze(payload)
        self._canonical_json = canonical_json(self._payload)
        self._digest = hashlib.sha256(self._canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DeploymentManifest":
        return cls(data)

    from_dict = from_mapping

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)

    @property
    def payload(self) -> Mapping[str, Any]:
        return self._payload

    @property
    def canonical_json(self) -> str:
        return self._canonical_json

    @property
    def digest(self) -> str:
        return self._digest

    canonical_digest = digest

    def __getattr__(self, name: str) -> Any:
        payload = self.__dict__.get("_payload")
        if not isinstance(payload, Mapping):
            raise AttributeError(name)
        try:
            return payload[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __repr__(self) -> str:
        return f"DeploymentManifest({self._canonical_json})"


@dataclass(frozen=True)
class StoredFact:
    outcome: WriteOutcome
    digest: str


class LiveStore:
    """Dedicated WAL SQLite history with append-only evidence tables."""

    schema_version = 1
    database_kind = "mrs3_portfolio_live"

    def __init__(
        self,
        path: str | Path,
        *,
        database_path: str | Path | None = None,
        performance_db: str | Path | None = None,
        portfolio_db: str | Path | None = None,
        forbidden_paths: tuple[str | Path, ...] = (),
    ) -> None:
        if self._is_forbidden_alias(path) or (database_path is not None and self._is_forbidden_alias(database_path)):
            raise LiveStoreError("Performance/Portfolio DB target is forbidden")
        if database_path is not None and str(database_path).strip().casefold() in {"performance db", "portfolio db", "performance", "portfolio"}:
            raise LiveStoreError("Performance/Portfolio DB target is forbidden")
        requested = Path(database_path) if database_path is not None else Path(path)
        if requested.exists() and requested.is_dir():
            requested = requested / "live.sqlite3"
        elif database_path is None and not requested.suffix:
            requested = requested / "live.sqlite3"
        self.database_path = requested.expanduser().resolve()
        self.path = self.database_path
        self.root = self.database_path.parent
        self._reject_database_target(
            self.database_path,
            tuple(item for item in (performance_db, portfolio_db, *forbidden_paths) if item is not None),
        )
        if self.database_path.exists() and self.database_path.is_dir():
            raise LiveStoreError("live database path is a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        if self.database_path.is_file() and self.database_path.stat().st_size:
            self._probe_existing_database()
        try:
            self._connection = sqlite3.connect(str(self.database_path), isolation_level=None, check_same_thread=False, timeout=5.0)
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA recursive_triggers=ON")
            mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise LiveStoreError("SQLite WAL is unavailable")
            self._initialize()
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    def _probe_existing_database(self) -> None:
        """Validate an existing file without opening it write-capable."""

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
            rows = connection.execute(
                "SELECT schema_version, database_kind, database_instance_id "
                "FROM schema_info"
            ).fetchall()
            if len(rows) != 1:
                raise LiveStoreError("existing SQLite schema marker is invalid")
            schema_version, database_kind, instance_id = rows[0]
            if (
                schema_version != self.schema_version
                or database_kind != self.database_kind
                or not isinstance(instance_id, str)
                or not instance_id.strip()
            ):
                raise LiveStoreError("existing SQLite schema marker is invalid")
        except LiveStoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            raise LiveStoreError("existing SQLite schema marker is invalid") from error
        finally:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.close()

    @staticmethod
    def _is_forbidden_alias(value: str | Path) -> bool:
        text = str(value).strip().replace("\\", "/")
        if not text:
            return False
        leaf = text.rstrip("/").rsplit("/", 1)[-1].casefold()
        stem = leaf.rsplit(".", 1)[0]
        compact = re.sub(r"[^a-z0-9]+", "", stem)
        if compact in {"performance", "performancedb", "portfolio", "portfoliodb"}:
            return True
        return bool(re.search(r"(?:^|[_.-])(performance|portfolio)(?:$|[_.-])", stem))

    @staticmethod
    def _reject_database_target(path: Path, forbidden: tuple[str | Path, ...]) -> None:
        resolved = path.resolve()
        if LiveStore._is_forbidden_alias(resolved):
            raise LiveStoreError("Performance/Portfolio DB target is forbidden")
        for item in forbidden:
            text = str(item).strip().casefold()
            if LiveStore._is_forbidden_alias(item) or text in {"performance db", "portfolio db", "performance", "portfolio"}:
                raise LiveStoreError("Performance/Portfolio DB target is forbidden")
            try:
                if Path(item).expanduser().resolve() == resolved:
                    raise LiveStoreError("Performance/Portfolio DB target is forbidden")
            except OSError:
                continue

    @property
    def connection(self) -> sqlite3.Connection:
        if self._closed:
            raise LiveStoreError("live store is closed")
        return self._connection

    def _initialize(self) -> None:
        connection = self.connection
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version INTEGER NOT NULL,
                database_kind TEXT NOT NULL,
                database_instance_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deployment_manifests (
                deployment_id TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                version TEXT NOT NULL,
                manifest_schema_version INTEGER NOT NULL DEFAULT 1,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS account_snapshots (
                deployment_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'REST',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, snapshot_id),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS position_snapshots (
                deployment_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'REST',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, snapshot_id, symbol, side),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS order_snapshots (
                deployment_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'REST',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, snapshot_id, order_id, revision),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS stream_events (
                deployment_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sequence INTEGER,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'WS',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, channel, event_id),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS execution_events (
                deployment_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'WS',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, account_id, execution_id),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS cashflow_events (
                deployment_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                cashflow_id TEXT NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'REST',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, account_id, cashflow_id),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS reconciliations (
                deployment_id TEXT NOT NULL,
                reconcile_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNKNOWN',
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'REST',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, reconcile_id),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS stream_checkpoints (
                deployment_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'WS',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, channel, sequence),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            CREATE TABLE IF NOT EXISTS watchdog_findings (
                deployment_id TEXT NOT NULL,
                finding_id TEXT NOT NULL,
                settings_version TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'UNKNOWN',
                interval_start TEXT,
                interval_end TEXT,
                manifest_id TEXT NOT NULL,
                manifest_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL DEFAULT 'SYSTEM',
                observed_at TEXT NOT NULL DEFAULT '',
                effective_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_sequence INTEGER NOT NULL DEFAULT 0,
                parent_identity TEXT,
                parent_payload_digest TEXT,
                correction_of TEXT,
                canonical_digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (deployment_id, finding_id, settings_version),
                FOREIGN KEY (deployment_id, manifest_id, manifest_version) REFERENCES deployment_manifests(deployment_id, manifest_id, version)
            );
            """
        )
        markers = connection.execute("SELECT schema_version, database_kind, database_instance_id FROM schema_info").fetchall()
        if len(markers) > 1:
            raise LiveStoreError("live SQLite schema_info must contain exactly one row")
        marker = markers[0] if markers else None
        if marker is None:
            connection.execute("INSERT INTO schema_info (singleton, schema_version, database_kind, database_instance_id) VALUES (1, ?, ?, ?)", (self.schema_version, self.database_kind, uuid4().hex))
        elif marker[:2] != (self.schema_version, self.database_kind):
            raise LiveStoreError("live SQLite schema marker mismatch")
        for table in _APPEND_ONLY:
            trigger = f"{table}_append_only"
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger}_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'append-only history'); END"
            )
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger}_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'append-only history'); END"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _append(
        self,
        table: str,
        identity: tuple[Any, ...],
        columns: tuple[str, ...],
        payload: Mapping[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WriteOutcome:
        if table not in _APPEND_ONLY:
            raise LiveStoreError("unknown live table")
        _reject_secrets(payload, "payload")
        payload = dict(payload)
        encoded_payload = _execution_payload(payload) if table == "execution_events" else payload
        encoded = canonical_json(encoded_payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        def write(conn: sqlite3.Connection) -> WriteOutcome:
            where = " AND ".join(f"{name} = ?" for name in columns)
            existing = conn.execute(f"SELECT canonical_digest FROM {table} WHERE {where}", identity).fetchone()
            if existing is not None:
                return WriteOutcome.DUPLICATE if existing[0] == digest else WriteOutcome.CONFLICT
            if table != "deployment_manifests":
                deployment_id = identity[0]
                requested_manifest_id = payload.get("manifest_id")
                requested_manifest_version = payload.get("manifest_version", payload.get("manifest_version_id"))
                manifest = conn.execute(
                    "SELECT manifest_id, version FROM deployment_manifests "
                    "WHERE deployment_id = ? ORDER BY rowid DESC LIMIT 1",
                    (deployment_id,),
                ).fetchone()
                if manifest is None:
                    raise LiveStoreError("fact write requires a stored deployment manifest")
                manifest_id = str(requested_manifest_id or manifest[0])
                manifest_version = str(requested_manifest_version if requested_manifest_version is not None else manifest[1])
                if (manifest_id, manifest_version) != (str(manifest[0]), str(manifest[1])):
                    raise LiveStoreError("fact manifest identity is not the active stored manifest")
            else:
                manifest_id = manifest_version = None

            parent_identity = payload.get("parent_identity")
            if parent_identity is None:
                parent_fields = {key[7:]: value for key, value in payload.items() if key.startswith("parent_") and key not in {"parent_identity", "parent_payload_digest"} and value is not None}
                if parent_fields:
                    parent_identity = parent_fields
            if isinstance(parent_identity, Mapping):
                parent_identity = canonical_json(parent_identity)
            elif parent_identity is not None:
                parent_identity = str(parent_identity)
            correction_of = payload.get("correction_of", payload.get("replacement_of", payload.get("supersedes")))
            if correction_of is not None:
                correction_of = str(correction_of)
                if parent_identity is None:
                    parent_identity = correction_of
            parent_digest = payload.get("parent_payload_digest", payload.get("parent_digest"))
            if parent_digest is not None:
                parent_digest = str(parent_digest)
                if not _HEX64.fullmatch(parent_digest):
                    raise ValueError("parent payload digest must be a 64-hex digest")
            schema_version = payload.get("schema_version", payload.get("fact_schema_version", 1))
            if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
                raise ValueError("fact schema_version must be a positive integer")
            source_kind = payload.get("source_kind") or ("SYSTEM" if table == "watchdog_findings" else ("REST" if table in {"account_snapshots", "position_snapshots", "order_snapshots", "cashflow_events", "reconciliations"} else "WS"))
            observed_at = payload.get("observed_at") or payload.get("observed_at_utc") or payload.get("interval_end") or ""
            effective_at = payload.get("effective_at") or payload.get("effective_at_utc") or observed_at
            source_id = str(payload.get("source_id", payload.get("event_id", identity[-1])))
            source_sequence = payload.get("source_sequence", payload.get("sequence", 0))
            if source_sequence is None:
                source_sequence = 0
            if not all(isinstance(item, str) for item in (source_kind, observed_at, effective_at, source_id)):
                raise ValueError("fact source fields must be strings")
            if isinstance(source_sequence, bool) or not isinstance(source_sequence, int):
                raise ValueError("fact source_sequence must be an integer")
            extra_columns: tuple[str, ...] = ()
            extra_values: tuple[Any, ...] = ()
            if table == "deployment_manifests":
                extra_columns, extra_values = ("manifest_schema_version",), (payload.get("manifest_schema_version", 1),)
            elif table == "stream_events":
                extra_columns, extra_values = ("sequence",), (payload.get("sequence", payload.get("seq")),)
            elif table == "reconciliations":
                extra_columns, extra_values = ("status",), (payload.get("status", "UNKNOWN"),)
            elif table == "watchdog_findings":
                extra_columns, extra_values = ("state", "interval_start", "interval_end"), (payload.get("state", "UNKNOWN"), payload.get("interval_start"), payload.get("interval_end"))
            if table == "deployment_manifests":
                insert_columns = (*columns, *extra_columns, "canonical_digest", "payload")
                values = tuple(identity) + extra_values + (digest, encoded)
            else:
                common_columns = ("manifest_id", "manifest_version", "schema_version", "source_kind", "observed_at", "effective_at", "source_id", "source_sequence", "parent_identity", "parent_payload_digest", "correction_of")
                common_values = (manifest_id, manifest_version, schema_version, source_kind, observed_at, effective_at, source_id, source_sequence, parent_identity, parent_digest, correction_of)
                insert_columns = (*columns, *common_columns, *extra_columns, "canonical_digest", "payload")
                values = tuple(identity) + common_values + extra_values + (digest, encoded)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in values)})",
                values,
            )
            return WriteOutcome.INSERTED
        if connection is not None:
            return write(connection)
        with self.transaction() as conn:
            return write(conn)

    @staticmethod
    def _record(value: Mapping[str, Any] | Any, **fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(value, Mapping):
            payload = dict(value)
        else:
            payload = {}
        payload.update({key: item for key, item in fields.items() if item is not None})
        return payload, payload

    def append_manifest(self, manifest: DeploymentManifest | Mapping[str, Any]) -> WriteOutcome:
        item = manifest if isinstance(manifest, DeploymentManifest) else DeploymentManifest(manifest)
        payload = item.to_dict()
        return self._append(
            "deployment_manifests",
            (item.deployment_id, item.manifest_id, str(item.version)),
            ("deployment_id", "manifest_id", "version"),
            payload,
        )

    save_manifest = append_manifest
    record_manifest = append_manifest

    def append_account_snapshot(
        self,
        deployment_id: str | Mapping[str, Any],
        snapshot_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        *,
        observed_at: str | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, snapshot_id = record.get("deployment_id"), record.get("snapshot_id")
            payload = record
        else:
            payload = dict(payload or {})
            payload.update(fields)
            payload.update({"deployment_id": deployment_id, "snapshot_id": snapshot_id})
        if not all(isinstance(item, str) and item for item in (deployment_id, snapshot_id)):
            raise ValueError("deployment_id and snapshot_id are required")
        observed = observed_at or str(payload.get("observed_at") or "")
        if not observed:
            raise ValueError("snapshot observed_at is required")
        payload["observed_at"] = observed
        return self._append("account_snapshots", (deployment_id, snapshot_id), ("deployment_id", "snapshot_id"), {**payload, "observed_at": observed})

    save_account_snapshot = append_account_snapshot

    def append_position_snapshot(
        self,
        deployment_id: str | Mapping[str, Any],
        snapshot_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        payload: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, snapshot_id = record.get("deployment_id"), record.get("snapshot_id")
            symbol, side, payload = record.get("symbol"), record.get("side"), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "snapshot_id": snapshot_id, "symbol": symbol, "side": side}
        if not all(isinstance(item, str) and item for item in (deployment_id, snapshot_id, symbol, side)):
            raise ValueError("position identity is incomplete")
        return self._append("position_snapshots", (deployment_id, snapshot_id, symbol, side), ("deployment_id", "snapshot_id", "symbol", "side"), payload or {})

    save_position_snapshot = append_position_snapshot

    def append_order_snapshot(
        self,
        deployment_id: str | Mapping[str, Any],
        snapshot_id: str | None = None,
        order_id: str | None = None,
        revision: str | int = 0,
        payload: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, snapshot_id = record.get("deployment_id"), record.get("snapshot_id")
            order_id, revision, payload = record.get("order_id"), record.get("revision", 0), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "snapshot_id": snapshot_id, "order_id": order_id, "revision": revision}
        if not all(isinstance(item, str) and item for item in (deployment_id, snapshot_id, order_id)):
            raise ValueError("order identity is incomplete")
        return self._append("order_snapshots", (deployment_id, snapshot_id, order_id, str(revision)), ("deployment_id", "snapshot_id", "order_id", "revision"), payload or {})

    save_order_snapshot = append_order_snapshot

    def append_stream_event(
        self,
        deployment_id: str | Mapping[str, Any],
        channel: str | None = None,
        event_id: str | int | None = None,
        payload: Mapping[str, Any] | None = None,
        *,
        sequence: int | None = None,
        source_kind: str = "WS",
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, channel = record.get("deployment_id"), record.get("channel")
            event_id, sequence, payload = record.get("event_id", record.get("id", record.get("sequence"))), record.get("sequence"), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "channel": channel, "event_id": event_id, "sequence": sequence, "source_kind": source_kind}
        if event_id is None:
            event_id = sequence
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int)):
            raise ValueError("stream sequence must be an integer")
        if not all(isinstance(item, str) and item for item in (deployment_id, channel)) or event_id is None:
            raise ValueError("stream event identity is incomplete")
        return self._append("stream_events", (deployment_id, channel, str(event_id)), ("deployment_id", "channel", "event_id"), payload or {})

    record_stream_event = append_stream_event

    def append_execution(
        self,
        deployment_id: str | Mapping[str, Any],
        account_id: str | None = None,
        execution_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        *,
        source_kind: str = "WS",
        observed_at: str | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, account_id = record.get("deployment_id"), record.get("account_id")
            execution_id, payload = record.get("execution_id", record.get("id")), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "account_id": account_id, "execution_id": execution_id, "source_kind": source_kind}
        if observed_at is not None:
            payload = {**(payload or {}), "observed_at": observed_at}
        if not all(isinstance(item, str) and item for item in (deployment_id, account_id, execution_id)):
            raise ValueError("execution identity is incomplete")
        outcome = self._append("execution_events", (deployment_id, account_id, execution_id), ("deployment_id", "account_id", "execution_id"), payload or {})
        return WriteOutcome.INCONSISTENT if outcome is WriteOutcome.CONFLICT else outcome

    record_execution = append_execution
    append_execution_event = append_execution

    def append_cashflow(
        self,
        deployment_id: str | Mapping[str, Any],
        account_id: str | None = None,
        cashflow_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        *,
        source_kind: str = "REST",
        observed_at: str | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, account_id = record.get("deployment_id"), record.get("account_id")
            cashflow_id, payload = record.get("cashflow_id", record.get("id")), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "account_id": account_id, "cashflow_id": cashflow_id, "source_kind": source_kind}
        if observed_at is not None:
            payload = {**(payload or {}), "observed_at": observed_at}
        if not all(isinstance(item, str) and item for item in (deployment_id, account_id, cashflow_id)):
            raise ValueError("cashflow identity is incomplete")
        return self._append("cashflow_events", (deployment_id, account_id, cashflow_id), ("deployment_id", "account_id", "cashflow_id"), payload or {})

    record_cashflow = append_cashflow
    append_cashflow_event = append_cashflow

    def append_reconciliation(
        self,
        deployment_id: str | Mapping[str, Any],
        reconcile_id: str | None = None,
        status: str | None = None,
        payload: Mapping[str, Any] | None = None,
        *,
        observed_at: str | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, reconcile_id = record.get("deployment_id"), record.get("reconcile_id", record.get("id"))
            status, payload = record.get("status"), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "reconcile_id": reconcile_id, "status": status}
        if not all(isinstance(item, str) and item for item in (deployment_id, reconcile_id, status)):
            raise ValueError("reconciliation identity is incomplete")
        if observed_at is not None:
            payload = {**(payload or {}), "observed_at": observed_at}
        return self._append("reconciliations", (deployment_id, reconcile_id), ("deployment_id", "reconcile_id"), payload or {})

    record_reconciliation = append_reconciliation

    def append_checkpoint(
        self,
        deployment_id: str | Mapping[str, Any],
        channel: str | None = None,
        sequence: int | None = None,
        payload: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, channel, sequence, payload = record.get("deployment_id"), record.get("channel"), record.get("sequence"), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "channel": channel, "sequence": sequence}
        if not all(isinstance(item, str) and item for item in (deployment_id, channel)) or not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("checkpoint identity is incomplete")
        return self._append("stream_checkpoints", (deployment_id, channel, sequence), ("deployment_id", "channel", "sequence"), payload or {})

    record_checkpoint = append_checkpoint
    append_stream_checkpoint = append_checkpoint

    def append_watchdog_finding(
        self,
        deployment_id: str | Mapping[str, Any],
        finding_id: str | None = None,
        settings_version: str | int = 1,
        state: str | None = None,
        payload: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> WriteOutcome:
        if isinstance(deployment_id, Mapping):
            record = dict(deployment_id)
            deployment_id, finding_id = record.get("deployment_id"), record.get("finding_id", record.get("id"))
            settings_version, state, payload = record.get("settings_version", 1), record.get("state"), record
        else:
            payload = {**dict(payload or {}), **fields, "deployment_id": deployment_id, "finding_id": finding_id, "settings_version": settings_version, "state": state}
        if not all(isinstance(item, str) and item for item in (deployment_id, finding_id, state)):
            raise ValueError("watchdog finding identity is incomplete")
        return self._append("watchdog_findings", (deployment_id, finding_id, str(settings_version)), ("deployment_id", "finding_id", "settings_version"), payload or {})

    record_watchdog_finding = append_watchdog_finding

    def append_reconcile_bundle(
        self,
        rest_snapshot: Mapping[str, Any],
        *,
        events: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]] = (),
        reconciliation: Mapping[str, Any] | None = None,
        checkpoints: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]] = (),
    ) -> tuple[WriteOutcome, ...]:
        """Persist one REST/WS application atomically, including checkpoint."""

        events = tuple(events or ())
        checkpoints = tuple(checkpoints or ())
        deployment_id = rest_snapshot.get("deployment_id")
        snapshot_id = rest_snapshot.get("snapshot_id")
        observed_at = rest_snapshot.get("observed_at")
        if not all(isinstance(item, str) and item for item in (deployment_id, snapshot_id, observed_at)):
            raise ValueError("REST snapshot identity is incomplete")
        _reject_secrets(rest_snapshot, "rest_snapshot")
        for name in ("positions", "orders", "executions"):
            items = rest_snapshot.get(name, ())
            if not isinstance(items, (list, tuple)):
                raise ValueError(f"REST {name} must be a sequence")
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError(f"REST {name} entries must be mappings")
                if name == "positions" and not all(isinstance(item.get(key), str) and item[key] for key in ("symbol", "side")):
                    raise ValueError("REST position identity is incomplete")
                order_id = item.get("order_id", item.get("id"))
                if name == "orders" and (not isinstance(order_id, (str, int)) or isinstance(order_id, bool) or (isinstance(order_id, str) and not order_id.strip())):
                    raise ValueError("REST order identity is incomplete")
                execution_id = item.get("execution_id", item.get("id"))
                if name == "executions" and (not isinstance(execution_id, (str, int)) or isinstance(execution_id, bool) or (isinstance(execution_id, str) and not execution_id.strip())):
                    raise ValueError("REST execution identity is incomplete")
        for event in events:
            if not isinstance(event, Mapping) or not isinstance(event.get("channel", event.get("kind")), str) or not event.get("channel", event.get("kind")):
                raise ValueError("WS event channel is incomplete")
            event_id = event.get("event_id", event.get("id", event.get("sequence")))
            if event_id is None or (isinstance(event_id, str) and not event_id.strip()) or isinstance(event_id, bool):
                raise ValueError("WS event identity is incomplete")
            _reject_secrets(event, "ws_event")
            if "execution_id" in event:
                execution_id = event.get("execution_id")
                account_id = event.get("account_id", rest_snapshot.get("account_id"))
                if execution_id is None or (isinstance(execution_id, str) and not execution_id.strip()) or not isinstance(account_id, str) or not account_id.strip():
                    raise ValueError("WS execution identity is incomplete")
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, Mapping) or not isinstance(checkpoint.get("channel"), str) or not checkpoint.get("channel") or not isinstance(checkpoint.get("sequence"), int) or isinstance(checkpoint.get("sequence"), bool):
                raise ValueError("checkpoint identity is incomplete")
            _reject_secrets(checkpoint, "checkpoint")
        if reconciliation is not None:
            if not isinstance(reconciliation, Mapping) or not isinstance(reconciliation.get("reconcile_id", reconciliation.get("id")), str) or not reconciliation.get("reconcile_id", reconciliation.get("id")) or not isinstance(reconciliation.get("status"), str) or not reconciliation.get("status"):
                raise ValueError("reconciliation identity is incomplete")
            _reject_secrets(reconciliation, "reconciliation")
        results: list[WriteOutcome] = []
        bundle_inconsistent = False
        with self.transaction() as connection:
            account = {key: value for key, value in rest_snapshot.items() if key not in {"positions", "orders", "executions"}}
            outcome = self._append("account_snapshots", (deployment_id, snapshot_id), ("deployment_id", "snapshot_id"), account, connection=connection)
            results.append(outcome)
            bundle_inconsistent |= outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}
            for position in rest_snapshot.get("positions", ()):
                row = {**position, "deployment_id": deployment_id, "snapshot_id": snapshot_id}
                outcome = self._append("position_snapshots", (deployment_id, snapshot_id, str(row.get("symbol", "")), str(row.get("side", ""))), ("deployment_id", "snapshot_id", "symbol", "side"), row, connection=connection)
                results.append(outcome)
                bundle_inconsistent |= outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}
            for order in rest_snapshot.get("orders", ()):
                row = {**order, "deployment_id": deployment_id, "snapshot_id": snapshot_id}
                outcome = self._append("order_snapshots", (deployment_id, snapshot_id, str(row.get("order_id", row.get("id", ""))), str(row.get("revision", 0))), ("deployment_id", "snapshot_id", "order_id", "revision"), row, connection=connection)
                results.append(outcome)
                bundle_inconsistent |= outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}
            for execution in rest_snapshot.get("executions", ()):
                row = {**execution, "deployment_id": deployment_id, "account_id": execution.get("account_id", rest_snapshot.get("account_id", ""))}
                execution_id = str(row.get("execution_id", row.get("id", "")))
                account_id = str(row.get("account_id", ""))
                if not account_id:
                    raise ValueError("REST execution account identity is incomplete")
                outcome = self._append("execution_events", (deployment_id, account_id, execution_id), ("deployment_id", "account_id", "execution_id"), row, connection=connection)
                results.append(WriteOutcome.INCONSISTENT if outcome is WriteOutcome.CONFLICT else outcome)
                bundle_inconsistent |= outcome is WriteOutcome.CONFLICT
            for event in events:
                row = {**event, "deployment_id": deployment_id}
                channel = str(row.get("channel", row.get("kind", "")))
                event_id = str(row.get("event_id", row.get("id", row.get("sequence", ""))))
                outcome = self._append("stream_events", (deployment_id, channel, event_id), ("deployment_id", "channel", "event_id"), row, connection=connection)
                results.append(outcome)
                bundle_inconsistent |= outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}
                if str(row.get("kind", row.get("event_type", ""))).casefold() in {"execution", "fill", "trade"} or "execution_id" in row:
                    execution_id = str(row.get("execution_id", row.get("id", event_id)))
                    account_id = str(row.get("account_id", rest_snapshot.get("account_id", "")))
                    if not account_id or not execution_id:
                        raise ValueError("WS execution identity is incomplete")
                    outcome = self._append("execution_events", (deployment_id, account_id, execution_id), ("deployment_id", "account_id", "execution_id"), row, connection=connection)
                    results.append(WriteOutcome.INCONSISTENT if outcome is WriteOutcome.CONFLICT else outcome)
                    bundle_inconsistent |= outcome is WriteOutcome.CONFLICT
            reconciliation_status = reconciliation.get("status") if isinstance(reconciliation, Mapping) else None
            if not bundle_inconsistent and reconciliation is not None and reconciliation_status == "HEALTHY":
                for checkpoint in checkpoints:
                    row = {**checkpoint, "deployment_id": deployment_id}
                    outcome = self._append("stream_checkpoints", (deployment_id, str(row.get("channel", "")), int(row.get("sequence", 0))), ("deployment_id", "channel", "sequence"), row, connection=connection)
                    results.append(outcome)
                    bundle_inconsistent |= outcome in {WriteOutcome.CONFLICT, WriteOutcome.INCONSISTENT}
            if reconciliation is not None:
                row = {**reconciliation, "deployment_id": deployment_id}
                if bundle_inconsistent:
                    row["status"] = "INCONSISTENT"
                    row["reasons"] = tuple(dict.fromkeys((*row.get("reasons", ()), "CONFLICTING_LIVE_FACT")))
                results.append(self._append("reconciliations", (deployment_id, str(row.get("reconcile_id", row.get("id", "")))), ("deployment_id", "reconcile_id"), row, connection=connection))
        return tuple(results)

    append_snapshot_and_checkpoint = append_reconcile_bundle

    def rows(self, table: str, *, deployment_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._lock:
            if table not in _APPEND_ONLY:
                raise ValueError("unknown live table")
            connection = self.connection
            query = f"SELECT * FROM {table}"
            args: tuple[Any, ...] = ()
            if deployment_id is not None:
                query += " WHERE deployment_id = ?"
                args = (deployment_id,)
            if table == "deployment_manifests":
                query += " ORDER BY version, manifest_id, canonical_digest, rowid"
            else:
                query += " ORDER BY effective_at, observed_at, source_kind, source_id, source_sequence, canonical_digest, rowid"
            rows = connection.execute(query, args).fetchall()
            names = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({table})").fetchall())
            result: list[dict[str, Any]] = []
            for row in rows:
                item = {name: row[index] for index, name in enumerate(names)}
                try:
                    payload = json.loads(item["payload"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict):
                    item.update({key: value for key, value in payload.items() if key not in item})
                result.append(item)
            return tuple(result)

    def latest_checkpoint(self, deployment_id: str, channel: str) -> dict[str, Any] | None:
        with self._lock:
            connection = self.connection
            row = connection.execute(
                "SELECT sequence, payload FROM stream_checkpoints WHERE deployment_id = ? AND channel = ? ORDER BY sequence DESC LIMIT 1",
                (deployment_id, channel),
            ).fetchone()
            if row is None:
                return None
            return {**json.loads(row[1]), "deployment_id": deployment_id, "channel": channel, "sequence": row[0]}

    read_checkpoint = latest_checkpoint

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> "LiveStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "DeploymentManifest",
    "LiveStore",
    "LiveStoreError",
    "StoreOutcome",
    "StoredFact",
    "WriteOutcome",
    "canonical_digest",
    "canonical_bytes",
    "canonical_decimal",
    "canonical_json",
    "canonical_source_order",
    "source_order",
]
