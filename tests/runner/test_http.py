from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mrs3.runner.http import (
    RowState,
    TesterHttpClient,
    TesterHttpError,
    parse_strategy_table,
    parse_wizard,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_parse_wizard_decodes_full_config_and_single_name() -> None:
    wizard = parse_wizard((FIXTURES / "tester_wizard.html").read_text(encoding="utf-8"))

    assert wizard.config["name_comment"] == "my_test"
    assert wizard.config["max_parallel_runs"] == 1
    assert wizard.settings == ("BABASTOCK_1",)


def test_wizard_post_uses_decoded_config_and_plain_strategy_name() -> None:
    requests: list[httpx.Request] = []
    wizard_html = (FIXTURES / "tester_wizard.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/htmx/tester/wizard":
            return httpx.Response(200, text=wizard_html)
        if request.method == "POST" and request.url.path == "/htmx/tester/wizard/run":
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    client = TesterHttpClient(
        "http://127.0.0.1:8087", transport=httpx.MockTransport(handler)
    )

    client.launch_strategy("BABASTOCK_1")

    assert requests[0].url.params["single"] == "QkFCQVNUT0NLXzE="
    payload = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/htmx/tester/wizard/run"
    assert payload["settings"] == ["BABASTOCK_1"]
    assert payload["config"]["name_comment"] == "my_test"
    assert all(request.url.path != "/htmx/tester/run" for request in requests)


def test_native_single_mode_uses_run_and_status_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/htmx/tester/run":
            return httpx.Response(200, text="started")
        if request.method == "GET" and request.url.path == "/htmx/tester/status":
            return httpx.Response(200, text='<span class="stat-value">Running</span>')
        return httpx.Response(404)

    client = TesterHttpClient(
        "http://127.0.0.1:8087", transport=httpx.MockTransport(handler)
    )

    client.run_tester()
    assert "Running" in client.tester_status()

    assert [request.url.path for request in requests] == [
        "/htmx/tester/run",
        "/htmx/tester/status",
    ]


def test_strategy_table_parses_ready_running_and_result_rows() -> None:
    rows = parse_strategy_table(
        (FIXTURES / "tester_table.html").read_text(encoding="utf-8")
    )

    assert [row.name for row in rows] == ["ADM1", "BABASTOCK_1", "RUNNING"]
    assert rows[0].state is RowState.RESULT
    assert rows[0].run_id == "6f195d8c"
    assert rows[1].state is RowState.TEST
    assert rows[2].state is RowState.RUNNING
    assert rows[2].percent == pytest.approx(62)


def test_strategy_table_prefers_live_progress_over_stale_result_link() -> None:
    rows = parse_strategy_table(
        """
        <table id="tester-strategies-table"><tbody><tr>
          <td>Bybit</td><td>ONUSDT</td><td>mrs3</td><td>5m</td><td>A</td>
          <td>
            <button hx-get="/htmx/tester/wizard/result?runId=old-run">Result</button>
            <progress value="28" max="100"></progress>
          </td>
        </tr></tbody></table>
        """
    )

    assert rows[0].state is RowState.RUNNING
    assert rows[0].percent == pytest.approx(28)


def test_launch_rejects_wizard_for_different_strategy() -> None:
    wizard_html = (FIXTURES / "tester_wizard.html").read_text(encoding="utf-8")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=wizard_html))
    client = TesterHttpClient("http://127.0.0.1:8087", transport=transport)

    with pytest.raises(TesterHttpError, match="different strategy"):
        client.launch_strategy("ADM1")
