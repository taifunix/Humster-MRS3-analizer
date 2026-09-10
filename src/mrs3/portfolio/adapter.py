"""Pure adapter from frozen Panel facts to sized multi-pair candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .candidate_search import search_portfolio_candidates
from .liquidity import ReferenceSnapshot
from .market_snapshot import MarketSnapshotError, load_market_snapshot
from .minute_capacity import (
    MinuteCapacityError,
    MinuteCapacityResult,
    backfill_missing_days,
    calculate_minute_capacity,
    fetch_bybit_trade_archive,
)
from .position_sizing import enrich_finalist_rows
from .position_sizing import size_composition
from .input import resolve_common_pretest_period
from .minute_refinement import refine_pretest_shortlist
from .spread_screen import read_spread_history, screen_spread


_SEARCH_PAYLOAD_FIELDS = frozenset({
    "equity", "equity_series", "equity_path", "minute_equity", "actions",
    "action_series", "strategy_actions", "minute_actions", "source_provenance",
})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: str
    variants: tuple[Mapping[str, Any], ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variants", tuple(_freeze(item) for item in self.variants))
        object.__setattr__(self, "excluded", tuple(_freeze(item) for item in self.excluded))
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(self.blockers)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))


def _evaluate_sized_composition(
    members: Sequence[Mapping[str, Any]],
    capacities: Mapping[str, MinuteCapacityResult],
    reference: ReferenceSnapshot,
    mark_prices: Mapping[str, Any],
    campaign_equity: Any,
    risk_dd: Any,
    reserve: Any,
) -> Mapping[str, Any]:
    sized = size_composition(
        members,
        capacities,
        reference,
        mark_prices,
        equity=campaign_equity,
        max_actual_equity_dd_pct=risk_dd,
        min_free_margin_reserve_pct=reserve,
        campaign_equity=campaign_equity,
    )
    if sized.status != "PASS" or sized.proxy is None:
        return {"status": "FAIL", "reason": sized.reason or "COMPOSITION_SIZING_FAILED"}
    return {
        **sized.proxy.as_dict(),
        "status": "PASS",
        "members": tuple(_plain(item) for item in sized.members),
        "k": sized.k,
        "k1": sized.k1,
        "corrective_reduction_applied": sized.corrective_reduction_applied,
        "initial_margin_usdt": sized.initial_margin_usdt,
    }


def _pretest_process_worker(members: Sequence[Mapping[str, Any]], context: tuple[Any, ...]) -> Mapping[str, Any]:
    """Windows-spawn-safe process worker receiving only daily proxy payload."""
    capacities, reference, mark_prices, campaign_equity, risk_dd, reserve = context
    result = dict(_evaluate_sized_composition(members, capacities, reference, mark_prices, campaign_equity, risk_dd, reserve))
    result.pop("equity_path", None)
    sized = result.get("members")
    if isinstance(sized, Sequence) and not isinstance(sized, (str, bytes)):
        result["members"] = tuple(
            {
                key: _plain(value)
                for key, value in member.items()
                if key not in _SEARCH_PAYLOAD_FIELDS
            }
            for member in sized
            if isinstance(member, Mapping)
        )
    return _plain(result)


def _exclusion(*, profile: str = "", reason: str, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = row or {}
    return {
        "profile": profile,
        "stage": "GENERATE_VARIANTS",
        "selection_reason": reason,
        "message": f"{profile}:{reason}".lstrip(":"),
        "strategy_id": row.get("strategy_id", ""),
        "result_id": row.get("result_id", ""),
        "symbol": row.get("symbol", ""),
        "side": row.get("side", ""),
    }


def build_portfolio_candidates(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    capacities: Mapping[str, MinuteCapacityResult],
    reference: ReferenceSnapshot,
    mark_prices: Mapping[str, Any],
    spread_observations: Mapping[str, Sequence[Mapping[str, Any]]],
    spread_history_statuses: Mapping[str, str],
    now_ms: int,
    workers: int = 1,
) -> AdapterResult:
    """Apply spread/sizing gates and enumerate profile candidates."""
    document = campaign.get("config_document") if isinstance(campaign, Mapping) else None
    launch = campaign.get("launch") if isinstance(campaign, Mapping) else None
    if not isinstance(document, Mapping) or not isinstance(launch, Mapping):
        return AdapterResult("FAIL", blockers=("CAMPAIGN_CONFIG_INVALID",))
    profiles_config = document.get("profiles")
    search_config = document.get("search")
    liquidity_config = document.get("liquidity")
    profiles = launch.get("profiles")
    if not all(isinstance(value, Mapping) for value in (profiles_config, search_config, liquidity_config)) or not isinstance(profiles, Sequence):
        return AdapterResult("FAIL", blockers=("CAMPAIGN_CONFIG_INVALID",))

    spread = screen_spread(selected_rows, spread_observations, spread_history_statuses)
    excluded = [_exclusion(reason=item.reason, row=item.row) for item in spread.exclusions]
    sizing = enrich_finalist_rows(
        spread.retained_rows,
        capacities,
        reference,
        mark_prices,
        now_ms=now_ms,
        maximum_age_hours=liquidity_config.get("maximum_age_hours"),
    )
    excluded.extend(_exclusion(reason=item.reason, row=item.row) for item in sizing.exclusions)
    if sizing.status != "PASS":
        reason = sizing.reason or "POSITION_SIZING_FAILED"
        return AdapterResult("FAIL", excluded=tuple(excluded), blockers=(reason,), warnings=spread.warnings)

    selected_symbols = tuple(sorted({str(row["symbol"]) for row in selected_rows if isinstance(row, Mapping) and row.get("symbol")}))
    warnings = [*spread.warnings]
    if any(row.get("capacity_status") == "PRELIMINARY" for row in sizing.rows):
        warnings.append("LIQUIDITY_CAPACITY_PRELIMINARY")
    variants: list[dict[str, Any]] = []
    blockers: list[str] = []
    period_evidence = None
    proxy_rows: tuple[Mapping[str, Any], ...] = sizing.rows
    period = None
    pretest_requested = str(campaign.get("stage1_mode", "")) == "PRETEST_PROXY"
    if pretest_requested or any(
        isinstance(row.get("equity", row.get("equity_series")), Sequence)
        and bool(row.get("equity", row.get("equity_series")))
        and row.get("initial_balance") is not None
        for row in sizing.rows
    ):
        composition = search_config.get("composition", {})
        parameters = composition.get("parameters", {}) if isinstance(composition, Mapping) else {}
        try:
            period = resolve_common_pretest_period(
                sizing.rows,
                minimum_common_days=int(parameters.get("minimum_common_days", 14)),
                minimum_daily_coverage_pct=int(parameters.get("minimum_daily_coverage_pct", 90)),
                maximum_forward_fill_gap_days=int(parameters.get("maximum_forward_fill_gap_days", 3)),
            )
        except (TypeError, ValueError):
            period = None
        if period is not None and period.available:
            period_evidence = {
                "status": period.status,
                "start_utc": period.start_utc,
                "end_utc": period.end_utc,
                "coverage_pct": period.coverage_pct,
                "evidence": period.evidence,
                "exclusions": period.exclusions,
            }
            normalized: list[Mapping[str, Any]] = []
            retained_ids = period.evidence.get("retained_identities") if isinstance(period.evidence, Mapping) else None
            retained_set = set(retained_ids) if isinstance(retained_ids, Sequence) and not isinstance(retained_ids, (str, bytes)) else None
            for row in sizing.rows:
                key = f"{str(row.get('symbol', '')).upper()}:{str(row.get('side', '')).upper()}:{row.get('strategy_id', '')}:{row.get('result_id', '')}"
                if retained_set is not None and key not in retained_set:
                    continue
                path = period.daily_paths.get(key)
                row_evidence = period.evidence.get("rows", {}).get(key, {}) if isinstance(period.evidence, Mapping) else {}
                normalized.append({
                    **dict(row),
                    **dict(row_evidence),
                    "minute_equity": row.get("minute_equity", row.get("equity", row.get("equity_series", ()))),
                    "equity": path or (),
                    "equity_series": path or (),
                    "path_policy": "SPARSE_OBSERVATION_FORWARD_FILL",
                    "calendar_days": period.evidence.get("calendar_days") if isinstance(period.evidence, Mapping) else None,
                })
            proxy_rows = tuple(normalized)
        elif pretest_requested:
            # Campaigns created by the current Panel must fail closed when
            # source equity facts cannot support PRETEST_PROXY.  Direct
            # package callers without this marker retain the legacy seam.
            for launch_profile in profiles:
                if isinstance(launch_profile, Mapping):
                    blockers.append(f"{launch_profile.get('profile_id', '')}:COMMON_PRETEST_PERIOD_UNAVAILABLE")
            return AdapterResult("FAIL", excluded=tuple(excluded), blockers=tuple(blockers), warnings=tuple(warnings))
    elif pretest_requested:
        for launch_profile in profiles:
            if isinstance(launch_profile, Mapping):
                blockers.append(f"{launch_profile.get('profile_id', '')}:COMMON_PRETEST_PERIOD_UNAVAILABLE")
        return AdapterResult("FAIL", excluded=tuple(excluded), blockers=tuple(blockers), warnings=tuple(warnings))
    source_lookup = {
        ":".join(str(row.get(field, "")) for field in ("symbol", "side", "strategy_id", "result_id")): row
        for row in proxy_rows
    }
    for launch_profile in profiles:
        if not isinstance(launch_profile, Mapping):
            blockers.append("CAMPAIGN_PROFILE_INVALID")
            continue
        profile_id = str(launch_profile.get("profile_id", ""))
        profile = profiles_config.get(profile_id)
        if not isinstance(profile, Mapping) or not isinstance(profile.get("ranking"), Mapping):
            blockers.append(f"{profile_id}:PROFILE_CONFIG_INVALID")
            continue
        max_candidates = launch_profile.get("max_candidates")
        has_proxy_paths = any(isinstance(row.get("equity", row.get("equity_series")), Sequence) and bool(row.get("equity", row.get("equity_series"))) and row.get("initial_balance") is not None for row in proxy_rows)
        sizing_context = None
        if period is not None and not period.available:
            blockers.append(f"{profile_id}:COMMON_PRETEST_PERIOD_UNAVAILABLE")
            continue
        if has_proxy_paths and isinstance(max_candidates, int) and max_candidates > 0:
            risk_dd = profile.get("max_actual_equity_dd_pct", {"AGGRESSIVE": "20", "BALANCED": "10", "CONSERVATIVE": "5"}.get(profile_id, "10"))
            reserve = profile.get("min_free_margin_reserve_pct", {"AGGRESSIVE": "20", "BALANCED": "40", "CONSERVATIVE": "60"}.get(profile_id, "40"))
            scenario = (document.get("scenarios") or {}).get(profile_id, {})
            max_balance = scenario.get("max_balance")
            if isinstance(max_balance, Mapping):
                max_balance = max_balance.get("amount", max_balance.get("value", "1"))
            campaign_equity = launch_profile.get("equity_usdt")
            if campaign_equity is None:
                campaign_equity = scenario.get("equity", max_balance if max_balance is not None else "1")
            sizing_context = (capacities, reference, mark_prices, campaign_equity, risk_dd, reserve)

            def evaluate(members: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
                return _evaluate_sized_composition(members, capacities, reference, mark_prices, campaign_equity, risk_dd, reserve)

            result = search_portfolio_candidates(
                proxy_rows,
                selected_symbols=selected_symbols,
                profile_id=profile_id,
                scenario_id=profile_id,
                max_candidates=max_candidates,
                max_enumerated_combinations=search_config.get("max_enumerated_combinations") or 100000,
                evaluator=evaluate,
                workers=workers,
                process_evaluator=_pretest_process_worker,
                process_context=(capacities, reference, mark_prices, campaign_equity, risk_dd, reserve),
            )
        else:
            result = search_portfolio_candidates(
                proxy_rows,
                selected_symbols=selected_symbols,
                profile_id=profile_id,
                scenario_id=profile_id,
                individual_max_dd_pct=profile.get("individual_max_dd_pct"),
                individual_net_pnl_min_exclusive=profile.get("individual_net_pnl_min_exclusive"),
                top_n_per_direction=profile["ranking"].get("top_n"),
                max_enumerated_combinations=search_config.get("max_enumerated_combinations") or 100000,
            )
        excluded.extend(
            _exclusion(
                profile=profile_id,
                reason=item.reason,
                row={"strategy_id": item.strategy, "result_id": item.result, "symbol": item.symbol, "side": item.side},
            )
            for item in result.excluded
        )
        if result.status != "PASS":
            blockers.append(f"{profile_id}:{result.reason or 'CANDIDATE_SEARCH_FAILED'}")
            continue
        profile_variants: list[dict[str, Any]] = []
        for daily_rank, candidate in enumerate(result.candidates, 1):
            compact_members = candidate.members
            members = tuple(source_lookup.get(str(member.get("_source_key", "")), member) for member in compact_members)
            metrics = dict(candidate.metrics)
            if sizing_context is not None:
                final_sizing = _evaluate_sized_composition(
                    members, *sizing_context[0:3], *sizing_context[3:],
                )
                if final_sizing.get("status") == "PASS":
                    members = tuple(final_sizing.get("members", members))
                    metrics = {**metrics, **final_sizing, "members": members}
            profile_variants.append({
                "schema_version": candidate.schema_version,
                "candidate_id": candidate.identity,
                "profile": candidate.profile_id,
                "scenario_id": candidate.scenario_id,
                "members": members,
                "member_count": len(members),
                "pair_count": len({member["symbol"] for member in members}),
                "metrics": metrics,
                "search_mode": result.mode,
                "evaluations": result.evaluated,
                "daily_pretest_rank": daily_rank,
                "final_pretest_rank": daily_rank,
                "pretest_period": period_evidence,
                "refinement": "DAILY",
                "gate": "PASS",
                "blocking_reasons": (),
            })
        if profile_variants and period is not None and period.available:
            period_diagnostics = period.evidence if isinstance(period.evidence, Mapping) else {}
            calendar_days = period_diagnostics.get("calendar_days")
            diagnostics_known = isinstance(calendar_days, int) and all(
                isinstance(member, Mapping) and member.get("in_window_observation_count") is not None
                for item in profile_variants
                for member in item.get("members", ())
            )
            try:
                minute_ineligible = diagnostics_known and any(
                    int(member["in_window_observation_count"]) <= calendar_days
                    for item in profile_variants
                    for member in item.get("members", ())
                )
            except (TypeError, ValueError):
                minute_ineligible = True
            if minute_ineligible:
                variants.extend({**item, "refinement": "DAILY", "refinement_status": "SKIPPED_INSUFFICIENT_OBSERVATIONS"} for item in profile_variants)
                continue
            minute_paths = {
                f"{row.get('symbol', '')}:{str(row.get('side', '')).upper()}:{row.get('strategy_id', '')}:{row.get('result_id', '')}": row.get("minute_equity", ())
                for row in proxy_rows
            }

            def evaluate_minute(members: Sequence[Mapping[str, Any]], _candidate: Mapping[str, Any]) -> Mapping[str, Any]:
                # Intraday paths can have a larger drawdown than the daily
                # proxy.  Re-run the same composition sizing so the final
                # shortlist carries the minute-constrained quantities.
                sized_composition = size_composition(
                    members,
                    capacities,
                    reference,
                    mark_prices,
                    equity=campaign_equity,
                    max_actual_equity_dd_pct=risk_dd,
                    min_free_margin_reserve_pct=reserve,
                    campaign_equity=campaign_equity,
                )
                if sized_composition.status != "PASS" or sized_composition.proxy is None:
                    return {"status": "FAIL", "reason": sized_composition.reason or "MINUTE_COMPOSITION_SIZING_FAILED"}
                return {
                    **sized_composition.proxy.as_dict(),
                    "status": "PASS",
                    "members": sized_composition.members,
                    "k": sized_composition.k,
                    "k1": sized_composition.k1,
                    "corrective_reduction_applied": sized_composition.corrective_reduction_applied,
                    "initial_margin_usdt": sized_composition.initial_margin_usdt,
                }

            refined = refine_pretest_shortlist(
                tuple({**item, "campaign_equity": campaign_equity} for item in profile_variants),
                minute_paths,
                start_utc=period.start_utc,
                end_utc=period.end_utc,
                max_gap_days=int(parameters.get("maximum_forward_fill_gap_days", 3)),
                evaluator=evaluate_minute,
            )
            if refined.status == "PASS":
                profile_variants = []
                for final_rank, item in enumerate(refined.candidates, 1):
                    updated = dict(item)
                    # Keep daily evidence separately, while all downstream
                    # ranking/export fields read the final minute metrics.
                    minute_metrics = updated.get("minute_metrics")
                    if isinstance(minute_metrics, Mapping):
                        updated["daily_metrics"] = updated.get("daily_metrics", updated.get("metrics", {}))
                        updated["metrics"] = minute_metrics
                    updated["final_pretest_rank"] = final_rank
                    updated["refinement"] = "MINUTE"
                    profile_variants.append(updated)
            else:
                warnings.append("MINUTE_REFINEMENT_UNAVAILABLE")
                profile_variants = [
                    {
                        **item,
                        "refinement": "DAILY",
                        "refinement_status": refined.status,
                        "refinement_reason": "MINUTE_REFINEMENT_UNAVAILABLE",
                        "refinement_detail": refined.reason,
                    }
                    for item in profile_variants
                ]
        variants.extend(profile_variants)
    excluded.sort(key=lambda item: (str(item["profile"]), str(item["symbol"]), str(item["side"]), str(item["strategy_id"]), str(item["result_id"]), str(item["selection_reason"])))
    outcome = "PASS" if variants and not blockers else ("PARTIAL" if variants else "FAIL")
    return AdapterResult(outcome, tuple(variants), tuple(excluded), tuple(blockers), tuple(warnings))


def _path(root: Path, value: Any) -> Path:
    candidate = Path(str(value))
    return candidate if candidate.is_absolute() else root / candidate


def _campaign_clock(campaign: Mapping[str, Any]) -> tuple[datetime, int]:
    raw = campaign.get("created_at_utc")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("CAMPAIGN_CLOCK_INVALID") from error
    if parsed.tzinfo is None:
        raise ValueError("CAMPAIGN_CLOCK_INVALID")
    utc = parsed.astimezone(timezone.utc)
    return utc, int(utc.timestamp() * 1000)


def run_portfolio_adapter(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    workspace_root: str | Path,
    market_fetcher: Any = None,
    archive_fetcher: Any = None,
    workers: int = 1,
) -> AdapterResult:
    """Load one frozen Campaign's current public/local facts and build candidates."""
    try:
        document = campaign.get("config_document") if isinstance(campaign, Mapping) else None
        if not isinstance(document, Mapping):
            return AdapterResult("FAIL", blockers=("CAMPAIGN_CONFIG_INVALID",))
        inputs = document.get("inputs")
        liquidity = document.get("liquidity")
        if not isinstance(inputs, Mapping) or not isinstance(liquidity, Mapping) or not isinstance(liquidity.get("parameters"), Mapping):
            return AdapterResult("FAIL", blockers=("CAMPAIGN_CONFIG_INVALID",))
        parameters = liquidity["parameters"]
        now, now_ms = _campaign_clock(campaign)
        symbols = tuple(sorted({str(row["symbol"]) for row in selected_rows if isinstance(row, Mapping) and row.get("symbol")}))
        if not symbols:
            return AdapterResult("FAIL", blockers=("NO_SELECTED_SYMBOLS",))
        lag = int(liquidity["archive_publication_lag_hours"])
        end_date = now.date() - timedelta(days=1)
        if now < datetime.combine(now.date(), time(), timezone.utc) + timedelta(hours=lag):
            end_date -= timedelta(days=1)
        days = tuple(end_date - timedelta(days=offset) for offset in range(6, -1, -1))
        minute_root = _path(Path(workspace_root), inputs["bybit_minute_data_root"])
        fetch_day = archive_fetcher or fetch_bybit_trade_archive
        capacities: dict[str, MinuteCapacityResult] = {}
        for symbol in symbols:
            backfill = backfill_missing_days(
                minute_root,
                symbol,
                days,
                fetch_day=fetch_day,
                enabled=liquidity["backfill_write_enabled"],
            )
            if backfill.failed and not any((minute_root / symbol / f"{symbol}{day.isoformat()}_1m.csv").is_file() for day in days):
                return AdapterResult("FAIL", blockers=(f"{symbol}:MINUTE_BACKFILL_FAILED",))
            capacities[symbol] = calculate_minute_capacity(
                minute_root,
                symbol,
                end_date=end_date,
                participation_pct=parameters["close_volume_participation_pct"],
                round_down_usdt=liquidity["round_down_usdt"],
                weekend_start_utc=liquidity["weekend_start_utc"],
                weekend_end_utc=liquidity["weekend_end_utc"],
                publication_lag_hours=lag,
                now=now,
            )
        market = load_market_snapshot(symbols, captured_at_ms=now_ms, fetcher=market_fetcher)
        spread = read_spread_history(
            _path(Path(workspace_root), inputs["collector_root"]), symbols,
            now_ms=now_ms, minimum_coverage_pct=liquidity["minimum_coverage_pct"],
        )
        return build_portfolio_candidates(
            selected_rows,
            campaign,
            capacities=capacities,
            reference=market.reference,
            mark_prices=market.mark_prices,
            spread_observations=spread.observations,
            spread_history_statuses=spread.statuses,
            now_ms=now_ms,
            workers=workers,
        )
    except MinuteCapacityError:
        return AdapterResult("FAIL", blockers=("MINUTE_CAPACITY_UNAVAILABLE",))
    except MarketSnapshotError:
        return AdapterResult("FAIL", blockers=("MARKET_SNAPSHOT_UNAVAILABLE",))
    except Exception:
        return AdapterResult("FAIL", blockers=("ADAPTER_FACTS_UNAVAILABLE",))


__all__ = ["AdapterResult", "build_portfolio_candidates", "run_portfolio_adapter"]
