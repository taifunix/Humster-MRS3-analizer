"""Deterministic, fixture-only Portfolio export and decision replay.

Export consumes the immutable M7 result and, when supplied, the durable M6
readback.  It never starts a tester, opens a network connection, or emits a
live trading command.  Publication stages into a sibling directory and keeps
the prior target in a recoverable sibling backup until replacement succeeds.
There is a process-crash window after moving the prior target and before
installing the staging directory; ordinary exceptions restore the backup.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .integration import IntegrationResult
from .search import Variant, canonical_candidate_identity


READY = "READY"
RESEARCH_ONLY = "RESEARCH_ONLY"
NEEDS_RETEST = "NEEDS_RETEST"
NEEDS_RESCREEN = "NEEDS_RESCREEN"

_SECRET_WORDS = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey")
_COMMAND_WORDS = {"command", "commands", "execute", "live_mode", "live_execution", "bot_execution"}
_SECRET_VALUE_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])(?:api[_ -]?(?:key|token)|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|credential)[ \t]*[:=][ \t]*\S+")
_COMMAND_VALUE_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])(?:command|live[_ -]?(?:mode|execution|command)|bot[_ -]?execution|(?:order|trade)[_ -]?command)[ \t]*[:=][ \t]*\S+|(?:^|[ \t])--(?:live|execute|trade|order)(?:$|[ \t])")
_SECRET_LITERAL_VALUES = frozenset({"SECRET"})
_SAFE_SYMBOL = re.compile(r"[A-Za-z0-9._-]+\Z")


class ExportError(ValueError):
    """Export or replay evidence cannot be trusted."""


def _safe_string(value: str) -> str:
    stripped = value.strip()
    if (
        _SECRET_VALUE_RE.search(value)
        or _COMMAND_VALUE_RE.search(value)
        or stripped in _SECRET_LITERAL_VALUES
    ):
        return "[REDACTED]"
    return value


def _safe(value: Any) -> Any:
    """Convert supported values to deterministic JSON values and drop secrets."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExportError("non-finite decimal cannot be exported")
        return format(value, "f")
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value):
        return _safe({item.name: getattr(value, item.name) for item in fields(value)})
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.casefold()
            if any(word in lowered for word in _SECRET_WORDS) or any(word in lowered for word in _COMMAND_WORDS):
                continue
            result[name] = _safe(item)
        return {name: result[name] for name in sorted(result)}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise ExportError("unordered values cannot be exported")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportError("non-finite float cannot be exported")
        return repr(value)
    if isinstance(value, str):
        return _safe_string(value)
    if value is None or isinstance(value, (int, bool)):
        return value
    raise ExportError(f"unsupported export value: {type(value).__name__}")


def canonical_export_json(value: Any) -> str:
    """Return canonical JSON used for every export digest and file."""
    try:
        return json.dumps(_safe(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ExportError("export value is not canonical JSON") from error


def _comparison_json(value: Any) -> str:
    """Canonical settings comparison that retains every tested input field."""
    def plain(item: Any) -> Any:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ExportError("non-finite decimal cannot be compared")
            return format(item, "f")
        if isinstance(item, datetime):
            parsed = item if item.tzinfo else item.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(item, Path):
            return item.as_posix()
        if is_dataclass(item):
            return plain({field.name: getattr(item, field.name) for field in fields(item)})
        if isinstance(item, Mapping):
            return {str(key): plain(value) for key, value in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (tuple, list)):
            return [plain(value) for value in item]
        if isinstance(item, (set, frozenset)):
            raise ExportError("unordered settings cannot be compared")
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ExportError("non-finite float cannot be compared")
            return repr(item)
        if item is None or isinstance(item, (str, int, bool)):
            return item
        raise ExportError(f"unsupported settings value: {type(item).__name__}")
    try:
        return json.dumps(plain(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ExportError("settings are not comparable") from error


def export_digest(value: Any) -> str:
    return hashlib.sha256(canonical_export_json(value).encode("utf-8")).hexdigest()


def composition_digest(composition: Any) -> str:
    """Digest PortfolioSet members together with their exact loads."""
    if composition is None:
        composition = {"members": []}
    return export_digest({"contract": "portfolio_set_composition_v1", "composition": composition})


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class PortfolioExport:
    output_dir: Path
    status: str
    reasons: tuple[str, ...]
    manifest_digest: str
    composition_digest: str
    artifacts: Mapping[str, ExportArtifact]
    manifest: Mapping[str, Any]
    evaluation_id: str | None = None

    @property
    def output_path(self) -> Path:
        return self.output_dir

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "manifest.json"

    @property
    def run_id(self) -> str | None:
        return _first(self.manifest, "run_id", default=None)

    @property
    def portfolio_set_id(self) -> str | None:
        return _first(self.manifest, "portfolio_set_id", default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "status": self.status,
            "reasons": list(self.reasons),
            "manifest_digest": self.manifest_digest,
            "composition_digest": self.composition_digest,
            "artifacts": {name: item.as_dict() for name, item in self.artifacts.items()},
            "manifest": self.manifest,
            "evaluation_id": self.evaluation_id,
        }


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        found = _get(value, key, None)
        if found is not None:
            return found
    return default


def _invoke(callback: Callable[..., Any], *values: Any) -> Any:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(values[0])
    parameters = tuple(signature.parameters.values())
    positional = [item for item in parameters if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)]
    arguments = values if any(item.kind == item.VAR_POSITIONAL for item in parameters) else values[: len(positional)]
    signature.bind(*arguments)
    return callback(*arguments)


def _candidate_payload(candidate: Any, *, side: str | None = None, variant: Variant | None = None) -> dict[str, Any]:
    if isinstance(candidate, Mapping):
        payload = dict(candidate)
    else:
        payload = {item.name: getattr(candidate, item.name) for item in fields(candidate)} if is_dataclass(candidate) else {"value": candidate}
    payload["candidate_id"] = str(_first(candidate, "strategy_id", "strategyId", "id", default=canonical_candidate_identity(candidate)))
    payload["result_id"] = _first(candidate, "result_id", "resultId", default=None)
    if side:
        payload["side"] = side
    if variant is not None:
        direction = variant.directions.get(side or "")
        if direction is not None:
            payload.update({
                "scalar": direction.scalar,
                "rounded_quantity": direction.quantity,
                "orders": direction.orders,
                "geometry": direction.geometry,
                "lot_x": direction.lot_x,
            })
        payload["priority"] = variant.priority
        payload["limiter"] = variant.limiter
    return payload


def _strategy_payloads(result: IntegrationResult, selected_variants: Iterable[Any] | None) -> dict[str, dict[str, Any]]:
    variants = tuple(selected_variants or ())
    if not variants:
        variants = tuple(
            attempt.variant for attempt in result.attempts
            if attempt.identity == result.selected and attempt.variant is not None
        )
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for variant in variants:
        if isinstance(variant, Variant):
            symbol = str(variant.slot.symbol)
            sides = tuple(sorted(variant.directions))
            legs = {
                side: _candidate_payload(getattr(variant.slot, side.casefold()), side=side, variant=variant)
                for side in sides
            }
            payload = {
                "schema": "portfolio_strategy_v1",
                "symbol": symbol,
                "composition": variant.composition,
                "scalar_pct": variant.scalar,
                "priority": variant.priority,
                "limiter": variant.limiter,
                "candidate_ids": {side: legs[side]["candidate_id"] for side in sides},
                "result_ids": {side: legs[side]["result_id"] for side in sides},
                "legs": legs,
            }
        else:
            symbol = str(_first(variant, "symbol", default=""))
            if symbol:
                payload = {
                    "schema": "portfolio_strategy_v1",
                    "symbol": symbol,
                    "candidate_ids": {"candidate": canonical_candidate_identity(variant)},
                    "candidate": variant,
                }
            else:
                continue
        grouped.setdefault(symbol, []).append((export_digest(payload), payload))
    payloads: dict[str, dict[str, Any]] = {}
    for symbol, entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        if len(entries) == 1:
            payloads[symbol] = entries[0][1]
        else:
            slots = [
                {"slot_id": f"{digest}:{index}", **payload}
                for index, (digest, payload) in enumerate(entries)
            ]
            payloads[symbol] = {
                "schema": "portfolio_strategy_v1",
                "symbol": symbol,
                "slot_count": len(entries),
                "candidate_ids": [slot["candidate_ids"] for slot in slots],
                "result_ids": [slot["result_ids"] for slot in slots],
                "slots": slots,
            }
    for symbol, payload in payloads.items():
        accounts = []
        for account_id, account in sorted(result.accounts.items(), key=lambda item: str(item[0])):
            if not isinstance(account, Mapping):
                continue
            account_symbol = _first(account, "symbol", default=None)
            if account_symbol is None:
                continue
            if str(account_symbol) != symbol:
                continue
            accounts.append({**dict(account), "account_id": str(account_id)})
        if accounts:
            payload["accounts"] = accounts
    return {symbol: payloads[symbol] for symbol in sorted(payloads)}


def _validate_symbol(symbol: str) -> None:
    if ".." in symbol or _SAFE_SYMBOL.fullmatch(symbol) is None:
        raise ExportError("strategy symbol is unsafe for a filename")


def _reference_map(reference: Any, symbols: Sequence[str]) -> Mapping[str, Any]:
    if not isinstance(reference, Mapping):
        return {}
    if isinstance(reference.get("symbols"), Mapping):
        return reference["symbols"]
    return {symbol: reference.get(symbol) for symbol in symbols}


def _reference_gate(reference: Any, symbols: Sequence[str], now_ms: int | None) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    values = _reference_map(reference, symbols)
    reasons: list[str] = []
    if now_ms is None:
        reasons.append("REFERENCE_CLOCK_UNAVAILABLE")
    if not values or any(values.get(symbol) is None for symbol in symbols):
        reasons.append("REFERENCE_MISSING")
        unique = tuple(dict.fromkeys(reasons))
        return "UNKNOWN", {"status": "UNKNOWN", "reason": unique[0], "reasons": unique}, unique
    witness: dict[str, Any] = {}
    for symbol in symbols:
        item = values[symbol]
        if not isinstance(item, Mapping):
            reasons.append("REFERENCE_QUALITY_UNKNOWN")
            continue
        captured = _first(item, "captured_at_ms", "timestamp_ms", "captured_at")
        expires = _first(item, "expires_at_ms", "expiry_ms", "expires_at")
        def milliseconds(value: Any) -> int:
            if isinstance(value, datetime):
                return int(value.timestamp() * 1000)
            if isinstance(value, str):
                try:
                    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
                except ValueError:
                    pass
            return int(value)
        try:
            fresh = now_ms is not None and milliseconds(captured) <= now_ms <= milliseconds(expires)
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            reasons.append("REFERENCE_STALE")
        quality_value = _first(item, "quality", "status", default="UNKNOWN")
        if isinstance(quality_value, Mapping):
            quality_value = _first(quality_value, "status", "value", default="UNKNOWN")
        quality = str(quality_value).upper()
        if quality != "PASS":
            reasons.append("REFERENCE_QUALITY_INSUFFICIENT" if quality == "FAIL" else "REFERENCE_QUALITY_UNKNOWN")
        witness[symbol] = item
    unique = tuple(dict.fromkeys(reasons))
    return ("PASS" if not unique else "UNKNOWN"), {"status": "PASS" if not unique else "UNKNOWN", "symbols": witness, "reason": unique[0] if unique else None}, unique


def _liquidity_gate(value: Any, symbols: Sequence[str]) -> tuple[str, Mapping[str, Any], tuple[str, ...]]:
    def record(status: str, reasons: Sequence[str], evidence: Any, **extra: Any) -> tuple[str, Mapping[str, Any], tuple[str, ...]]:
        unique = tuple(dict.fromkeys(str(reason) for reason in reasons if reason))
        return status, {"status": status, "reason": unique[0] if unique else None, "reasons": unique, "evidence": _safe(evidence), **extra}, unique

    if value is None:
        return record("UNKNOWN", ("GLOBAL_LIQUIDITY_NOT_EVALUATED",), value)
    if isinstance(value, Mapping):
        reasons: list[str] = []
        if value.get("stale") is True or value.get("fresh") is False:
            reasons.append("LIQUIDITY_STALE")
        quality = _first(value, "quality", "quality_status", default=None)
        if isinstance(quality, Mapping):
            quality = _first(quality, "status", "value", default=None)
        if quality is not None and str(quality).upper() != "PASS":
            reasons.append("LIQUIDITY_QUALITY_INSUFFICIENT")
        if reasons:
            return record("UNKNOWN", reasons, value)
        status = str(value.get("status", "UNKNOWN")).upper()
        if status == "PASS":
            return record("PASS", (), value)
        reason = str(value.get("reason") or ("GLOBAL_LIQUIDITY_FAILED" if status == "FAIL" else "GLOBAL_LIQUIDITY_UNKNOWN"))
        return record("FAIL" if status == "FAIL" else "UNKNOWN", (reason,), value)
    if value is True:
        return record("PASS", (), value, symbols=list(symbols))
    return record("FAIL", ("GLOBAL_LIQUIDITY_FAILED",), value)


def _metrics_from_store(store: Any, result: IntegrationResult, execution_facts: Any, attempt_id: str | None = None) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    if execution_facts is not None:
        if not isinstance(execution_facts, Mapping):
            raise ExportError("execution_facts must be a mapping")
        return execution_facts.get("metrics", execution_facts), execution_facts
    if store is None or not result.run_id or not callable(getattr(store, "read_portfolio_run", None)):
        return None, None
    try:
        persisted = store.read_portfolio_run(result.run_id, attempt_id) if attempt_id is not None else store.read_portfolio_run(result.run_id)
    except Exception:
        return None, {"status": "UNREADABLE"}
    if not persisted:
        return None, None
    return persisted.get("metrics"), persisted


def _durable_portfolio_run(store: Any, result: IntegrationResult, attempt_id: str | None) -> Mapping[str, Any] | None:
    if store is None or not result.run_id or not callable(getattr(store, "read_portfolio_run", None)):
        return None
    try:
        return store.read_portfolio_run(result.run_id, attempt_id) if attempt_id is not None else store.read_portfolio_run(result.run_id)
    except Exception:
        return {"status": "UNREADABLE"}


def _json_payload(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        return parsed
    return value


def _stored_validation(store: Any, result: IntegrationResult) -> Mapping[str, Any]:
    if store is None or not result.evaluation_id or not callable(getattr(store, "get_evaluation", None)):
        return {"status": "UNAVAILABLE", "source": "Portfolio DB"}
    try:
        row = store.get_evaluation(result.evaluation_id)
    except Exception:
        return {"status": "UNAVAILABLE", "source": "Portfolio DB"}
    payload = _json_payload(row.get("payload")) if isinstance(row, Mapping) else None
    if not isinstance(payload, Mapping):
        return {"status": "UNAVAILABLE", "source": "Portfolio DB"}
    return {
        "status": "PERSISTED",
        "source": "Portfolio DB",
        "validation_passes": payload.get("validation_passes", ()),
        "attempts": payload.get("attempts", ()),
    }


def _summary_metrics(metrics: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(metrics, Mapping):
        return {}
    entries = tuple(value for value in metrics.values() if isinstance(value, Mapping))
    if not entries:
        return metrics
    result: dict[str, Any] = {}
    for name in ("initial_equity", "final_equity", "primary_result", "realized_pnl", "actual_drawdown"):
        values = [value.get(name) for value in entries if value.get(name) is not None]
        if values:
            try:
                result[name] = sum((Decimal(str(value)) for value in values), Decimal("0"))
            except Exception:
                pass
    values = [value.get("actual_drawdown_pct") for value in entries if value.get("actual_drawdown_pct") is not None]
    if values:
        try:
            result["actual_drawdown_pct"] = max(Decimal(str(value)) for value in values)
        except Exception:
            pass
    result["coverage"] = "COMPLETE" if all(value.get("coverage") == "COMPLETE" for value in entries) else "PARTIAL"
    return result


def _report_value(value: Any) -> Any:
    if value is None:
        return "UNAVAILABLE"
    if isinstance(value, str) and not value.strip():
        return "UNAVAILABLE"
    if isinstance(value, (Mapping, tuple, list)) and not value:
        return "UNAVAILABLE"
    return value


def _canonical_account_cap(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return "bool", str(value).casefold()
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return "text", str(value)
    if not number.is_finite():
        return "text", str(value)
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"", "-0"}:
        normalized = "0"
    return "number", normalized


def _account_cap(account_values: Sequence[Mapping[str, Any]]) -> tuple[Any, bool]:
    values = [
        _first(account, "max_balance", "cap", default=None)
        for account in account_values
    ]
    values = [value for value in values if value is not None]
    if not values:
        return None, False
    canonical = [_canonical_account_cap(value) for value in values]
    if len(set(canonical)) != 1:
        return None, True
    return canonical[0][1], False


def _execution_metric_retest(value: Any, key: str | None = None) -> tuple[bool, tuple[str, ...]]:
    if isinstance(value, Mapping):
        reasons: list[str] = []
        status = str(value.get("status", "")).upper()
        if status == NEEDS_RETEST:
            reason = value.get("reason")
            reasons.append(str(reason) if reason is not None else ("LEVERAGE_MISMATCH" if key == "leverage" else NEEDS_RETEST))
        found = status == NEEDS_RETEST
        for child_key, child in value.items():
            child_found, child_reasons = _execution_metric_retest(child, str(child_key))
            found = found or child_found
            reasons.extend(child_reasons)
        return found, tuple(dict.fromkeys(reasons))
    if isinstance(value, (tuple, list)):
        found = False
        reasons: list[str] = []
        for child in value:
            child_found, child_reasons = _execution_metric_retest(child, key)
            found = found or child_found
            reasons.extend(child_reasons)
        return found, tuple(dict.fromkeys(reasons))
    return False, ()


def _persist_evaluation(
    store: Any,
    result: IntegrationResult,
    plan: tuple[str, str, Mapping[str, Any], Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    evaluation_id, decision_id, decision_content, evaluation_content = plan
    try:
        store.create_campaign(decision_id, export_digest(decision_content), decision_content)
        store.create_evaluation(
            evaluation_id,
            result.run_id,
            decision_id,
            evaluation_content,
            execution_campaign_id=result.execution_campaign_id,
        )
        return evaluation_id, decision_id
    except Exception as error:
        raise ExportError("evaluation refresh could not be persisted") from error


def _evaluation_plan(
    store: Any,
    result: IntegrationResult,
    reference: Any,
    gates: Mapping[str, Any],
    status: str,
    reasons: Sequence[str],
    attempt_id: str | None,
) -> tuple[str, str, Mapping[str, Any], Mapping[str, Any]] | None:
    if store is None or not result.run_id or not result.execution_campaign_id or not result.decision_campaign_id:
        return None
    if reference is None or _first(gates.get("exchange_reference"), "status", default="UNKNOWN") != "PASS":
        return None
    safe_reference = _safe(reference)
    safe_gates = _safe(gates)
    safe_reasons = _safe(tuple(reasons))
    digest = export_digest({"reference": safe_reference, "gates": safe_gates, "status": status, "reasons": safe_reasons, "attempt_id": attempt_id})
    try:
        base_row = store.get_campaign(result.decision_campaign_id)
    except Exception as error:
        raise ExportError("decision campaign lookup failed") from error
    base = _json_payload(base_row.get("content", {})) if isinstance(base_row, Mapping) else {}
    decision_content = {"base_decision_campaign_id": result.decision_campaign_id, "reference_digest": digest, "reference": safe_reference, "base": _safe(base)}
    decision_digest = export_digest(decision_content)
    decision_id = f"decision-{decision_digest[:32]}"
    evaluation_content = {"status": status, "reasons": safe_reasons, "reference_digest": digest, "gates": safe_gates, "base_evaluation_id": result.evaluation_id, "attempt_id": attempt_id}
    evaluation_identity = {"run_id": result.run_id, "decision": decision_id, "content": evaluation_content}
    evaluation_id = f"evaluation-{export_digest(evaluation_identity)[:32]}"
    return evaluation_id, decision_id, decision_content, evaluation_content


def _write(path: Path, value: Any, root: Path) -> ExportArtifact:
    payload = (canonical_export_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ExportArtifact(path.relative_to(root).as_posix(), hashlib.sha256(payload).hexdigest(), len(payload))


def _write_text(path: Path, value: str, root: Path) -> ExportArtifact:
    payload = value.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ExportArtifact(path.relative_to(root).as_posix(), hashlib.sha256(payload).hexdigest(), len(payload))


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _publish_staging(staging: Path, target: Path) -> None:
    """Replace target while retaining a rollback copy until replacement succeeds."""
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.backup-", dir=target.parent))
        backup.rmdir()
        target.replace(backup)
    try:
        staging.replace(target)
    except Exception:
        if backup is not None and (backup.exists() or backup.is_symlink()):
            _remove_path(target)
            backup.replace(target)
        raise
    if backup is not None:
        try:
            _remove_path(backup)
        except OSError:
            # The new target and durable rows are already published consistently.
            pass


def _readback_attempt_id(value: Any) -> str | None:
    attempt = _first(value, "attempt_id", default=None)
    if attempt is None:
        payload = _json_payload(_get(value, "payload", None))
        attempt = _first(payload, "attempt_id", default=None)
    return str(attempt) if attempt is not None else None


def _store_row_exists(store: Any, getter_name: str, row_id: str, *, table: str | None = None, key: str | None = None) -> bool | None:
    getter = getattr(store, getter_name, None)
    if callable(getter):
        try:
            return getter(row_id) is not None
        except Exception:
            return None
    database_path = getattr(store, "path", None)
    if table is None or key is None or database_path is None:
        return None
    try:
        import duckdb
        with duckdb.connect(str(database_path), read_only=True) as database:
            return database.execute(f"SELECT 1 FROM {table} WHERE {key} = ? LIMIT 1", [row_id]).fetchone() is not None
    except Exception:
        return None


def _delete_store_row(store: Any, kind: str, row_id: str) -> None:
    deleter = getattr(store, f"delete_{kind}", None)
    if callable(deleter):
        deleter(row_id)
        return
    publisher = getattr(store, "_publish", None)
    if not callable(publisher):
        return

    if kind == "portfolio_set":
        for table in ("portfolio_set_series", "portfolio_set_members", "portfolio_sets"):
            publisher(lambda database, table=table: database.execute(f"DELETE FROM {table} WHERE portfolio_set_id = ?", [row_id]))
        return

    def delete(database: Any) -> None:
        if kind == "evaluation":
            database.execute("DELETE FROM evaluations WHERE evaluation_id = ?", [row_id])
        elif kind == "campaign":
            database.execute("DELETE FROM campaigns WHERE campaign_id = ?", [row_id])

    publisher(delete)


def _cleanup_store_rows(store: Any, rows: Iterable[tuple[str, str, bool | None]]) -> None:
    for kind, row_id, was_present in rows:
        if was_present is not False:
            continue
        try:
            _delete_store_row(store, kind, row_id)
        except Exception:
            pass


def _persisted_executable_identity(source: Any, run_id: str, attempt_id: str | None) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Read the selected run and its stored executable identity."""
    try:
        readback = source.read_portfolio_run(run_id, attempt_id) if attempt_id is not None else source.read_portfolio_run(run_id)
    except Exception:
        return {"status": "UNREADABLE"}, None
    identity = readback.get("executable_identity") if isinstance(readback, Mapping) else None
    if isinstance(identity, str):
        identity = _json_payload(identity)
    if isinstance(identity, Mapping):
        return readback, identity
    database_path = getattr(source, "path", None)
    if database_path is None:
        return readback, None
    try:
        import duckdb
        with duckdb.connect(str(database_path), read_only=True) as database:
            selected_attempt = attempt_id
            if selected_attempt is None:
                row = database.execute(
                    "SELECT attempt_id FROM portfolio_runs WHERE run_id = ? ORDER BY created_at_utc DESC LIMIT 1",
                    [run_id],
                ).fetchone()
                selected_attempt = str(row[0]) if row is not None else None
            if selected_attempt is None:
                return readback, None
            row = database.execute(
                "SELECT executable_identity FROM portfolio_runs WHERE run_id = ? AND attempt_id = ?",
                [run_id, selected_attempt],
            ).fetchone()
    except Exception:
        return readback, None
    if row is None:
        return readback, None
    identity = _json_payload(row[0])
    return readback, identity if isinstance(identity, Mapping) else None


def _tick_replay(identity: Mapping[str, Any] | None, persisted: Mapping[str, Any] | None) -> dict[str, Any]:
    if persisted is None:
        return {"available": False, "reason": "EXECUTION_EVIDENCE_MISSING"}
    if str(persisted.get("status", "")).upper() == "UNREADABLE":
        return {"available": False, "reason": "EXECUTION_EVIDENCE_UNREADABLE"}
    if str(persisted.get("status", "")).upper() != "COMMITTED":
        return {"available": False, "reason": "EXECUTION_EVIDENCE_INCOMPLETE"}
    if not isinstance(identity, Mapping):
        return {"available": False, "reason": "BINARY_OR_TICKS_ABSENT"}
    binary = _first(identity, "binary_identity", "binary", default=None)
    ticks = _first(identity, "tick_identity", "ticks", default=None)
    if not binary or not ticks:
        return {"available": False, "reason": "BINARY_OR_TICKS_ABSENT"}
    artifacts = _first(identity, "artifacts", "tick_artifacts", default=None)
    if not artifacts:
        return {"available": False, "reason": "BINARY_OR_TICKS_ABSENT"}
    paths = []
    for artifact in artifacts if isinstance(artifacts, (list, tuple)) else (artifacts,):
        paths.append(_first(artifact, "path", default=artifact) if isinstance(artifact, Mapping) else artifact)
    try:
        available = bool(paths) and all(Path(path).is_file() for path in paths)
    except (TypeError, ValueError, OSError):
        available = False
    if not available:
        return {"available": False, "reason": "BINARY_OR_TICKS_ABSENT"}
    return {"available": True, "reason": None}


def export_portfolio(
    result: IntegrationResult,
    output_dir: str | Path,
    *,
    store: Any = None,
    reference: Mapping[str, Any] | None = None,
    refresh_reference: Callable[..., Any] | None = None,
    reference_refresh: Callable[..., Any] | None = None,
    shared_liquidity: Any = None,
    global_liquidity: Any = None,
    portfolio_set: Any = None,
    composition: Any = None,
    previous_composition: Any = None,
    tested_settings: Mapping[str, Any] | None = None,
    exported_settings: Mapping[str, Any] | None = None,
    validated_settings: Mapping[str, Any] | None = None,
    export_settings: Mapping[str, Any] | None = None,
    execution_facts: Mapping[str, Any] | None = None,
    selected_variants: Iterable[Any] | None = None,
    attempt_id: str | int | None = None,
    now_ms: int | None = None,
) -> PortfolioExport:
    """Write one deterministic fixture export package for an M7 result."""
    if not isinstance(result, IntegrationResult):
        raise ExportError("IntegrationResult is required")
    target = Path(output_dir).resolve()
    tested_settings = tested_settings if tested_settings is not None else validated_settings
    exported_settings = exported_settings if exported_settings is not None else export_settings
    variants = tuple(selected_variants or ())
    strategies = _strategy_payloads(result, variants)
    symbols = tuple(str(name) for name in strategies)
    if not symbols:
        raise ExportError("selected strategy evidence is missing")
    folded_symbols: dict[str, str] = {}
    for symbol in symbols:
        _validate_symbol(symbol)
        folded = symbol.casefold()
        previous = folded_symbols.setdefault(folded, symbol)
        if previous != symbol:
            raise ExportError("strategy symbols collide case-insensitively")
    # The tested payload is the source of truth for export.  A supplied
    # executable payload is compared below but never silently substituted.
    symbol_keyed_settings = isinstance(tested_settings, Mapping) and any(symbol in tested_settings for symbol in symbols)
    for symbol, payload in strategies.items():
        if symbol_keyed_settings:
            if isinstance(tested_settings, Mapping) and symbol in tested_settings:
                payload["settings"] = tested_settings[symbol]
        elif tested_settings is not None:
            payload["settings"] = tested_settings
    resolved_attempt_id = str(attempt_id) if attempt_id is not None else None
    now = now_ms
    refresher = refresh_reference or reference_refresh
    if refresher is not None:
        try:
            reference = _invoke(refresher, symbols, now)
        except Exception as error:
            reference = None
            refresh_error = str(error)
        else:
            refresh_error = None
    else:
        refresh_error = None
    reference_status, reference_gate, reference_reasons = _reference_gate(reference, symbols, now)
    liquidity_value = global_liquidity if global_liquidity is not None else shared_liquidity
    if callable(liquidity_value):
        try:
            liquidity_value = _invoke(liquidity_value, strategies, result.accounts)
        except Exception:
            liquidity_value = {"status": "UNKNOWN", "reason": "GLOBAL_LIQUIDITY_UNKNOWN"}
    liquidity_status, liquidity_gate, liquidity_reasons = _liquidity_gate(liquidity_value, symbols)
    if refresh_error:
        reference_reasons = (*reference_reasons, "REFERENCE_REQUEST_FAILED")
        gate_reasons = tuple(dict.fromkeys((*reference_gate.get("reasons", ()), "REFERENCE_REQUEST_FAILED")))
        reference_gate = {**reference_gate, "reason": "REFERENCE_REQUEST_FAILED", "reasons": gate_reasons}

    settings_unverified = tested_settings is None or exported_settings is None
    settings_changed = (
        not settings_unverified
        and _comparison_json(tested_settings) != _comparison_json(exported_settings)
    )
    if not settings_unverified:
        settings_unverified = any(
            not isinstance(settings, Mapping) or any(symbol not in settings for symbol in symbols)
            for settings in (tested_settings, exported_settings)
        )
    metrics, persisted = _metrics_from_store(store, result, execution_facts, resolved_attempt_id)
    execution_metric_retest, execution_metric_reasons = _execution_metric_retest(metrics)
    durable_persisted = _durable_portfolio_run(store, result, resolved_attempt_id) if execution_facts is not None else persisted
    if resolved_attempt_id is None:
        resolved_attempt_id = _readback_attempt_id(durable_persisted) or _readback_attempt_id(persisted)
    execution_source = "injected_facts" if execution_facts is not None else "portfolio_db"
    validation_facts = _stored_validation(store, result)
    evidence_reasons: list[str] = []
    if durable_persisted is None:
        evidence_reasons.append("EXECUTION_EVIDENCE_MISSING")
    elif str(durable_persisted.get("status", "")).upper() == "UNREADABLE":
        evidence_reasons.append("EXECUTION_EVIDENCE_UNREADABLE")
    elif str(durable_persisted.get("status", "")).upper() != "COMMITTED":
        evidence_reasons.append("EXECUTION_EVIDENCE_INCOMPLETE")
    if execution_facts is not None and str(persisted.get("status", "")).upper() != "COMMITTED":
        evidence_reasons.append("EXECUTION_EVIDENCE_INCOMPLETE")
    if settings_changed:
        evidence_reasons.append("EXECUTABLE_PAYLOAD_CHANGED")
    if settings_unverified:
        evidence_reasons.append("EXECUTABLE_PAYLOAD_UNVERIFIED")
    evidence_reasons.extend(execution_metric_reasons)

    selected_composition = composition if composition is not None else portfolio_set
    selected_composition = selected_composition if selected_composition is not None else {"members": [{"portfolio_id": result.evaluation_id or result.run_id, "load": "1"}]}
    set_digest = composition_digest(selected_composition)
    composition_reasons: tuple[str, ...] = ()
    if previous_composition is not None and composition_digest(previous_composition) != set_digest:
        composition_reasons = ("PORTFOLIO_SET_CHANGED",)
    execution_status = "PASS" if durable_persisted is not None and str(durable_persisted.get("status", "")).upper() == "COMMITTED" and (execution_facts is None or str(persisted.get("status", "")).upper() == "COMMITTED") else "UNKNOWN"
    execution_reason = None if execution_status == "PASS" else (
        "EXECUTION_EVIDENCE_MISSING"
        if durable_persisted is None
        else "EXECUTION_EVIDENCE_UNREADABLE"
        if str(durable_persisted.get("status", "")).upper() == "UNREADABLE"
        else "EXECUTION_EVIDENCE_INCOMPLETE"
    )
    settings_reason = "EXECUTABLE_PAYLOAD_UNVERIFIED" if settings_unverified else ("EXECUTABLE_PAYLOAD_CHANGED" if settings_changed else None)
    gates = {
        "exchange_reference": reference_gate,
        "shared_liquidity": liquidity_gate,
        "execution": {"status": execution_status, "reason": execution_reason, "source": execution_source, "run_id": result.run_id, "attempt_id": resolved_attempt_id, "evaluation_id": result.evaluation_id},
        "settings": {"status": "NEEDS_RETEST" if settings_changed or settings_unverified else "PASS", "reason": settings_reason, "tested": tested_settings, "exported": exported_settings},
    }
    reasons = tuple(dict.fromkeys((*result.reasons, *reference_reasons, *liquidity_reasons, *evidence_reasons, *composition_reasons)))
    settings_require_retest = settings_changed or settings_unverified
    if settings_require_retest or execution_metric_retest or any(reason in {"LEVERAGE_MISMATCH", "LEVERAGE_UNVERIFIED", "NEEDS_RETEST"} for reason in reasons):
        status = NEEDS_RETEST
    elif composition_reasons:
        status = NEEDS_RESCREEN
    else:
        status = RESEARCH_ONLY
    liquidity_blocked = liquidity_status != "PASS"
    leverage_blocked = any(reason in {"LEVERAGE_MISMATCH", "LEVERAGE_UNVERIFIED"} for reason in evidence_reasons)
    existing_blocker = execution_metric_retest or any(
        reason in {"LEVERAGE_MISMATCH", "LEVERAGE_UNVERIFIED", "EXECUTABLE_PAYLOAD_CHANGED", "EXECUTABLE_PAYLOAD_UNVERIFIED", "EXECUTION_EVIDENCE_MISSING", "EXECUTION_EVIDENCE_INCOMPLETE", "NEEDS_RETEST"}
        for reason in reasons
    )
    can_refresh_evaluation = (
        reference_status == "PASS"
        and not liquidity_blocked
        and execution_status == "PASS"
        and not settings_changed
        and not settings_unverified
        and not execution_metric_retest
        and not leverage_blocked
        and not existing_blocker
    )
    evaluation_plan = _evaluation_plan(store, result, reference, gates, status, reasons, resolved_attempt_id) if can_refresh_evaluation else None
    evaluation_id, decision_campaign_id = (
        (evaluation_plan[0], evaluation_plan[1])
        if evaluation_plan is not None
        else (result.evaluation_id, result.decision_campaign_id)
    )
    campaign = result.campaign
    account_values = tuple(value for value in result.accounts.values() if isinstance(value, Mapping))
    account_cap, account_caps_conflict = _account_cap(account_values)
    campaign_cap = _first(campaign.search_facts, "max_balance", "cap", default=None)
    scenario = {
        "accounts": result.accounts,
        "deposit": _first(campaign.initial_equity, "amount", "value", default=campaign.initial_equity),
        "initial_wallet": campaign.initial_wallet,
        "initial_equity": campaign.initial_equity,
        "cap": campaign_cap if campaign_cap is not None else (None if account_caps_conflict else account_cap),
        "limiter": _first(campaign.search_facts, "limiter", default=None),
        "priorities": _first(campaign.search_facts, "priorities", "priority", default=None),
        "opposite_policy": _first(campaign.search_facts, "opposite_policy", default=None),
    }
    manifest_attempt_id = resolved_attempt_id
    manifest_body = {
        "schema": "portfolio_export_v1",
        "manifest_digest_scope": "manifest_body_without_manifest_digest",
        "status": status,
        "reasons": reasons,
        "run_id": result.run_id,
        "attempt_id": manifest_attempt_id,
        "evaluation_id": evaluation_id,
        "execution_campaign_id": result.execution_campaign_id,
        "decision_campaign_id": decision_campaign_id,
        "portfolio_set_id": f"portfolio-set-{set_digest[:32]}",
        "composition_digest": set_digest,
        "campaign_digest": campaign.canonical_digest,
        "periods": {"development": campaign.development_window, "validation": campaign.validation_window, "upstream_used": campaign.upstream_used_periods},
        "scenario": scenario,
        "candidate_ids": {symbol: payload["candidate_ids"] for symbol, payload in strategies.items()},
        "strategies": strategies,
        "metrics": metrics or {},
        "validation": validation_facts,
        "gates": gates,
        "selection_reasons": list(reasons),
        "limitations": ["fixture-only research evidence", "exact tick replay unavailable when binary/ticks are absent", "no live bot execution"],
    }
    manifest_body = _safe(manifest_body)
    manifest_digest = export_digest(manifest_body)
    manifest = {**manifest_body, "manifest_digest": manifest_digest}
    summary = _summary_metrics(metrics)
    equity_fields = (("initial", "initial_equity"), ("final", "final_equity"))
    report_lines = [
        "Portfolio export report",
        f"Status: {status}",
        f"Account/scenario deposit: {canonical_export_json(_report_value(manifest_body['scenario'].get('deposit')))}",
        f"Accounts: {canonical_export_json(_report_value(manifest_body['scenario'].get('accounts', {})))}",
        f"Cap: {canonical_export_json(_report_value(manifest_body['scenario'].get('cap')))}",
        f"Limiter/priority/opposite mode: {canonical_export_json({key: _report_value(manifest_body['scenario'].get(key)) for key in ('limiter', 'priorities', 'opposite_policy')})}",
        f"Periods: {canonical_export_json(manifest_body['periods'])}",
        f"Candidate IDs: {canonical_export_json(manifest_body['candidate_ids'])}",
        f"Initial/final equity: {canonical_export_json({key: _report_value(_first(summary, name)) for key, name in equity_fields})}",
        f"Primary result: {canonical_export_json(_report_value(_first(summary, 'primary_result')))}",
        f"Realized PnL: {canonical_export_json(_report_value(_first(summary, 'realized_pnl')))}",
        f"DD coverage: {canonical_export_json(_report_value({key: _report_value(_first(summary, key)) for key in ('actual_drawdown_pct', 'coverage')}))}",
        f"Metrics: {canonical_export_json(_report_value(manifest_body['metrics']))}",
        f"Validation disposition: {canonical_export_json(manifest_body['validation'])}",
        f"Gates: {canonical_export_json(manifest_body['gates'])}",
        f"Selection reasons: {canonical_export_json(reasons)}",
        f"Limitations: {canonical_export_json(manifest_body['limitations'])}",
    ]
    set_id = str(manifest["portfolio_set_id"])
    stored_rows: list[tuple[str, str, bool | None]] = []
    if evaluation_plan is not None and store is not None:
        stored_rows.extend(
            (
                ("evaluation", str(evaluation_plan[0]), _store_row_exists(store, "get_evaluation", str(evaluation_plan[0]))),
                ("campaign", str(evaluation_plan[1]), _store_row_exists(store, "get_campaign", str(evaluation_plan[1]))),
            )
        )
    if store is not None and callable(getattr(store, "create_portfolio_set", None)):
        stored_rows.append(("portfolio_set", set_id, _store_row_exists(store, "get_portfolio_set", set_id, table="portfolio_sets", key="portfolio_set_id")))

    artifacts: dict[str, ExportArtifact] = {}
    staging_parent = target.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=staging_parent))
    try:
        for symbol, payload in strategies.items():
            path = staging / "strategies" / f"{symbol}.json"
            artifacts[f"strategies/{symbol}.json"] = _write(path, payload, staging)
        artifacts["portfolio.json"] = _write(staging / "portfolio.json", manifest, staging)
        artifacts["portfolio_set.json"] = _write(staging / "portfolio_set.json", {"schema": "portfolio_set_manifest_v1", "portfolio_set_id": manifest["portfolio_set_id"], "composition_digest": set_digest, "composition": selected_composition}, staging)
        artifacts["report.txt"] = _write_text(staging / "report.txt", "\n".join(report_lines) + "\n", staging)
        artifacts["manifest.json"] = _write(staging / "manifest.json", {"schema": "portfolio_export_manifest_v1", "manifest_digest": manifest_digest, "artifacts": {name: item.as_dict() for name, item in artifacts.items()}}, staging)
        if evaluation_plan is not None:
            _persist_evaluation(store, result, evaluation_plan)
        if store is not None and callable(getattr(store, "create_portfolio_set", None)):
            raw_members = _first(selected_composition, "members", default=())
            members = tuple(
                str(_first(member, "portfolio_id", "id", default=member))
                for member in raw_members
            ) if isinstance(raw_members, (list, tuple)) else ()
            try:
                store.create_portfolio_set(set_id, set_digest, {"composition": selected_composition}, members)
            except Exception as error:
                raise ExportError("PortfolioSet composition could not be persisted") from error
        _publish_staging(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        _cleanup_store_rows(store, stored_rows)
        raise
    return PortfolioExport(target, status, reasons, manifest_digest, set_digest, artifacts, manifest, evaluation_id)


def replay_decision(store_or_path: Any, evaluation_id: str, *, store: Any = None) -> dict[str, Any]:
    """Replay a decision from Portfolio DB rows only.

    Binary/tick inputs are deliberately not consulted; the returned fact makes
    that exact replay limitation explicit.
    """
    if store is not None:
        source = store
    elif callable(getattr(store_or_path, "get_evaluation", None)):
        source = store_or_path
    else:
        from .store import PortfolioStore
        source = PortfolioStore(store_or_path)
    try:
        evaluation = source.get_evaluation(evaluation_id)
    except Exception as error:
        raise ExportError("persisted evaluation evidence is missing") from error
    if not evaluation:
        raise ExportError("persisted evaluation evidence is missing")
    try:
        evaluation_payload = _json_payload(evaluation.get("payload"))
        run_id = str(evaluation["trading_run_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ExportError("persisted decision linkage is malformed") from error
    try:
        run = source.get_trading_run(run_id)
    except Exception as error:
        raise ExportError("persisted trading run lookup failed") from error
    if not run:
        raise ExportError("persisted decision linkage is malformed")
    try:
        decision_id = str(evaluation["decision_campaign_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ExportError("persisted decision linkage is malformed") from error
    try:
        decision = source.get_campaign(decision_id)
    except Exception as error:
        raise ExportError("persisted decision campaign lookup failed") from error
    if not decision:
        raise ExportError("persisted decision linkage is malformed")
    replay_attempt = _first(evaluation_payload, "attempt_id", default=None)
    persisted, executable_identity = _persisted_executable_identity(source, run_id, str(replay_attempt) if replay_attempt is not None else None)
    return {
        "evaluation_id": evaluation_id,
        "trading_run_id": run_id,
        "execution_campaign_id": evaluation.get("execution_campaign_id", run.get("execution_campaign_id")),
        "decision_campaign_id": decision_id,
        "evaluation": evaluation_payload,
        "trading_run": _json_payload(run.get("payload")),
        "decision_campaign": _json_payload(decision.get("content")),
        "exact_tick_replay": _tick_replay(executable_identity, persisted),
    }


replay_portfolio_decision = replay_decision
build_portfolio_export = export_portfolio
export_portfolio_package = export_portfolio
PortfolioExporter = export_portfolio


__all__ = [
    "READY", "RESEARCH_ONLY", "NEEDS_RETEST", "NEEDS_RESCREEN", "ExportError", "ExportArtifact", "PortfolioExport",
    "canonical_export_json", "export_digest", "composition_digest", "export_portfolio", "build_portfolio_export", "export_portfolio_package", "PortfolioExporter", "replay_decision", "replay_portfolio_decision",
]
