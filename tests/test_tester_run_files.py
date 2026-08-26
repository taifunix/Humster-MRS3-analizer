from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.config import AlgorithmConfig
from mrs3.tester_run_files import publish_run_snapshots


def test_publish_run_snapshots_replaces_runs_and_configures_tester(tmp_path: Path) -> None:
    template = tmp_path / "run_snapshot.json"
    template.write_text(json.dumps({
        "settings": [{"name": "template", "basic": {"strategy": "mrs3", "symbol": "OLD", "time_frame": "5m", "use_long": True, "use_short": False}, "mrs3": {
            "ma_long": [{"id": 1, "len": 1, "multiplier": 1.0, "lot_x": 0.0}], "ma_short": [],
            "ma_close_long": {"len": 1, "multiplier": 1.0}, "ma_close_short": {"len": 1, "multiplier": 1.0},
        }}], "tester_config": {"MakerFee": 0.00001, "StartDate": "old", "EndDate": "old", "max_parallel_runs": 1},
    }), encoding="utf-8")
    bot_root = tmp_path / "bot"; runs = bot_root / "tester" / "runs"; runs.mkdir(parents=True)
    (runs / "old.json").write_text("old", encoding="utf-8")
    tester_config = bot_root / "tester" / "config_tester.json"; tester_config.write_text('{"use_runs": false}', encoding="utf-8")
    structure = {"candidate_id": "CANDIDATE", "structure_id": "STR", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "order_count": 1, "common_close_ma": 7, "orders": ({"point_id": "P", "plateau_id": "PLAT", "open_ma": 5, "shift_bp": 100, "close_support": 1.0, "source_pnl_pct": 10},)}

    result = publish_run_snapshots(template, bot_root, tester_config, [structure], "2026-08-01", "2026-08-18", 7, AlgorithmConfig.defaults(), analysis_run_id="a" * 64)

    files = list(runs.glob("*.json")); assert result["run_count"] == len(files) == 1; assert not (runs / "old.json").exists()
    snapshot = json.loads(files[0].read_text(encoding="utf-8")); settings = snapshot["settings"][0]
    assert settings["basic"]["symbol"] == "BTCUSDT" and settings["basic"]["time_frame"] == "1h"
    assert settings["mrs3"]["ma_long"][0] == {"id": 1, "len": 5, "multiplier": 0.99, "lot_x": 1.0}
    assert snapshot["tester_config"] == {"MakerFee": 0.00001, "StartDate": "2026-08-01T00:00:00", "EndDate": "2026-08-18T00:00:00", "max_parallel_runs": 7, "name_comment": "runs", "use_runs": True}
    assert '"MakerFee": 0.00001' in files[0].read_text(encoding="utf-8")
    assert json.loads(tester_config.read_text(encoding="utf-8"))["use_runs"] is True
    assert snapshot["tester_config"]["name_comment"] == "runs"
    assert snapshot["tester_config"]["use_runs"] is True
    manifest = json.loads((bot_root / "tester" / "runs_manifest.json").read_text(encoding="utf-8"))
    assert manifest["analysis_run_id"] == "a" * 64 and manifest["entries"][0]["strategy_name"] == settings["name"]


def test_publish_run_snapshots_reads_bot_template_with_bom_and_trailing_comma(tmp_path: Path) -> None:
    template = tmp_path / "run_snapshot.json"
    template.write_bytes(b'\xef\xbb\xbf{"settings":[{"name":"template","basic":{"strategy":"mrs3","symbol":"OLD","time_frame":"5m","use_long":true,"use_short":false},"mrs3":{"ma_long":[{"id":1,"len":1,"multiplier":1.0,"lot_x":0.0}],"ma_short":[],"ma_close_long":{"len":1,"multiplier":1.0},"ma_close_short":{"len":1,"multiplier":1.0}}}],"tester_config":{},}')
    bot_root = tmp_path / "bot"; tester_config = bot_root / "tester" / "config_tester.json"; tester_config.parent.mkdir(parents=True)
    tester_config.write_text("{}", encoding="utf-8")
    structure = {"candidate_id": "C", "structure_id": "S", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h", "order_count": 1, "common_close_ma": 7, "orders": ({"point_id": "P", "plateau_id": "PLAT", "open_ma": 5, "shift_bp": 100, "close_support": 1.0, "source_pnl_pct": 10},)}

    result = publish_run_snapshots(template, bot_root, tester_config, [structure], "2026-08-01", "2026-08-18", 1, AlgorithmConfig.defaults(), analysis_run_id="a" * 64)

    assert result["run_count"] == 1


def test_publish_run_snapshots_refuses_a_runs_directory_symlink(tmp_path: Path, monkeypatch) -> None:
    bot_root = tmp_path / "bot"; runs = bot_root / "tester" / "runs"; runs.mkdir(parents=True)
    tester_config = bot_root / "tester" / "config_tester.json"; tester_config.write_text("{}", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == runs or real_is_symlink(path))

    with pytest.raises(ValueError, match="symbolic"):
        publish_run_snapshots(tmp_path / "template.json", bot_root, tester_config, [{}], "2026-08-01", "2026-08-18", 1, AlgorithmConfig.defaults(), analysis_run_id="a" * 64)
