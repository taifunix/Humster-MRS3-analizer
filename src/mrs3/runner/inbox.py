from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

from .config import RunnerConfig
from .results import WizardResult, extract_html_strategy_name as _extract_html_strategy_name

MAX_INBOX_CAPTURE_WORKERS = 16


class InboxCaptureError(RuntimeError):
    """Raised when immutable tester evidence cannot be captured safely."""


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _reject_reparse_components(path: Path) -> None:
    raw = Path(path).absolute()
    for current in (*reversed(raw.parents), raw):
        if _is_reparse(current):
            raise InboxCaptureError("refusing to replace an inbox path containing a symlink or reparse point")


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
    provenance: Mapping[str, object] | None = None,
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
            if not source.is_file():
                source = config.strategy_dir / f"{name}.json"
            try:
                source_bytes = source.read_bytes()
                strategy = json.loads(source_bytes.decode("utf-8"))
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
            staged_strategy = inbox / "strategies" / f"{name}.json"
            staged_bytes = _atomic_bytes(staged_strategy, source_bytes)
            source_strategy_hash = sha256(staged_bytes).hexdigest()
            report_hash = sha256(report_bytes).hexdigest()
            entry_id = sha256(
                _canonical_json({"strategy": strategy_id, "report": report_hash, "run": result.run_id})
            ).hexdigest()[:32]
            entries.append(
                {
                    "manifest_entry_id": entry_id,
                    "strategy_name": name,
                    "strategy_version_id": strategy_id,
                    "strategy_path": str(staged_strategy.resolve()),
                    "report_path": str(report_path.resolve()),
                    "wizard_run_id": result.run_id,
                    "exchange_name": exchange_name,
                    "source_strategy_sha256": source_strategy_hash,
                    "source_report_sha256": report_hash,
                }
            )
        manifest = {
            "schema_version": 1,
            "batch_id": batch_id,
            "expected_strategy_names": list(expected_names),
            "tester_config_sha256": tester_config_hash,
            "commission_contract": contract,
            "commission_contract_id": contract_id,
            "source_mode": "direct",
            "entries": entries,
        }
        if provenance is not None:
            if not isinstance(provenance, Mapping):
                raise InboxCaptureError("provenance must be an object")
            required = ("analysis_run_id", "generation_manifest_sha256", "strategy_json_sha256")
            missing = [key for key in required if not provenance.get(key)]
            if missing:
                raise InboxCaptureError("provenance is incomplete: " + ", ".join(missing))
            if not isinstance(provenance["analysis_run_id"], str) or not provenance["analysis_run_id"].strip():
                raise InboxCaptureError("provenance is incomplete: analysis_run_id must be a string")
            generation_hash = provenance["generation_manifest_sha256"]
            if not isinstance(generation_hash, str) or len(generation_hash) != 64:
                raise InboxCaptureError("provenance is incomplete: generation_manifest_sha256 must be a SHA-256 hash")
            strategy_hashes = provenance["strategy_json_sha256"]
            if not isinstance(strategy_hashes, Mapping):
                raise InboxCaptureError("provenance is incomplete: strategy_json_sha256 must be an object")
            expected_hash_names = {f"{name}.json" for name in expected_names}
            if set(strategy_hashes) != expected_hash_names or any(
                not isinstance(value, str) or len(value) != 64 for value in strategy_hashes.values()
            ):
                raise InboxCaptureError("provenance is incomplete: strategy_json_sha256 must cover the batch")
            manifest["v6_provenance"] = json.loads(json.dumps(dict(provenance), sort_keys=True))
        _atomic_bytes(inbox / "inbox_manifest.json", _canonical_json(manifest))
        return inbox
    except BaseException:
        import shutil

        shutil.rmtree(inbox, ignore_errors=True)
        raise


def capture_run_snapshot_inbox(
    config: RunnerConfig,
    job_id: str,
    snapshots: Mapping[str, Mapping[str, object]],
    reports: Mapping[str, Path],
    *,
    tester_config_bytes: bytes,
    provenance: Mapping[str, object],
    test_start: str,
    test_end: str,
    run_mode: str = "RUNS",
    workers: int = 1,
    strategy_paths: Mapping[str, Path] | None = None,
    replace_existing: bool = False,
) -> Path:
    """Capture one completed tester job in the immutable inbox format.

    ``SINGLE_MODE`` deliberately stores strategy paths as metadata.  The
    generated JSON remains owned by ``Output/strategies`` and is hashed again
    by the v2 importer before staging.
    """
    names = tuple(sorted(snapshots))
    if not names or set(reports) != set(names):
        raise InboxCaptureError("tester reports do not match expected strategies")
    if not isinstance(job_id, str) or not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
        raise InboxCaptureError("inbox job id is unsafe")
    if run_mode not in {"RUNS", "FAST", "SINGLE_MODE"}:
        raise InboxCaptureError("unsupported tester run mode")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise InboxCaptureError("inbox workers must be a positive integer")
    contract, contract_id, tester_config_hash = _commission_contract(config, tester_config_bytes)
    _reject_reparse_components(config.inbox_root)
    inbox_root = config.inbox_root.resolve()
    inbox = inbox_root / job_id
    _reject_reparse_components(inbox)
    if inbox.exists():
        if not replace_existing:
            raise InboxCaptureError(f"inbox already exists: {inbox}")
        _reject_reparse_components(inbox)
        if not inbox.is_dir() or inbox.parent != inbox_root:
            raise InboxCaptureError("refusing to replace an unsafe inbox path")
        shutil.rmtree(inbox)
    entries: list[dict[str, object]] = []
    try:
        def capture(name: str) -> dict[str, object]:
            strategy = snapshots[name]
            exchange = strategy.get("exchange")
            exchange_name = exchange.get("name") if isinstance(exchange, Mapping) else None
            if strategy.get("name") != name or not isinstance(exchange_name, str) or not exchange_name.strip():
                raise InboxCaptureError("RUNS snapshot strategy is invalid")
            report_path = reports[name]
            if not report_path.is_file() or extract_html_strategy_name(report_path) != name:
                raise InboxCaptureError("RUNS HTML report does not match snapshot")
            if strategy_paths is not None:
                source = Path(strategy_paths.get(name, ""))
                if source.is_symlink() or not source.is_file():
                    raise InboxCaptureError("SINGLE_MODE strategy source is invalid")
                strategy_bytes = source.read_bytes()
                strategy_path = source.resolve()
            else:
                strategy_bytes = _canonical_json(strategy)
                strategy_path = inbox / "strategies" / f"{name}.json"
                if run_mode == "SINGLE_MODE":
                    raise InboxCaptureError("SINGLE_MODE strategy source is required")
                strategy_bytes = _atomic_bytes(strategy_path, strategy_bytes)
            report_bytes = report_path.read_bytes()
            strategy_id = sha256(_canonical_json(strategy)).hexdigest()
            report_hash = sha256(report_bytes).hexdigest()
            return {
                "manifest_entry_id": sha256(_canonical_json({"strategy": strategy_id, "report": report_hash, "run": job_id})).hexdigest()[:32],
                "strategy_name": name,
                "strategy_version_id": strategy_id,
                "strategy_path": str(strategy_path),
                "report_path": str(report_path.resolve()),
                "wizard_run_id": f"runs:{job_id}:{name}",
                "exchange_name": exchange_name,
                "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
                "source_report_sha256": report_hash,
            }

        with ThreadPoolExecutor(max_workers=min(workers, MAX_INBOX_CAPTURE_WORKERS, len(names))) as executor:
            entries.extend(executor.map(capture, names))
        strategy_hashes = provenance.get("strategy_json_sha256")
        if not isinstance(strategy_hashes, Mapping) or set(strategy_hashes) != {f"{name}.json" for name in names}:
            raise InboxCaptureError("RUNS provenance does not cover snapshots")
        manifest = {
            "schema_version": 1,
            "batch_id": job_id,
            "expected_strategy_names": list(names),
            "tester_config_sha256": tester_config_hash,
            "commission_contract": contract,
            "commission_contract_id": contract_id,
            "source_mode": "metadata_only" if run_mode == "SINGLE_MODE" else "direct",
            "run_mode": run_mode,
            "test_start": test_start,
            "test_end": test_end,
            "inbox_ready": True,
            "entries": entries,
            "v6_provenance": json.loads(json.dumps(dict(provenance), sort_keys=True)),
        }
        _atomic_bytes(inbox / "inbox_manifest.json", _canonical_json(manifest))
        return inbox
    except BaseException:
        try:
            _reject_reparse_components(config.inbox_root)
            _reject_reparse_components(inbox)
        except InboxCaptureError:
            pass
        else:
            shutil.rmtree(inbox, ignore_errors=True)
        raise
