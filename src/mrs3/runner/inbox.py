from __future__ import annotations

from hashlib import sha256
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tempfile
from typing import Mapping

from .config import RunnerConfig
from .results import WizardResult, extract_html_strategy_name as _extract_html_strategy_name


class InboxCaptureError(RuntimeError):
    """Raised when immutable tester evidence cannot be captured safely."""


_COMMISSION_FIELDS = (
    "MakerFee",
    "TakerFee",
    "SlippagePercent",
    "FundingRate",
    "FundingIntervalHours",
)


def _canonical_decimal(value: object, field: str) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise InboxCaptureError(f"invalid {field}") from error
    if not decimal.is_finite():
        raise InboxCaptureError(f"non-finite {field}")
    result = format(decimal, "f").rstrip("0").rstrip(".")
    if result in {"", "-0"}:
        return "0"
    return result


def extract_html_strategy_name(path: Path) -> str:
    name = _extract_html_strategy_name(path)
    if name is None:
        raise InboxCaptureError("HTML report has no embedded strategy name")
    return name


def _atomic_bytes(target: Path, data: bytes) -> bytes:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", dir=target.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
        temporary.replace(target)
        copied = target.read_bytes()
        if copied != data:
            raise InboxCaptureError(f"atomic copy changed bytes: {target}")
        return copied
    except OSError as error:
        raise InboxCaptureError(f"could not write inbox file: {target}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _commission_contract(
    config: RunnerConfig, tester_config_bytes: bytes | None = None
) -> tuple[dict[str, str], str, str]:
    snapshot = tester_config_bytes
    if snapshot is None:
        try:
            snapshot = config.tester_config.read_bytes()
        except OSError as error:
            raise InboxCaptureError("could not read tester_config") from error
    try:
        document = json.loads(snapshot.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InboxCaptureError("could not read tester_config") from error
    if not isinstance(document, dict):
        values = None
    elif "tester_config" in document:
        # Keep the documented nested form strict when it is present.
        values = document["tester_config"]
    else:
        # Hamster Bot's local config is also emitted as a flat JSON object.
        values = document
    if not isinstance(values, dict):
        raise InboxCaptureError("tester_config object is missing")
    missing = next((field for field in _COMMISSION_FIELDS if field not in values), None)
    if missing is not None:
        raise InboxCaptureError(f"missing commission field: {missing}")
    contract = {field: _canonical_decimal(values[field], field) for field in _COMMISSION_FIELDS}
    canonical = _canonical_json(contract)
    return contract, sha256(canonical).hexdigest(), sha256(snapshot).hexdigest()


def capture_verified_inbox(
    config: RunnerConfig,
    output_csv: Path,
    plan: object,
    results: tuple[WizardResult, ...],
    report_paths: Mapping[str, Path],
    *,
    tester_config_bytes: bytes | None = None,
) -> Path:
    contract, contract_id, tester_config_hash = _commission_contract(config, tester_config_bytes)
    expected_names = tuple(plan.expected_names)
    if len(results) != len({result.strategy_names[0] for result in results}):
        raise InboxCaptureError("duplicate verified results")
    by_name = {result.strategy_names[0]: result for result in results}
    if set(by_name) != set(expected_names):
        raise InboxCaptureError("verified results do not match expected strategies")
    batch_id = output_csv.resolve().stem
    inbox = config.inbox_root.resolve() / batch_id
    if inbox.exists():
        raise InboxCaptureError(f"inbox already exists: {inbox}")
    entries: list[dict[str, object]] = []
    try:
        for name in expected_names:
            result = by_name[name]
            report_path = report_paths.get(name)
            if report_path is None or not report_path.is_file():
                raise InboxCaptureError(f"HTML report is missing for {name}")
            report_bytes = report_path.read_bytes()
            if extract_html_strategy_name(report_path) != name:
                raise InboxCaptureError(f"HTML strategy name differs for {name}")
            source = plan.strategy_source / f"{name}.json"
            try:
                strategy = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InboxCaptureError(f"invalid strategy JSON for {name}") from error
            if not isinstance(strategy, dict):
                raise InboxCaptureError(f"strategy JSON is not an object for {name}")
            exchange = strategy.get("exchange")
            exchange_name = exchange.get("name") if isinstance(exchange, dict) else None
            if not isinstance(exchange_name, str) or not exchange_name.strip():
                raise InboxCaptureError(f"strategy exchange.name is missing for {name}")
            strategy_bytes = _canonical_json(strategy)
            strategy_id = sha256(strategy_bytes).hexdigest()
            strategy_target = inbox / "strategies" / f"{strategy_id}.json"
            strategy_bytes = _atomic_bytes(strategy_target, strategy_bytes)
            report_hash = sha256(report_bytes).hexdigest()
            entry_id = sha256(
                _canonical_json({"strategy": strategy_id, "report": report_hash, "run": result.run_id})
            ).hexdigest()[:32]
            final_report = inbox / "reports" / f"{entry_id}.html"
            copied_report_bytes = _atomic_bytes(final_report, report_bytes)
            entries.append(
                {
                    "manifest_entry_id": entry_id,
                    "strategy_name": name,
                    "strategy_version_id": strategy_id,
                    "strategy_path": str(strategy_target.relative_to(inbox)).replace("\\", "/"),
                    "report_path": str(final_report.relative_to(inbox)).replace("\\", "/"),
                    "wizard_run_id": result.run_id,
                    "exchange_name": exchange_name,
                    "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
                    "source_report_sha256": sha256(copied_report_bytes).hexdigest(),
                }
            )
        manifest = {
            "schema_version": 1,
            "batch_id": batch_id,
            "expected_strategy_names": list(expected_names),
            "tester_config_sha256": tester_config_hash,
            "commission_contract": contract,
            "commission_contract_id": contract_id,
            "entries": entries,
        }
        _atomic_bytes(inbox / "inbox_manifest.json", _canonical_json(manifest))
        return inbox
    except BaseException:
        import shutil

        shutil.rmtree(inbox, ignore_errors=True)
        raise
