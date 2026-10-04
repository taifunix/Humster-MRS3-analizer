import hashlib
import json

from scripts.equity_regime_m3_replay import assess_frozen_row, run_replay


def _metric(trend: str, endpoint: str) -> dict[str, object]:
    return {
        "trend30": trend,
        "endpoint30": endpoint,
        "grid_points": 113,
    }


def _row(result_id: int = 1) -> dict[str, object]:
    return {
        "result_id": result_id,
        "strategy_id": 10 + result_id,
        "symbol": f"SYM{result_id}",
        "side": "LONG",
        "facts": {
            "status": "READY",
            "reason": None,
            "raw_count": 100,
            "windows": {
                "28": _metric("-3", "-4"),
                "14": _metric("3", "3"),
                "7": _metric("3", "3"),
            },
            "pre28": _metric("3", "3"),
            "dd": {"14": "23", "7": "4"},
            "hwm": {
                "28": {"value": "10", "time": "2026-01-01T00:00:00Z"},
                "14": {"value": "11", "time": "2026-01-02T00:00:00Z"},
                "7": {"value": "12", "time": "2026-01-03T00:00:00Z"},
                "0": {"value": "13", "time": "2026-01-04T00:00:00Z"},
            },
            "stages": [1, 1, 1],
            "held_weekly_breakout": True,
            "close": "13",
        },
    }


def test_frozen_row_replay_preserves_full_ordered_hard_reason_set():
    assessment = assess_frozen_row(_row())

    assert assessment.state == "DROP"
    assert assessment.decision == "DROP"
    assert assessment.rank is None
    assert assessment.reasons == ("DD_14_7_GTE_23", "W28_DOWN")


def test_run_replay_writes_canonical_jsonl_and_reports_its_digest(tmp_path):
    source = tmp_path / "facts.jsonl"
    output = tmp_path / "replay.jsonl"
    source.write_text(json.dumps(_row(), separators=(",", ":")) + "\n", encoding="utf-8")

    summary = run_replay(source, output, expected_rows=1)

    payload = output.read_bytes()
    parsed = json.loads(payload)
    assert parsed["result_id"] == 1
    assert parsed["state"] == "DROP"
    assert parsed["reasons"] == ["DD_14_7_GTE_23", "W28_DOWN"]
    assert summary["rows"] == 1
    assert summary["states"] == {"DROP": 1}
    assert summary["valid_unclassified_geometry"] == 0
    assert summary["output_sha256"] == hashlib.sha256(payload).hexdigest().upper()
