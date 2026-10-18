"""History sufficiency of a pair from its listing day (trade store rules: 4 warm-up days, 20-day periods,
READY needs two periods)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

WARMUP_DAYS = 4
PERIOD_DAYS = 20


def _days_word(count: int) -> str:
    tail, tens = count % 10, count % 100
    if tail == 1 and tens != 11:
        return "день"
    if tail in (2, 3, 4) and tens not in (12, 13, 14):
        return "дня"
    return "дней"


def sufficiency(listing_ms: int, today: date) -> dict[str, object]:
    listing_day = datetime.fromtimestamp(listing_ms / 1000, tz=timezone.utc).date()
    days = (today - listing_day).days
    research_from = listing_day + timedelta(days=WARMUP_DAYS + PERIOD_DAYS)
    ready_from = listing_day + timedelta(days=WARMUP_DAYS + 2 * PERIOD_DAYS)
    if today >= ready_from:
        status = "READY возможен"
    elif today >= research_from:
        status = "можно исследовать"
    else:
        status = f"мало истории ({days} {_days_word(days)})"
    return {"listing_day": listing_day.isoformat(), "days": days, "research_from": research_from.isoformat(),
            "ready_from": ready_from.isoformat(), "status": status}
