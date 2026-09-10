"""Optional fixed-shortlist minute refinement for PRETEST_PROXY candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import inspect
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .pretest_proxy import compute_proxy_metrics


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("INVALID_MINUTE_TIMESTAMP")
    if result.tzinfo is None:
        raise ValueError("INVALID_MINUTE_TIMESTAMP")
    return result.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("INVALID_MINUTE_VALUE")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("INVALID_MINUTE_VALUE") from error
    if not result.is_finite():
        raise ValueError("INVALID_MINUTE_VALUE")
    return result


def _point(value: Any) -> tuple[datetime, Decimal]:
    if isinstance(value, Mapping):
        return _utc(value.get("timestamp_utc", value.get("timestamp", value.get("time")))), _decimal(value.get("equity", value.get("value")))
    if isinstance(value, Sequence) and len(value) == 2:
        return _utc(value[0]), _decimal(value[1])
    raise ValueError("INVALID_MINUTE_POINT")


def _member_key(member: Mapping[str, Any]) -> str:
    return f"{member.get('symbol', '')}:{str(member.get('side', '')).upper()}:{member.get('strategy_id', '')}:{member.get('result_id', '')}"


def _metric(metrics: Mapping[str, Any], *names: str) -> Decimal | None:
    for name in names:
        value = metrics.get(name)
        if value in (None, "UNKNOWN", "NOT_TESTED"):
            continue
        try:
            parsed = _decimal(value)
        except ValueError:
            continue
        return parsed
    return None


def _effective_dd(
    daily: Mapping[str, Any], minute: Mapping[str, Any], *, recomputed: bool
) -> dict[str, Any]:
    """Carry the larger DD into final metrics and reject unsafe re-sizing."""
    merged = dict(minute)
    for names in (
        ("proxy_max_drawdown_usdt", "proxy_max_dd_usdt", "max_drawdown_usdt"),
        ("proxy_max_drawdown_pct", "proxy_max_dd_pct", "max_drawdown_pct"),
    ):
        daily_value = _metric(daily, *names)
        minute_value = _metric(minute, *names)
        if recomputed and daily_value is not None and minute_value is None:
            raise ValueError("MINUTE_EFFECTIVE_DD_UNAVAILABLE")
        if daily_value is None or minute_value is None:
            continue
        value = max(daily_value, minute_value)
        merged[names[0]] = value
        if "proxy_max_dd_usdt" in minute or "proxy_max_dd_usdt" in daily:
            merged["proxy_max_dd_usdt"] = value
        if "proxy_max_dd_pct" in minute or "proxy_max_dd_pct" in daily:
            merged["proxy_max_dd_pct"] = value
        if recomputed and daily_value > minute_value:
            # The injected evaluator may have sized against the smaller
            # intraday DD.  Preserve the whole daily shortlist when this
            # cannot be corrected with an auditable second sizing pass.
            raise ValueError("MINUTE_EFFECTIVE_DD_UNSAFE")
    return merged


def _calendar_days(candidate: Mapping[str, Any]) -> int | None:
    value = candidate.get("calendar_days", candidate.get("pretest_calendar_days"))
    period = candidate.get("pretest_period")
    if value is None and isinstance(period, Mapping):
        value = period.get("calendar_days")
        if value is None and isinstance(period.get("evidence"), Mapping):
            value = period["evidence"].get("calendar_days")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _minute_eligible(candidate: Mapping[str, Any]) -> bool | None:
    """Return eligibility when the sparse period diagnostics are present."""
    days = _calendar_days(candidate)
    if days is None:
        return None
    members = tuple(item for item in candidate.get("members", ()) if isinstance(item, Mapping))
    if not members:
        return False
    counts = [item.get("in_window_observation_count") for item in members]
    if any(value is None for value in counts):
        return False
    try:
        return all(int(value) > days for value in counts)
    except (TypeError, ValueError):
        return False


def _minute_grid(
    points: Sequence[Any],
    start: datetime | None,
    end: datetime | None,
    max_gap_days: int,
    *,
    seed_value: Any | None = None,
    has_action_before_first: bool = False,
    has_action_at_or_before_start: bool | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
    parsed = sorted((_point(item) for item in points), key=lambda item: item[0])
    if not parsed:
        return (), "MINUTE_SERIES_EMPTY"
    if any(left[0] >= right[0] for left, right in zip(parsed, parsed[1:])):
        return (), "MINUTE_TIMESTAMPS_NOT_INCREASING"
    explicit_start = start is not None
    start = start or parsed[0][0]
    end = end or parsed[-1][0] + timedelta(minutes=1)
    if end <= start:
        return (), "MINUTE_PERIOD_INVALID"
    parsed = [item for item in parsed if item[0] < end]
    if not parsed:
        return (), "MINUTE_SERIES_EMPTY"
    minute = start
    index = 0
    previous: tuple[datetime, Decimal] | None = None
    seeded = False
    for timestamp, value in parsed:
        if timestamp <= start:
            previous = (timestamp, value)
        else:
            break
    if previous is not None and not explicit_start and has_action_before_first:
        return (), "MINUTE_SERIES_REQUIRES_START_SAMPLE"
    if previous is None:
        action_before_start = has_action_before_first if has_action_at_or_before_start is None else has_action_at_or_before_start
        if seed_value is None or action_before_start:
            return (), "MINUTE_SERIES_REQUIRES_START_SAMPLE"
        try:
            parsed_seed = _decimal(seed_value)
        except ValueError:
            return (), "MINUTE_INVALID_START_SEED"
        if parsed_seed <= 0:
            return (), "MINUTE_INVALID_START_SEED"
        previous = (start, parsed_seed)
        seeded = True
    result: list[Mapping[str, Any]] = []
    while minute <= end:
        while index < len(parsed) and parsed[index][0] <= minute:
            previous = parsed[index]
            index += 1
        if previous is None:
            return tuple(result), "MINUTE_SERIES_REQUIRES_START_SAMPLE"
        # CURRENT_RESULT is a sparse observation path.  Keep max_gap in the
        # compatibility signature, but forward-fill through gaps; the gap is a
        # diagnostic and cannot change retention or sizing.
        result.append({"timestamp_utc": minute, "equity": previous[1], "real": previous[0] == minute and not seeded, "filled": previous[0] != minute or seeded})
        seeded = False
        minute += timedelta(minutes=1)
    if not result:
        return (), "MINUTE_SERIES_REQUIRES_BACKFILL"
    return tuple(result), None


@dataclass(frozen=True, slots=True)
class MinuteRefinementResult:
    status: str
    candidates: tuple[Mapping[str, Any], ...]
    reason: str | None = None
    refined_count: int = 0
    budget_consumed: int = 0
    basis: str = "DAILY"


def refine_pretest_shortlist(
    shortlist: Sequence[Mapping[str, Any]],
    minute_paths: Mapping[str, Sequence[Any]],
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    max_gap_days: int = 3,
    evaluator: Any | None = None,
) -> MinuteRefinementResult:
    """Refine a fixed daily shortlist; any failure falls back to daily rows."""
    daily = tuple(dict(item) for item in shortlist if isinstance(item, Mapping))
    eligibility = [_minute_eligible(item) for item in daily]
    if any(item is False for item in eligibility) or (any(item is True for item in eligibility) and any(item is None for item in eligibility)):
        return MinuteRefinementResult("PASS", tuple(MappingProxyType(item) for item in daily), None, 0, 0, "DAILY")
    refined: list[dict[str, Any]] = []
    for index, candidate in enumerate(daily):
        identity = str(candidate.get("candidate_id", candidate.get("identity", index)))
        campaign = candidate.get("campaign_equity", 1)
        members = tuple(item for item in candidate.get("members", ()) if isinstance(item, Mapping))
        try:
            if members:
                member_paths: list[tuple[Mapping[str, Any], ...]] = []
                minute_member_rows: list[dict[str, Any]] = []
                for member in members:
                    raw_path = minute_paths.get(_member_key(member), ())
                    first_sample = None
                    try:
                        first_sample = min((_point(item)[0] for item in raw_path), default=None)
                    except (TypeError, ValueError):
                        first_sample = None
                    has_action_before_first = False
                    has_action_at_or_before_start: bool | None = None
                    actions = member.get("actions", member.get("action_series", ()))
                    if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
                        for action in actions:
                            if not isinstance(action, Mapping):
                                continue
                            try:
                                timestamp = _utc(action.get("timestamp_utc", action.get("timestamp")))
                                if start_utc is not None:
                                    if timestamp <= _utc(start_utc):
                                        has_action_at_or_before_start = True
                                    elif has_action_at_or_before_start is None:
                                        has_action_at_or_before_start = False
                                if first_sample is not None and timestamp < first_sample:
                                    has_action_before_first = True
                            except (TypeError, ValueError):
                                raise ValueError("INVALID_MINUTE_ACTION")
                    path, reason = _minute_grid(
                        raw_path,
                        start_utc,
                        end_utc,
                        max_gap_days,
                        seed_value=member.get("initial_balance", member.get("source_initial_balance")),
                        has_action_before_first=has_action_before_first,
                        has_action_at_or_before_start=has_action_at_or_before_start,
                    )
                    if reason:
                        raise ValueError(reason)
                    minute_member_rows.append({**dict(member), "equity": path, "equity_series": path})
                    if evaluator is None:
                        initial = member.get("initial_balance", member.get("source_initial_balance"))
                        tested = member.get("tested_size_usdt")
                        actual = member.get("actual_size_usdt", member.get("position_size_usdt"))
                        member_paths.append(tuple(compute_proxy_metrics(path, campaign_equity=campaign, source_initial_balance=initial, tested_size_usdt=tested, actual_size_usdt=actual).equity_path))
                if evaluator is not None:
                    result = _invoke_evaluator(evaluator, tuple(minute_member_rows), candidate)
                    if str(result.get("status", "PASS")) not in {"PASS", "OK"}:
                        raise ValueError(str(result.get("reason", "MINUTE_COMPOSITION_UNAVAILABLE")))
                    metrics = result.get("metrics", result)
                    final_members = result.get("members", tuple(minute_member_rows))
                    if not isinstance(metrics, Mapping) or not isinstance(final_members, Sequence) or isinstance(final_members, (str, bytes)):
                        raise ValueError("INVALID_MINUTE_EVALUATION")
                    daily_metrics = candidate.get("metrics", candidate.get("daily_metrics", {}))
                    if not isinstance(daily_metrics, Mapping):
                        daily_metrics = {}
                    metrics = _effective_dd(daily_metrics, metrics, recomputed=True)
                    refined.append({
                        **candidate,
                        "members": tuple(final_members),
                        "daily_metrics": daily_metrics,
                        "minute_metrics": dict(metrics),
                        "refinement_basis": "MINUTE",
                        "daily_sizes_preserved": False,
                        "minute_sizes_recomputed": True,
                    })
                    continue
                timestamps = tuple(point["timestamp_utc"] for point in member_paths[0])
                if any(tuple(point["timestamp_utc"] for point in path) != timestamps for path in member_paths[1:]):
                    raise ValueError("MINUTE_PERIOD_MISMATCH")
                combined = tuple({"timestamp_utc": timestamp, "equity": Decimal(str(campaign)) + sum((path[offset]["equity"] - Decimal(str(campaign)) for path in member_paths), Decimal(0))} for offset, timestamp in enumerate(timestamps))
                metrics = compute_proxy_metrics(combined, campaign_equity=campaign)
            else:
                path, reason = _minute_grid(
                    minute_paths.get(identity, ()),
                    start_utc,
                    end_utc,
                    max_gap_days,
                    seed_value=candidate.get("initial_balance", candidate.get("source_initial_balance")),
                )
                if reason:
                    raise ValueError(reason)
                metrics = compute_proxy_metrics(path, campaign_equity=campaign)
            daily_metrics = candidate.get("metrics", candidate.get("daily_metrics", {}))
            if not isinstance(daily_metrics, Mapping):
                daily_metrics = {}
            metrics_dict = _effective_dd(daily_metrics, metrics.as_dict(), recomputed=False)
        except (TypeError, ValueError, ArithmeticError) as error:
            return MinuteRefinementResult("MINUTE_REFINEMENT_UNAVAILABLE", tuple(MappingProxyType(item) for item in daily), str(error) or "MINUTE_REFINEMENT_UNAVAILABLE", 0, 0, "DAILY")
        refined.append({
            **candidate,
            "daily_metrics": daily_metrics,
            "minute_metrics": metrics_dict,
            "refinement_basis": "MINUTE",
            "daily_sizes_preserved": True,
        })

    order = {"AGGRESSIVE": ("proxy_pnl_usdt", "proxy_recovery_factor", "proxy_max_drawdown_pct"), "BALANCED": ("proxy_recovery_factor", "proxy_pnl_usdt", "proxy_max_drawdown_pct"), "CONSERVATIVE": ("proxy_recovery_factor", "proxy_max_drawdown_pct", "proxy_pnl_usdt")}
    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        metrics = item["minute_metrics"]
        profile = str(item.get("profile", item.get("profile_id", "BALANCED")))
        values = []
        for field in order.get(profile, order["BALANCED"]):
            value = Decimal(str(metrics.get(field, 0) if metrics.get(field) not in (None, "UNKNOWN") else 0))
            values.append(value if field == "proxy_max_drawdown_pct" else -value)
        return (*values, str(item.get("candidate_id", "")))
    refined.sort(key=sort_key)
    return MinuteRefinementResult("PASS", tuple(MappingProxyType(item) for item in refined), None, len(refined), 0, "MINUTE")


def _invoke_evaluator(evaluator: Any, members: tuple[Mapping[str, Any], ...], candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        parameters = tuple(inspect.signature(evaluator).parameters.values())
        if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters) or len(parameters) > 1:
            result = evaluator(members, candidate)
        else:
            result = evaluator(members)
    except (ArithmeticError, KeyError, TypeError, ValueError, OSError) as error:
        return {"status": "UNKNOWN", "reason": str(error) or "MINUTE_EVALUATION_INVALID"}
    if isinstance(result, Mapping):
        return dict(result)
    if hasattr(result, "as_dict"):
        value = result.as_dict()
        return dict(value) if isinstance(value, Mapping) else {"status": "UNKNOWN", "reason": "INVALID_MINUTE_EVALUATION"}
    return {"status": "UNKNOWN", "reason": "INVALID_MINUTE_EVALUATION"}


refine_pretest_candidates = refine_pretest_shortlist
refine_minute_shortlist = refine_pretest_shortlist

__all__ = ["MinuteRefinementResult", "refine_minute_shortlist", "refine_pretest_candidates", "refine_pretest_shortlist"]
