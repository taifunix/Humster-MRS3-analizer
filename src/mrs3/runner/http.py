from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from enum import Enum
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
from lxml import html as lxml_html


class TesterHttpError(RuntimeError):
    """Raised when an HTMX response violates the tester contract."""

    __test__ = False


class RowState(str, Enum):
    TEST = "TEST"
    RUNNING = "RUNNING"
    RESULT = "RESULT"


@dataclass(frozen=True, slots=True)
class WizardLaunch:
    config: dict[str, object]
    settings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategyRow:
    exchange: str
    symbol: str
    strategy_type: str
    timeframe: str
    name: str
    state: RowState
    percent: float | None = None
    run_id: str | None = None


def _document(fragment: str):
    try:
        return lxml_html.fragment_fromstring(fragment, create_parent="div")
    except (ValueError, lxml_html.ParserError) as error:
        raise TesterHttpError("tester returned invalid HTML") from error


def _node_text_by_id(document: object, element_id: str) -> str:
    nodes = document.xpath(f'.//*[@id="{element_id}"]')
    if len(nodes) != 1:
        raise TesterHttpError(f"wizard HTML must contain one #{element_id}")
    return "".join(nodes[0].itertext()).strip()


def _decode_base64(value: str, label: str) -> bytes:
    try:
        return b64decode(value, validate=True)
    except ValueError as error:
        raise TesterHttpError(f"invalid Base64 in {label}") from error


def parse_wizard(fragment: str) -> WizardLaunch:
    document = _document(fragment)
    config_b64 = _node_text_by_id(document, "tester-wizard-json-b64")
    setting_b64 = _node_text_by_id(document, "tester-wizard-settings-data")
    is_multi = _node_text_by_id(document, "tester-wizard-is-multi").casefold()
    if is_multi != "false":
        raise TesterHttpError("single-strategy wizard unexpectedly returned multi mode")
    try:
        config = json.loads(_decode_base64(config_b64, "wizard config").decode("utf-8"))
        setting = _decode_base64(setting_b64, "wizard setting").decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TesterHttpError("wizard Base64 payload is not valid UTF-8 JSON") from error
    if not isinstance(config, dict):
        raise TesterHttpError("wizard config must decode to a JSON object")
    if not setting:
        raise TesterHttpError("wizard returned an empty strategy name")
    return WizardLaunch(config=config, settings=(setting,))


def _result_run_id(action_cell: object) -> tuple[bool, str | None]:
    found = False
    run_id: str | None = None
    for node in action_cell.xpath('.//*[@hx-get]'):
        target = str(node.get("hx-get", ""))
        node_text = " ".join(node.itertext()).strip().casefold()
        if "/htmx/tester/wizard/result" not in target and "результат" not in node_text:
            continue
        found = True
        values = parse_qs(urlparse(target).query).get("runId", ())
        if values:
            run_id = values[0]
            break
    return found, run_id


def _progress_percent(action_cell: object) -> float | None:
    for element in action_cell.xpath(".//progress"):
        raw = element.get("value")
        if raw is not None:
            try:
                return float(str(raw).replace(",", "."))
            except ValueError:
                pass
    for attribute in ("aria-valuenow", "data-progress", "data-percent"):
        values = action_cell.xpath(f'.//*[@{attribute}]/@{attribute}')
        for raw in values:
            try:
                return float(str(raw).replace(",", "."))
            except ValueError:
                pass
    text = " ".join(" ".join(action_cell.itertext()).split())
    match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%", text)
    return float(match.group(1).replace(",", ".")) if match else None


def parse_strategy_table(fragment: str) -> tuple[StrategyRow, ...]:
    document = _document(fragment)
    rows: list[StrategyRow] = []
    seen: set[str] = set()
    for table_row in document.xpath('.//table[@id="tester-strategies-table"]//tbody/tr'):
        cells = table_row.xpath("./td")
        if len(cells) < 6:
            raise TesterHttpError("strategy table row has fewer than six cells")
        values = [" ".join(" ".join(cell.itertext()).split()) for cell in cells[:5]]
        exchange, symbol, strategy_type, timeframe, name = values
        if not name:
            raise TesterHttpError("strategy table contains an empty name")
        if name in seen:
            raise TesterHttpError(f"strategy table contains duplicate name: {name}")
        seen.add(name)
        action_cell = cells[-1]
        has_result, run_id = _result_run_id(action_cell)
        percent = _progress_percent(action_cell)
        if percent is not None:
            state = RowState.RUNNING
        elif has_result:
            state = RowState.RESULT
        else:
            state = RowState.TEST
        rows.append(
            StrategyRow(
                exchange=exchange,
                symbol=symbol,
                strategy_type=strategy_type,
                timeframe=timeframe,
                name=name,
                state=state,
                percent=percent,
                run_id=run_id,
            )
        )
    return tuple(rows)


class TesterHttpClient:
    __test__ = False

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "text/html, application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TesterHttpClient:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def list_strategies(self) -> tuple[StrategyRow, ...]:
        response = self._client.get("/htmx/tester/strategies-table")
        response.raise_for_status()
        return parse_strategy_table(response.text)

    def run_tester(self) -> None:
        response = self._client.post("/htmx/tester/run")
        response.raise_for_status()

    def tester_status(self) -> str:
        response = self._client.get("/htmx/tester/status")
        response.raise_for_status()
        return response.text

    def launch_strategy(self, name: str) -> WizardLaunch:
        encoded = b64encode(name.encode("utf-8")).decode("ascii")
        response = self._client.get("/htmx/tester/wizard", params={"single": encoded})
        response.raise_for_status()
        wizard = parse_wizard(response.text)
        if wizard.settings != (name,):
            raise TesterHttpError(
                f"wizard returned a different strategy: {wizard.settings!r} instead of {name!r}"
            )
        launched = self._client.post(
            "/htmx/tester/wizard/run",
            json={"config": wizard.config, "settings": list(wizard.settings)},
        )
        launched.raise_for_status()
        return wizard

    def shutdown(self) -> None:
        response = self._client.post("/htmx/system/shutdown")
        response.raise_for_status()
