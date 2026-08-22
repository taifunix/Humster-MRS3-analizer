"""Redacted provenance gates for the Strategies/DD5 panel screen."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence


class PanelProvenanceError(ValueError):
    """Raised when an artifact cannot join the v2 provenance chain."""


Loader = Callable[[str], object]


def _load(value: object, loader: Loader | Callable[[], object] | None) -> Mapping[str, object]:
    if loader is not None:
        try:
            value = loader(str(value))
        except TypeError:
            value = loader()  # type: ignore[call-arg]
    if not isinstance(value, Mapping):
        raise PanelProvenanceError("provenance artifact is malformed")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PanelProvenanceError(f"{field} is missing")
    return value.strip()


def _field(document: Mapping[str, object], *names: str) -> object:
    metadata = document.get("metadata")
    for source in (document, metadata if isinstance(metadata, Mapping) else {}):
        for name in names:
            if name in source:
                return source[name]
    return None


def _id(value: object, field: str) -> str:
    result = _text(value, field)
    if "/" in result or "\\" in result or result in {".", ".."}:
        raise PanelProvenanceError(f"{field} is not an opaque ID")
    return result


def _basename(value: object, field: str) -> str:
    result = _text(value, field).replace("\\", "/").rstrip("/")
    name = result.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise PanelProvenanceError(f"{field} is missing")
    return name


def _status(document: Mapping[str, object]) -> str:
    for field in ("status", "state", "final_state", "commit_state"):
        value = document.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    if document.get("committed") is True:
        return "COMMITTED"
    return ""


def _mode(document: Mapping[str, object]) -> str:
    for field in ("event_mode", "mode", "build_mode", "artifact_type"):
        value = document.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _reject_legacy(document: Mapping[str, object]) -> None:
    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key).casefold().replace(" ", "_").replace("-", "_")
                if key_text in {
                    "legacy_csv", "runner_csv", "csv_only", "csv", "csv_path",
                    "output_csv", "duckdb_direct",
                }:
                    raise PanelProvenanceError("legacy CSV or DUCKDB_DIRECT evidence is not allowed")
                visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            token = value.strip().casefold().replace("-", "_")
            if token in {"legacy_csv", "runner_csv", "csv_only", "duckdb_direct"}:
                raise PanelProvenanceError("legacy CSV or DUCKDB_DIRECT evidence is not allowed")

    visit(document)


def _require_real_events(document: Mapping[str, object]) -> None:
    mode = _mode(document)
    if mode == "LEGACY_TRADES_PROXY":
        raise PanelProvenanceError("legacy_trades_proxy is not allowed")
    event_mode = document.get("event_mode")
    if event_mode is not None and str(event_mode).strip().lower() != "real_independent_events":
        raise PanelProvenanceError("unsupported event mode")


def _canonical_surface(document: Mapping[str, object]) -> bool:
    for field in ("canonical", "is_canonical"):
        if field in document:
            return document[field] is True
    for field in ("surface_kind", "surface_type", "contract"):
        value = document.get(field)
        if isinstance(value, str):
            return value.strip().upper() in {"CANONICAL", "SOURCE_V6_CANONICAL"}
    # Source v6 manifests are canonical by their versioned immutable contract.
    return (
        document.get("surface_schema_version") == 6
        and isinstance(document.get("manifest_sha256"), str)
        and isinstance(document.get("frozen_facts_sha256"), str)
    )


def validate_selected_surface(
    surface: object,
    loader: Loader | Callable[[], object] | None = None,
) -> dict[str, object]:
    """Validate one immutable canonical/committed surface without exposing paths."""

    document = _load(surface, loader)
    _reject_legacy(document)
    if not _canonical_surface(document):
        raise PanelProvenanceError("selected surface is not canonical")
    if _status(document) != "COMMITTED":
        raise PanelProvenanceError("selected surface is not committed")
    _require_real_events(document)
    surface_id = _id(document.get("surface_id"), "surface_id")
    manifest_sha256 = _text(
        document.get("manifest_sha256", document.get("source_manifest_sha256")),
        "manifest_sha256",
    )
    return {
        "status": "VALID",
        "surface_id": surface_id,
        "manifest_sha256": manifest_sha256,
    }


def validate_analysis_identity(
    surface: Mapping[str, object],
    identity: object,
    loader: Loader | Callable[[], object] | None = None,
) -> dict[str, object]:
    """Require a committed analysis identity tied to the selected surface."""

    selected = _load(surface, None)
    document = _load(identity, loader)
    _reject_legacy(document)
    if _status(document) != "COMMITTED":
        raise PanelProvenanceError("analysis identity is not committed")
    _require_real_events(document)
    surface_id = _id(selected.get("surface_id"), "surface_id")
    identity_surface = _id(
        _field(document, "surface_id", "source_surface_id"),
        "analysis surface_id",
    )
    if identity_surface != surface_id:
        raise PanelProvenanceError("analysis identity does not match selected surface")
    expected_manifest = str(selected.get("manifest_sha256"))
    actual_manifest = _field(document, "manifest_sha256", "source_manifest_sha256")
    if actual_manifest is not None and str(actual_manifest) != expected_manifest:
        raise PanelProvenanceError("analysis identity does not match surface manifest")
    analysis_run_id = _id(_field(document, "analysis_run_id", "analysis_identity_id"), "analysis_run_id")
    for field in ("analysis_identity_sha256", "canonical_identity_sha256"):
        if field in document and str(document[field]) != analysis_run_id:
            raise PanelProvenanceError("analysis identity hash does not match analysis_run_id")
    return {
        "status": "VALID",
        "surface_id": surface_id,
        "analysis_run_id": analysis_run_id,
        "manifest_sha256": expected_manifest,
    }


def _ready_artifacts(document: Mapping[str, object]) -> tuple[str, ...]:
    raw = document.get("artifacts", document.get("strategies"))
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise PanelProvenanceError("strategy batch artifact list is missing")
    names: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            status = item.get("status", item.get("readiness"))
            if status is not None and str(status).strip().upper() not in {"READY", "READY_MRS3_STRUCTURE"}:
                raise PanelProvenanceError("strategy batch contains a non-READY artifact")
            value = item.get("filename", item.get("name", item.get("json_filename")))
        else:
            value = item
        name = _basename(value, "strategy filename")
        if name.lower().endswith(".csv"):
            raise PanelProvenanceError("legacy CSV strategy artifacts are not allowed")
        if not name.lower().endswith(".json"):
            raise PanelProvenanceError("strategy batch contains a non-JSON artifact")
        names.append(name)
    if not names or len(set(names)) != len(names):
        raise PanelProvenanceError("strategy batch artifact list is empty or duplicated")
    return tuple(sorted(names))


def validate_strategy_batch(
    identity: Mapping[str, object],
    batch: object,
    loader: Loader | Callable[[], object] | None = None,
) -> dict[str, object]:
    """Validate a selected READY JSON list against its generation manifest."""

    expected = _load(identity, None)
    document = _load(batch, loader)
    _reject_legacy(document)
    status = _status(document)
    if status and status not in {"READY", "COMMITTED", "READY_FOR_TEST"}:
        raise PanelProvenanceError("strategy batch is not READY")
    analysis_run_id = _id(expected.get("analysis_run_id"), "analysis_run_id")
    batch_analysis_id = document.get("analysis_run_id")
    if batch_analysis_id is not None and str(batch_analysis_id) != analysis_run_id:
        raise PanelProvenanceError("strategy batch does not match analysis identity")
    manifest = document.get("manifest")
    if not isinstance(manifest, Mapping):
        raise PanelProvenanceError("strategy generation manifest is missing")
    if manifest.get("analysis_run_id") is not None and str(manifest["analysis_run_id"]) != analysis_run_id:
        raise PanelProvenanceError("strategy manifest does not match analysis identity")
    expected_manifest_sha = expected.get("manifest_sha256")
    manifest_source_sha = manifest.get("source_manifest_sha256")
    if expected_manifest_sha is not None and manifest_source_sha is not None and str(manifest_source_sha) != str(expected_manifest_sha):
        raise PanelProvenanceError("strategy manifest does not match selected surface")
    names = _ready_artifacts(document)
    hashes = manifest.get("strategy_json_sha256")
    if not isinstance(hashes, Mapping) or {str(key) for key in hashes} != set(names):
        raise PanelProvenanceError("strategy artifacts do not match generation manifest")
    strategy_count = manifest.get("strategy_count", len(names))
    if isinstance(strategy_count, bool) or int(strategy_count) != len(names):
        raise PanelProvenanceError("strategy count does not match generation manifest")
    batch_id = _id(document.get("batch_id", document.get("tester_batch_id")), "batch_id")
    generation_hash = _text(
        document.get("generation_manifest_sha256", manifest.get("generation_manifest_sha256")),
        "generation_manifest_sha256",
    )
    if manifest.get("generation_manifest_sha256") is not None and str(manifest["generation_manifest_sha256"]) != generation_hash:
        raise PanelProvenanceError("strategy generation manifest hash mismatch")
    manifest_path = document.get("manifest_path", "strategy_manifest.json")
    return {
        "status": "VALID",
        "batch_id": batch_id,
        "analysis_run_id": analysis_run_id,
        "manifest": _basename(manifest_path, "manifest_path"),
        "generation_manifest_sha256": generation_hash,
        "strategy_json_sha256": {str(name): str(hashes[name]) for name in names},
        "artifacts": list(names),
        "strategy_count": len(names),
    }


def _source_pnl_field(document: Mapping[str, object]) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, Mapping):
            return any(check(key, item) for key, item in value.items())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(visit(item) for item in value)
        return False

    def check(key: object, value: object) -> bool:
        compact = str(key).casefold().replace(" ", "_").replace("-", "_")
        if "source_pnl" in compact or compact in {"sourcepnl", "source_pnl_pct"}:
            return True
        if compact in {"tested_pnl_kind", "pnl_kind"} and str(value).strip().upper() not in {
            "TESTER",
            "TESTER_EVIDENCE",
        }:
            return True
        return visit(value)

    return visit(document)


def validate_performance_evidence(
    batch: Mapping[str, object],
    evidence: object,
    loader: Loader | Callable[[], object] | None = None,
) -> dict[str, object]:
    """Require linked tester evidence, a committed zero-quarantine import and calculation-only DD5."""

    expected = _load(batch, None)
    document = _load(evidence, loader)
    _reject_legacy(document)
    if _source_pnl_field(document):
        raise PanelProvenanceError("Source PnL cannot be treated as tested PnL")
    imported = document.get("import")
    imported = imported if isinstance(imported, Mapping) else document
    batch_id = _id(expected.get("batch_id"), "batch_id")
    actual_batch_id = _id(imported.get("batch_id", document.get("batch_id")), "batch_id")
    if actual_batch_id != batch_id:
        raise PanelProvenanceError("Performance evidence does not match tester batch")
    analysis_id = _id(expected.get("analysis_run_id"), "analysis_run_id")
    if document.get("analysis_run_id") is not None and str(document["analysis_run_id"]) != analysis_id:
        raise PanelProvenanceError("Performance evidence does not match analysis identity")
    quarantine = imported.get("quarantine")
    quarantine_count = quarantine.get("count") if isinstance(quarantine, Mapping) else None
    raw_quarantine_count = imported.get(
        "quarantined_count",
        imported.get("quarantine_count", quarantine_count),
    )
    if _status(imported) != "COMMITTED" or int(raw_quarantine_count if raw_quarantine_count is not None else -1) != 0:
        raise PanelProvenanceError("Performance evidence requires committed zero-quarantine import")
    tester = document.get("tester_evidence")
    if not isinstance(tester, Mapping) or str(tester.get("batch_id")) != batch_id:
        raise PanelProvenanceError("linked tester evidence is missing")
    tester_provenance = tester.get("v6_provenance")
    if isinstance(tester_provenance, Mapping):
        tester = {**tester, **tester_provenance}
    if tester.get("analysis_run_id") is not None and str(tester["analysis_run_id"]) != analysis_id:
        raise PanelProvenanceError("tester evidence does not match analysis identity")
    if (
        tester.get("generation_manifest_sha256") is not None
        and str(tester["generation_manifest_sha256"]) != str(expected.get("generation_manifest_sha256"))
    ):
        raise PanelProvenanceError("tester evidence does not match strategy manifest")
    expected_hashes = expected.get("strategy_json_sha256")
    if tester.get("strategy_json_sha256") is not None and tester["strategy_json_sha256"] != expected_hashes:
        raise PanelProvenanceError("tester evidence does not match strategy JSON batch")
    if str(tester.get("status", "")).upper() not in {"RECONCILED", "COMMITTED", "RECONCILED_ZERO_QUARANTINE"}:
        raise PanelProvenanceError("tester evidence is not reconciled")
    tested_count = int(tester.get("tested_count", tester.get("count", 0)))
    manifest = expected.get("manifest")
    manifest_count = manifest.get("strategy_count") if isinstance(manifest, Mapping) else None
    strategy_count = int(expected.get("strategy_count", manifest_count or 0))
    if tested_count != strategy_count:
        raise PanelProvenanceError("tester evidence is incomplete")
    dd5 = document.get("dd5")
    if not isinstance(dd5, Mapping) and any(key in document for key in ("dd5_run_id", "dd5_mode")):
        dd5 = {"dd5_run_id": document.get("dd5_run_id"), "status": document.get("dd5_mode")}
    if not isinstance(dd5, Mapping):
        raise PanelProvenanceError("DD5 evidence is missing")
    if str(dd5.get("status", dd5.get("mode", ""))).upper() != "CALCULATION_ONLY":
        raise PanelProvenanceError("DD5 must be CALCULATION_ONLY")
    dd5_run_id = _id(dd5.get("dd5_run_id"), "dd5_run_id")
    import_id = _id(imported.get("import_id", document.get("import_id")), "import_id")
    return {
        "status": "VALID",
        "batch_id": batch_id,
        "analysis_run_id": analysis_id,
        "import_id": import_id,
        "dd5_run_id": dd5_run_id,
        "tested_count": tested_count,
        "dd5_mode": "CALCULATION_ONLY",
    }


def validate_artifact_gate(
    *,
    surface_loader: Loader | Callable[[], object],
    analysis_loader: Loader | Callable[[], object],
    strategy_loader: Loader | Callable[[], object],
    performance_loader: Loader | Callable[[], object],
    surface_key: str,
    analysis_key: str,
    batch_key: str,
    performance_key: str,
) -> dict[str, object]:
    """Validate the complete immutable surface -> tester -> Performance -> DD5 chain."""

    surface = validate_selected_surface(surface_key, surface_loader)
    identity = validate_analysis_identity(surface, analysis_key, analysis_loader)
    batch = validate_strategy_batch(identity, batch_key, strategy_loader)
    performance = validate_performance_evidence(batch, performance_key, performance_loader)
    return {
        "status": "READY",
        "surface_id": surface["surface_id"],
        "analysis_run_id": identity["analysis_run_id"],
        "batch_id": batch["batch_id"],
        "import_id": performance["import_id"],
        "dd5_run_id": performance["dd5_run_id"],
        "strategy_count": batch["strategy_count"],
        "tested_count": performance["tested_count"],
        "dd5_mode": "CALCULATION_ONLY",
    }


__all__ = [
    "PanelProvenanceError",
    "validate_analysis_identity",
    "validate_artifact_gate",
    "validate_performance_evidence",
    "validate_selected_surface",
    "validate_strategy_batch",
]
