"""Direct tester CSV -> validated MRS3 point frames (no HTML or Source v6 DB).

Contract: csv-direct-input-v2. One CSV row is one tested parameter combination
of one symbol; ``TotalTrades`` is the number of completed positions (the tester
fixed its former fill-count bug). Point metrics follow the Source v6 definitions
so the shared analysis engine receives the same quantities it always did:

* ``pnl_pct``: ``TotalPnL / InitialBalance * 100`` (cross-checked against the
  rounded ``TotalPnLPercent``);
* ``dd_pct``: ``MaxDrawdownPercent`` (equity drawdown from its peak);
* ``win_rate_pct``: ``Win / (Win + Los) * 100``;
* ``activity_30d``: ``TotalTrades * 30 / max(EffectiveDays, 30)`` as an exact
  Decimal. CSV has no trade timestamps, so the Source v6 "trades in the last
  30 days" is replaced by the average monthly rate; the cap keeps it at or
  below ``TotalTrades`` exactly as v6 does for histories shorter than 30 days.

Like Source v6, only fixed-lot runs without uPnL sizing are admitted, read from
the tester's ``run_snapshot_*.json``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from typing import Mapping, Sequence
from zipfile import ZipFile

import pandas as pd


INPUT_CONTRACT = "csv-direct-input-v2"
TRADE_CONTRACT = "completed_positions-v1"
ACTIVITY_CONTRACT = "trades_per_30d_capped-v1"
MONTH_DAYS = Decimal("30")
DAY_SECONDS = Decimal("86400")
# TotalPnLPercent is printed with two decimals; allow its rounding plus slack.
_PNL_PERCENT_TOLERANCE = Decimal("0.0101")
_WIN_RATE_TOLERANCE = Decimal("0.011")
_SYMBOL = "settings[*].basic.symbol"
_TIMEFRAME = "settings[*].basic.time_frame"
_USE_FIX = "settings[*].basic.use_fix"
_USE_UPNL = "settings[*].exchange.use_upnl"
_REQUIRED = frozenset({
    _SYMBOL, _TIMEFRAME, "StartDate", "EndDate", "InitialBalance", "TotalPnL",
    "TotalPnLPercent", "MaxDrawdownPercent", "TotalTrades", "Win", "Los",
    "WinRate", "Fills", "ProfitFactor",
})


class DirectCsvError(ValueError):
    """Invalid CSV, missing run identity, or ambiguous grid."""


@dataclass(frozen=True)
class DirectCsvInput:
    frame: pd.DataFrame
    scope_keys: tuple[str, ...]
    csv_sha256: tuple[tuple[str, str], ...]
    input_digest: str
    window_start: datetime
    window_end: datetime
    total_trades: int
    total_fills: int

    def scopes(self) -> dict[str, pd.DataFrame]:
        return {
            key: group.reset_index(drop=True)
            for key, group in self.frame.groupby("scope_key", sort=True)
        }


@dataclass(frozen=True)
class _SnapshotFacts:
    ma_identity: dict[tuple[str, str], tuple[str, str, str, str]]
    fixed_lot_settings: tuple[bool, bool] | None


_Source = tuple[str, tuple[int, str], bytes]


def _read_sources(paths: Sequence[str | Path]) -> tuple[list[_Source], list[_Source]]:
    """Collect CSVs and snapshots, each tagged with its run: one folder or ZIP folder.

    Labels carry only names relative to the supplied input, never local paths.
    A snapshot describes only the CSVs of its own run.
    """
    csvs: list[_Source] = []
    snapshots: list[_Source] = []

    def add_file(label: str, run: tuple[int, str], payload: bytes) -> None:
        filename = Path(label.rsplit("!", 1)[-1]).name
        if filename.startswith("reports_history") and filename.endswith(".csv"):
            csvs.append((label, run, payload))
        elif filename.startswith("run_snapshot_") and filename.endswith(".json"):
            snapshots.append((label, run, payload))

    for index, original in enumerate(paths):
        path = Path(original)
        if path.is_dir():
            for pattern in ("reports_history*.csv", "run_snapshot_*.json"):
                for child in sorted(path.rglob(pattern)):
                    relative = child.relative_to(path)
                    add_file(f"{path.name}/{relative.as_posix()}", (index, relative.parent.as_posix()), child.read_bytes())
        elif path.suffix.lower() == ".zip":
            with ZipFile(path) as archive:
                for member in sorted(archive.namelist()):
                    # Do not extract/read any potentially gigantic HTML reports.
                    if member.endswith("/"):
                        continue
                    if Path(member).name.startswith(("reports_history", "run_snapshot_")):
                        run = (index, Path(member).parent.as_posix())
                        add_file(f"{path.name}!{member}", run, archive.read(member))
        elif path.is_file():
            add_file(path.name, (index, "."), path.read_bytes())
            # A CSV picked inside its run folder brings that run's snapshot.
            for sibling in sorted(path.parent.glob("run_snapshot_*.json")):
                add_file(sibling.name, (index, "."), sibling.read_bytes())
        else:
            raise DirectCsvError(f"unsupported or missing input path: {path.name}")
    if not csvs:
        raise DirectCsvError("no reports_history*.csv in supplied inputs")
    return csvs, snapshots


def _nearest_snapshot_facts(facts_by_run: Mapping[tuple[int, str], "_SnapshotFacts"], run: tuple[int, str]) -> "_SnapshotFacts":
    """The snapshot of the CSV's own folder, else of its nearest ancestor in the same input."""
    index, folder = run
    current = Path(folder)
    while True:
        key = (index, current.as_posix())
        if key in facts_by_run:
            return facts_by_run[key]
        if current.as_posix() in {".", ""}:
            return _SnapshotFacts({}, None)
        current = current.parent


def _decimal(raw: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise DirectCsvError(f"{field}: invalid decimal {raw!r}") from error
    if not parsed.is_finite():
        raise DirectCsvError(f"{field}: nonfinite decimal")
    return parsed


def _integer(raw: object, field: str, *, positive: bool = False) -> int:
    value = _decimal(raw, field)
    if value != value.to_integral_value() or value < (1 if positive else 0):
        raise DirectCsvError(f"{field}: expected {'positive' if positive else 'nonnegative'} integer")
    return int(value)


def _boolean(raw: object, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().casefold()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise DirectCsvError(f"{field}: expected boolean, got {raw!r}")


def _utc(raw: object, field: str) -> datetime:
    if isinstance(raw, pd.Timestamp):
        value = raw.to_pydatetime()
    elif isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise DirectCsvError(f"{field}: invalid datetime {raw!r}") from error
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_facts(snapshots: Sequence[tuple[str, bytes]]) -> _SnapshotFacts:
    identities: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    fixed_lot_settings: set[tuple[bool, bool]] = set()
    for label, body in snapshots:
        try:
            parsed = json.loads(body)
            settings = parsed["settings"]
        except (KeyError, ValueError, TypeError) as error:
            raise DirectCsvError(f"{label}: invalid run snapshot") from error
        if not isinstance(settings, list):
            raise DirectCsvError(f"{label}: settings must be a list")
        for setting in settings:
            if not isinstance(setting, dict):
                raise DirectCsvError(f"{label}: malformed settings item")
            basic, strategy = setting.get("basic"), setting.get("mrs2")
            if not isinstance(basic, dict) or not isinstance(strategy, dict):
                continue
            exchange = setting.get("exchange")
            use_fix = basic.get("use_fix")
            use_upnl = exchange.get("use_upnl") if isinstance(exchange, dict) else None
            if not isinstance(use_fix, bool) or not isinstance(use_upnl, bool):
                raise DirectCsvError(f"{label}: fixed-lot settings use_fix/use_upnl are missing or malformed")
            fixed_lot_settings.add((use_fix, use_upnl))
            symbol = str(basic.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            for side in ("LONG", "SHORT"):
                suffix = side.lower()
                opening, closing = strategy.get(f"ma_{suffix}"), strategy.get(f"ma_close_{suffix}")
                if not isinstance(opening, dict) or not isinstance(closing, dict):
                    continue
                identity = tuple(str(value).strip() for value in (
                    opening.get("type", ""), opening.get("source", ""),
                    closing.get("type", ""), closing.get("source", ""),
                ))
                if any(not value or value == "None" for value in identity):
                    raise DirectCsvError(f"{label}: missing {side} MA type/source for {symbol}")
                key = (symbol, side)
                if key in identities and identities[key] != identity:
                    raise DirectCsvError(f"conflicting snapshot MA identities for {symbol}/{side}")
                identities[key] = identity  # type: ignore[assignment]
    if len(fixed_lot_settings) > 1:
        raise DirectCsvError("run snapshots disagree on use_fix/use_upnl")
    return _SnapshotFacts(identities, next(iter(fixed_lot_settings)) if fixed_lot_settings else None)


def _side_columns(row: Mapping[str, str]) -> tuple[str, str, str, str]:
    selections = []
    for side in ("LONG", "SHORT"):
        suffix = side.lower()
        multiplier = f"settings[*].mrs2.ma_{suffix}.multiplier"
        if str(row.get(multiplier, "")).strip():
            selections.append((side, multiplier, f"settings[*].mrs2.ma_{suffix}.len", f"settings[*].mrs2.ma_close_{suffix}.len"))
    if len(selections) != 1:
        raise DirectCsvError("row must have exactly one active LONG/SHORT MA multiplier")
    return selections[0]


def _shift_bp(value: object, side: str) -> int:
    multiplier = _decimal(value, f"{side} entry multiplier")
    shift = (Decimal(1) - multiplier if side == "LONG" else multiplier - Decimal(1)) * Decimal(10000)
    rounded = shift.to_integral_value(rounding=ROUND_HALF_UP)
    # Mirror the strict PointIdentity conversion; accidental 9.6bp rounding
    # changes the strategy and should never silently pass.
    if shift < 0 or abs(shift - rounded) > Decimal("0.000001"):
        raise DirectCsvError(f"{side}: multiplier {value} does not encode an exact integer shift_bp")
    return int(rounded)


def _row_fixed_lot(row: Mapping[str, str], template: tuple[bool, bool] | None, context: str) -> bool | None:
    """Source v6 fixed-lot admission; a swept CSV column overrides the template."""
    use_fix_raw = str(row.get(_USE_FIX) or "").strip()
    use_upnl_raw = str(row.get(_USE_UPNL) or "").strip()
    use_fix = _boolean(use_fix_raw, f"{context} use_fix") if use_fix_raw else (template[0] if template else None)
    use_upnl = _boolean(use_upnl_raw, f"{context} use_upnl") if use_upnl_raw else (template[1] if template else None)
    if use_fix is None or use_upnl is None:
        return None
    return use_fix and not use_upnl


def load_csv_direct(
    paths: Sequence[str | Path],
    *,
    listing_dates: Mapping[str, object],
    ma_identity: Mapping[tuple[str, str], tuple[str, str, str, str]] | None = None,
    enforce_rectangular: bool = True,
    allow_shared_snapshot_template: bool = False,
    require_fixed_lot: bool = True,
) -> DirectCsvInput:
    """Validate and normalize one-window tester CSV exports.

    Required listing dates come from the existing project registry or dates.xlsx.
    MA type/source are read from run_snapshot_*.json, or passed as explicit
    known canonical values. CSV alone does not contain those four properties.
    """
    csvs, snapshots = _read_sources(paths)
    runs = {run for _label, run, _data in snapshots}
    facts_by_run = {
        run: _snapshot_facts([(label, data) for label, item, data in snapshots if item == run])
        for run in runs
    }
    explicit = {(key[0].upper(), key[1].upper()): identity for key, identity in (ma_identity or {}).items()}
    listing = {str(key).strip().upper(): _utc(value, f"listing_date[{key}]") for key, value in listing_dates.items()}
    raw_points: list[dict[str, object]] = []
    seen: set[str] = set()
    windows: set[tuple[datetime, datetime]] = set()
    file_digests: list[tuple[str, str]] = []
    for label, run, data in csvs:
        facts = _nearest_snapshot_facts(facts_by_run, run)
        identities = dict(facts.ma_identity)
        for key, identity in explicit.items():
            if key in identities and identities[key] != identity:
                raise DirectCsvError(f"conflicting MA type/source for {key}")
            identities[key] = identity
        digest = sha256(data).hexdigest()
        file_digests.append((label, digest))
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DirectCsvError(f"{label}: CSV is not valid UTF-8") from error
        reader = csv.DictReader(StringIO(text))
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            duplicates = sorted({name for name in fields if fields.count(name) > 1})
            raise DirectCsvError(f"{label}: duplicate CSV headers {duplicates}")
        missing = _REQUIRED.difference(fields)
        if missing:
            raise DirectCsvError(f"{label}: missing CSV columns {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            context = f"{label}:{line}"
            if None in row:
                raise DirectCsvError(f"{context}: unexpected CSV columns")
            if "PairsCount" in row and _integer(row["PairsCount"], f"{context} PairsCount") != 1:
                raise DirectCsvError(f"{context}: aggregated multi-symbol report cannot be assigned to one point")
            if require_fixed_lot:
                fixed_lot = _row_fixed_lot(row, facts.fixed_lot_settings, context)
                if fixed_lot is None:
                    raise DirectCsvError(f"{context}: fixed-lot sizing is unknown; run_snapshot_*.json is required")
                if not fixed_lot:
                    raise DirectCsvError(f"{context}: only fixed-lot runs (use_fix=true, use_upnl=false) are analyzable")
            symbol = str(row[_SYMBOL] or "").strip().upper()
            timeframe = str(row[_TIMEFRAME] or "").strip()
            if not symbol or not timeframe or "|" in symbol or "|" in timeframe:
                raise DirectCsvError(f"{context}: invalid symbol or timeframe")
            if symbol not in listing:
                raise DirectCsvError(f"{context}: missing listing date for {symbol}")
            side, mult_col, open_col, close_col = _side_columns(row)
            if (symbol, side) not in identities:
                # Runner snapshots may only name one base symbol even when
                # the optimizer sweeps several symbols using that template.
                # This inference is opt-in and allowed only with a unique MA
                # identity for this side, never silently guessed.
                options = {properties for (name, direction), properties in identities.items() if direction == side}
                if allow_shared_snapshot_template and len(options) == 1:
                    identities[(symbol, side)] = next(iter(options))
                else:
                    raise DirectCsvError(f"{context}: MA type/source unavailable for {symbol}/{side}; provide canonical template identity")
            otype, osource, ctype, csource = identities[(symbol, side)]
            if any("|" in value for value in (otype, osource, ctype, csource)):
                raise DirectCsvError(f"{context}: invalid MA type/source")
            shift_bp = _shift_bp(row[mult_col], side)
            open_ma = _integer(row.get(open_col), f"{context} open_ma", positive=True)
            close_ma = _integer(row.get(close_col), f"{context} close_ma", positive=True)
            start = _utc(row["StartDate"], f"{context} StartDate")
            end = _utc(row["EndDate"], f"{context} EndDate")
            if end <= start:
                raise DirectCsvError(f"{context}: EndDate must exceed StartDate")
            windows.add((start, end))
            effective_start = max(start, listing[symbol])
            seconds = Decimal(str((end - effective_start).total_seconds()))
            if seconds <= 0:
                raise DirectCsvError(f"{context}: non-positive effective test duration for {symbol}")
            days = seconds / DAY_SECONDS
            trades = _integer(row["TotalTrades"], f"{context} TotalTrades")
            wins = _integer(row["Win"], f"{context} Win")
            losses = _integer(row["Los"], f"{context} Los")
            fills = _integer(row["Fills"], f"{context} Fills")
            if wins + losses > trades or fills < trades:
                raise DirectCsvError(f"{context}: inconsistent TotalTrades, Win, Los or Fills")
            initial = _decimal(row["InitialBalance"], f"{context} InitialBalance")
            if initial <= 0:
                raise DirectCsvError(f"{context}: InitialBalance must be positive")
            total_pnl = _decimal(row["TotalPnL"], f"{context} TotalPnL")
            reported_pnl_pct = _decimal(row["TotalPnLPercent"], f"{context} TotalPnLPercent")
            pnl = total_pnl / initial * Decimal(100)
            if abs(pnl - reported_pnl_pct) > _PNL_PERCENT_TOLERANCE:
                raise DirectCsvError(f"{context}: TotalPnL/InitialBalance disagrees with TotalPnLPercent")
            dd = _decimal(row["MaxDrawdownPercent"], f"{context} MaxDrawdownPercent")
            reported_win_rate = _decimal(row["WinRate"], f"{context} WinRate")
            if dd < 0 or reported_win_rate < 0 or reported_win_rate > 100:
                raise DirectCsvError(f"{context}: invalid drawdown or win rate")
            classified = wins + losses
            # Source v6 win rate: wins over classified (non-breakeven) trades.
            win_rate = Decimal(100) * Decimal(wins) / Decimal(classified) if classified else Decimal(0)
            per_trade = Decimal(100) * Decimal(wins) / Decimal(trades) if trades else Decimal(0)
            if min(abs(win_rate - reported_win_rate), abs(per_trade - reported_win_rate)) > _WIN_RATE_TOLERANCE:
                raise DirectCsvError(f"{context}: WinRate disagrees with completed trades")
            pf_raw = str(row.get("ProfitFactor") or "").strip()
            profit_factor = _decimal(pf_raw, f"{context} ProfitFactor") if pf_raw.casefold() not in ("", "n/a", "na", "null", "none", "-") else None
            if profit_factor is not None and profit_factor < 0:
                raise DirectCsvError(f"{context}: invalid ProfitFactor")
            point_id = "|".join((symbol, side, timeframe, str(shift_bp), otype, osource, str(open_ma), ctype, csource, str(close_ma)))
            if point_id in seen:
                raise DirectCsvError(f"{context}: duplicate point {point_id}")
            seen.add(point_id)
            scope_key = f"{symbol}|{side}|{timeframe}"
            activity = Decimal(trades) * MONTH_DAYS / max(days, MONTH_DAYS)
            raw_points.append({
                "point_id": point_id, "scope_key": scope_key,
                "symbol": symbol, "side": side, "timeframe": timeframe,
                "shift_bp": shift_bp, "open_ma": open_ma, "close_ma": close_ma,
                "pnl_pct": pnl, "dd_pct": dd,
                "trades": trades, "wins": wins, "losses": losses,
                "fills": fills, "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "activity_30d": activity,
                "effective_days": days,
                "report_start": pd.Timestamp(start), "report_end": pd.Timestamp(end),
                "listing_date": pd.Timestamp(listing[symbol]),
                "run_tag": str(row.get("RunTag") or ""),
            })
    if len(windows) != 1:
        raise DirectCsvError(f"CSV inputs have {len(windows)} distinct report windows; stitching is not supported")
    if not raw_points:
        raise DirectCsvError("all CSV files contain zero rows")
    raw_points.sort(key=lambda value: str(value["point_id"]))
    frame = pd.DataFrame.from_records(raw_points)
    if enforce_rectangular:
        for scope_key, group in frame.groupby("scope_key"):
            expected = group["shift_bp"].nunique() * group["open_ma"].nunique() * group["close_ma"].nunique()
            if len(group) != expected:
                raise DirectCsvError(f"{scope_key}: incomplete rectangular grid ({len(group)}/{expected})")
    start, end = next(iter(windows))
    input_digest = sha256(_stable_json(raw_points).encode("utf-8")).hexdigest()
    return DirectCsvInput(
        frame=frame, scope_keys=tuple(sorted(frame["scope_key"].unique())),
        csv_sha256=tuple(sorted(file_digests)), input_digest=input_digest,
        window_start=start, window_end=end,
        total_trades=sum(int(p["trades"]) for p in raw_points),
        total_fills=sum(int(p["fills"]) for p in raw_points),
    )


def _stable_json(points: Sequence[Mapping[str, object]]) -> str:
    """Canonical normalized rows, stable under CSV row reordering and run tags."""
    def plain(value: object) -> object:
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, Decimal):
            return format(value, "f")
        return value
    stable = [
        {field: plain(value) for field, value in point.items() if field != "run_tag"}
        for point in points
    ]
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
