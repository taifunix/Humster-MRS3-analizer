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
from .spread_screen import read_spread_history, screen_spread


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
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
    for launch_profile in profiles:
        if not isinstance(launch_profile, Mapping):
            blockers.append("CAMPAIGN_PROFILE_INVALID")
            continue
        profile_id = str(launch_profile.get("profile_id", ""))
        profile = profiles_config.get(profile_id)
        if not isinstance(profile, Mapping) or not isinstance(profile.get("ranking"), Mapping):
            blockers.append(f"{profile_id}:PROFILE_CONFIG_INVALID")
            continue
        result = search_portfolio_candidates(
            sizing.rows,
            selected_symbols=selected_symbols,
            profile_id=profile_id,
            scenario_id=profile_id,
            individual_max_dd_pct=profile.get("individual_max_dd_pct"),
            individual_net_pnl_min_exclusive=profile.get("individual_net_pnl_min_exclusive"),
            top_n_per_direction=profile["ranking"].get("top_n"),
            max_enumerated_combinations=search_config.get("max_enumerated_combinations"),
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
        for candidate in result.candidates:
            variants.append({
                "schema_version": candidate.schema_version,
                "candidate_id": candidate.identity,
                "profile": candidate.profile_id,
                "scenario_id": candidate.scenario_id,
                "members": candidate.members,
                "member_count": len(candidate.members),
                "pair_count": len({member["symbol"] for member in candidate.members}),
                "gate": "PASS",
                "blocking_reasons": (),
            })
    variants.sort(key=lambda item: (item["profile"], item["candidate_id"]))
    excluded.sort(key=lambda item: (str(item["profile"]), str(item["symbol"]), str(item["side"]), str(item["strategy_id"]), str(item["result_id"]), str(item["selection_reason"])))
    return AdapterResult("PASS" if variants else "FAIL", tuple(variants), tuple(excluded), tuple(blockers), tuple(warnings))


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
        )
    except MinuteCapacityError:
        return AdapterResult("FAIL", blockers=("MINUTE_CAPACITY_UNAVAILABLE",))
    except MarketSnapshotError:
        return AdapterResult("FAIL", blockers=("MARKET_SNAPSHOT_UNAVAILABLE",))
    except Exception:
        return AdapterResult("FAIL", blockers=("ADAPTER_FACTS_UNAVAILABLE",))


__all__ = ["AdapterResult", "build_portfolio_candidates", "run_portfolio_adapter"]
