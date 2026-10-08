"""Immutable finalist snapshots and strict XLSX review import."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from typing import Mapping, Sequence
from uuid import uuid4
import zipfile

import duckdb
from openpyxl import load_workbook
import pandas as pd

from .performance_v2_equity_cache import equity_source_revision
from .performance_v2_equity_quality import ALGORITHM_VERSION
from .performance_v2_equity_regime import (
    ALGORITHM_VERSION as EQUITY_REGIME_ALGORITHM_VERSION,
    assess_equity_regime,
)
from .performance_v2_equity_regime_cache import (
    EquityRegimeCacheError,
    decode_equity_regime_assessment,
    decode_equity_regime_facts,
    encode_equity_regime_facts,
)
from .performance_v2_selection import (
    SelectionConfig, SelectionRequest, effective_selection_stages, parse_selection_request,
)


SELECTION_CONTRACT_VERSION_V1 = "performance-v2-selection-review-v1"
SELECTION_CONTRACT_VERSION_V2 = "performance-v2-selection-review-v2"
SELECTION_CONTRACT_VERSION = SELECTION_CONTRACT_VERSION_V1
EQUITY_QUALITY_RANK_POLICY_VERSION = "equity-quality-rank-v1"
WORKBOOK_SCHEMA_VERSION = "1"
META_SHEET = "_MRS_SELECTION_META"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
STATUSES = frozenset({"FINALIST", "RESERVE", "ANALOG", "FILTERED", "REJECTED"})
_EQUITY_DECISION_FACT_FIELDS = (
    "state", "reason", "erf_disposition", "horizon_days", "equity_class",
    "score12", "drawdown", "peak_gap", "windows",
)


def _equity_decision_facts(facts: Mapping[str, object]) -> dict[str, object]:
    return {key: facts.get(key) for key in _EQUITY_DECISION_FACT_FIELDS}


class SelectionReviewError(ValueError):
    def __init__(self, code: str, message: str = "", *, details: object = None) -> None:
        self.code = code
        self.details = details
        super().__init__(message or code)


def _rollback_quietly(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        connection.execute("rollback")
    except Exception:
        pass


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_contract(request: SelectionRequest, config: SelectionConfig) -> tuple[str, str, str, str]:
    request_data = asdict(request)
    if not request.finalists_only:
        request_data.pop("finalists_only")
    for stage in request_data["stages"]:
        if stage["method"] in (None, "robust_v1") or not stage["enabled"]:
            stage.pop("method")
    request_json = canonical_json(request_data)
    config_json = canonical_json(asdict(config))
    return request_json, sha256(request_json.encode()).hexdigest(), config_json, sha256(config_json.encode()).hexdigest()


def _legacy_effective_stage_order(request: SelectionRequest, lot_enabled: bool) -> list[str]:
    ids = [stage.id for stage in request.stages]
    if "filter_lot_variant_redundancy" in ids:
        ids.remove("filter_lot_variant_redundancy")
        ids.insert(0, "filter_lot_variant_redundancy")
    elif lot_enabled:
        ids.insert(0, "filter_lot_variant_redundancy")
    if "filter_equity_regime" in ids:
        ids.remove("filter_equity_regime")
        ids.insert(1 if ids and ids[0] == "filter_lot_variant_redundancy" else 0, "filter_equity_regime")
    return ids


def database_instance_id(connection: duckdb.DuckDBPyConnection) -> str:
    row = connection.execute("select value from schema_info where key = 'database_instance_id'").fetchone()
    if not row:
        raise SelectionReviewError("SELECTION_REVIEW_DATABASE_MISMATCH")
    return str(row[0])


def _equity_quality_rank_enabled(request: SelectionRequest | None) -> bool:
    return bool(request and any(
        stage.id == "rank_robust_top_n" and stage.enabled and stage.method == "equity_quality_v1"
        for stage in request.stages
    ))


def _equity_filter_enabled(request: SelectionRequest) -> bool:
    return any(stage.id == "filter_equity_regime" and stage.enabled for stage in request.stages)


def _equity_regime_publication_data(
    connection: duckdb.DuckDBPyConnection,
    request: SelectionRequest,
    result: pd.DataFrame,
    expected: Mapping[int, int],
    current_sources: Mapping[int, tuple[int, str]],
) -> tuple[dict[int, str], list[list[object]]]:
    """Validate canonical regime evidence and prepare durable rejection sources."""
    filter_enabled = _equity_filter_enabled(request)
    if not filter_enabled and not _equity_quality_rank_enabled(request):
        return {}, []
    evidence = result.attrs.get("equity_regime_evidence")
    expected_keys = {str(strategy_id) for strategy_id in expected}
    if not isinstance(evidence, Mapping) or set(evidence) != expected_keys:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")

    hard_reasons = {"DD_14_7_GTE_23", "W28_DOWN", "PRE28_AND_W28_NOT_UP"}
    records_by_result: dict[int, list[tuple[int, str, str, str]]] = {}
    stale: set[int] = set()
    for strategy_id, result_id in expected.items():
        item = evidence[str(strategy_id)]
        if not isinstance(item, Mapping) or set(item) != {
            "strategy_id", "result_id", "source_revision", "facts_sha256",
            "classifier_algo_version", "equity_regime_json", "equity_filter_enabled",
        }:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        source_result_id = item.get("result_id")
        source_revision = item.get("source_revision")
        facts_sha256 = item.get("facts_sha256")
        payload = item.get("equity_regime_json")
        if (
            type(item.get("strategy_id")) is not int or item["strategy_id"] != strategy_id
            or type(source_result_id) is not int or source_result_id != result_id
            or not isinstance(source_revision, str) or len(source_revision) != 64
            or not isinstance(facts_sha256, str) or len(facts_sha256) != 64
            or not isinstance(payload, str)
            or item.get("classifier_algo_version") != EQUITY_REGIME_ALGORITHM_VERSION
            or item.get("equity_filter_enabled") is not filter_enabled
        ):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        try:
            int(source_revision, 16)
            int(facts_sha256, 16)
        except ValueError:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION") from None
        if current_sources.get(strategy_id) != (result_id, source_revision):
            stale.add(strategy_id)
            continue
        records_by_result.setdefault(result_id, []).append(
            (strategy_id, source_revision, facts_sha256, payload)
        )
    if stale:
        raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=sorted(stale))

    result_ids = sorted(records_by_result)
    assessments: dict[int, str] = {}
    rejection_sources: list[list[object]] = []

    def validate_facts(result_id: int, facts: object) -> None:
        facts_payload = encode_equity_regime_facts(facts)
        actual_facts_sha256 = sha256(facts_payload.encode("utf-8")).hexdigest()
        for strategy_id, source_revision, facts_sha256, payload in records_by_result[result_id]:
            if actual_facts_sha256 != facts_sha256:
                stale.add(strategy_id)
                continue
            try:
                assessment_digest = sha256(payload.encode("utf-8")).hexdigest()
                assessment = decode_equity_regime_assessment(
                    payload, assessment_digest, expected_result_id=result_id,
                )
            except (EquityRegimeCacheError, UnicodeEncodeError):
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION") from None
            if assessment.facts != facts or assess_equity_regime(facts) != assessment:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
            assessments[strategy_id] = payload
            if filter_enabled and assessment.decision == "DROP":
                reasons = set(assessment.reasons)
                if not reasons or not reasons.issubset(hard_reasons):
                    raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
                created_at = datetime.now(timezone.utc)
                rejection_sources.extend([
                    [strategy_id, "EQUITY_REGIME_FILTER", reason, result_id, None,
                     EQUITY_REGIME_ALGORITHM_VERSION, source_revision, facts_sha256, created_at]
                    for reason in assessment.reasons
                ])

    cached_rows = connection.execute(
        """select result_id, source_revision, facts_json, facts_sha256
             from equity_quality_metrics
            where algo_version = ? and result_id in (select unnest(?::bigint[]))""",
        [EQUITY_REGIME_ALGORITHM_VERSION, result_ids],
    ).fetchall() if result_ids else []
    cache_by_result = {int(row[0]): row for row in cached_rows}
    for result_id in result_ids:
        cached = cache_by_result.get(result_id)
        if cached is None or any(
            cached[1] != source_revision
            for _, source_revision, _, _ in records_by_result[result_id]
        ):
            raise SelectionReviewError(
                "SELECTION_CACHE_INCOMPLETE",
                "Selection cache is incomplete; recalculate it before exporting",
                details={"result_ids": [result_id]},
            )
        if any(
            cached[3] != facts_sha256
            for _, _, facts_sha256, _ in records_by_result[result_id]
        ):
            stale.update(record[0] for record in records_by_result[result_id])
            continue
        try:
            facts = decode_equity_regime_facts(
                cached[2], cached[3], expected_result_id=result_id,
            )
        except EquityRegimeCacheError:
            raise SelectionReviewError(
                "SELECTION_CACHE_INCOMPLETE",
                "Selection cache is invalid; recalculate it before exporting",
                details={"result_ids": [result_id]},
            ) from None
        validate_facts(result_id, facts)
    if stale:
        raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=sorted(stale))
    return assessments, rejection_sources


def _equity_regime_evidence_identity(
    request: SelectionRequest, result: pd.DataFrame, expected: Mapping[int, int],
) -> dict[int, tuple[int, str, str, str]]:
    if not _equity_filter_enabled(request) and not _equity_quality_rank_enabled(request):
        return {}
    evidence = result.attrs.get("equity_regime_evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != {str(value) for value in expected}:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
    identities: dict[int, tuple[int, str, str, str]] = {}
    for strategy_id, expected_result_id in expected.items():
        raw = evidence[str(strategy_id)]
        if not isinstance(raw, Mapping):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        result_id = raw.get("result_id")
        source_revision = raw.get("source_revision")
        facts_sha256 = raw.get("facts_sha256")
        payload = raw.get("equity_regime_json")
        if (
            type(result_id) is not int or result_id != expected_result_id
            or not isinstance(source_revision, str) or len(source_revision) != 64
            or not isinstance(facts_sha256, str) or len(facts_sha256) != 64
            or not isinstance(payload, str)
        ):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        identities[strategy_id] = (result_id, source_revision, facts_sha256, payload)
    return identities


def new_run_metadata(
    connection: duckdb.DuckDBPyConnection, request: SelectionRequest | None = None,
) -> dict[str, str]:
    return {
        "workbook_schema_version": WORKBOOK_SCHEMA_VERSION,
        "selection_run_id": str(uuid4()),
        "database_instance_id": database_instance_id(connection),
        "selection_contract_version": (
            SELECTION_CONTRACT_VERSION_V2 if _equity_quality_rank_enabled(request)
            else SELECTION_CONTRACT_VERSION_V1
        ),
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def equity_quality_snapshot_metadata(
    request: SelectionRequest, config: SelectionConfig, result: pd.DataFrame,
) -> dict[str, object] | None:
    """Build reproducible policy/source evidence from the cached facts used by ranking."""
    if not _equity_quality_rank_enabled(request):
        return None
    sources = result.attrs.get("equity_quality_facts")
    if not isinstance(sources, Mapping) or set(sources) != {str(int(value)) for value in result["strategy_id"]}:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
    checked_sources: dict[str, object] = {}
    for strategy_id, raw in sources.items():
        if not isinstance(raw, Mapping):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        result_id = _whole_number(raw.get("result_id"), "SELECTION_REVIEW_INVALID_SELECTION", optional=False)
        source_revision = raw.get("source_revision")
        facts_sha256 = raw.get("facts_sha256")
        facts = raw.get("facts")
        if (
            not isinstance(source_revision, str) or len(source_revision) != 64
            or not isinstance(facts_sha256, str) or len(facts_sha256) != 64
            or not isinstance(facts, Mapping) or facts.get("result_id") != result_id
            or sha256(canonical_json(facts).encode()).hexdigest() != facts_sha256
        ):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        checked_sources[str(strategy_id)] = {
            "result_id": result_id,
            "source_revision": source_revision,
            "facts_sha256": facts_sha256,
            "decision_facts": _equity_decision_facts(facts),
            "facts": facts,
        }
    return {
        "policy_id": "equity_quality_rank",
        "policy_version": EQUITY_QUALITY_RANK_POLICY_VERSION,
        "method": "equity_quality_v1",
        "algorithm_version": ALGORITHM_VERSION,
        "effective_stage_order": [stage.id for stage in effective_selection_stages(request, config)],
        "sources": checked_sources,
    }


def _current_equity_revisions(
    connection: duckdb.DuckDBPyConnection, strategy_ids: Sequence[int],
) -> dict[int, tuple[int, str]]:
    if not strategy_ids:
        return {}
    rows = connection.execute(
        """select s.strategy_id, r.result_id, r.imported_at_utc, r.report_start_utc,
                  r.report_end_utc, r.effective_start_utc, r.effective_end_utc,
                  r.optimizer_source_metadata_json
             from strategies s join strategy_results r on r.result_id = s.current_result_id
            where s.strategy_id in (select unnest(?::bigint[]))""",
        [list(strategy_ids)],
    ).fetchall()
    current: dict[int, tuple[int, str]] = {}
    for row in rows:
        metadata = dict(zip((
            "strategy_id", "result_id", "imported_at_utc", "report_start_utc", "report_end_utc",
            "effective_start_utc", "effective_end_utc", "optimizer_source_metadata_json",
        ), row))
        for name in (
            "imported_at_utc", "report_start_utc", "report_end_utc",
            "effective_start_utc", "effective_end_utc",
        ):
            if metadata[name] is not None:
                value = metadata[name]
                if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
                metadata[name] = metadata[name].astimezone(timezone.utc)
        current[int(row[0])] = (int(row[1]), equity_source_revision(metadata))
    return current


def _equity_snapshot_stale_ids(
    connection: duckdb.DuckDBPyConnection, snapshot: Mapping[str, object],
) -> list[int]:
    sources = snapshot.get("sources")
    if not isinstance(sources, Mapping):
        raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
    ids = [int(strategy_id) for strategy_id in sources]
    current = _current_equity_revisions(connection, ids)
    stale: list[int] = []
    for strategy_id, source in sources.items():
        if not isinstance(source, Mapping):
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        pair = current.get(int(strategy_id))
        if (
            pair is None or pair[0] != source.get("result_id")
            or pair[1] != source.get("source_revision")
        ):
            stale.append(int(strategy_id))
    return sorted(stale)


def _rejected_strategy_ids(
    connection: duckdb.DuckDBPyConnection,
    strategy_ids: Sequence[int] | None = None,
) -> set[int]:
    if strategy_ids is not None and not strategy_ids:
        return set()
    ids = None if strategy_ids is None else list(strategy_ids)
    if ids is None:
        query = """select strategy_id from strategy_tags where tag = 'REJECTED'
                   union select strategy_id from strategy_rejection_sources"""
        params: list[object] = []
    else:
        query = """select strategy_id from strategy_tags where tag = 'REJECTED'
                    and strategy_id in (select unnest(?::bigint[]))
                   union select strategy_id from strategy_rejection_sources
                    where strategy_id in (select unnest(?::bigint[]))"""
        params = [ids, ids]
    try:
        rows = connection.execute(query, params).fetchall()
    except duckdb.CatalogException:
        version_row = connection.execute(
            "select value from schema_info where key = 'schema_version'"
        ).fetchone()
        if version_row is None or str(version_row[0]) not in {"5", "6", "7", "8"}:
            raise
        fallback = "select strategy_id from strategy_tags where tag = 'REJECTED'"
        fallback_params: list[object] = []
        if ids is not None:
            fallback += " and strategy_id in (select unnest(?::bigint[]))"
            fallback_params = [ids]
        rows = connection.execute(fallback, fallback_params).fetchall()
    return {int(row[0]) for row in rows}


def apply_prior_rejected(connection: duckdb.DuckDBPyConnection, candidates: pd.DataFrame) -> pd.DataFrame:
    output = candidates.copy()
    rejected = _rejected_strategy_ids(connection)
    retest = {int(row[0]) for row in connection.execute("select strategy_id from strategy_tags where tag = 'RETEST'").fetchall()}
    output["prior_rejected"] = output["strategy_id"].map(lambda value: int(value) in rejected)
    output["prior_retest"] = output["strategy_id"].map(lambda value: int(value) in retest)
    return output


def _selection_rows(result: pd.DataFrame) -> list[dict[str, object]]:
    """Materialize rows without copying publication evidence for each column."""
    rows = result.copy(deep=False)
    rows.attrs.clear()
    return rows.to_dict(orient="records")


_AUTOMATIC_REJECTION_FILTERS = (
    ("filter_lot_variant_redundancy", "eliminated_by_filter_lot_variant_redundancy", "SELECTION_LOT_VARIANT"),
    ("filter_hard_cutoffs", "eliminated_by_filter_hard_cutoffs", "SELECTION_HARD_CUTOFF"),
    ("ab_deterioration", "eliminated_by_ab_deterioration", "SELECTION_AB_DETERIORATION"),
)


def automatic_filter_rejected_ids(
    result: pd.DataFrame, request: SelectionRequest, config: SelectionConfig,
) -> dict[str, set[int]]:
    enabled = {
        stage.id for stage in effective_selection_stages(request, config) if stage.enabled
    }
    rows = _selection_rows(result)
    # The stage-specific trace flag is the exclusion evidence. Auto status and
    # reason are presentation fields and may be normalized after the stages run.
    rejected_by_source = {
        source: {
            int(row["strategy_id"])
            for row in rows
            if bool(row.get(column))
        }
        for stage_id, column, source in _AUTOMATIC_REJECTION_FILTERS
        if stage_id in enabled and column in result
    }
    owner_by_strategy: dict[int, str] = {}
    for source, strategy_ids in rejected_by_source.items():
        for strategy_id in strategy_ids:
            if strategy_id in owner_by_strategy:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
            owner_by_strategy[strategy_id] = source
    return rejected_by_source


def automatic_filter_rejected_strategy_ids(
    result: pd.DataFrame, request: SelectionRequest, config: SelectionConfig,
) -> set[int]:
    return set().union(*automatic_filter_rejected_ids(result, request, config).values())


def hard_cutoff_rejected_ids(
    result: pd.DataFrame, request: SelectionRequest, config: SelectionConfig,
) -> set[int]:
    """Compatibility wrapper for callers that only need hard-cutoff IDs."""
    return automatic_filter_rejected_ids(result, request, config).get(
        "SELECTION_HARD_CUTOFF", set()
    )


def _cell(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, Decimal):
        return float(value)
    return value


def _insert_rows(
    connection: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...], rows: list[list[object]]
) -> None:
    if not rows:
        return
    relation = f"_selection_input_{uuid4().hex}"
    connection.register(relation, pd.DataFrame(rows, columns=columns))
    try:
        names = ", ".join(columns)
        connection.execute(f"insert into {table} ({names}) select {names} from {relation}")
    finally:
        connection.unregister(relation)


def persist_selection_snapshot(
    connection: duckdb.DuckDBPyConnection,
    request: SelectionRequest,
    config: SelectionConfig,
    result: pd.DataFrame,
    metadata: Mapping[str, str],
    workbook_bytes: bytes,
) -> str:
    """Persist one immutable selection snapshot.

    The original single-run API remains intentionally small.  It delegates to
    :func:`persist_selection_snapshots` so callers which publish a combined
    workbook can validate and commit every pair in one transaction.
    """
    return persist_selection_snapshots(
        connection,
        ({"request": request, "config": config, "result": result, "metadata": metadata},),
        workbook_bytes=workbook_bytes,
    )[0]


def persist_selection_snapshots(
    connection: duckdb.DuckDBPyConnection,
    snapshots: Sequence[Mapping[str, object]],
    *,
    workbook_bytes: bytes | None = None,
    workbook_sha256: str | None = None,
) -> tuple[str, ...]:
    """Atomically persist several immutable selection snapshots.

    ``selection_review_imports.workbook_sha256`` is unique in the existing
    schema, while ``selection_runs.workbook_sha256`` is not.  Every group run
    therefore records the same actual combined workbook hash and its review
    import receives the existing deterministic per-run key.  All stale checks
    happen before the transaction and all inserts happen in one transaction,
    so a failed pair cannot leave a partial ledger.
    """
    if not snapshots:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
    if workbook_sha256 is None:
        if workbook_bytes is None:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
        workbook_sha256 = sha256(workbook_bytes).hexdigest()
    if not isinstance(workbook_sha256, str) or len(workbook_sha256) != 64:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
    try:
        int(workbook_sha256, 16)
    except ValueError:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None

    prepared: list[dict[str, object]] = []
    seen_run_ids: set[str] = set()
    for item in snapshots:
        if not isinstance(item, Mapping):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        request = item.get("request")
        config = item.get("config")
        result = item.get("result")
        metadata = item.get("metadata")
        if not isinstance(request, SelectionRequest) or not isinstance(config, SelectionConfig) or not isinstance(result, pd.DataFrame) or not isinstance(metadata, Mapping):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        required = {"strategy_id", "result_id", "auto_status"}
        if not required.issubset(result.columns):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        run_id = str(metadata.get("selection_run_id") or "")
        if not run_id or run_id in seen_run_ids:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        seen_run_ids.add(run_id)
        expected_contract_version = (
            SELECTION_CONTRACT_VERSION_V2 if _equity_quality_rank_enabled(request)
            else SELECTION_CONTRACT_VERSION_V1
        )
        if metadata.get("selection_contract_version") != expected_contract_version:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        request_json, request_hash, config_json, config_hash = canonical_contract(request, config)
        request_extra = item.get("request_json_extra")
        if request_extra is not None:
            if not isinstance(request_extra, Mapping):
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        equity_snapshot = equity_quality_snapshot_metadata(request, config, result)
        if equity_snapshot is not None:
            stale = _equity_snapshot_stale_ids(connection, equity_snapshot)
            if stale:
                raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
        request_extra_json = {} if request_extra is None else _json_value(request_extra)
        if not isinstance(request_extra_json, dict):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        if equity_snapshot is not None:
            if "equity_quality_snapshot" in request_extra_json and request_extra_json["equity_quality_snapshot"] != equity_snapshot:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
            request_extra_json["equity_quality_snapshot"] = equity_snapshot
        request_extra_json["effective_stage_order"] = [
            stage.id for stage in effective_selection_stages(request, config)
        ]
        expected = {int(row.strategy_id): int(row.result_id) for row in result.itertuples()}
        if len(expected) != len(result) or any(strategy_id <= 0 or result_id <= 0 for strategy_id, result_id in expected.items()):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        regime_identities = _equity_regime_evidence_identity(request, result, expected)
        if regime_identities:
            regime_snapshot = {
                "algorithm_version": EQUITY_REGIME_ALGORITHM_VERSION,
                "sources": {
                    str(strategy_id): {
                        "result_id": identity[0], "source_revision": identity[1],
                    }
                    for strategy_id, identity in sorted(regime_identities.items())
                },
            }
            if ("equity_regime_snapshot" in request_extra_json
                    and request_extra_json["equity_regime_snapshot"] != regime_snapshot):
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
            request_extra_json["equity_regime_snapshot"] = regime_snapshot
        elif "equity_regime_snapshot" in request_extra_json:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        if request_extra_json:
            parsed_request = json.loads(request_json)
            parsed_request.update(request_extra_json)
            request_json = canonical_json(parsed_request)
            request_hash = sha256(request_json.encode()).hexdigest()
        current = dict(connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[]))",
            [list(expected)],
        ).fetchall()) if expected else {}
        stale = sorted(strategy_id for strategy_id, result_id in expected.items() if current.get(strategy_id) != result_id)
        if stale:
            raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
        source_revisions = result.attrs.get("source_revisions")
        if source_revisions is None:
            source_revisions = _current_equity_revisions(connection, list(expected))
        elif not isinstance(source_revisions, Mapping) or set(source_revisions) != set(expected):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_SELECTION")
        current_sources = _current_equity_revisions(connection, list(expected))
        stale = sorted(strategy_id for strategy_id, result_id in expected.items()
                       if source_revisions.get(strategy_id, (None,))[0] != result_id
                       or current_sources.get(strategy_id) != source_revisions.get(strategy_id))
        if stale:
            raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
        rank_stage = next((stage for stage in request.stages if stage.id == "rank_robust_top_n"), None)
        top_n = rank_stage.top_n if rank_stage and rank_stage.top_n else 20
        representative_count = int(result["auto_status"].isin(["FINALIST", "RESERVE"]).sum())
        run_hash = workbook_sha256
        prepared.append({
            "request": request, "config": config, "result": result, "metadata": metadata,
            "request_json_extra": request_extra,
            "equity_snapshot": equity_snapshot,
            "run_id": run_id, "request_json": request_json, "request_hash": request_hash,
            "config_json": config_json, "config_hash": config_hash, "expected": expected,
            "source_revisions": source_revisions,
            "rejected_ids_by_source": automatic_filter_rejected_ids(result, request, config),
            "top_n": top_n, "representative_count": representative_count, "run_hash": run_hash,
        })

    now = datetime.now(timezone.utc)
    connection.execute("begin transaction")
    try:
        # Validate every source while the publication transaction is open,
        # before inserting any run, snapshot, or durable rejection evidence.
        regime_source_by_strategy: dict[int, tuple[int, str, str, str]] = {}
        for item in prepared:
            request = item["request"]
            config = item["config"]
            result = item["result"]
            if item["equity_snapshot"] is not None:
                stale = _equity_snapshot_stale_ids(connection, item["equity_snapshot"])
                if stale:
                    raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
            current_result_ids = dict(connection.execute(
                "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[]))",
                [list(item["expected"])],
            ).fetchall()) if item["expected"] else {}
            stale = sorted(
                strategy_id for strategy_id, result_id in item["expected"].items()
                if current_result_ids.get(strategy_id) != result_id
            )
            if stale:
                raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
            current_sources = _current_equity_revisions(connection, list(item["expected"]))
            stale = sorted(strategy_id for strategy_id, source in item["source_revisions"].items()
                           if current_sources.get(strategy_id) != source)
            if stale:
                raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
            for strategy_id, identity in _equity_regime_evidence_identity(
                request, result, item["expected"],
            ).items():
                prior_identity = regime_source_by_strategy.setdefault(strategy_id, identity)
                if prior_identity != identity:
                    raise SelectionReviewError(
                        "SELECTION_REVIEW_STALE_RESULTS", details=[strategy_id],
                    )
            regime_json, rejection_sources = _equity_regime_publication_data(
                connection, request, result, item["expected"], current_sources,
            )
            item["equity_regime_json_by_strategy"] = regime_json
            item["rejection_sources"] = [
                [*source[:4], item["run_id"], *source[5:8], now]
                for source in rejection_sources
            ]

        for item in prepared:
            request = item["request"]
            config = item["config"]
            result = item["result"]
            metadata = item["metadata"]
            connection.execute(
                """insert into selection_runs (
                    selection_run_id, database_instance_id, symbol, side, selection_contract_version,
                    request_json, request_sha256, config_json, config_sha256, candidate_count,
                    representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [item["run_id"], metadata["database_instance_id"], request.symbol, request.side,
                 metadata["selection_contract_version"], item["request_json"], item["request_hash"],
                 item["config_json"], item["config_hash"], len(result), item["representative_count"],
                 int((result["auto_status"] == "FINALIST").sum()), item["top_n"], item["run_hash"], now],
            )
            stage_columns = [f"eliminated_by_{stage.id}" for stage in effective_selection_stages(request, config) if stage.enabled]
            rows: list[list[object]] = []
            for row in result.to_dict(orient="records"):
                trace = canonical_json({column.removeprefix("eliminated_by_"): bool(row.get(column)) for column in stage_columns})
                rows.append([
                    item["run_id"], int(row["strategy_id"]), int(row["result_id"]), str(row["auto_status"]),
                    _cell(row.get("final_score")), _cell(row.get("final_rank")), _cell(row.get("elimination_reason")),
                    _cell(row.get("analog_group_key")), _cell(row.get("auto_analog_of_strategy_id")),
                    bool(row.get("prior_rejected", False)), trace,
                    item["equity_regime_json_by_strategy"].get(int(row["strategy_id"])),
                ])
            _insert_rows(connection, "selection_results", (
                "selection_run_id", "strategy_id", "result_id_at_selection", "auto_status", "auto_score",
                "auto_rank", "auto_reason", "analog_group_key", "auto_analog_of_strategy_id", "prior_rejected",
                "stage_trace_json", "equity_regime_json",
            ), rows)
            if item["rejection_sources"]:
                connection.executemany(
                    """insert into strategy_rejection_sources (
                           strategy_id, source_kind, reason_code, first_result_id,
                           first_selection_run_id, classifier_algo_version, source_revision,
                           facts_sha256, created_at_utc
                       ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       on conflict (strategy_id, source_kind, reason_code) do nothing""",
                    item["rejection_sources"],
                )
            for source, strategy_ids in item["rejected_ids_by_source"].items():
                for strategy_id in sorted(strategy_ids):
                    connection.execute(
                        """insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc)
                           values (?, 'REJECTED', ?, ?, ?)
                           on conflict (strategy_id, tag) do update set
                               source = excluded.source, source_ref = excluded.source_ref,
                               updated_at_utc = excluded.updated_at_utc""",
                        [strategy_id, source, item["run_id"], now],
                    )
        connection.execute("commit")
    except duckdb.ConstraintException as error:
        _rollback_quietly(connection)
        if "workbook_sha256" in str(error):
            raise SelectionReviewError("SELECTION_REVIEW_ALREADY_IMPORTED") from error
        raise
    except Exception:
        _rollback_quietly(connection)
        raise
    return tuple(str(item["run_id"]) for item in prepared)


def _bounded_xlsx(data: bytes) -> None:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
    except (zipfile.BadZipFile, OSError):
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None


def _whole_number(value: object, code: str, *, optional: bool = True) -> int | None:
    if value is None or value == "":
        if optional:
            return None
        raise SelectionReviewError(code)
    if isinstance(value, bool):
        raise SelectionReviewError(code)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise SelectionReviewError(code) from None
    if number <= 0 or float(value) != number:
        raise SelectionReviewError(code)
    return number


def _normalize_retest(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return False
        if normalized == "RETEST":
            return True
    raise SelectionReviewError("SELECTION_REVIEW_INVALID_RETEST")


def _equivalent_selection_runs(
    connection: duckdb.DuckDBPyConnection, run_id: str, latest_run_id: str
) -> bool:
    if run_id == latest_run_id:
        return True
    contracts = connection.execute(
        "select request_sha256, config_sha256 from selection_runs where selection_run_id in (?, ?) order by selection_run_id",
        [run_id, latest_run_id],
    ).fetchall()
    if len(contracts) != 2 or contracts[0] != contracts[1]:
        return False
    columns = (
        "strategy_id, result_id_at_selection, auto_status, auto_score, auto_rank, "
        "auto_reason, analog_group_key, auto_analog_of_strategy_id, prior_rejected, stage_trace_json"
    )
    rows = [
        connection.execute(
            f"select {columns} from selection_results where selection_run_id = ? order by strategy_id",
            [selection_run_id],
        ).fetchall()
        for selection_run_id in (run_id, latest_run_id)
    ]
    return rows[0] == rows[1]


def _parse_workbook(data: bytes) -> tuple[dict[str, str], list[dict[str, object]]]:
    _bounded_xlsx(data)
    try:
        workbook = load_workbook(BytesIO(data), data_only=False, read_only=True)
    except Exception:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None
    try:
        if any(cell.data_type == "f" for worksheet in workbook.worksheets for row in worksheet.iter_rows() for cell in row):
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
        if META_SHEET not in workbook.sheetnames or "All candidates" not in workbook.sheetnames:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        meta_sheet = workbook[META_SHEET]
        metadata = {str(key): str(value) for key, value in meta_sheet.iter_rows(min_row=1, max_col=2, values_only=True) if key and value is not None}
        if metadata.get("workbook_schema_version") != WORKBOOK_SCHEMA_VERSION or metadata.get("selection_contract_version") not in {
            SELECTION_CONTRACT_VERSION_V1, SELECTION_CONTRACT_VERSION_V2,
        }:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        sheet = workbook["All candidates"]
        header_cells = next(sheet.iter_rows(min_row=1, max_row=1), ())
        raw_headers = [cell.value for cell in header_cells]
        header_pairs = [
            (position, header)
            for position, header in enumerate(raw_headers)
            if isinstance(header, str) and header.strip()
        ]
        headers = [header for _, header in header_pairs]
        positions = {header: position for position, header in header_pairs}
        required = {"ID", "Result ID", "Стратегия", "Auto Status", "User Status", "RETEST", "Auto Rank", "User Rank", "Auto Analog Of ID", "Analog Of ID", "Comment"}
        if (len(headers) != len(set(headers)) or not required.issubset(headers)
                or positions["RETEST"] not in {positions["User Status"] + 1, positions["User Rank"] + 1}):
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        index = {header: position for position, header in header_pairs}
        rows: list[dict[str, object]] = []
        for cells in sheet.iter_rows(min_row=2):
            values = [cell.value for cell in cells]
            if all(value is None for value in values):
                continue
            rows.append({header: values[position] if position < len(values) else None for header, position in index.items()})
        return metadata, rows
    except SelectionReviewError:
        raise
    except Exception:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None
    finally:
        workbook.close()


def _parse_selection_user_fields_workbook(
    data: bytes,
) -> tuple[dict[str, str], list[tuple[object, object, object]]]:
    try:
        workbook = load_workbook(BytesIO(data), data_only=False, read_only=True)
    except Exception:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None
    try:
        if META_SHEET not in workbook.sheetnames or "All candidates" not in workbook.sheetnames:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        meta_sheet = workbook[META_SHEET]
        metadata = {
            str(key): str(value)
            for key, value in meta_sheet.iter_rows(min_col=1, max_col=2, values_only=True)
            if key and value is not None
        }
        if (
            metadata.get("workbook_schema_version") != WORKBOOK_SCHEMA_VERSION
            or metadata.get("selection_contract_version") != SELECTION_CONTRACT_VERSION_V1
            or not metadata.get("selection_run_id")
            or not metadata.get("database_instance_id")
        ):
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        sheet = workbook["All candidates"]
        header_cells = next(sheet.iter_rows(min_row=1, max_row=1), ())
        positions: dict[str, int] = {}
        for position, cell in enumerate(header_cells):
            if cell.value in {"ID", "User Status", "User Rank"}:
                if cell.value in positions:
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
                positions[cell.value] = position
        if set(positions) != {"ID", "User Status", "User Rank"}:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        rows: list[tuple[object, object, object]] = []
        for cells in sheet.iter_rows(min_row=2):
            if all(cell.value is None for cell in cells):
                continue
            id_cell = cells[positions["ID"]]
            status_cell = cells[positions["User Status"]]
            rank_cell = cells[positions["User Rank"]]
            if id_cell.data_type == "f" or status_cell.data_type == "f":
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
            rows.append((id_cell.value, status_cell.value, rank_cell.value))
        return metadata, rows
    except SelectionReviewError:
        raise
    except Exception:
        raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE") from None
    finally:
        workbook.close()


def _validate_selection_user_field_ids(
    connection: duckdb.DuckDBPyConnection, run_id: str, strategy_ids: Sequence[int],
) -> None:
    if not strategy_ids:
        return
    strategy_set = set(strategy_ids)
    known_ids = {
        int(row[0]) for row in connection.execute(
            "select strategy_id from strategies where strategy_id in (select unnest(?::bigint[]))",
            [list(strategy_ids)],
        ).fetchall()
    }
    run_ids = {
        int(row[0]) for row in connection.execute(
            "select strategy_id from selection_results where selection_run_id = ? and strategy_id in (select unnest(?::bigint[]))",
            [run_id, list(strategy_ids)],
        ).fetchall()
    }
    if known_ids != strategy_set or run_ids != strategy_set:
        raise SelectionReviewError(
            "SELECTION_REVIEW_ROWSET_MISMATCH",
            "Every submitted ID must exist in the database and in the workbook selection run",
            details=sorted(strategy_set - (known_ids & run_ids)),
        )


def _validate_selection_user_field_ranks(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    decisions: Sequence[Sequence[object]],
) -> dict[int, dict[str, object]] | None:
    if not any(status == "FINALIST" and rank is not None for _strategy_id, status, rank in decisions):
        return None
    run_strategy_ids = [
        int(row[0]) for row in connection.execute(
            "select strategy_id from selection_results where selection_run_id = ?", [run_id],
        ).fetchall()
    ]
    prior_reviews = latest_user_reviews_by_strategy(connection, run_strategy_ids)
    submitted = {
        int(strategy_id): (None if status is None else str(status), rank)
        for strategy_id, status, rank in decisions
    }
    rank_owners: dict[int, int] = {}
    for strategy_id in run_strategy_ids:
        if strategy_id in submitted:
            status, rank = submitted[strategy_id]
        else:
            review = prior_reviews.get(strategy_id, {})
            status, rank = review.get("user_status"), review.get("user_rank")
        if status != "FINALIST" or rank is None:
            continue
        rank = int(rank)
        if rank in rank_owners and rank_owners[rank] != strategy_id:
            raise SelectionReviewError(
                "SELECTION_REVIEW_INVALID_RANK",
                "FINALIST User Rank values must be unique within the selection run",
            )
        rank_owners[rank] = strategy_id
    return prior_reviews


def import_selection_user_fields(
    connection: duckdb.DuckDBPyConnection, data: bytes,
) -> dict[str, object]:
    """Append operator status/rank cells, treating blanks as explicit clears."""
    metadata, submitted_rows = _parse_selection_user_fields_workbook(data)
    run_id = metadata["selection_run_id"]
    decisions: list[list[object]] = []
    strategy_ids: set[int] = set()
    ranks: set[int] = set()
    for raw_id, raw_status, raw_rank in submitted_rows:
        status_text = "" if raw_status is None else str(raw_status).strip().upper()
        if status_text and status_text not in {"FINALIST", "RESERVE", "REJECTED"}:
            raise SelectionReviewError(
                "SELECTION_REVIEW_INVALID_STATUS",
                "User Status must be FINALIST, RESERVE, REJECTED, or blank",
            )
        strategy_id = _whole_number(raw_id, "SELECTION_REVIEW_ROWSET_MISMATCH", optional=False)
        if strategy_id in strategy_ids:
            raise SelectionReviewError("SELECTION_REVIEW_ROWSET_MISMATCH", "Strategy IDs must be unique in the workbook")
        strategy_ids.add(strategy_id)
        rank = None
        if raw_rank not in (None, ""):
            if status_text not in {"", "FINALIST"}:
                raise SelectionReviewError(
                    "SELECTION_REVIEW_INVALID_RANK", "User Rank must be blank for RESERVE and REJECTED",
                )
            try:
                rank = _whole_number(raw_rank, "SELECTION_REVIEW_INVALID_RANK", optional=False)
            except SelectionReviewError:
                raise SelectionReviewError(
                    "SELECTION_REVIEW_INVALID_RANK", "FINALIST User Rank must be a positive integer",
                ) from None
            if status_text == "FINALIST" and rank in ranks:
                raise SelectionReviewError(
                    "SELECTION_REVIEW_INVALID_RANK", "FINALIST User Rank values must be unique in the workbook",
                )
            if status_text == "FINALIST":
                ranks.add(rank)
        decisions.append([strategy_id, status_text or None, rank])

    if not decisions:
        return {
            "selection_run_id": run_id,
            "row_count": 0,
            "applied_count": 0,
            "unchanged_count": 0,
            "finalist_count": 0,
        }

    workbook_hash = sha256(data).hexdigest()
    review_id = str(uuid4())
    now = datetime.now(timezone.utc)
    connection.execute("begin transaction")
    try:
        instance_id = database_instance_id(connection)
        if metadata["database_instance_id"] != instance_id:
            raise SelectionReviewError("SELECTION_REVIEW_DATABASE_MISMATCH")
        run = connection.execute(
            "select database_instance_id, selection_contract_version from selection_runs where selection_run_id = ?",
            [run_id],
        ).fetchone()
        if not run:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH", "Selection run does not exist in this database")
        if run[0] != instance_id or run[1] != SELECTION_CONTRACT_VERSION_V1:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH", "Selection run metadata does not match this workbook")
        if connection.execute(
            "select 1 from selection_review_imports where workbook_sha256 = ?", [workbook_hash],
        ).fetchone():
            raise SelectionReviewError("SELECTION_REVIEW_ALREADY_IMPORTED")
        _validate_selection_user_field_ids(connection, run_id, tuple(strategy_ids))
        run_reviews = _validate_selection_user_field_ranks(connection, run_id, decisions)
        prior_reviews = run_reviews if run_reviews is not None else latest_user_reviews_by_strategy(
            connection, [row[0] for row in decisions],
        )
        connection.execute(
            """insert into selection_review_imports (
                review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count
            ) values (?, ?, ?, ?, ?)""",
            [review_id, run_id, workbook_hash, now, len(decisions)],
        )
        review_rows = [
            [review_id, strategy_id, status, rank,
             None,
             prior_reviews.get(strategy_id, {}).get("comment")]
            for strategy_id, status, rank in decisions
        ]
        _insert_rows(connection, "selection_review_rows", (
            "review_import_id", "strategy_id", "user_status", "user_rank",
            "user_analog_of_strategy_id", "comment",
        ), review_rows)
        applied_ids = [row[0] for row in decisions]
        if applied_ids:
            connection.execute(
                "delete from strategy_tags where tag = 'REJECTED' and strategy_id in (select unnest(?::bigint[]))",
                [applied_ids],
            )
            rejected = [
                [strategy_id, "REJECTED", "SELECTION_REVIEW", review_id, now]
                for strategy_id, status, _rank in decisions if status == "REJECTED"
            ]
            _insert_rows(connection, "strategy_tags", (
                "strategy_id", "tag", "source", "source_ref", "updated_at_utc",
            ), rejected)
        connection.execute("commit")
    except duckdb.ConstraintException as error:
        _rollback_quietly(connection)
        if "workbook_sha256" in str(error):
            raise SelectionReviewError("SELECTION_REVIEW_ALREADY_IMPORTED") from error
        raise
    except Exception:
        _rollback_quietly(connection)
        raise
    return {
        "review_import_id": review_id,
        "selection_run_id": run_id,
        "row_count": len(decisions),
        "applied_count": len(decisions),
        "unchanged_count": 0,
        "finalist_count": sum(row[1] == "FINALIST" for row in decisions),
    }


def import_retest_tags(connection: duckdb.DuckDBPyConnection, data: bytes) -> dict[str, int]:
    """Apply only explicit RETEST cells from a trusted review workbook."""
    try:
        metadata, rows = _parse_workbook(data)
    except SelectionReviewError as error:
        raise SelectionReviewError("RETEST_TAG_IMPORT_INVALID_FILE") from error
    try:
        instance_id = database_instance_id(connection)
    except SelectionReviewError as error:
        raise SelectionReviewError("RETEST_TAG_IMPORT_DATABASE_MISMATCH") from error
    if metadata.get("database_instance_id") != instance_id:
        raise SelectionReviewError("RETEST_TAG_IMPORT_DATABASE_MISMATCH")
    try:
        strategy_ids = {
            _whole_number(row["ID"], "RETEST_TAG_IMPORT_STRATEGY_MISMATCH", optional=False)
            for row in rows if _normalize_retest(row["RETEST"])
        }
    except SelectionReviewError as error:
        code = "RETEST_TAG_IMPORT_INVALID_RETEST" if error.code == "SELECTION_REVIEW_INVALID_RETEST" else error.code
        raise SelectionReviewError(code) from error
    if not strategy_ids:
        return {"row_count": 0, "retest_count": 0}
    existing = {
        int(row[0]) for row in connection.execute(
            "select strategy_id from strategies where strategy_id in (select unnest(?::bigint[]))", [list(strategy_ids)]
        ).fetchall()
    }
    if existing != strategy_ids:
        raise SelectionReviewError("RETEST_TAG_IMPORT_STRATEGY_MISMATCH", details=sorted(strategy_ids - existing))
    now = datetime.now(timezone.utc)
    rows_to_insert = [[strategy_id, "RETEST", "RETEST_WORKFLOW", sha256(data).hexdigest(), now] for strategy_id in sorted(strategy_ids)]
    connection.execute("begin transaction")
    try:
        connection.executemany(
            """insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc)
               values (?, ?, ?, ?, ?)
               on conflict (strategy_id, tag) do update set
                   source = excluded.source,
                   source_ref = excluded.source_ref,
                   updated_at_utc = excluded.updated_at_utc""",
            rows_to_insert,
        )
        connection.execute("commit")
    except Exception:
        _rollback_quietly(connection)
        raise
    return {"row_count": len(strategy_ids), "retest_count": len(strategy_ids)}


def import_selection_review(connection: duckdb.DuckDBPyConnection, data: bytes) -> dict[str, object]:
    metadata, rows = _parse_workbook(data)
    if metadata.get("database_instance_id") != database_instance_id(connection):
        raise SelectionReviewError("SELECTION_REVIEW_DATABASE_MISMATCH")
    run_id = metadata.get("selection_run_id", "")
    run = connection.execute(
        "select symbol, side, selection_contract_version, request_json, config_json from selection_runs where selection_run_id = ?",
        [run_id],
    ).fetchone()
    if not run:
        raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
    if metadata.get("selection_contract_version") != run[2]:
        raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
    equity_snapshot: Mapping[str, object] | None = None
    if run[2] == SELECTION_CONTRACT_VERSION_V2:
        try:
            request_document = json.loads(run[3])
            if not isinstance(request_document, Mapping):
                raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
            stages = request_document.get("stages")
            if not isinstance(stages, list) or any(not isinstance(stage, Mapping) for stage in stages):
                raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
            rank_stage = next(
                stage for stage in stages
                if stage.get("id") == "rank_robust_top_n"
            )
            snapshot_value = request_document["equity_quality_snapshot"]
            parsed_stages: list[dict[str, object]] = []
            canonical_stage_keys = {
                "id", "enabled", "scope", "min_shift_pct", "pnl_tolerance_pct", "top_n",
            }
            for stage in stages:
                stage_keys = set(stage)
                if stage_keys not in (canonical_stage_keys, canonical_stage_keys | {"method"}):
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
                stage_id = stage.get("id")
                payload = {key: stage[key] for key in ("id", "enabled", "scope")}
                relevant = {"id", "enabled", "scope"}
                if stage_id == "filter_min_shift":
                    relevant.add("min_shift_pct")
                    payload["min_shift_pct"] = stage["min_shift_pct"]
                elif stage_id in {"pareto_shift_near_tie", "pareto_close_ma_near_tie"}:
                    relevant.add("pnl_tolerance_pct")
                    payload["pnl_tolerance_pct"] = stage["pnl_tolerance_pct"]
                elif stage_id == "rank_robust_top_n":
                    relevant.add("top_n")
                    payload["top_n"] = stage["top_n"]
                    if "method" in stage:
                        payload["method"] = stage["method"]
                if any(stage[key] is not None for key in canonical_stage_keys - relevant):
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
                if "method" in stage and stage_id != "rank_robust_top_n":
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
                parsed_stages.append(payload)
            saved_request = parse_selection_request({
                "symbol": request_document.get("symbol"),
                "side": request_document.get("side"),
                "finalists_only": request_document.get("finalists_only", False),
                "stages": parsed_stages,
            }, allow_retired_enabled=True)
            config_document = json.loads(run[4])
            if not isinstance(config_document, Mapping) or type(
                config_document.get("lot_variant_redundancy_enabled")
            ) is not bool:
                raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
            if "effective_stage_order" in request_document:
                expected_stage_order = [
                    stage.id for stage in effective_selection_stages(
                        saved_request,
                        SelectionConfig(lot_variant_redundancy_enabled=config_document["lot_variant_redundancy_enabled"]),
                    )
                ]
                if request_document["effective_stage_order"] != expected_stage_order:
                    raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
            else:
                expected_stage_order = _legacy_effective_stage_order(
                    saved_request, config_document["lot_variant_redundancy_enabled"]
                )
        except (AttributeError, KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH") from None
        if (
            rank_stage.get("enabled") is not True
            or rank_stage.get("method") != "equity_quality_v1"
            or not isinstance(snapshot_value, Mapping)
            or snapshot_value.get("policy_id") != "equity_quality_rank"
            or snapshot_value.get("policy_version") != EQUITY_QUALITY_RANK_POLICY_VERSION
            or snapshot_value.get("method") != "equity_quality_v1"
            or snapshot_value.get("algorithm_version") != ALGORITHM_VERSION
            or not isinstance(snapshot_value.get("effective_stage_order"), list)
            or snapshot_value.get("effective_stage_order") != expected_stage_order
            or not isinstance(snapshot_value.get("sources"), Mapping)
        ):
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        for strategy_id, source in snapshot_value["sources"].items():
            try:
                parsed_id = int(strategy_id)
                result_id = int(source["result_id"])
                source_revision = source["source_revision"]
                facts_sha256 = source["facts_sha256"]
                facts = source["facts"]
                decision_facts = source["decision_facts"]
            except (KeyError, TypeError, ValueError):
                raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH") from None
            if (
                str(parsed_id) != strategy_id or parsed_id <= 0 or result_id <= 0
                or not isinstance(source_revision, str) or len(source_revision) != 64
                or not isinstance(facts_sha256, str) or len(facts_sha256) != 64
                or not isinstance(facts, Mapping) or facts.get("result_id") != result_id
                or sha256(canonical_json(facts).encode()).hexdigest() != facts_sha256
                or not isinstance(decision_facts, Mapping)
                or set(decision_facts) != set(_EQUITY_DECISION_FACT_FIELDS)
                or decision_facts != _equity_decision_facts(facts)
            ):
                raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
        equity_snapshot = snapshot_value
    latest = connection.execute(
        "select selection_run_id from selection_runs where symbol = ? and side = ? order by created_at_utc desc, selection_run_id desc limit 1",
        list(run[:2]),
    ).fetchone()
    latest_run_id = latest[0] if latest else ""
    if latest_run_id != run_id and not _equivalent_selection_runs(connection, run_id, latest_run_id):
        raise SelectionReviewError("SELECTION_REVIEW_NOT_LATEST_RUN")
    workbook_hash = sha256(data).hexdigest()
    if connection.execute("select 1 from selection_review_imports where workbook_sha256 = ?", [workbook_hash]).fetchone():
        raise SelectionReviewError("SELECTION_REVIEW_ALREADY_IMPORTED")
    snapshot_rows = connection.execute(
        """select strategy_id, result_id_at_selection, auto_status, auto_rank, auto_analog_of_strategy_id
             from selection_results where selection_run_id = ?""", [run_id]
    ).fetchall()
    snapshot = {int(row[0]): row[1:] for row in snapshot_rows}
    if equity_snapshot is not None:
        if set(equity_snapshot["sources"]) != {str(strategy_id) for strategy_id in snapshot}:
            raise SelectionReviewError("SELECTION_REVIEW_SCHEMA_MISMATCH")
    names = dict(connection.execute(
        "select strategy_id, strategy_name from strategies where strategy_id in (select unnest(?::bigint[]))", [list(snapshot)]
    ).fetchall()) if snapshot else {}
    submitted: dict[int, dict[str, object]] = {}
    for row in rows:
        strategy_id = _whole_number(row["ID"], "SELECTION_REVIEW_ROWSET_MISMATCH", optional=False)
        if strategy_id in submitted:
            raise SelectionReviewError("SELECTION_REVIEW_ROWSET_MISMATCH")
        submitted[strategy_id] = row
    retest_by_id: dict[int, bool] = {}
    ranks: set[int] = set()
    for strategy_id, row in submitted.items():
        status = str(row["User Status"] or "").strip().upper()
        if status not in STATUSES:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_STATUS")
        rank = _whole_number(row["User Rank"], "SELECTION_REVIEW_INVALID_RANK") if status in {"FINALIST", "RESERVE"} else None
        if rank is not None:
            if rank in ranks:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_RANK")
            ranks.add(rank)
        comment = "" if row["Comment"] is None else str(row["Comment"])
        if len(comment) > 1000:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_FILE")
        retest_by_id[strategy_id] = _normalize_retest(row["RETEST"])
    if set(submitted) != set(snapshot):
        raise SelectionReviewError("SELECTION_REVIEW_ROWSET_MISMATCH")
    decisions: list[list[object]] = []
    for strategy_id, row in submitted.items():
        result_id, auto_status, auto_rank, auto_analog = snapshot[strategy_id]
        if (str(row["Стратегия"]) != names.get(strategy_id)
                or _whole_number(row["Result ID"], "SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED", optional=False) != result_id
                or str(row["Auto Status"]) != auto_status
                or _whole_number(row["Auto Rank"], "SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED") != auto_rank
                or _whole_number(row["Auto Analog Of ID"], "SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED") != auto_analog):
            raise SelectionReviewError("SELECTION_REVIEW_AUTOMATIC_FIELDS_CHANGED")
        status = str(row["User Status"]).strip().upper()
        rank = _whole_number(row["User Rank"], "SELECTION_REVIEW_INVALID_RANK") if status in {"FINALIST", "RESERVE"} else None
        analog = _whole_number(row["Analog Of ID"], "SELECTION_REVIEW_INVALID_ANALOG")
        if status == "ANALOG":
            if analog is None or analog == strategy_id or analog not in submitted:
                raise SelectionReviewError("SELECTION_REVIEW_INVALID_ANALOG")
            target_status = str(submitted[analog]["User Status"] or "").strip().upper()
            if target_status not in {"FINALIST", "RESERVE"}:
                status, analog = "FILTERED", None
        elif analog is not None:
            raise SelectionReviewError("SELECTION_REVIEW_INVALID_ANALOG")
        decisions.append([strategy_id, status, rank, analog, "" if row["Comment"] is None else str(row["Comment"])])
    current = dict(connection.execute(
        "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[]))", [list(snapshot)]
    ).fetchall())
    stale = sorted(strategy_id for strategy_id, values in snapshot.items() if current.get(strategy_id) != values[0])
    if stale:
        raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
    if equity_snapshot is not None:
        stale = _equity_snapshot_stale_ids(connection, equity_snapshot)
        if stale:
            raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
    review_id = str(uuid4())
    now = datetime.now(timezone.utc)
    connection.execute("begin transaction")
    try:
        latest_again = connection.execute(
            "select selection_run_id from selection_runs where symbol = ? and side = ? order by created_at_utc desc, selection_run_id desc limit 1", list(run[:2])
        ).fetchone()
        latest_again_id = latest_again[0] if latest_again else ""
        if latest_again_id != run_id and not _equivalent_selection_runs(connection, run_id, latest_again_id):
            raise SelectionReviewError("SELECTION_REVIEW_NOT_LATEST_RUN")
        current_again = dict(connection.execute(
            "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[]))", [list(snapshot)]
        ).fetchall())
        stale = sorted(strategy_id for strategy_id, values in snapshot.items() if current_again.get(strategy_id) != values[0])
        if stale:
            raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
        if equity_snapshot is not None:
            stale = _equity_snapshot_stale_ids(connection, equity_snapshot)
            if stale:
                raise SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=stale)
        connection.execute(
            """insert into selection_review_imports (
                review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count
            ) values (?, ?, ?, ?, ?)""",
            [review_id, run_id, workbook_hash, now, len(decisions)],
        )
        _insert_rows(connection, "selection_review_rows", (
            "review_import_id", "strategy_id", "user_status", "user_rank", "user_analog_of_strategy_id", "comment",
        ), [[review_id, *decision] for decision in decisions])
        ids = list(snapshot)
        connection.execute("delete from strategy_tags where tag = 'REJECTED' and strategy_id in (select unnest(?::bigint[]))", [ids])
        rejected = [[strategy_id, "REJECTED", "SELECTION_REVIEW", review_id, now] for strategy_id, status, *_ in decisions if status == "REJECTED"]
        _insert_rows(connection, "strategy_tags", (
            "strategy_id", "tag", "source", "source_ref", "updated_at_utc",
        ), rejected)
        blank_retest_ids = [strategy_id for strategy_id in ids if not retest_by_id[strategy_id]]
        if blank_retest_ids:
            connection.execute(
                "delete from strategy_tags where tag = 'RETEST' and source = 'SELECTION_REVIEW' and strategy_id in (select unnest(?::bigint[]))",
                [blank_retest_ids],
            )
        asserted_retest = [
            [strategy_id, "RETEST", "SELECTION_REVIEW", review_id, now]
            for strategy_id in ids if retest_by_id[strategy_id]
        ]
        if asserted_retest:
            connection.executemany(
                """insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc)
                   values (?, ?, ?, ?, ?)
                   on conflict (strategy_id, tag) do update set
                       source = excluded.source,
                       source_ref = excluded.source_ref,
                       updated_at_utc = excluded.updated_at_utc""",
                asserted_retest,
            )
        connection.execute("commit")
    except duckdb.ConstraintException as error:
        _rollback_quietly(connection)
        if "workbook_sha256" in str(error):
            raise SelectionReviewError("SELECTION_REVIEW_ALREADY_IMPORTED") from error
        raise
    except Exception:
        _rollback_quietly(connection)
        raise
    return {"review_import_id": review_id, "selection_run_id": run_id, "row_count": len(decisions), "finalist_count": sum(row[1] == "FINALIST" for row in decisions)}


def latest_user_reviews_by_strategy(
    connection: duckdb.DuckDBPyConnection,
    strategy_ids: Sequence[int] | None = None,
) -> dict[int, dict[str, object]]:
    """Return the newest accepted user row for each strategy identity."""
    if strategy_ids is not None and not strategy_ids:
        return {}
    where = "where rows.strategy_id in (select unnest(?::bigint[]))" if strategy_ids is not None else ""
    params = [list(strategy_ids)] if strategy_ids is not None else []
    reviews: dict[int, dict[str, object]] = {}
    for strategy_id, status, rank, analog, comment in connection.execute(
        f"""select rows.strategy_id, rows.user_status, rows.user_rank,
                          rows.user_analog_of_strategy_id, rows.comment
                   from selection_review_rows rows
                   join selection_review_imports imports using (review_import_id)
                  {where}
                  order by imports.imported_at_utc desc, imports.review_import_id desc""",
        params,
    ).fetchall():
        reviews.setdefault(int(strategy_id), {
            "user_status": None if status is None else str(status),
            "user_rank": None if rank is None else int(rank),
            "user_analog_of_strategy_id": None if analog is None else int(analog),
            "comment": comment,
        })
    return reviews


def effective_selection_decisions(
    connection: duckdb.DuckDBPyConnection,
    *,
    symbol: str | None = None,
    strategy_ids: Sequence[int] | None = None,
) -> dict[int, tuple[str | None, int | None, str | None]]:
    """Resolve ordinary selection snapshots and reviewed scoped overlays.

    A regular selection run is a complete replacement for its Pair+Side, while
    the newest accepted user row for a strategy identity survives later
    unreviewed runs. A server scoped run (RETEST_COHORT or CURRENT_EFFECTIVE) is
    dormant until a review is imported, then only its reviewed rows overlay the
    prior decision.
    """
    if strategy_ids is not None:
        strategy_ids = tuple(strategy_ids)
        if symbol is None:
            raise ValueError("symbol is required when strategy_ids is supplied")
        if len(set(strategy_ids)) != len(strategy_ids):
            raise ValueError("strategy_ids must not contain duplicate IDs")
        if not strategy_ids:
            return {}
    # The run/review queries below stay Pair+Side scoped so overlay activation
    # sees the complete history for the selected symbol. Their per-strategy
    # payload is narrowed separately by strategy_ids in review/result queries.
    run_scope = "where runs.symbol = ?" if symbol is not None else ""
    scope_params = [symbol] if symbol is not None else []
    runs = connection.execute(
        f"""select runs.selection_run_id, runs.symbol, runs.side, runs.request_json
               from selection_runs runs {run_scope}
              order by runs.created_at_utc asc, runs.selection_run_id asc""", scope_params
    ).fetchall()
    states: dict[tuple[str, str], dict[int, tuple[str | None, int | None, str | None]]] = {}
    latest_reviews = latest_user_reviews_by_strategy(connection, strategy_ids)
    overlay_run_ids: set[str] = set()
    for run_id, _run_symbol, _run_side, raw_request in runs:
        try:
            request = json.loads(str(raw_request))
        except (TypeError, ValueError):
            request = {}
        if isinstance(request, Mapping) and request.get("ranking_scope") in {"RETEST_COHORT", "CURRENT_EFFECTIVE"}:
            overlay_run_ids.add(str(run_id))

    review_strategy_ids_by_run: dict[str, set[int]] = {}
    reviewed_runs: set[str] = set()
    # Review membership is run-level and therefore remains symbol-scoped; the
    # selected strategy IDs only control which payload rows can enter a state.
    review_row_scope = ""
    review_params = list(scope_params)
    if strategy_ids is not None:
        review_row_scope = " and (rows.strategy_id is null or rows.strategy_id in (select unnest(?::bigint[])))"
        review_params.append(list(strategy_ids))
    for run_id, strategy_id in connection.execute(
        f"""with latest_imports as (
                   select ranked.selection_run_id, ranked.review_import_id
                     from (
                           select imports.selection_run_id, imports.review_import_id,
                                  row_number() over (
                                      partition by imports.selection_run_id
                                      order by imports.imported_at_utc desc, imports.review_import_id desc
                                  ) as rn
                             from selection_review_imports imports
                             join selection_runs runs using (selection_run_id)
                            {run_scope}
                          ) ranked
                    where ranked.rn = 1
               )
               select latest_imports.selection_run_id, rows.strategy_id
                 from latest_imports
                 left join selection_review_rows rows
                    on rows.review_import_id = latest_imports.review_import_id
                   {review_row_scope}""",
         review_params,
    ).fetchall():
        run_key = str(run_id)
        if run_key not in overlay_run_ids:
            continue
        reviewed_runs.add(run_key)
        if strategy_id is not None:
            review_strategy_ids_by_run.setdefault(run_key, set()).add(int(strategy_id))

    run_positions = {str(run[0]): index for index, run in enumerate(runs)}
    result_scope = run_scope
    result_params = list(scope_params)
    if strategy_ids is not None:
        result_scope += (" and" if result_scope else "where") + (
            " results.strategy_id in (select unnest(?::bigint[]))"
        )
        result_params.append(list(strategy_ids))
    result_cursor = connection.execute(
        f"""select results.selection_run_id, results.strategy_id, results.prior_rejected
               from selection_results results
               join selection_runs runs using (selection_run_id)
              {result_scope}
              order by runs.created_at_utc asc, runs.selection_run_id asc, results.rowid asc""",
        result_params,
    )
    def result_rows():
        while batch := result_cursor.fetchmany(1024):
            yield from batch

    result_iter = iter(result_rows())
    pending_result = next(result_iter, None)
    latest_run_for_strategy: dict[int, str] = {}

    for run_position, (run_id, run_symbol, run_side, _raw_request) in enumerate(runs):
        run_key = str(run_id)
        group = (str(run_symbol), str(run_side))
        overlay = run_key in overlay_run_ids
        active = not overlay or run_key in reviewed_runs
        if active:
            if not overlay:
                states[group] = {}
            state = states.setdefault(group, {})
        else:
            state = None
        review_strategy_ids = review_strategy_ids_by_run.get(run_key, set())
        while pending_result is not None:
            result_run_key = str(pending_result[0])
            result_position = run_positions.get(result_run_key)
            if result_position is None:
                pending_result = next(result_iter, None)
                continue
            if result_position < run_position:
                raise ValueError("selection results out of order")
            if result_position > run_position:
                break
            strategy_id, prior_rejected = int(pending_result[1]), pending_result[2]
            if active and (not overlay or strategy_id in review_strategy_ids):
                assert state is not None
                latest_run_for_strategy[strategy_id] = run_key
                if strategy_id in latest_reviews:
                    review_row = latest_reviews[strategy_id]
                    status, rank = review_row["user_status"], review_row["user_rank"]
                    state[strategy_id] = (status, None if rank is None else int(rank), run_key)
                elif prior_rejected:
                    state[strategy_id] = ("REJECTED", None, run_key)
            pending_result = next(result_iter, None)
    while pending_result is not None:
        if run_positions.get(str(pending_result[0])) is not None:
            raise ValueError("selection results out of order")
        pending_result = next(result_iter, None)
    decisions = {strategy_id: decision for state in states.values() for strategy_id, decision in state.items()}
    for strategy_id in _rejected_strategy_ids(connection, strategy_ids):
        if strategy_id in latest_run_for_strategy:
            prior = decisions.get(strategy_id)
            rank = (prior[1] if prior else latest_reviews.get(strategy_id, {}).get("user_rank"))
            decisions[strategy_id] = ("REJECTED", rank, latest_run_for_strategy[strategy_id])
    return decisions


def latest_effective_finalists(connection: duckdb.DuckDBPyConnection, symbol: str) -> tuple[bool, set[int]]:
    has_runs = connection.execute("select 1 from selection_runs where symbol = ? limit 1", [symbol]).fetchone() is not None
    decisions = effective_selection_decisions(connection, symbol=symbol)
    return has_runs, {strategy_id for strategy_id, decision in decisions.items() if decision[0] == "FINALIST"}
