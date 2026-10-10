"""Atomic v11 publication writes for new Performance v2 mutation routes.

This module intentionally owns the only new publication writer. Existing
snapshot/review callers remain in performance_v2_selection_review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

import duckdb

from .performance_v2_store import PerformanceV2WriterLock, require_performance_v2


_REVIEW_KEY_DOMAIN = "performance_v2_selection_review_import_v11"
_OVERLAY_RUN_KEY_DOMAIN = "performance_v2_selection_overlay_run_v11"
_OPERATION_KEY_DOMAIN = "performance_v2_publication_operation_v11"
_AGGREGATE_MANIFEST_DOMAIN = "performance_v2_aggregate_manifest_v11"
_ALLOWED_AUTO_STATUSES = frozenset({"FINALIST", "RESERVE", "ANALOG", "FILTERED"})
_ALLOWED_REVIEW_STATUSES = frozenset({"FINALIST", "RESERVE", "ANALOG", "FILTERED", "REJECTED"})


class PublicationError(ValueError):
    """A typed, zero-write publication failure."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class PublicationConflict(PublicationError):
    """The operation key or immutable upload identity conflicts."""


class PublicationNotFound(PublicationError):
    """A publication/source artifact is unknown, retired, or expired."""


class PublicationValidationError(PublicationError):
    """The frozen package is malformed or stale."""


@dataclass(frozen=True, slots=True)
class PublicationReview:
    strategy_id: int
    user_status: str | None
    user_rank: int | None
    user_analog_of_strategy_id: int | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class PublicationRun:
    selection_run_id: str
    database_instance_id: str | None
    symbol: str
    side: str
    selection_contract_version: str
    request_json: str
    request_sha256: str
    config_json: str
    config_sha256: str
    candidate_count: int
    representative_count: int
    auto_finalist_count: int
    top_n: int
    results: tuple[Mapping[str, object], ...] = ()
    workbook_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationPartition:
    pair: str
    side: str
    source_run_id: str | None
    overlay_run: PublicationRun | None
    reviews: tuple[PublicationReview, ...] = ()
    rejection_sources: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationAggregate:
    aggregate_import_id: str
    uploaded_workbook_sha256: str
    partition_rowsets_json: str
    candidate_identities_json: str
    operation_digest: str | None = None
    manifest_contract_version: str | None = None
    source_revision: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationPackage:
    publication_id: str
    publication_kind: str
    operation_key: str
    operation_digest: str
    manifest_contract_version: str
    decision_group_id: str
    database_instance_id: str | None
    source_revision: str
    controls_json: str
    controls_sha256: str
    render_model_json: str
    render_model_sha256: str
    evaluated_rowset_sha256: str
    partitions: tuple[PublicationPartition, ...]
    export_workbook_sha256: str | None = None
    aggregate: PublicationAggregate | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PublicationPackage":
        if not isinstance(raw, Mapping):
            raise PublicationValidationError("INVALID_ARGUMENT", "publication package must be an object")

        def text(name: str, optional: bool = False) -> str | None:
            value = raw.get(name)
            if value is None and optional:
                return None
            if not isinstance(value, str):
                raise PublicationValidationError("INVALID_ARGUMENT", f"{name} must be text")
            return value

        def nested_text(mapping: Mapping[str, object], name: str, optional: bool = False) -> str | None:
            value = mapping.get(name)
            if value is None and optional:
                return None
            if not isinstance(value, str):
                raise PublicationValidationError("INVALID_ARGUMENT", f"{name} must be text")
            return value

        def integer(mapping: Mapping[str, object], name: str) -> int:
            value = mapping.get(name)
            if type(value) is not int:
                raise PublicationValidationError("INVALID_ARGUMENT", f"{name} must be an integer")
            return value

        def optional_integer(mapping: Mapping[str, object], name: str) -> int | None:
            value = mapping.get(name)
            if value is None:
                return None
            if type(value) is not int:
                raise PublicationValidationError("INVALID_ARGUMENT", f"{name} must be an integer")
            return value

        def run(value: object) -> PublicationRun | None:
            if value is None:
                return None
            if isinstance(value, PublicationRun):
                return value
            if not isinstance(value, Mapping):
                raise PublicationValidationError("INVALID_ARGUMENT", "overlay_run must be an object")
            rows = value.get("results", ())
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                raise PublicationValidationError("INVALID_ARGUMENT", "overlay_run.results must be a list")
            decoded_results: list[Mapping[str, object]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise PublicationValidationError("INVALID_ARGUMENT", "overlay_run result must be an object")
                decoded_results.append(row)
            return PublicationRun(
                selection_run_id=nested_text(value, "selection_run_id") or "",
                database_instance_id=nested_text(value, "database_instance_id", True),
                symbol=nested_text(value, "symbol") or "", side=nested_text(value, "side") or "",
                selection_contract_version=nested_text(value, "selection_contract_version") or "",
                request_json=nested_text(value, "request_json") or "", request_sha256=nested_text(value, "request_sha256") or "",
                config_json=nested_text(value, "config_json") or "", config_sha256=nested_text(value, "config_sha256") or "",
                candidate_count=integer(value, "candidate_count"),
                representative_count=integer(value, "representative_count"),
                auto_finalist_count=integer(value, "auto_finalist_count"), top_n=integer(value, "top_n"),
                results=tuple(decoded_results),
                workbook_sha256=nested_text(value, "workbook_sha256", True),
            )

        raw_partitions = raw.get("partitions", ())
        if not isinstance(raw_partitions, Sequence) or isinstance(raw_partitions, (str, bytes, bytearray)):
            raise PublicationValidationError("INVALID_ARGUMENT", "partitions must be a list")
        partitions: list[PublicationPartition] = []
        for item in raw_partitions:
            if not isinstance(item, Mapping):
                raise PublicationValidationError("INVALID_ARGUMENT", "partition must be an object")
            reviews = item.get("reviews", ())
            if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes, bytearray)):
                raise PublicationValidationError("INVALID_ARGUMENT", "reviews must be a list")
            decoded_review_list: list[PublicationReview] = []
            for review in reviews:
                if isinstance(review, PublicationReview):
                    decoded_review_list.append(review)
                elif isinstance(review, Mapping):
                    user_status = nested_text(review, "user_status", True)
                    comment = nested_text(review, "comment", True)
                    decoded_review_list.append(PublicationReview(
                        strategy_id=integer(review, "strategy_id"),
                        user_status=user_status,
                        user_rank=optional_integer(review, "user_rank"),
                        user_analog_of_strategy_id=optional_integer(review, "user_analog_of_strategy_id"),
                        comment=comment,
                    ))
                else:
                    raise PublicationValidationError("INVALID_ARGUMENT", "review must be an object")
            decoded_reviews = tuple(decoded_review_list)
            sources = item.get("rejection_sources", ())
            if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
                raise PublicationValidationError("INVALID_ARGUMENT", "rejection_sources must be a list")
            decoded_sources: list[Mapping[str, object]] = []
            for source in sources:
                if not isinstance(source, Mapping):
                    raise PublicationValidationError("INVALID_ARGUMENT", "rejection source must be an object")
                decoded_sources.append(source)
            partitions.append(PublicationPartition(
                pair=nested_text(item, "pair") or "", side=nested_text(item, "side") or "",
                source_run_id=nested_text(item, "source_run_id", True),
                overlay_run=run(item.get("overlay_run")), reviews=decoded_reviews,
                rejection_sources=tuple(decoded_sources),
            ))
        aggregate_raw = raw.get("aggregate")
        aggregate = None
        if aggregate_raw is not None:
            if not isinstance(aggregate_raw, Mapping):
                raise PublicationValidationError("INVALID_ARGUMENT", "aggregate must be an object")
            aggregate = PublicationAggregate(
                aggregate_import_id=nested_text(aggregate_raw, "aggregate_import_id") or "",
                uploaded_workbook_sha256=nested_text(aggregate_raw, "uploaded_workbook_sha256") or "",
                partition_rowsets_json=nested_text(aggregate_raw, "partition_rowsets_json") or "",
                candidate_identities_json=nested_text(aggregate_raw, "candidate_identities_json") or "",
                operation_digest=nested_text(aggregate_raw, "operation_digest", True),
                manifest_contract_version=nested_text(aggregate_raw, "manifest_contract_version", True),
                source_revision=nested_text(aggregate_raw, "source_revision", True),
            )
        return cls(
            publication_id=text("publication_id") or "", publication_kind=text("publication_kind") or "",
            operation_key=text("operation_key") or "", operation_digest=text("operation_digest") or "",
            manifest_contract_version=text("manifest_contract_version") or "",
            decision_group_id=text("decision_group_id") or "",
            database_instance_id=text("database_instance_id", True),
            source_revision=text("source_revision") or "", controls_json=text("controls_json") or "",
            controls_sha256=text("controls_sha256") or "", render_model_json=text("render_model_json") or "",
            render_model_sha256=text("render_model_sha256") or "", evaluated_rowset_sha256=text("evaluated_rowset_sha256") or "",
            export_workbook_sha256=text("export_workbook_sha256", True),
            partitions=tuple(partitions), aggregate=aggregate,
        )


@dataclass(frozen=True, slots=True)
class PublicationResult:
    publication_id: str
    code: str = "COMMITTED"
    normalized_latest: datetime | None = None
    review_import_ids: tuple[str, ...] = ()
    aggregate_import_id: str | None = None
    decision_group_id: str | None = None


def _canonical_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\0" in value:
        raise PublicationValidationError("INVALID_ARGUMENT", f"invalid {field}")
    return value.lower()


def _domain_digest(domain: str, value: str) -> str:
    return sha256(domain.encode("utf-8") + b"\0" + value.encode("utf-8")).hexdigest()


def review_import_key(reference_kind: str, reference_id: str, selection_run_id: str) -> str:
    if reference_kind not in {"publication_id", "aggregate_import_id"}:
        raise PublicationValidationError("INVALID_ARGUMENT", "invalid review reference kind")
    reference = _canonical_id(reference_id, "reference_id")
    run = _canonical_id(selection_run_id, "selection_run_id")
    return _domain_digest(_REVIEW_KEY_DOMAIN, "\0".join((reference_kind, reference, run)))


def overlay_run_key(
    publication_id: str, pair: str, side: str, source_run_id: str, overlay_run_id: str,
) -> str:
    manifest = json.dumps(
        [_canonical_id(publication_id, "publication_id"), pair, side,
         _canonical_id(source_run_id, "source_run_id"), _canonical_id(overlay_run_id, "overlay_run_id")],
        ensure_ascii=False, separators=(",", ":"),
    )
    return _domain_digest(_OVERLAY_RUN_KEY_DOMAIN, manifest)


def operation_digest(manifest: object) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _domain_digest(_OPERATION_KEY_DOMAIN, encoded)


def aggregate_manifest_digest(manifest: object) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _domain_digest(_AGGREGATE_MANIFEST_DOMAIN, encoded)


def _as_package(package: PublicationPackage | Mapping[str, object]) -> PublicationPackage:
    if isinstance(package, PublicationPackage):
        return package
    if isinstance(package, Mapping):
        return PublicationPackage.from_mapping(package)
    raise PublicationValidationError("INVALID_ARGUMENT", "publication package must be an object")


def _validate_package(package: PublicationPackage) -> None:
    _canonical_id(package.publication_id, "publication_id")
    _canonical_id(package.operation_key, "operation_key")
    if not package.operation_digest or not isinstance(package.operation_digest, str):
        raise PublicationValidationError("INVALID_ARGUMENT", "operation_digest is required")
    if package.publication_kind not in {"AUTO_REJECTION_OVERLAY", "AGGREGATE_REVIEW"}:
        raise PublicationValidationError("INVALID_ARGUMENT", "unsupported publication kind")
    if (
        package.publication_kind == "AUTO_REJECTION_OVERLAY" and package.aggregate is not None
    ) or (
        package.publication_kind == "AGGREGATE_REVIEW" and package.aggregate is None
    ):
        raise PublicationValidationError("INVALID_ARGUMENT", "publication kind and aggregate do not match")
    for name in (
        "manifest_contract_version", "decision_group_id", "source_revision", "controls_json",
        "controls_sha256", "render_model_json", "render_model_sha256", "evaluated_rowset_sha256",
    ):
        if not isinstance(getattr(package, name), str) or not getattr(package, name):
            raise PublicationValidationError("INVALID_ARGUMENT", f"{name} is required")
    for name in ("controls_json", "render_model_json"):
        try:
            json.loads(getattr(package, name))
        except (TypeError, ValueError):
            raise PublicationValidationError("INVALID_ARGUMENT", f"{name} is not JSON") from None
    if package.export_workbook_sha256 is not None and not isinstance(package.export_workbook_sha256, str):
        raise PublicationValidationError("INVALID_ARGUMENT", "export_workbook_sha256 is invalid")
    if not package.partitions:
        raise PublicationValidationError("INVALID_ARGUMENT", "at least one partition is required")
    seen_partitions: set[tuple[str, str]] = set()
    seen_runs: set[str] = set()
    seen_strategies: set[int] = set()
    for partition in package.partitions:
        pair = _canonical_id(partition.pair, "pair")
        side = _canonical_id(partition.side, "side").upper()
        if partition.pair != pair.upper() or partition.side != side:
            raise PublicationValidationError("INVALID_ARGUMENT", "pair and side must be canonical uppercase")
        if side not in {"LONG", "SHORT"}:
            raise PublicationValidationError("INVALID_ARGUMENT", "invalid side")
        key = (pair, side)
        if key in seen_partitions:
            raise PublicationValidationError("DUPLICATE_PARTITION")
        seen_partitions.add(key)
        if partition.source_run_id is not None:
            seen_runs.add(_canonical_id(partition.source_run_id, "source_run_id"))
        overlay = partition.overlay_run
        if package.aggregate is not None and overlay is not None and partition.source_run_id is None:
            raise PublicationValidationError("OVERLAY_SOURCE_REQUIRED")
        if overlay is None:
            if package.publication_kind == "AUTO_REJECTION_OVERLAY":
                raise PublicationValidationError("INVALID_ARGUMENT", "automatic publication needs an overlay run")
        else:
            overlay_id = _canonical_id(overlay.selection_run_id, "selection_run_id")
            if overlay_id in seen_runs:
                raise PublicationValidationError("DUPLICATE_SELECTION_RUN")
            seen_runs.add(overlay_id)
            if overlay.symbol != pair.upper() or overlay.side != side:
                raise PublicationValidationError("PARTITION_MISMATCH")
            if package.aggregate is None and package.publication_kind == "AUTO_REJECTION_OVERLAY":
                try:
                    request = json.loads(overlay.request_json)
                except (TypeError, ValueError):
                    raise PublicationValidationError("INVALID_REQUEST") from None
                if not isinstance(request, Mapping) or request.get("ranking_scope") != "AUTOMATIC_REJECTION_OVERLAY":
                    raise PublicationValidationError("INVALID_OVERLAY_MARKER")
            if min(overlay.candidate_count, overlay.representative_count, overlay.auto_finalist_count) < 0 or overlay.top_n < 1:
                raise PublicationValidationError("INVALID_ARGUMENT", "invalid overlay run counts")
            result_ids: set[int] = set()
            for row in overlay.results:
                if not isinstance(row, Mapping):
                    raise PublicationValidationError("INVALID_SELECTION_ROWSET")
                strategy_id = row.get("strategy_id")
                if type(strategy_id) is not int or strategy_id <= 0 or strategy_id in result_ids:
                    raise PublicationValidationError("INVALID_SELECTION_ROWSET")
                result_ids.add(strategy_id)
                if row.get("auto_status") not in _ALLOWED_AUTO_STATUSES:
                    raise PublicationValidationError("INVALID_SELECTION_ROWSET")
                if type(row.get("result_id_at_selection")) is not int:
                    raise PublicationValidationError("INVALID_SELECTION_ROWSET")
                if not isinstance(row.get("stage_trace_json"), str):
                    raise PublicationValidationError("INVALID_SELECTION_ROWSET")
        review_ids: set[int] = set()
        ranks: set[int] = set()
        for review in partition.reviews:
            if type(review.strategy_id) is not int or review.strategy_id <= 0 or review.strategy_id in review_ids:
                raise PublicationValidationError("INVALID_REVIEW_ROWSET")
            review_ids.add(review.strategy_id)
            if review.user_status is not None and review.user_status not in _ALLOWED_REVIEW_STATUSES:
                raise PublicationValidationError("INVALID_STATUS")
            if review.user_status in {"FINALIST", "RESERVE"}:
                if type(review.user_rank) is not int or review.user_rank <= 0 or review.user_rank in ranks:
                    raise PublicationValidationError("INVALID_RANK")
                ranks.add(review.user_rank)
            elif review.user_rank is not None:
                raise PublicationValidationError("INVALID_RANK")
            if review.user_status != "ANALOG" and review.user_analog_of_strategy_id is not None:
                raise PublicationValidationError("INVALID_ANALOG")
        partition_strategies = result_ids | review_ids if overlay is not None else review_ids
        if seen_strategies.intersection(partition_strategies):
            raise PublicationValidationError("DUPLICATE_STRATEGY")
        seen_strategies.update(partition_strategies)
        for source in partition.rejection_sources:
            if (
                source.get("source_kind") != "EQUITY_REGIME_FILTER"
                or not isinstance(source.get("reason_code"), str)
                or type(source.get("strategy_id")) is not int
                or type(source.get("first_result_id")) is not int
                or not isinstance(source.get("classifier_algo_version"), str)
                or not isinstance(source.get("source_revision"), str)
                or not isinstance(source.get("facts_sha256"), str)
            ):
                raise PublicationValidationError("INVALID_REJECTION_SOURCE")
        if partition.rejection_sources and not partition.reviews:
            raise PublicationValidationError("INVALID_REJECTION_SOURCE_SCOPE")
    if package.aggregate is not None:
        _canonical_id(package.aggregate.aggregate_import_id, "aggregate_import_id")
        if not package.aggregate.uploaded_workbook_sha256:
            raise PublicationValidationError("INVALID_ARGUMENT", "uploaded workbook hash is required")
        if package.aggregate.operation_digest not in (None, package.operation_digest):
            raise PublicationValidationError("AGGREGATE_OPERATION_CONFLICT")
    elif package.publication_kind == "AGGREGATE_REVIEW":
        raise PublicationValidationError("INVALID_ARGUMENT", "aggregate publication needs an import header")


def _rollback(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        connection.execute("rollback")
    except Exception:
        pass


def _effective_rejected(connection: duckdb.DuckDBPyConnection, strategy_id: int) -> bool:
    return connection.execute(
        """select 1 from strategy_tags where strategy_id = ? and tag = 'REJECTED'
           union all select 1 from strategy_rejection_sources where strategy_id = ?
           union all select 1 from (
               select rows.user_status,
                      row_number() over (order by imports.imported_at_utc desc, imports.review_import_id desc) rn
                 from selection_review_rows rows
                 join selection_review_imports imports using (review_import_id)
                where rows.strategy_id = ?
           ) where rn = 1 and user_status = 'REJECTED' limit 1""",
        [strategy_id, strategy_id, strategy_id],
    ).fetchone() is not None


def _revalidate_source_runs(connection: duckdb.DuckDBPyConnection, package: PublicationPackage, db_id: str) -> None:
    if package.database_instance_id is not None and _canonical_id(package.database_instance_id, "database_instance_id") != db_id.lower():
        raise PublicationValidationError("DATABASE_INSTANCE_MISMATCH")
    for partition in package.partitions:
        if partition.source_run_id is not None:
            row = connection.execute(
                "select database_instance_id, symbol, side from selection_runs where selection_run_id = ?",
                [_canonical_id(partition.source_run_id, "source_run_id")],
            ).fetchone()
            if row is None:
                raise PublicationNotFound("SOURCE_RUN_NOT_FOUND")
            if str(row[0]).lower() != db_id.lower():
                raise PublicationValidationError("SOURCE_RUN_DATABASE_MISMATCH")
            if (str(row[1]).lower(), str(row[2]).upper()) != (_canonical_id(partition.pair, "pair"), _canonical_id(partition.side, "side").upper()):
                raise PublicationValidationError("SOURCE_RUN_PARTITION_MISMATCH")
        is_aggregate = package.publication_kind == "AGGREGATE_REVIEW"
        target_run = partition.source_run_id if is_aggregate and partition.source_run_id else (
            partition.overlay_run.selection_run_id if partition.overlay_run else partition.source_run_id
        )
        if target_run is not None and partition.reviews and (
            is_aggregate or partition.overlay_run is None
        ):
            target_run = _canonical_id(target_run, "selection_run_id")
            for review in partition.reviews:
                selected = connection.execute(
                    """select result_id_at_selection from selection_results
                       where selection_run_id = ? and strategy_id = ?""",
                    [target_run, review.strategy_id],
                ).fetchone()
                if selected is None:
                    raise PublicationValidationError("REVIEW_ROW_OUTSIDE_RUN")
                current_result = connection.execute(
                    "select current_result_id from strategies where strategy_id = ?",
                    [review.strategy_id],
                ).fetchone()
                if current_result is None:
                    raise PublicationNotFound("STRATEGY_NOT_FOUND")
                if current_result[0] != selected[0]:
                    raise PublicationValidationError("STALE_RESULTS")
        overlay = partition.overlay_run
        if overlay is None:
            continue
        if overlay.database_instance_id is not None and _canonical_id(
            overlay.database_instance_id, "database_instance_id"
        ) != db_id.lower():
            raise PublicationValidationError("OVERLAY_DATABASE_MISMATCH")
        overlay_id = _canonical_id(overlay.selection_run_id, "selection_run_id")
        if connection.execute("select 1 from selection_runs where selection_run_id = ?", [overlay_id]).fetchone() is not None:
            raise PublicationConflict("SELECTION_RUN_EXISTS")
        ids = [int(row["strategy_id"]) for row in overlay.results]
        if ids:
            current = dict(connection.execute(
                "select strategy_id, current_result_id from strategies where strategy_id in (select unnest(?::bigint[]))",
                [ids],
            ).fetchall())
            for row in overlay.results:
                strategy_id = int(row["strategy_id"])
                if strategy_id not in current:
                    raise PublicationNotFound("STRATEGY_NOT_FOUND")
                if current[strategy_id] is None or int(current[strategy_id]) != int(row["result_id_at_selection"]):
                    raise PublicationValidationError("STALE_RESULTS")
            review_ids = {review.strategy_id for review in partition.reviews}
            if not review_ids.issubset(set(ids)):
                raise PublicationValidationError("REVIEW_ROW_OUTSIDE_OVERLAY")


def _normalized_latest(connection: duckdb.DuckDBPyConnection) -> datetime:
    current, review_latest, aggregate_latest, publication_latest, run_latest = connection.execute(
        """select current_timestamp,
                  (select max(imported_at_utc) from selection_review_imports),
                  (select max(imported_at_utc) from selection_aggregate_imports),
                  (select max(created_at_utc) from selection_publications),
                  (select max(created_at_utc) from selection_runs)"""
    ).fetchone()
    value = current
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    for latest in (review_latest, aggregate_latest, publication_latest, run_latest):
        if latest is None:
            continue
        if latest.tzinfo is None or latest.utcoffset() is None:
            latest = latest.replace(tzinfo=timezone.utc)
        latest_value = latest.astimezone(timezone.utc) + timedelta(microseconds=1)
        if latest_value > value:
            value = latest_value
    return value


def _insert_overlay_run(
    connection: duckdb.DuckDBPyConnection, package: PublicationPackage, partition: PublicationPartition,
    normalized_latest: datetime,
) -> None:
    overlay = partition.overlay_run
    if overlay is None:
        return
    overlay_id = _canonical_id(overlay.selection_run_id, "selection_run_id")
    db_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    workbook = overlay_run_key(
        package.publication_id, partition.pair, partition.side,
        partition.source_run_id or "none", overlay_id,
    )
    if overlay.workbook_sha256 is not None and overlay.workbook_sha256 != workbook:
        raise PublicationValidationError("OVERLAY_WORKBOOK_KEY_MISMATCH")
    connection.execute(
        """insert into selection_runs (
            selection_run_id, database_instance_id, symbol, side,
            selection_contract_version, request_json, request_sha256, config_json,
            config_sha256, candidate_count, representative_count, auto_finalist_count,
            top_n, workbook_sha256, created_at_utc
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [overlay_id, db_id, partition.pair, partition.side, overlay.selection_contract_version,
         overlay.request_json, overlay.request_sha256, overlay.config_json, overlay.config_sha256,
         overlay.candidate_count, overlay.representative_count, overlay.auto_finalist_count,
         overlay.top_n, workbook, normalized_latest],
    )
    for row in overlay.results:
        connection.execute(
            """insert into selection_results (
                selection_run_id, strategy_id, result_id_at_selection, auto_status,
                auto_score, auto_rank, auto_reason, analog_group_key,
                auto_analog_of_strategy_id, prior_rejected, stage_trace_json, equity_regime_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [overlay_id, row["strategy_id"], row["result_id_at_selection"], row["auto_status"],
             row.get("auto_score"), row.get("auto_rank"), row.get("auto_reason"), row.get("analog_group_key"),
             row.get("auto_analog_of_strategy_id"), bool(row.get("prior_rejected", False)),
             row["stage_trace_json"], row.get("equity_regime_json")],
        )


def _write_review_projection(
    connection: duckdb.DuckDBPyConnection,
    package: PublicationPackage,
    partition: PublicationPartition,
    normalized_latest: datetime,
    aggregate_id: str | None,
) -> str | None:
    if not partition.reviews:
        return None
    is_aggregate = package.publication_kind == "AGGREGATE_REVIEW"
    target_run = partition.source_run_id if is_aggregate and partition.source_run_id else (
        partition.overlay_run.selection_run_id if partition.overlay_run else partition.source_run_id
    )
    if target_run is None:
        raise PublicationValidationError("REVIEW_TARGET_RUN_MISSING")
    target_run = _canonical_id(target_run, "selection_run_id")
    reference_kind = "aggregate_import_id" if is_aggregate else "publication_id"
    reference_id = aggregate_id or package.publication_id
    workbook_key = review_import_key(reference_kind, reference_id, target_run)
    prior_effective_rejected = {
        row.strategy_id: _effective_rejected(connection, row.strategy_id)
        for row in partition.reviews
    }
    effective_reviews = tuple(
        row for row in partition.reviews
        if is_aggregate or not prior_effective_rejected[row.strategy_id]
    )
    if not effective_reviews:
        return None
    review_id = str(uuid4())
    connection.execute(
        """insert into selection_review_imports (
            review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count, aggregate_import_id
        ) values (?, ?, ?, ?, ?, ?)""",
        [review_id, target_run, workbook_key, normalized_latest, len(effective_reviews), aggregate_id],
    )
    connection.executemany(
        """insert into selection_review_rows (
            review_import_id, strategy_id, user_status, user_rank,
            user_analog_of_strategy_id, comment
        ) values (?, ?, ?, ?, ?, ?)""",
        [[review_id, row.strategy_id, row.user_status, row.user_rank,
          row.user_analog_of_strategy_id, row.comment] for row in effective_reviews],
    )
    for row in effective_reviews:
        if row.user_status == "REJECTED":
            connection.execute(
                "delete from strategy_tags where strategy_id = ? and tag = 'REJECTED'",
                [row.strategy_id],
            )
            connection.execute(
                """insert into strategy_tags (strategy_id, tag, source, source_ref, updated_at_utc)
                   values (?, 'REJECTED', ?, ?, ?)""",
                [row.strategy_id, "SELECTION_REVIEW" if aggregate_id else "SELECTION_AUTOMATIC_REJECTION",
                 review_id, normalized_latest],
            )
    for source in partition.rejection_sources:
        if source.get("strategy_id") not in {
            row.strategy_id for row in effective_reviews if row.user_status == "REJECTED"
        }:
            continue
        existing_source = connection.execute(
            """select 1 from strategy_rejection_sources
                 where strategy_id = ? and source_kind = ? and reason_code = ?""",
            [source["strategy_id"], source["source_kind"], source["reason_code"]],
        ).fetchone()
        if existing_source is not None:
            continue
        connection.execute(
            """insert into strategy_rejection_sources (
                strategy_id, source_kind, reason_code, first_result_id,
                first_selection_run_id, classifier_algo_version, source_revision,
                facts_sha256, created_at_utc
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [source["strategy_id"], source["source_kind"], source["reason_code"], source["first_result_id"],
             target_run, source["classifier_algo_version"], source["source_revision"], source["facts_sha256"], normalized_latest],
        )
    return review_id


def _lookup_operation(
    connection: duckdb.DuckDBPyConnection, operation_key: str,
) -> tuple[object, ...] | None:
    return connection.execute(
        "select publication_id, operation_digest, retired_at_utc from selection_publications where operation_key = ?",
        [operation_key],
    ).fetchone()


def _retry_existing_operation(
    connection: duckdb.DuckDBPyConnection,
    existing: tuple[object, ...],
    package: PublicationPackage,
) -> PublicationResult:
    if existing[2] is not None:
        raise PublicationNotFound("PUBLICATION_KEY_NOT_FOUND")
    if str(existing[1]) != package.operation_digest:
        raise PublicationConflict("OPERATION_KEY_CONFLICT")
    decision_group_id = connection.execute(
        "select decision_group_id from selection_publications where publication_id = ?",
        [str(existing[0])],
    ).fetchone()[0]
    if package.publication_kind != "AGGREGATE_REVIEW":
        return PublicationResult(
            str(existing[0]), "ALREADY_COMMITTED_REEXPORT_REQUIRED",
            decision_group_id=str(decision_group_id),
        )
    if package.aggregate is None:
        raise PublicationValidationError("INVALID_ARGUMENT", "aggregate publication needs an import header")
    aggregate_id = _canonical_id(package.aggregate.aggregate_import_id, "aggregate_import_id")
    stored = connection.execute(
        """select aggregate_import_id, uploaded_workbook_sha256, lifecycle_status
             from selection_aggregate_imports where publication_id = ?""",
        [str(existing[0])],
    ).fetchone()
    if stored is None or str(stored[2]) != "ACTIVE":
        raise PublicationNotFound("AGGREGATE_NOT_FOUND")
    if str(stored[0]) != aggregate_id:
        raise PublicationConflict("AGGREGATE_ID_CONFLICT")
    if str(stored[1]) != package.aggregate.uploaded_workbook_sha256:
        raise PublicationConflict("UPLOAD_DIGEST_CONFLICT")
    return PublicationResult(
        str(existing[0]), "ALREADY_IMPORTED", aggregate_import_id=aggregate_id,
        decision_group_id=str(decision_group_id),
    )


def _insert_publication_header(
    connection: duckdb.DuckDBPyConnection,
    package: PublicationPackage,
    publication_id: str,
    operation_key: str,
    db_id: str,
    normalized_latest: datetime,
) -> None:
    connection.execute(
        """insert into selection_publications (
            publication_id, publication_kind, operation_key, operation_digest,
            manifest_contract_version, decision_group_id, database_instance_id,
            source_revision, controls_json, controls_sha256, render_model_json,
            render_model_sha256, evaluated_rowset_sha256, export_workbook_sha256,
            created_at_utc
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [publication_id, package.publication_kind, operation_key, package.operation_digest,
         package.manifest_contract_version, package.decision_group_id, db_id, package.source_revision,
         package.controls_json, package.controls_sha256, package.render_model_json, package.render_model_sha256,
         package.evaluated_rowset_sha256, package.export_workbook_sha256, normalized_latest],
    )


def publish_publication(
    connection: duckdb.DuckDBPyConnection,
    database_root: Path,
    package: PublicationPackage | Mapping[str, object],
) -> PublicationResult:
    """Commit one frozen publication and all partitions atomically."""
    frozen = _as_package(package)
    _validate_package(frozen)
    require_performance_v2(connection)
    with PerformanceV2WriterLock(Path(database_root)):
        connection.execute("begin transaction")
        try:
            publication_id = _canonical_id(frozen.publication_id, "publication_id")
            operation_key = _canonical_id(frozen.operation_key, "operation_key")
            existing = _lookup_operation(connection, operation_key)
            if existing is not None:
                _rollback(connection)
                return _retry_existing_operation(connection, existing, frozen)
            db_id = str(connection.execute(
                "select value from schema_info where key = 'database_instance_id'"
            ).fetchone()[0])
            _revalidate_source_runs(connection, frozen, db_id)
            normalized_latest = _normalized_latest(connection)
            _insert_publication_header(
                connection, frozen, publication_id, operation_key, db_id, normalized_latest,
            )
            if frozen.publication_kind == "AGGREGATE_REVIEW":
                aggregate = frozen.aggregate
                if aggregate is None:
                    raise PublicationValidationError("INVALID_ARGUMENT", "aggregate publication needs an import header")
                connection.execute(
                    """insert into selection_aggregate_imports (
                        aggregate_import_id, publication_id, operation_key, operation_digest,
                        manifest_contract_version, source_revision, partition_rowsets_json,
                        candidate_identities_json, uploaded_workbook_sha256, lifecycle_status,
                        imported_at_utc
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                    [_canonical_id(aggregate.aggregate_import_id, "aggregate_import_id"), publication_id, operation_key,
                     frozen.operation_digest, aggregate.manifest_contract_version or frozen.manifest_contract_version,
                     aggregate.source_revision or frozen.source_revision, aggregate.partition_rowsets_json,
                     aggregate.candidate_identities_json, aggregate.uploaded_workbook_sha256, normalized_latest],
                )
            import_ids: list[str] = []
            for partition in frozen.partitions:
                if partition.source_run_id is not None:
                    connection.execute(
                        """insert into selection_publication_runs (
                            publication_id, pair, side, role, selection_run_id
                        ) values (?, ?, ?, 'SOURCE', ?)""",
                        [publication_id, partition.pair, partition.side,
                         _canonical_id(partition.source_run_id, "source_run_id")],
                    )
                if partition.overlay_run is not None:
                    _insert_overlay_run(connection, frozen, partition, normalized_latest)
                    connection.execute(
                        """insert into selection_publication_runs (
                            publication_id, pair, side, role, selection_run_id
                        ) values (?, ?, ?, 'OVERLAY', ?)""",
                        [publication_id, partition.pair, partition.side,
                         _canonical_id(partition.overlay_run.selection_run_id, "selection_run_id")],
                    )
                review_id = _write_review_projection(
                    connection, frozen, partition, normalized_latest,
                    _canonical_id(frozen.aggregate.aggregate_import_id, "aggregate_import_id")
                    if frozen.publication_kind == "AGGREGATE_REVIEW" and frozen.aggregate else None,
                )
                if review_id:
                    import_ids.append(review_id)
            connection.execute("commit")
            return PublicationResult(
                publication_id, normalized_latest=normalized_latest,
                review_import_ids=tuple(import_ids),
                aggregate_import_id=(
                    _canonical_id(frozen.aggregate.aggregate_import_id, "aggregate_import_id")
                    if frozen.publication_kind == "AGGREGATE_REVIEW" and frozen.aggregate else None
                ),
                decision_group_id=frozen.decision_group_id,
            )
        except PublicationError:
            _rollback(connection)
            raise
        except duckdb.ConstraintException as error:
            _rollback(connection)
            raced = _lookup_operation(connection, operation_key)
            if raced is not None:
                return _retry_existing_operation(connection, raced, frozen)
            raise PublicationValidationError("PUBLICATION_CONSTRAINT", str(error)) from error
        except Exception:
            _rollback(connection)
            raise


def lookup_publication(
    connection: duckdb.DuckDBPyConnection, operation_key: str,
) -> PublicationResult:
    """Read-only operation-key lookup used by re-export paths."""
    require_performance_v2(connection)
    key = _canonical_id(operation_key, "operation_key")
    row = connection.execute(
        "select publication_id, retired_at_utc, decision_group_id from selection_publications where operation_key = ?",
        [key],
    ).fetchone()
    if row is None or row[1] is not None:
        raise PublicationNotFound("PUBLICATION_KEY_NOT_FOUND")
    return PublicationResult(
        str(row[0]), "ALREADY_COMMITTED_REEXPORT_REQUIRED", decision_group_id=str(row[2]),
    )
