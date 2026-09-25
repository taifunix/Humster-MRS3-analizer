from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_performance_v2 import PerformanceV2ApiError, parse_performance_v2_export_query


def test_performance_v2_export_query_preserves_union_and_retest_intersection() -> None:
    selection = parse_performance_v2_export_query("status=FINALIST&status=RESERVE")
    assert selection.statuses == ("FINALIST", "RESERVE")
    assert parse_performance_v2_export_query("status=FINALIST&retest=1").retest is True
    assert parse_performance_v2_export_query("retest=1").statuses == ()
    assert parse_performance_v2_export_query("all_active=true").all_active is True
    with pytest.raises(PerformanceV2ApiError) as raised:
        parse_performance_v2_export_query("all_active=true&retest=1")
    assert (raised.value.code, raised.value.status) == ("all_active_mixed_with_status", 400)


def test_performance_v2_export_rejects_unknown_query_parameter(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/api/v2/strategies/performance-v2/export?wat=1")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        assert response.status == 400
        assert body == {"error": "unknown_query_parameter", "message": "Unknown query parameter."}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
