from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import duckdb

from .performance import ParsedPerformanceReport, PerformanceParseError, parse_performance_report
from .performance_store import initialize_performance_database


class PerformanceImportError(RuntimeError):
    """Raised when an evidence batch cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class PerformanceImportRequest:
    inbox: Path
    database: Path


@dataclass(frozen=True, slots=True)
class PerformanceImportResult:
    import_id: str
    imported_count: int = 0
    skipped_count: int = 0
    quarantined_count: int = 0


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PerformanceImportError(f"invalid JSON: {path.name}") from error


def _decimal(metrics: dict[str, str], *names: str) -> Decimal:
    for name in names:
        if name in metrics:
            try:
                value = Decimal(metrics[name].strip())
            except (InvalidOperation, ValueError) as error:
                raise PerformanceImportError(f"required metric is not numeric: {name}") from error
            if value.is_finite():
                return value
            raise PerformanceImportError(f"required metric is not finite: {name}")
    raise PerformanceImportError(f"required metric is missing: {names[0]}")


def _canonical_contract(manifest: dict[str, object]) -> tuple[str, str]:
    contract = manifest.get("commission_contract")
    contract_id = manifest.get("commission_contract_id")
    if not isinstance(contract, dict) or not isinstance(contract_id, str):
        raise PerformanceImportError("commission contract is missing")
    fields = ("MakerFee", "TakerFee", "SlippagePercent", "FundingRate", "FundingIntervalHours")
    normalized: dict[str, str] = {}
    for field in fields:
        if field not in contract:
            raise PerformanceImportError(f"commission field is missing: {field}")
        try:
            value = Decimal(str(contract[field]))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise PerformanceImportError(f"commission field is invalid: {field}") from error
        if not value.is_finite():
            raise PerformanceImportError(f"commission field is invalid: {field}")
        normalized[field] = format(value, "f").rstrip("0").rstrip(".") or "0"
    calculated = _sha256(_canonical(normalized))
    if calculated != contract_id:
        raise PerformanceImportError("commission contract hash mismatch")
    return calculated, json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _inventory_json(parsed: ParsedPerformanceReport) -> dict[str, object]:
    inventory = parsed.inventory
    return {
        "metric_count": inventory.metric_count,
        "metric_headers": list(inventory.metric_headers),
        "trade_headers": list(inventory.trade_headers),
        "trade_row_count": inventory.trade_row_count,
        "wallet_sample_count": inventory.wallet_sample_count,
        "equity_sample_count": inventory.equity_sample_count,
        "minimum_timestamp": inventory.minimum_timestamp.isoformat(),
        "maximum_timestamp": inventory.maximum_timestamp.isoformat(),
    }


def _audit_path(inbox: Path) -> Path:
    return inbox / "import_audit.v4.json"


def _write_audit(inbox: Path, audit: dict[str, object]) -> None:
    target = _audit_path(inbox)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(audit, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


_CHECKLIST_FIELDS = (
    "manifest_entry_id", "report_path", "source_html_sha256", "status",
    "safe_to_delete", "cleanup_state", "deleted_at_utc",
)


def _write_checklist(inbox: Path, rows: list[dict[str, str]]) -> None:
    target = inbox / "html_delete_checklist.v4.csv"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=_CHECKLIST_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _base_audit(manifest: dict[str, object], import_id: str, entries: list[dict[str, object]], status: str) -> dict[str, object]:
    return {
        "schema_version": 4,
        "batch_id": manifest.get("batch_id"),
        "import_id": import_id,
        "status": status,
        "quarantine_count": sum(entry.get("status") == "QUARANTINED" for entry in entries),
        "entries": entries,
    }


def _safe_report_path(inbox: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PerformanceImportError("report path is outside inbox")
    candidate = (inbox / relative).resolve()
    try:
        candidate.relative_to(inbox.resolve())
    except ValueError as error:
        raise PerformanceImportError("report path is outside inbox") from error
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_inbox_manifest(inbox: Path, manifest: dict[str, object]) -> None:
    if manifest.get("schema_version") != 1:
        raise PerformanceImportError("invalid inbox manifest schema_version")
    for field in ("batch_id", "tester_config_sha256"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise PerformanceImportError(f"inbox manifest field is missing: {field}")
    expected = manifest.get("expected_strategy_names")
    entries = manifest.get("entries")
    if not isinstance(expected, list) or not all(isinstance(name, str) and name for name in expected):
        raise PerformanceImportError("inbox manifest expected_strategy_names is invalid")
    if len(set(expected)) != len(expected):
        raise PerformanceImportError("duplicate expected strategy name")
    if not isinstance(entries, list) or not entries:
        raise PerformanceImportError("inbox manifest entries are missing")
    _canonical_contract(manifest)
    required = (
        "manifest_entry_id", "strategy_name", "strategy_version_id", "strategy_path",
        "report_path", "wizard_run_id", "exchange_name", "source_strategy_sha256",
        "source_report_sha256",
    )
    identities: set[tuple[str, str, str]] = set()
    names: list[str] = []
    report_paths: set[str] = set()
    strategy_paths: set[str] = set()
    entry_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(field), str) or not entry[field].strip() for field in required):
            raise PerformanceImportError("inbox manifest entry is incomplete")
        identity = (entry["manifest_entry_id"], entry["strategy_version_id"], entry["strategy_name"])
        if identity in identities or entry["manifest_entry_id"] in entry_ids:
            raise PerformanceImportError("duplicate inbox manifest entry identity")
        identities.add(identity)
        entry_ids.add(entry["manifest_entry_id"])
        names.append(entry["strategy_name"])
        if entry["strategy_name"] in names[:-1]:
            raise PerformanceImportError("duplicate manifest strategy name")
        if entry["report_path"] in report_paths:
            raise PerformanceImportError("duplicate manifest report path")
        if entry["strategy_path"] in strategy_paths:
            raise PerformanceImportError("duplicate manifest strategy path")
        report_paths.add(entry["report_path"])
        strategy_paths.add(entry["strategy_path"])
        _safe_report_path(inbox, entry["report_path"])
        _safe_report_path(inbox, entry["strategy_path"])
    if sorted(names) != sorted(expected):
        raise PerformanceImportError("inbox manifest strategy names do not match expected names")


def _prepare_entry(inbox: Path, manifest_entry: dict[str, object]) -> dict[str, object]:
    report_path = inbox / str(manifest_entry["report_path"])
    strategy_path = inbox / str(manifest_entry["strategy_path"])
    report_bytes = report_path.read_bytes()
    if _sha256(report_bytes) != manifest_entry.get("source_report_sha256"):
        raise PerformanceImportError("source report hash mismatch")
    strategy_bytes = strategy_path.read_bytes()
    if _sha256(strategy_bytes) != manifest_entry.get("source_strategy_sha256"):
        raise PerformanceImportError("source strategy hash mismatch")
    strategy = _read_json(strategy_path)
    if not isinstance(strategy, dict):
        raise PerformanceImportError("strategy JSON is not an object")
    strategy_id = _sha256(_canonical(strategy))
    if strategy_id != manifest_entry.get("strategy_version_id"):
        raise PerformanceImportError("strategy version hash mismatch")
    exchange = strategy.get("exchange")
    exchange_name = exchange.get("name") if isinstance(exchange, dict) else None
    if not isinstance(exchange_name, str) or not exchange_name.strip():
        raise PerformanceImportError("strategy exchange.name is missing")
    parsed = parse_performance_report(report_bytes)
    if parsed.settings.get("name") != manifest_entry.get("strategy_name"):
        raise PerformanceImportError("strategy name mismatch")
    settings = strategy.get("settings")
    if settings is None:
        settings = {key: value for key, value in strategy.items() if key != "exchange"}
    if not isinstance(settings, dict) or _canonical(parsed.settings) != _canonical(settings):
        raise PerformanceImportError("parsed HTML settings mismatch inbox strategy settings")
    contract_id, commission_json = _canonical_contract(_read_json(inbox / "inbox_manifest.json"))
    start = parsed.inventory.minimum_timestamp
    end = parsed.inventory.maximum_timestamp
    identity = {
        "strategy_version_id": strategy_id,
        "period_start_utc_ms": int(start.timestamp() * 1000),
        "period_end_utc_ms": int(end.timestamp() * 1000),
        "exchange_normalized": exchange_name.strip().casefold(),
        "commission_contract_id": contract_id,
    }
    test_run_id = _sha256(_canonical(identity))
    payload = {
        "settings": parsed.settings,
        "period_start_utc_ms": identity["period_start_utc_ms"],
        "period_end_utc_ms": identity["period_end_utc_ms"],
        "metrics": parsed.metrics,
        "actions": parsed.actions,
        "wallet_series": parsed.wallet_series,
        "equity_series": parsed.equity_series,
    }
    payload_hash = _sha256(_canonical(_json_value(payload)))
    basic = parsed.settings.get("basic")
    if not isinstance(basic, dict):
        raise PerformanceImportError("settings basic object is missing")
    initial_balance = _decimal(parsed.metrics, "Initial balance", "InitialBalance")
    metrics = {
        "final_balance": _decimal(parsed.metrics, "Final balance", "FinalBalance"),
        "total_pnl": _decimal(parsed.metrics, "Total PnL", "TotalPnL"),
        "total_pnl_pct": _decimal(parsed.metrics, "Total PnL, %", "TotalPnLPercent"),
        "max_drawdown": _decimal(parsed.metrics, "Max Drawdown", "MaxDrawdown"),
        "max_drawdown_pct": _decimal(parsed.metrics, "Max Drawdown, %", "MaxDrawdownPercent"),
        "total_fees": _decimal(parsed.metrics, "Total fees", "Total fees"),
        "win_rate_pct": _decimal(parsed.metrics, "Win Rate, %", "WinRate"),
        "profit_factor": _decimal(parsed.metrics, "Profit Factor"),
        "days_in_test": _decimal(parsed.metrics, "Days in test"),
        "total_trades": int(_decimal(parsed.metrics, "Total Trades")),
        "win_trades": int(_decimal(parsed.metrics, "Win Trades")),
        "loss_trades": int(_decimal(parsed.metrics, "Los Trades", "Loss Trades")),
    }
    return {
        "entry": manifest_entry, "parsed": parsed, "strategy": strategy,
        "strategy_id": strategy_id, "exchange": exchange_name.strip(),
        "commission_id": contract_id, "commission_json": commission_json,
        "test_run_id": test_run_id, "payload_hash": payload_hash,
        "initial_balance": initial_balance, "metrics": metrics,
        "period_start": start, "period_end": end,
        "settings_json": _canonical(parsed.settings).decode("utf-8"),
        "metrics_json": _canonical(parsed.metrics).decode("utf-8"),
        "source_hash": _sha256(report_bytes), "source_size": len(report_bytes),
    }


def _write_import_rows(connection: duckdb.DuckDBPyConnection, import_id: str, prepared: list[dict[str, object]], manifest: dict[str, object]) -> tuple[int, int]:
    imported = skipped = 0
    for item in prepared:
        existing = connection.execute(
            "select result_payload_sha256 from backtest_runs where test_run_id = ?",
            [item["test_run_id"]],
        ).fetchone()
        if existing is not None:
            if existing[0] != item["payload_hash"]:
                raise PerformanceImportError("IDENTITY_CONFLICT")
            skipped += 1
            item["import_status"] = "SKIPPED"
        else:
            parsed: ParsedPerformanceReport = item["parsed"]
            strategy = item["strategy"]
            basic = parsed.settings["basic"]
            connection.execute(
                "insert into strategy_versions select ?, ?, ?, ?, ?, ?, ? where not exists (select 1 from strategy_versions where strategy_version_id = ?)",
                [item["strategy_id"], parsed.settings["name"], basic.get("symbol", ""), basic.get("side", strategy.get("side", "")), basic.get("time_frame", ""), item["settings_json"], datetime.now(timezone.utc), item["strategy_id"]],
            )
            connection.execute(
                "insert into backtest_runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [item["test_run_id"], item["strategy_id"], item["period_start"], item["period_end"], item["exchange"], item["commission_id"], item["commission_json"], item["initial_balance"], item["source_hash"], item["payload_hash"], import_id, datetime.now(timezone.utc)],
            )
            metrics = item["metrics"]
            connection.execute(
                "insert into backtest_metrics (test_run_id, final_balance, total_pnl, total_pnl_pct, max_drawdown, max_drawdown_pct, total_fees, win_rate_pct, profit_factor, days_in_test, total_trades, win_trades, loss_trades, metrics_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [item["test_run_id"], metrics["final_balance"], metrics["total_pnl"], metrics["total_pnl_pct"], metrics["max_drawdown"], metrics["max_drawdown_pct"], metrics["total_fees"], metrics["win_rate_pct"], metrics["profit_factor"], metrics["days_in_test"], metrics["total_trades"], metrics["win_trades"], metrics["loss_trades"], item["metrics_json"]],
            )
            for index, action in enumerate(parsed.actions):
                connection.execute(
                    "insert into backtest_actions (test_run_id, action_index, timestamp_utc, symbol, action, position_side, pnl, raw_action_json) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    [item["test_run_id"], index, datetime.fromisoformat(action["Timestamp"].replace("Z", "+00:00")), action.get("Symbol"), action.get("Action"), action.get("Post Side"), Decimal(action.get("PnL", "0") or "0"), _canonical(action).decode("utf-8")],
                )
            for index, (wallet, equity) in enumerate(zip(parsed.wallet_series, parsed.equity_series, strict=True)):
                timestamp = datetime.fromtimestamp(wallet[0] / 1000, tz=timezone.utc)
                connection.execute("insert into backtest_equity values (?, ?, ?, ?, ?)", [item["test_run_id"], index, timestamp, wallet[1], equity[1]])
            imported += 1
            item["import_status"] = "IMPORTED"
        parsed = item["parsed"]
        entry = item["entry"]
        connection.execute(
            "insert into import_files values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [import_id, entry["manifest_entry_id"], item["strategy_id"], entry["strategy_name"], entry["report_path"], item["source_hash"], item["source_size"], item["test_run_id"], len(parsed.actions), len(parsed.equity_series), item["import_status"], None, None, True, "DELETE_READY", None],
        )
    connection.execute(
        "insert into import_runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [import_id, manifest["batch_id"], datetime.now(timezone.utc), datetime.now(timezone.utc), len(prepared), imported, skipped, 0, "COMMITTED", _canonical(manifest).decode("utf-8")],
    )
    return imported, skipped


def _verify_readback(database: Path, prepared: list[dict[str, object]], import_id: str) -> None:
    with duckdb.connect(str(database), read_only=True) as connection:
        for item in prepared:
            test_run_id = item["test_run_id"]
            row = connection.execute("select result_payload_sha256 from backtest_runs where test_run_id = ?", [test_run_id]).fetchone()
            if row is None or row[0] != item["payload_hash"]:
                raise PerformanceImportError("readback verification failed: payload hash")
            actions = connection.execute("select count(*) from backtest_actions where test_run_id = ?", [test_run_id]).fetchone()[0]
            equity = connection.execute("select count(*) from backtest_equity where test_run_id = ?", [test_run_id]).fetchone()[0]
            if actions != len(item["parsed"].actions) or equity != len(item["parsed"].equity_series):
                raise PerformanceImportError("readback verification failed: action/equity counts")
            file_row = connection.execute("select source_html_sha256, action_count, equity_sample_count, safe_to_delete, cleanup_state from import_files where import_id = ? and manifest_entry_id = ?", [import_id, item["entry"]["manifest_entry_id"]]).fetchone()
            if file_row is None or file_row[0] != item["source_hash"] or file_row[1:3] != (actions, equity) or file_row[3:] != (True, "DELETE_READY"):
                raise PerformanceImportError("readback verification failed: import file evidence")


def import_performance_batch(request: PerformanceImportRequest) -> PerformanceImportResult:
    inbox = Path(request.inbox)
    manifest = _read_json(inbox / "inbox_manifest.json")
    if not isinstance(manifest, dict):
        raise PerformanceImportError("invalid inbox manifest")
    _validate_inbox_manifest(inbox, manifest)
    import_id = uuid.uuid4().hex
    audit_entries: list[dict[str, object]] = []
    prepared: list[dict[str, object]] = []
    for entry in manifest["entries"]:
        try:
            item = _prepare_entry(inbox, entry)
            prepared.append(item)
            audit_entries.append({"manifest_entry_id": entry.get("manifest_entry_id"), "status": "READY", "inventory": _inventory_json(item["parsed"])})
        except (OSError, PerformanceParseError, PerformanceImportError, KeyError, TypeError, ValueError) as error:
            audit_entries.append({"manifest_entry_id": entry.get("manifest_entry_id"), "status": "QUARANTINED", "error_classification": type(error).__name__, "error_message": str(error)})
    if any(entry["status"] == "QUARANTINED" for entry in audit_entries):
        _write_audit(inbox, _base_audit(manifest, import_id, audit_entries, "QUARANTINED"))
        _write_checklist(inbox, [{"manifest_entry_id": str(entry.get("manifest_entry_id", "")), "report_path": str(entry.get("report_path", "")), "source_html_sha256": str(entry.get("source_report_sha256", "")), "status": "QUARANTINED", "safe_to_delete": "NO", "cleanup_state": "RETAIN", "deleted_at_utc": ""} for entry in manifest["entries"]])
        return PerformanceImportResult(import_id, quarantined_count=sum(entry["status"] == "QUARANTINED" for entry in audit_entries))
    try:
        initialize_performance_database(request.database)
    except Exception as error:
        _write_audit(inbox, _base_audit(manifest, import_id, audit_entries, "FAILED"))
        _write_checklist(inbox, [{"manifest_entry_id": str(entry.get("manifest_entry_id", "")), "report_path": str(entry.get("report_path", "")), "source_html_sha256": str(entry.get("source_report_sha256", "")), "status": "FAILED", "safe_to_delete": "NO", "cleanup_state": "RETAIN", "deleted_at_utc": ""} for entry in manifest["entries"]])
        raise PerformanceImportError("database initialization failed") from error
    try:
        with duckdb.connect(str(request.database)) as connection:
            connection.execute("begin")
            imported, skipped = _write_import_rows(connection, import_id, prepared, manifest)
            connection.execute("commit")
        _verify_readback(request.database, prepared, import_id)
    except Exception as error:
        _write_audit(inbox, _base_audit(manifest, import_id, audit_entries, "FAILED"))
        raise error if isinstance(error, PerformanceImportError) else PerformanceImportError(str(error)) from error
    for audit_entry, item in zip(audit_entries, prepared, strict=True):
        audit_entry.update({
            "status": item["import_status"],
            "source_html_sha256": item["source_hash"],
            "test_run_id": item["test_run_id"],
            "result_payload_sha256": item["payload_hash"],
            "action_count": len(item["parsed"].actions),
            "equity_sample_count": len(item["parsed"].equity_series),
        })
    _write_audit(inbox, _base_audit(manifest, import_id, audit_entries, "COMMITTED"))
    _write_checklist(inbox, [{"manifest_entry_id": str(item["entry"]["manifest_entry_id"]), "report_path": str(item["entry"]["report_path"]), "source_html_sha256": str(item["source_hash"]), "status": item["import_status"], "safe_to_delete": "YES", "cleanup_state": "DELETE_READY", "deleted_at_utc": ""} for item in prepared])
    return PerformanceImportResult(import_id, imported, skipped)


def resume_performance_cleanup(request: PerformanceImportRequest) -> None:
    inbox = Path(request.inbox)
    checklist = inbox / "html_delete_checklist.v4.csv"
    with checklist.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    audit = _read_json(inbox / "import_audit.v4.json")
    if not isinstance(audit, dict) or audit.get("schema_version") != 4 or audit.get("status") != "COMMITTED" or audit.get("quarantine_count") != 0:
        raise PerformanceImportError("cleanup requires valid v4 audit evidence")
    audit_entries = {str(entry.get("manifest_entry_id")): entry for entry in audit.get("entries", []) if isinstance(entry, dict)}
    eligible: list[tuple[dict[str, str], Path]] = []
    try:
        with duckdb.connect(str(request.database), read_only=True) as connection:
            schema = connection.execute("select value from schema_info where key = 'schema_version'").fetchone()
            if schema is None or schema[0] != "1":
                raise PerformanceImportError("cleanup requires schema v1 readback evidence")
            for row in rows:
                if row["safe_to_delete"] != "YES" or row["cleanup_state"] not in {"DELETE_READY", "DELETING", "DELETED"}:
                    continue
                report = _safe_report_path(inbox, row["report_path"])
                evidence = audit_entries.get(row["manifest_entry_id"])
                if evidence is None or evidence.get("status") not in {"IMPORTED", "SKIPPED"} or evidence.get("source_html_sha256") != row["source_html_sha256"]:
                    raise PerformanceImportError("cleanup requires matching audit evidence")
                db_row = connection.execute("select source_html_sha256, status, safe_to_delete, cleanup_state from import_files where import_id = ? and manifest_entry_id = ?", [audit["import_id"], row["manifest_entry_id"]]).fetchone()
                if db_row is None or db_row[0] != row["source_html_sha256"] or db_row[1] not in {"IMPORTED", "SKIPPED"} or not db_row[2] or db_row[3] not in {"DELETE_READY", "DELETING", "DELETED"}:
                    raise PerformanceImportError("cleanup requires database readback evidence")
                eligible.append((row, report))
    except PerformanceImportError:
        raise
    except Exception as error:
        raise PerformanceImportError("cleanup database readback failed") from error
    for row in rows:
        matching = next((item for item in eligible if item[0] is row), None)
        if matching is None:
            continue
        report = matching[1]
        with duckdb.connect(str(request.database), read_only=True) as readback:
            db_state = readback.execute(
                "select cleanup_state from import_files where import_id = ? and manifest_entry_id = ?",
                [audit["import_id"], row["manifest_entry_id"]],
            ).fetchone()[0]
        if db_state == "DELETING" and row["cleanup_state"] == "DELETED" and not report.exists():
            deleted_at = row["deleted_at_utc"] or datetime.now(timezone.utc).isoformat()
            with duckdb.connect(str(request.database)) as connection:
                connection.execute(
                    "update import_files set cleanup_state='DELETED', deleted_at_utc=? where import_id=? and manifest_entry_id=?",
                    [deleted_at, audit["import_id"], row["manifest_entry_id"]],
                )
            continue
        if not report.is_file() or _file_sha256(report) != row["source_html_sha256"]:
            raise PerformanceImportError("report hash mismatch immediately before delete")
        row["cleanup_state"] = "DELETING"
        _write_checklist(inbox, rows)
        with duckdb.connect(str(request.database)) as connection:
            connection.execute("update import_files set cleanup_state = 'DELETING' where import_id = ? and manifest_entry_id = ?", [audit["import_id"], row["manifest_entry_id"]])
        report.unlink(missing_ok=True)
        row["cleanup_state"] = "DELETED"
        row["deleted_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_checklist(inbox, rows)
        with duckdb.connect(str(request.database)) as connection:
            connection.execute("update import_files set cleanup_state = 'DELETED', deleted_at_utc = ? where import_id = ? and manifest_entry_id = ?", [row["deleted_at_utc"], audit["import_id"], row["manifest_entry_id"]])
