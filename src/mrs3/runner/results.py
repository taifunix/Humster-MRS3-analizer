from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import html as stdlib_html
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Mapping
from urllib.parse import unquote, urlparse

from lxml import etree, html as lxml_html
import pandas as pd


class ResultParseError(RuntimeError):
    """Raised when a tester result artifact is structurally incomplete."""


class ResultMismatchError(ResultParseError):
    """Raised when JSON and HTML describe different test results."""


@dataclass(frozen=True, slots=True)
class WizardResult:
    run_id: str
    timestamp: str
    strategy_names: tuple[str, ...]
    stats: dict[str, object]
    chart_url: str
    report_name: str
    period: str
    elapsed: str


@dataclass(frozen=True, slots=True)
class HtmlReport:
    path: Path
    strategy_name: str
    symbol: str
    timeframe: str
    strategy_type: str
    exchange: str | None
    settings: dict[str, object]
    metrics: dict[str, str]
    trade_rows: tuple[dict[str, str], ...]

    def decimal_metric(self, name: str) -> Decimal | None:
        value = self.metrics.get(name)
        return _parse_decimal(value) if value is not None else None


def _report_basename(chart_url: str) -> str:
    path = PurePosixPath(unquote(urlparse(chart_url).path))
    if (
        len(path.parts) < 3
        or tuple(part.casefold() for part in path.parts[-3:-1])
        != ("tester-report", "my_test")
        or not path.name.casefold().endswith(".html")
    ):
        raise ResultParseError(f"unsafe or unexpected chartUrl: {chart_url}")
    return path.name


def load_wizard_results(
    path: Path, *, fallback_report_names: Mapping[str, str] | None = None
) -> tuple[WizardResult, ...]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), parse_float=Decimal, parse_int=Decimal
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResultParseError(f"cannot parse wizard result JSON: {path}") from error
    if not isinstance(document, list):
        raise ResultParseError("wizard result must be a JSON array")
    results: list[WizardResult] = []
    seen_run_ids: set[str] = set()
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise ResultParseError(f"wizard result entry {index} is not an object")
        run_id = entry.get("runId")
        strategies = entry.get("strategies")
        stats = entry.get("stats")
        chart_url = entry.get("chartUrl")
        if not isinstance(run_id, str) or not run_id:
            raise ResultParseError(f"wizard result entry {index} has no runId")
        if run_id in seen_run_ids:
            raise ResultParseError(f"duplicate wizard runId: {run_id}")
        seen_run_ids.add(run_id)
        if (
            not isinstance(strategies, list)
            or not strategies
            or not all(isinstance(name, str) and name for name in strategies)
        ):
            raise ResultParseError(f"wizard result {run_id} has invalid strategies")
        if not isinstance(stats, dict):
            raise ResultParseError(f"wizard result {run_id} has invalid stats")
        if not isinstance(chart_url, str):
            raise ResultParseError(f"wizard result {run_id} has no chartUrl")
        fallback_name = (
            fallback_report_names.get(strategies[0])
            if not chart_url
            and len(strategies) == 1
            and fallback_report_names is not None
            else None
        )
        if fallback_name is not None and (
            Path(fallback_name).name != fallback_name
            or not fallback_name.casefold().endswith(".html")
        ):
            raise ResultParseError(f"unsafe fallback report name: {fallback_name}")
        report_name = fallback_name or _report_basename(chart_url)
        results.append(
            WizardResult(
                run_id=run_id,
                timestamp=str(entry.get("timestamp", "")),
                strategy_names=tuple(strategies),
                stats={str(key): value for key, value in stats.items()},
                chart_url=chart_url,
                report_name=report_name,
                period=str(entry.get("period", "")),
                elapsed=str(entry.get("elapsed", "")),
            )
        )
    return tuple(results)


def _text(node: object) -> str:
    return " ".join(" ".join(node.itertext()).split())


def _extract_metrics(document: object) -> dict[str, str]:
    metrics: dict[str, str] = {}
    for table in document.xpath("//table"):
        headers = [_text(cell) for cell in table.xpath(".//thead/tr[1]/th")]
        if len(headers) < 2 or headers[:2] != ["Metric", "Value"]:
            continue
        for row in table.xpath(".//tbody/tr"):
            cells = row.xpath("./th|./td")
            if len(cells) < 2:
                continue
            key, value = _text(cells[0]), _text(cells[1])
            if key in metrics and metrics[key] != value:
                raise ResultParseError(f"HTML contains conflicting metric: {key}")
            metrics[key] = value
    if not metrics:
        raise ResultParseError("HTML report contains no Metric/Value tables")
    return metrics


def _extract_settings(document: object) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for pre in document.xpath("//pre"):
        raw = "".join(pre.itertext()).strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str) and isinstance(
            value.get("basic"), dict
        ):
            candidates.append(value)
    if len(candidates) != 1:
        raise ResultParseError(
            f"HTML report must contain one complete strategy settings object, found {len(candidates)}"
        )
    return candidates[0]


def extract_html_strategy_name(path: Path) -> str | None:
    """Read the embedded strategy name without constructing an HTML DOM."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    candidates: list[str] = []
    for raw in re.findall(r"<pre\b[^>]*>(.*?)</pre\s*>", source, flags=re.IGNORECASE | re.DOTALL):
        try:
            settings = json.loads(stdlib_html.unescape(raw).strip())
        except json.JSONDecodeError:
            continue
        name = settings.get("name") if isinstance(settings, dict) else None
        basic = settings.get("basic") if isinstance(settings, dict) else None
        if isinstance(name, str) and name and isinstance(basic, dict):
            candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def _extract_trades(document: object) -> tuple[dict[str, str], ...]:
    matching: list[tuple[dict[str, str], ...]] = []
    for table in document.xpath("//table"):
        headers = [_text(cell) for cell in table.xpath(".//thead/tr[1]/th")]
        if not {"Timestamp", "Symbol", "Action", "PnL"}.issubset(headers):
            continue
        rows: list[dict[str, str]] = []
        for row in table.xpath(".//tbody/tr"):
            cells = [_text(cell) for cell in row.xpath("./th|./td")]
            if len(cells) != len(headers):
                raise ResultParseError("HTML trade row width differs from its header")
            rows.append(dict(zip(headers, cells, strict=True)))
        matching.append(tuple(rows))
    if len(matching) != 1:
        raise ResultParseError(
            f"HTML report must contain one trades table, found {len(matching)}"
        )
    return matching[0]


def _extract_exchange(document: object) -> str | None:
    for paragraph in document.xpath("//p[contains(translate(., 'EXCHANGE', 'exchange'), 'exchange:')]"):
        match = re.search(r"exchange:\s*([^|\n]+)", _text(paragraph), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def parse_html_report(path: Path) -> HtmlReport:
    try:
        document = lxml_html.parse(str(path)).getroot()
    except (OSError, etree.ParserError) as error:
        raise ResultParseError(f"cannot parse HTML report: {path}") from error
    metrics = _extract_metrics(document)
    settings = _extract_settings(document)
    basic = settings["basic"]
    required = {"symbol", "time_frame", "strategy"}
    if not required.issubset(basic):
        raise ResultParseError("strategy settings basic object is incomplete")
    return HtmlReport(
        path=path.resolve(),
        strategy_name=str(settings["name"]),
        symbol=str(basic["symbol"]),
        timeframe=str(basic["time_frame"]),
        strategy_type=str(basic["strategy"]),
        exchange=_extract_exchange(document),
        settings=settings,
        metrics=metrics,
        trade_rows=_extract_trades(document),
    )


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    compact = value.strip().replace("\u00a0", " ")
    if compact.casefold() in {"", "n/a", "na", "—", "-"}:
        return None
    match = re.search(r"[-+]?\d[\d\s.,]*", compact)
    if not match:
        return None
    token = match.group(0).replace(" ", "")
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _required_html_metric(report: HtmlReport, name: str) -> Decimal:
    value = report.decimal_metric(name)
    if value is None:
        raise ResultParseError(f"HTML report has no numeric metric: {name}")
    return value


def _stat(result: WizardResult, key: str) -> Decimal:
    if key not in result.stats:
        raise ResultParseError(f"wizard result {result.run_id} has no stat: {key}")
    try:
        return Decimal(str(result.stats[key]))
    except InvalidOperation as error:
        raise ResultParseError(
            f"wizard result {result.run_id} stat is not numeric: {key}"
        ) from error


CORE_METRICS = {
    "InitialBalance": "Initial balance",
    "FinalBalance": "Final balance",
    "TotalPnL": "Total PnL",
    "TotalPnLPercent": "Total PnL, %",
    "TotalTrades": "Total Trades",
    "WinRate": "Win Rate, %",
    "MaxDrawdown": "Max Drawdown",
    "MaxDrawdownPercent": "Max Drawdown, %",
    "TotalFees": "Total fees",
}


def _optional_metric(report: HtmlReport, name: str) -> float | None:
    value = report.decimal_metric(name)
    return float(value) if value is not None else None


def _prefixed_metric(report: HtmlReport, prefix: str) -> float | None:
    matches = [key for key in report.metrics if key.startswith(prefix)]
    if len(matches) != 1:
        return None
    return _optional_metric(report, matches[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_row(result: WizardResult, report: HtmlReport) -> dict[str, object]:
    return {
        "strategy_name": report.strategy_name,
        "run_id": result.run_id,
        "timestamp": result.timestamp,
        "period": result.period,
        "elapsed": result.elapsed,
        "exchange": report.exchange,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "strategy_type": report.strategy_type,
        "report_file": result.report_name,
        "report_sha256": _sha256(report.path),
        "initial_balance": float(_stat(result, "InitialBalance")),
        "final_balance": float(_stat(result, "FinalBalance")),
        "total_pnl": float(_stat(result, "TotalPnL")),
        "total_pnl_pct": float(_stat(result, "TotalPnLPercent")),
        "total_trades": int(_stat(result, "TotalTrades")),
        "win_trades": int(_required_html_metric(report, "Win Trades")),
        "loss_trades": int(_required_html_metric(report, "Los Trades")),
        "win_rate_pct": float(_stat(result, "WinRate")),
        "max_drawdown": float(_stat(result, "MaxDrawdown")),
        "max_drawdown_pct": float(_stat(result, "MaxDrawdownPercent")),
        "total_fees": float(_stat(result, "TotalFees")),
        "profit_factor": _prefixed_metric(report, "Profit Factor"),
        "gross_profit": _optional_metric(report, "Gross profit"),
        "gross_loss": _optional_metric(report, "Gross loss"),
        "trading_volume_usdt": _optional_metric(report, "Trading volume (USDT)"),
        "total_transactions": _optional_metric(report, "Total transactions (buy/sell)"),
        "funding_net": _optional_metric(report, "Funding net"),
        "funding_received": _optional_metric(report, "Funding accrued (received)"),
        "funding_paid": _optional_metric(report, "Funding paid"),
        "expectancy_per_trade": _optional_metric(report, "Expectancy per trade"),
        "position_avg_pct": _optional_metric(report, "Position avg, % of margin balance"),
        "position_max_pct": _optional_metric(report, "Position max, % of margin balance"),
        "risk_reward": _prefixed_metric(report, "Risk/Reward"),
        "recovery_factor": _prefixed_metric(report, "Recovery Factor"),
        "report_range": report.metrics.get("Report range"),
        "days_in_test": _optional_metric(report, "Days in test"),
        "months_in_test": _optional_metric(report, "Months in test"),
        "months_with_data": _optional_metric(report, "Months with data"),
        "pairs_count": _optional_metric(report, "Pairs count"),
        "trade_row_count": len(report.trade_rows),
        "chart_url": result.chart_url,
        "strategy_settings_json": json.dumps(
            report.settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "trades_json": json.dumps(
            report.trade_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "html_metrics_json": json.dumps(
            report.metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }


def _name_verified_result_row(
    result: WizardResult, strategy_name: str, report_path: Path
) -> dict[str, object]:
    """Persist runner evidence without walking the report's metric/trade tables."""
    def wizard_stat(name: str) -> object | None:
        value = result.stats.get(name)
        return float(value) if isinstance(value, (int, float, Decimal)) else None

    return {
        "strategy_name": strategy_name,
        "run_id": result.run_id,
        "timestamp": result.timestamp,
        "period": result.period,
        "elapsed": result.elapsed,
        "report_file": result.report_name,
        "report_sha256": _sha256(report_path),
        "chart_url": result.chart_url,
        "verification_mode": "strategy_name_only",
        "initial_balance": wizard_stat("InitialBalance"),
        "final_balance": wizard_stat("FinalBalance"),
        "total_pnl": wizard_stat("TotalPnL"),
        "total_pnl_pct": wizard_stat("TotalPnLPercent"),
        "total_trades": wizard_stat("TotalTrades"),
        "win_rate_pct": wizard_stat("WinRate"),
        "max_drawdown": wizard_stat("MaxDrawdown"),
        "max_drawdown_pct": wizard_stat("MaxDrawdownPercent"),
        "total_fees": wizard_stat("TotalFees"),
        "strategy_settings_json": "",
        "trades_json": "[]",
        "html_metrics_json": "{}",
    }


def reconcile_results(
    expected_names: tuple[str, ...],
    wizard_results: tuple[WizardResult, ...],
    report_dir: Path,
    tolerance: Decimal,
    report_paths: Mapping[str, Path] | None = None,
) -> pd.DataFrame:
    if len(set(expected_names)) != len(expected_names) or not expected_names:
        raise ValueError("expected strategy names must be non-empty and unique")
    by_name: dict[str, WizardResult] = {}
    for result in wizard_results:
        if len(result.strategy_names) != 1 or result.strategy_names[0] not in expected_names:
            raise ResultMismatchError(
                f"unexpected strategy set in run {result.run_id}: {result.strategy_names!r}"
            )
        name = result.strategy_names[0]
        if name in by_name:
            raise ResultMismatchError(f"multiple wizard results for strategy: {name}")
        by_name[name] = result
    missing = [name for name in expected_names if name not in by_name]
    if missing:
        raise ResultMismatchError(f"missing wizard results for: {', '.join(missing)}")

    rows: list[dict[str, object]] = []
    resolved_report_dir = report_dir.resolve()
    for name in expected_names:
        result = by_name[name]
        report_path = (report_paths or {}).get(name)
        if report_path is None:
            report_path = (resolved_report_dir / result.report_name).resolve()
        else:
            report_path = report_path.resolve()
        if not report_path.is_file():
            raise ResultParseError(f"HTML report is missing for {name}: {report_path}")
        embedded_name = extract_html_strategy_name(report_path)
        if embedded_name is None:
            raise ResultParseError(f"HTML strategy name is unavailable for {name}: {report_path}")
        if embedded_name != name:
            raise ResultMismatchError(
                f"HTML strategy name differs for {name}: {embedded_name}"
            )
        rows.append(_name_verified_result_row(result, embedded_name, report_path))
    return pd.DataFrame(rows)


def write_results_csv_atomic(frame: pd.DataFrame, path: Path) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}.", suffix=".csv", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        frame.to_csv(
            temporary,
            index=False,
            float_format="%.12g",
            lineterminator="\n",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
