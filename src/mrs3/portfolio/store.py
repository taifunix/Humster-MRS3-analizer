"""Small transactional Portfolio DuckDB store and its DB-scoped lease."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import sys
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID, uuid4

import duckdb
import psutil

if os.name == "nt":
    import msvcrt
else:
    import fcntl


LOCK_OWNER_UNVERIFIABLE = "LOCK_OWNER_UNVERIFIABLE"
LOCK_MANUAL_CLEAR = "LOCK_MANUAL_CLEAR"
LOCK_GATE_UNAVAILABLE = "LOCK_GATE_UNAVAILABLE"
LOCK_RELEASE_FAILED = "LOCK_RELEASE_FAILED"
SCHEMA_VERSION = 1
DATABASE_KIND = "mrs3_portfolio"
_TIMESTAMP_UTC_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{3}|\d{6}))?Z$"
)


class PortfolioStoreError(RuntimeError):
    """A fail-closed Portfolio store or lease error."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def canonical_database_path(path: str | os.PathLike[str]) -> Path:
    """Resolve aliases before deriving the one lock path for a database."""
    return Path(os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _process_start_identity(pid: int) -> str:
    return str(psutil.Process(pid).create_time())


def _boot_identity() -> str:
    boot_id = Path("/proc/sys/kernel/random/boot_id")
    if boot_id.is_file():
        with suppress(OSError):
            value = boot_id.read_text(encoding="ascii").strip()
            if value:
                return value
    # Windows derives this from uptime and can drift by fractions of a second
    # between processes; whole-second identity keeps the boot token stable.
    return str(int(psutil.boot_time()))


def _container_identity() -> str:
    # This is compatibility metadata only; PID namespace identity is the
    # reclaim proof. Do not infer container identity from cgroup/cpuset.
    return f"host:{platform.node() or socket.gethostname()}"


def _pid_namespace_identity(machine_identity: str) -> str:
    if os.name == "nt":
        return f"windows-host:{machine_identity}" if machine_identity else "unknown"
    if sys.platform.startswith("linux"):
        try:
            return str(os.stat("/proc/self/ns/pid").st_ino)
        except (AttributeError, OSError):
            return "unknown"
    return "unknown"


def _known_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.lower() != "unknown"


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability for platforms exposing directory FDs."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _locked_gate(path: Path, existing: Any = None):
    """Serialize every lock-path mutation with a portable OS file lock."""
    if existing is not None:
        yield existing
        return
    try:
        handle = path.open("a+b")
    except OSError as error:
        raise PortfolioStoreError(
            "Portfolio database lease gate is unavailable", code=LOCK_GATE_UNAVAILABLE
        ) from error
    locked = False
    try:
        try:
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise PortfolioStoreError("Portfolio database lease is busy", code="LOCK_BUSY") from error
        locked = True
        yield handle
    finally:
        if locked:
            with suppress(OSError):
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            handle.close()


def _publish_attestation(audit_path: Path, event: Mapping[str, Any]) -> Path:
    """Publish one immutable event file atomically and durably."""
    audit_path.mkdir(parents=True, exist_ok=True)
    _fsync_directory(audit_path.parent)
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    for _ in range(3):
        token = uuid4().hex
        temporary = audit_path / f".{token}.tmp"
        published = audit_path / f"event-{token}.json"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, published)
            except FileExistsError:
                continue
            _fsync_directory(audit_path)
            return published
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
    raise PortfolioStoreError("could not publish unique lock attestation")


class PortfolioDBLease:
    """An O_EXCL metadata lease keyed by a canonical Portfolio DB path."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.path = canonical_database_path(database_path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.gate_path = Path(str(self.lock_path) + ".gate.v1")
        self.audit_path = Path(str(self.lock_path) + ".audit.v1")
        self._handle: Any = None
        self._owner: dict[str, Any] | None = None

    @property
    def identity(self) -> dict[str, Any]:
        pid = os.getpid()
        try:
            start = _process_start_identity(pid)
        except Exception:
            start = "unknown"
        host = socket.gethostname()
        return {
            "pid": pid,
            "process_start_identity": start,
            "host_identity": host,
            "machine_identity": platform.node() or host,
            "boot_identity": _boot_identity(),
            "container_identity": _container_identity(),
            "pid_namespace_identity": _pid_namespace_identity(platform.node() or host),
            "lock_kind": "portfolio_db_lease",
            "canonical_path": str(self.path),
            "target_path": str(self.path),
        }

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _unverifiable(self, detail: str) -> PortfolioStoreError:
        return PortfolioStoreError(f"{LOCK_OWNER_UNVERIFIABLE}: {detail}", code=LOCK_OWNER_UNVERIFIABLE)

    def _same_host_boot(self, owner: Mapping[str, Any]) -> bool:
        current = self.identity
        host = owner.get("host_identity", owner.get("host"))
        boot = owner.get("boot_identity", owner.get("boot"))
        machine = owner.get("machine_identity")
        namespace = owner.get("pid_namespace_identity")
        container = owner.get("container_identity")
        current_container = current.get("container_identity")
        return bool(
            _known_identity(host)
            and _known_identity(boot)
            and _known_identity(machine)
            and _known_identity(namespace)
            and _known_identity(container)
            and _known_identity(current_container)
            and host == current["host_identity"]
            and machine == current["machine_identity"]
            and boot == current["boot_identity"]
            and namespace == current["pid_namespace_identity"]
            and container == current_container
        )

    def _is_proven_dead(self, owner: Mapping[str, Any]) -> bool:
        pid = owner.get("pid")
        expected = owner.get("process_start_identity", owner.get("process_start"))
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not expected
            or str(expected).lower() == "unknown"
        ):
            raise self._unverifiable("missing PID/start identity")
        try:
            process = psutil.Process(pid)
            actual = str(process.create_time())
        except psutil.NoSuchProcess:
            return True
        except (psutil.AccessDenied, psutil.ZombieProcess) as error:
            raise self._unverifiable(f"cannot inspect PID {pid}") from error
        try:
            return not math.isclose(float(str(expected)), float(actual), rel_tol=0, abs_tol=1e-6)
        except ValueError:
            return str(expected) != actual

    def _open_new(self) -> Any:
        owner = {**self.identity, "acquisition_token": uuid4().hex, "acquired_at_utc": _utc_now()}
        temporary = self.lock_path.with_name(f".{self.lock_path.name}.{owner['acquisition_token']}.tmp")
        try:
            with temporary.open("xb") as staged:
                staged.write(json.dumps(owner, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                staged.flush()
                os.fsync(staged.fileno())
            with _locked_gate(self.gate_path) as gate:
                try:
                    os.link(temporary, self.lock_path)
                except FileExistsError as error:
                    try:
                        self._handle_existing_owner(error, gate)
                    except PortfolioStoreError as owner_error:
                        if owner_error.code != "LOCK_BUSY":
                            raise
                    try:
                        os.link(temporary, self.lock_path)
                    except FileExistsError as race:
                        raise PortfolioStoreError(
                            "Portfolio database lease is busy", code="LOCK_BUSY"
                        ) from race
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        self._owner = owner
        return None

    def _handle_existing_owner(self, error: OSError, gate: Any) -> None:
        try:
            stale_bytes = self.lock_path.read_bytes()
            owner = json.loads(stale_bytes.decode("utf-8"))
        except FileNotFoundError as read_error:
            raise PortfolioStoreError("Portfolio database lease is busy", code="LOCK_BUSY") from read_error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as read_error:
            raise self._unverifiable("lock metadata is missing or invalid") from read_error
        if not isinstance(owner, dict):
            raise self._unverifiable("lock metadata is missing or invalid") from error
        if not self._same_host_boot(owner):
            raise self._unverifiable("owner host/boot is foreign or unknown")
        if not self._is_proven_dead(owner):
            raise PortfolioStoreError("Portfolio database lease is busy", code="LOCK_BUSY")
        if not _remove_lock_if_unchanged(self.lock_path, stale_bytes, gate):
            raise PortfolioStoreError("Portfolio database lease is busy", code="LOCK_BUSY")

    def __enter__(self) -> "PortfolioDBLease":
        if not self.lock_path.parent.is_dir():
            raise PortfolioStoreError("Portfolio database parent does not exist")
        self._handle = self._open_new()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            handle.close()
        if self._owner is not None:
            expected = json.dumps(self._owner, sort_keys=True, separators=(",", ":")).encode("utf-8")
            for attempt in range(3):
                try:
                    _remove_lock_if_unchanged(self.lock_path, expected)
                    break
                except PortfolioStoreError as error:
                    if error.code not in {"LOCK_BUSY", LOCK_GATE_UNAVAILABLE}:
                        raise
                    if attempt == 2:
                        raise PortfolioStoreError(
                            "Portfolio database lease release failed", code=LOCK_RELEASE_FAILED
                        ) from error
                    time.sleep(0.01)


def _remove_lock_if_unchanged(lock_path: Path, expected: bytes, gate: Any = None) -> bool:
    """Remove only the inode whose bytes were observed, using a hardlink guard."""
    guard = lock_path.with_name(f".{lock_path.name}.remove-{uuid4().hex}.tmp")
    with _locked_gate(Path(str(lock_path) + ".gate.v1"), gate):
        try:
            try:
                os.link(lock_path, guard)
            except (FileExistsError, FileNotFoundError):
                return False
            try:
                if guard.read_bytes() != expected or not os.path.samefile(lock_path, guard):
                    return False
                lock_path.unlink()
                return True
            except FileNotFoundError:
                return False
            finally:
                with suppress(FileNotFoundError):
                    guard.unlink()
        finally:
            with suppress(FileNotFoundError):
                guard.unlink()


def manual_clear_lock(
    database_path: str | os.PathLike[str], *, operator_identity: str, reason: str
) -> None:
    """Append durable operator evidence, then remove one explicitly named lock."""
    if not isinstance(operator_identity, str) or not operator_identity.strip():
        raise ValueError("operator_identity is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    lease = PortfolioDBLease(database_path)
    with _locked_gate(lease.gate_path) as gate:
        try:
            stale_bytes = lease.lock_path.read_bytes()
            stale_owner = json.loads(stale_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortfolioStoreError("cannot read lock owner for manual clear") from error
        if not isinstance(stale_owner, dict):
            raise PortfolioStoreError("cannot read lock owner for manual clear")
        event = {
            "attestation_version": 1,
            "lock_kind": stale_owner.get("lock_kind", "portfolio_db_lease"),
            "lock_path": str(lease.lock_path),
            "canonical_path": str(lease.path),
            "target_path": str(lease.path),
            "stale_owner": stale_owner,
            "operator_identity": operator_identity,
            "cleared_at_utc": _utc_now(),
            "reason": LOCK_MANUAL_CLEAR,
            "operator_reason": reason,
        }
        _publish_attestation(lease.audit_path, event)
        if not _remove_lock_if_unchanged(lease.lock_path, stale_bytes, gate):
            raise PortfolioStoreError("lock changed during manual clear")


clear_portfolio_lock_manually = manual_clear_lock


_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id VARCHAR PRIMARY KEY,
    canonical_digest VARCHAR NOT NULL UNIQUE,
    content VARCHAR NOT NULL,
    created_at_utc VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_snapshots (
    snapshot_id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL REFERENCES campaigns(campaign_id),
    canonical_digest VARCHAR NOT NULL,
    content VARCHAR NOT NULL,
    created_at_utc VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_runs (
    run_id VARCHAR PRIMARY KEY,
    execution_campaign_id VARCHAR NOT NULL REFERENCES campaigns(campaign_id),
    payload VARCHAR NOT NULL,
    created_at_utc VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id VARCHAR PRIMARY KEY,
    trading_run_id VARCHAR NOT NULL REFERENCES trading_runs(run_id),
    execution_campaign_id VARCHAR NOT NULL REFERENCES campaigns(campaign_id),
    decision_campaign_id VARCHAR NOT NULL REFERENCES campaigns(campaign_id),
    payload VARCHAR NOT NULL,
    created_at_utc VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_sets (
    portfolio_set_id VARCHAR PRIMARY KEY,
    canonical_digest VARCHAR NOT NULL UNIQUE,
    content VARCHAR NOT NULL,
    created_at_utc VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_set_members (
    portfolio_set_id VARCHAR NOT NULL REFERENCES portfolio_sets(portfolio_set_id),
    member_ordinal BIGINT NOT NULL,
    member_id VARCHAR NOT NULL,
    PRIMARY KEY (portfolio_set_id, member_ordinal)
);
CREATE TABLE IF NOT EXISTS trading_run_series (
    run_id VARCHAR NOT NULL REFERENCES trading_runs(run_id),
    series_name VARCHAR NOT NULL,
    timestamp_utc VARCHAR NOT NULL,
    source_ordinal BIGINT NOT NULL,
    numeric_value DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (run_id, series_name, source_ordinal)
);
CREATE TABLE IF NOT EXISTS evaluation_series (
    evaluation_id VARCHAR NOT NULL REFERENCES evaluations(evaluation_id),
    series_name VARCHAR NOT NULL,
    timestamp_utc VARCHAR NOT NULL,
    source_ordinal BIGINT NOT NULL,
    numeric_value DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (evaluation_id, series_name, source_ordinal)
);
CREATE TABLE IF NOT EXISTS portfolio_set_series (
    portfolio_set_id VARCHAR NOT NULL REFERENCES portfolio_sets(portfolio_set_id),
    series_name VARCHAR NOT NULL,
    timestamp_utc VARCHAR NOT NULL,
    source_ordinal BIGINT NOT NULL,
    numeric_value DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (portfolio_set_id, series_name, source_ordinal)
);
CREATE TABLE IF NOT EXISTS current_results (
    result_id VARCHAR PRIMARY KEY,
    payload VARCHAR NOT NULL,
    replaced_at_utc VARCHAR NOT NULL
);
"""


def _ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    if "schema_info" in tables:
        try:
            rows = connection.execute(
                "SELECT schema_version, database_kind, database_instance_id FROM schema_info"
            ).fetchall()
        except duckdb.Error as error:
            raise PortfolioStoreError("Portfolio schema marker mismatch") from error
        if len(rows) != 1:
            raise PortfolioStoreError("Portfolio schema marker mismatch")
        version, kind, instance_id = rows[0]
        try:
            uuid4_type = UUID(str(instance_id))
        except (ValueError, AttributeError, TypeError):
            uuid4_type = None
        if version != SCHEMA_VERSION or kind != DATABASE_KIND or uuid4_type is None:
            raise PortfolioStoreError("Portfolio schema marker mismatch")
    else:
        if tables:
            raise PortfolioStoreError("Portfolio schema marker is missing")
        connection.execute(
            "CREATE TABLE schema_info (schema_version INTEGER NOT NULL, database_kind VARCHAR NOT NULL, database_instance_id VARCHAR NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_info VALUES (?, ?, ?)", [SCHEMA_VERSION, DATABASE_KIND, str(uuid4())]
        )
    connection.execute(_SCHEMA)


def _payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PortfolioStoreError("content is not serializable") from error


def _numeric_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (Decimal, int, str)):
        raise PortfolioStoreError("numeric series values require Decimal, integer, or decimal string")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PortfolioStoreError("numeric series value is not a decimal") from error
    if not number.is_finite():
        raise PortfolioStoreError("numeric series value must be finite")
    scale = max(0, -number.as_tuple().exponent)
    integer_digits = max(0, number.adjusted() + 1) if number else 0
    if scale > 12:
        raise PortfolioStoreError("numeric series value scale exceeds 12")
    if integer_digits > 26:
        raise PortfolioStoreError("numeric series value integer digits exceed 26 for DECIMAL(38,12)")
    return number


def _timestamp_utc(value: Any) -> str:
    if not isinstance(value, str):
        raise PortfolioStoreError("timestamp_utc must be canonical UTC ISO text")
    match = _TIMESTAMP_UTC_RE.fullmatch(value)
    if match is None:
        raise PortfolioStoreError("timestamp_utc must use seconds, milliseconds, or microseconds and end with Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise PortfolioStoreError("timestamp_utc is not a valid UTC ISO timestamp") from error
    return value


def _row_dict(connection: duckdb.DuckDBPyConnection, table: str, key: str, value: str) -> dict[str, Any] | None:
    result = connection.execute(f"SELECT * FROM {table} WHERE {key} = ?", [value])
    row = result.fetchone()
    if row is None:
        return None
    return dict(zip([item[0] for item in result.description], row))


class PortfolioStore:
    """The intentionally small M1 publication surface."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.path = canonical_database_path(database_path)

    def _publish(self, operation: Callable[[duckdb.DuckDBPyConnection], Any]) -> Any:
        with PortfolioDBLease(self.path):
            try:
                connection = duckdb.connect(str(self.path))
            except duckdb.Error as error:
                raise PortfolioStoreError("cannot open Portfolio database") from error
            try:
                connection.execute("BEGIN TRANSACTION")
                _ensure_schema(connection)
                result = operation(connection)
                connection.execute("COMMIT")
                return result
            except PortfolioStoreError:
                with suppress(duckdb.Error):
                    connection.execute("ROLLBACK")
                raise
            except Exception as error:
                with suppress(duckdb.Error):
                    connection.execute("ROLLBACK")
                raise PortfolioStoreError("Portfolio publication rolled back") from error
            finally:
                connection.close()

    def initialize(self) -> None:
        self._publish(lambda _connection: None)

    def create_campaign(self, campaign_id: str, canonical_digest: str, content: Any) -> str:
        if not campaign_id or not canonical_digest:
            raise ValueError("campaign_id and canonical_digest are required")
        encoded = _payload(content)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            by_id = db.execute(
                "SELECT campaign_id, canonical_digest, content FROM campaigns WHERE campaign_id = ?", [campaign_id]
            ).fetchone()
            if by_id is not None:
                if by_id[1:] == (canonical_digest, encoded):
                    return str(by_id[0])
                raise PortfolioStoreError("campaign identity mismatch")
            by_digest = db.execute(
                "SELECT campaign_id, canonical_digest, content FROM campaigns WHERE canonical_digest = ?",
                [canonical_digest],
            ).fetchone()
            if by_digest is not None:
                if by_digest[2] == encoded:
                    return str(by_digest[0])
                raise PortfolioStoreError("campaign digest collision")
            by_content = db.execute("SELECT campaign_id FROM campaigns WHERE content = ?", [encoded]).fetchone()
            if by_content is not None:
                raise PortfolioStoreError("campaign content/digest mismatch")
            db.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?, ?)",
                [campaign_id, canonical_digest, encoded, _utc_now()],
            )
            return campaign_id

        try:
            return self._publish(publish)
        except PortfolioStoreError as error:
            if error.code != "LOCK_BUSY":
                raise
            deadline = time.monotonic() + 0.5
            while True:
                try:
                    with duckdb.connect(str(self.path), read_only=True) as db:
                        row = db.execute(
                            "SELECT campaign_id, content FROM campaigns WHERE canonical_digest = ?",
                            [canonical_digest],
                        ).fetchone()
                        if row is None:
                            row = db.execute(
                                "SELECT campaign_id, canonical_digest FROM campaigns WHERE content = ?", [encoded]
                            ).fetchone()
                            if row is not None:
                                raise PortfolioStoreError("campaign content/digest mismatch")
                        elif row[1] == encoded:
                            return str(row[0])
                        elif row is not None:
                            raise PortfolioStoreError("campaign digest collision")
                except PortfolioStoreError:
                    raise
                except duckdb.Error:
                    pass
                if not PortfolioDBLease(self.path).lock_path.exists() or time.monotonic() >= deadline:
                    raise error
                time.sleep(0.01)

    def save_campaign_snapshot(self, snapshot_id: str, campaign_id: str, canonical_digest: str, content: Any) -> str:
        encoded = _payload(content)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            if db.execute("SELECT 1 FROM campaigns WHERE campaign_id = ?", [campaign_id]).fetchone() is None:
                raise PortfolioStoreError("unknown campaign")
            row = db.execute("SELECT campaign_id, canonical_digest, content FROM campaign_snapshots WHERE snapshot_id = ?", [snapshot_id]).fetchone()
            if row is not None:
                if row[1:] == (canonical_digest, encoded) and row[0] == campaign_id:
                    return snapshot_id
                raise PortfolioStoreError("snapshot identity mismatch")
            db.execute("INSERT INTO campaign_snapshots VALUES (?, ?, ?, ?, ?)", [snapshot_id, campaign_id, canonical_digest, encoded, _utc_now()])
            return snapshot_id

        return self._publish(publish)

    def create_trading_run(self, run_id: str, execution_campaign_id: str, payload: Any) -> str:
        encoded = _payload(payload)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            row = db.execute("SELECT execution_campaign_id, payload FROM trading_runs WHERE run_id = ?", [run_id]).fetchone()
            if row is not None:
                if row == (execution_campaign_id, encoded):
                    return run_id
                raise PortfolioStoreError("trading run execution identity is immutable")
            if db.execute("SELECT 1 FROM campaigns WHERE campaign_id = ?", [execution_campaign_id]).fetchone() is None:
                raise PortfolioStoreError("unknown execution campaign")
            db.execute("INSERT INTO trading_runs VALUES (?, ?, ?, ?)", [run_id, execution_campaign_id, encoded, _utc_now()])
            return run_id

        return self._publish(publish)

    def create_evaluation(
        self,
        evaluation_id: str,
        trading_run_id: str,
        decision_campaign_id: str,
        payload: Any,
        *,
        execution_campaign_id: str | None = None,
    ) -> str:
        encoded = _payload(payload)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            run = db.execute("SELECT execution_campaign_id FROM trading_runs WHERE run_id = ?", [trading_run_id]).fetchone()
            if run is None:
                raise PortfolioStoreError("unknown trading run")
            execution = str(run[0])
            if execution_campaign_id is not None and execution_campaign_id != execution:
                raise PortfolioStoreError("evaluation execution campaign mismatch")
            if db.execute("SELECT 1 FROM campaigns WHERE campaign_id = ?", [decision_campaign_id]).fetchone() is None:
                raise PortfolioStoreError("unknown decision campaign")
            row = db.execute("SELECT trading_run_id, execution_campaign_id, decision_campaign_id, payload FROM evaluations WHERE evaluation_id = ?", [evaluation_id]).fetchone()
            expected = (trading_run_id, execution, decision_campaign_id, encoded)
            if row is not None:
                if row == expected:
                    return evaluation_id
                raise PortfolioStoreError("evaluation identity mismatch")
            db.execute("INSERT INTO evaluations VALUES (?, ?, ?, ?, ?, ?)", [evaluation_id, trading_run_id, execution, decision_campaign_id, encoded, _utc_now()])
            return evaluation_id

        return self._publish(publish)

    def create_portfolio_set(
        self,
        portfolio_set_id: str,
        canonical_digest: str,
        content: Any,
        members: Iterable[str] = (),
    ) -> str:
        encoded = _payload(content)
        member_ids = tuple(str(member) for member in members)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            row = db.execute("SELECT canonical_digest, content FROM portfolio_sets WHERE portfolio_set_id = ?", [portfolio_set_id]).fetchone()
            if row is not None:
                existing_members = tuple(
                    item[0]
                    for item in db.execute(
                        "SELECT member_id FROM portfolio_set_members WHERE portfolio_set_id = ? ORDER BY member_ordinal",
                        [portfolio_set_id],
                    ).fetchall()
                )
                if row == (canonical_digest, encoded) and existing_members == member_ids:
                    return portfolio_set_id
                raise PortfolioStoreError("portfolio set identity mismatch")
            digest_row = db.execute("SELECT portfolio_set_id, content FROM portfolio_sets WHERE canonical_digest = ?", [canonical_digest]).fetchone()
            if digest_row is not None:
                existing_members = tuple(
                    item[0]
                    for item in db.execute(
                        "SELECT member_id FROM portfolio_set_members WHERE portfolio_set_id = ? ORDER BY member_ordinal",
                        [digest_row[0]],
                    ).fetchall()
                )
                if digest_row[1] == encoded and existing_members == member_ids:
                    return str(digest_row[0])
                raise PortfolioStoreError("portfolio set digest collision")
            db.execute("INSERT INTO portfolio_sets VALUES (?, ?, ?, ?)", [portfolio_set_id, canonical_digest, encoded, _utc_now()])
            if member_ids:
                db.executemany("INSERT INTO portfolio_set_members VALUES (?, ?, ?)", [(portfolio_set_id, i, member) for i, member in enumerate(member_ids)])
            return portfolio_set_id

        return self._publish(publish)

    def _insert_series(self, db: duckdb.DuckDBPyConnection, table: str, owner_column: str, owner_id: str, points: Iterable[Any], series_name: str) -> int:
        count = 0
        for ordinal, point in enumerate(points):
            if isinstance(point, Mapping):
                timestamp = point.get("timestamp_utc", point.get("timestamp"))
                value = point.get("numeric_value", point.get("value"))
                source_ordinal = point.get("source_ordinal", ordinal)
            elif isinstance(point, (tuple, list)) and len(point) in (2, 3):
                timestamp, value = point[:2]
                source_ordinal = point[2] if len(point) == 3 else ordinal
            else:
                raise PortfolioStoreError("invalid numeric series point")
            if isinstance(source_ordinal, bool) or not isinstance(source_ordinal, int):
                raise PortfolioStoreError("invalid numeric series point")
            timestamp = _timestamp_utc(timestamp)
            numeric_value = _numeric_decimal(value)
            existing = db.execute(f"SELECT timestamp_utc, numeric_value FROM {table} WHERE {owner_column} = ? AND series_name = ? AND source_ordinal = ?", [owner_id, series_name, source_ordinal]).fetchone()
            if existing is not None:
                if existing != (timestamp, numeric_value):
                    raise PortfolioStoreError("numeric series identity mismatch")
            else:
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)", [owner_id, series_name, timestamp, source_ordinal, numeric_value])
            count += 1
        return count

    def insert_series(self, run_id: str, points: Iterable[Any], series_name: str = "equity") -> int:
        def publish(db: duckdb.DuckDBPyConnection) -> int:
            if db.execute("SELECT 1 FROM trading_runs WHERE run_id = ?", [run_id]).fetchone() is None:
                raise PortfolioStoreError("unknown trading run")
            return self._insert_series(db, "trading_run_series", "run_id", run_id, points, series_name)

        return self._publish(publish)

    def insert_evaluation_series(self, evaluation_id: str, points: Iterable[Any], series_name: str = "equity") -> int:
        def publish(db: duckdb.DuckDBPyConnection) -> int:
            if db.execute("SELECT 1 FROM evaluations WHERE evaluation_id = ?", [evaluation_id]).fetchone() is None:
                raise PortfolioStoreError("unknown evaluation")
            return self._insert_series(db, "evaluation_series", "evaluation_id", evaluation_id, points, series_name)

        return self._publish(publish)

    def insert_portfolio_set_series(self, portfolio_set_id: str, points: Iterable[Any], series_name: str = "equity") -> int:
        def publish(db: duckdb.DuckDBPyConnection) -> int:
            if db.execute("SELECT 1 FROM portfolio_sets WHERE portfolio_set_id = ?", [portfolio_set_id]).fetchone() is None:
                raise PortfolioStoreError("unknown portfolio set")
            return self._insert_series(db, "portfolio_set_series", "portfolio_set_id", portfolio_set_id, points, series_name)

        return self._publish(publish)

    def replace_current_result(self, result_id: str, payload: Any) -> str:
        encoded = _payload(payload)

        def publish(db: duckdb.DuckDBPyConnection) -> str:
            db.execute(
                "INSERT INTO current_results VALUES (?, ?, ?) ON CONFLICT (result_id) DO UPDATE SET payload = excluded.payload, replaced_at_utc = excluded.replaced_at_utc",
                [result_id, encoded, _utc_now()],
            )
            return result_id

        return self._publish(publish)

    upsert_current_result = replace_current_result

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with duckdb.connect(str(self.path), read_only=True) as db:
            return _row_dict(db, "campaigns", "campaign_id", campaign_id)

    def get_campaign_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Read frozen snapshot facts without consulting the source database."""
        with duckdb.connect(str(self.path), read_only=True) as db:
            return _row_dict(db, "campaign_snapshots", "snapshot_id", snapshot_id)

    read_campaign_snapshot = get_campaign_snapshot

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with duckdb.connect(str(self.path), read_only=True) as db:
            return _row_dict(db, "evaluations", "evaluation_id", evaluation_id)

    def get_trading_run(self, run_id: str) -> dict[str, Any] | None:
        with duckdb.connect(str(self.path), read_only=True) as db:
            return _row_dict(db, "trading_runs", "run_id", run_id)


__all__ = [
    "LOCK_MANUAL_CLEAR",
    "LOCK_OWNER_UNVERIFIABLE",
    "LOCK_GATE_UNAVAILABLE",
    "LOCK_RELEASE_FAILED",
    "PortfolioDBLease",
    "PortfolioStore",
    "PortfolioStoreError",
    "canonical_database_path",
    "clear_portfolio_lock_manually",
    "manual_clear_lock",
]
