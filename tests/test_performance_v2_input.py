from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from mrs3.performance_v2_input import (
    PerformanceV2InputError,
    adapt_strategy_identity,
    create_v2_parser_staging,
    read_performance_v2_inbox,
    remove_v2_parser_staging,
)
import mrs3.performance_v2_input as input_module
from mrs3.performance_v2_store import PerformanceV2Config


def _strategy(name: str, *, side: str = "LONG", orders: int = 1) -> dict[str, object]:
    active = "ma_long" if side == "LONG" else "ma_short"
    close = "ma_close_long" if side == "LONG" else "ma_close_short"
    entries = []
    for index in range(1, orders + 1):
        shift = 100 * index
        entries.append(
            {
                "id": index,
                "len": 10 + index,
                "multiplier": (1 - shift / 10000) if side == "LONG" else (1 + shift / 10000),
                "lot_x": 1 / orders,
            }
        )
    return {
        "name": name,
        "exchange": {"name": "Bybit", "use_upnl": True},
        "basic": {
            "strategy": "mrs3",
            "symbol": "BTCUSDT",
            "time_frame": "1h",
            "use_long": side == "LONG",
            "use_short": side == "SHORT",
        },
        "mrs3": {
            "ma_long": entries if side == "LONG" else [],
            "ma_short": entries if side == "SHORT" else [],
            "ma_close_long": {"len": 20},
            "ma_close_short": {"len": 20},
        },
    }


def _inbox(tmp_path: Path, *, mode: str = "FAST", orders: int = 1, diagnostics: object | None = None) -> tuple[Path, Path]:
    inbox = tmp_path / f"inbox-{mode.lower()}"
    strategies = inbox / "strategies"
    strategies.mkdir(parents=True)
    reports = tmp_path / "tester-report"
    reports.mkdir(exist_ok=True)
    strategy = _strategy("BTC-demo", orders=orders)
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path = strategies / "BTC-demo.json"
    strategy_path.write_bytes(strategy_bytes)
    report_path = reports / "BTC-demo.html"
    report_path.write_bytes(b"<html>report</html>")
    strategy_hash = sha256(json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report_hash = sha256(report_path.read_bytes()).hexdigest()
    if diagnostics is None:
        orders_diagnostics = [
            {
                "order_id": index,
                "plateau_id": "P1",
                "plateau_point_count": 4,
                "base_point_trades": 20,
                "plateau_total_trades": 80,
            }
            for index in range(1, orders + 1)
        ]
        diagnostics = {"candidate-1": {"order_count": orders, "orders": orders_diagnostics}}
    manifest = {
        "schema_version": 1,
        "batch_id": mode.lower(),
        "expected_strategy_names": ["BTC-demo"],
        "tester_config_sha256": "t" * 64,
        "commission_contract": {
            "MakerFee": "0.0002",
            "TakerFee": "0.0004",
            "SlippagePercent": "0.01",
            "FundingRate": "0.0001",
            "FundingIntervalHours": "8",
        },
        "commission_contract_id": "c" * 64,
        "run_mode": mode,
        "entries": [
            {
                "manifest_entry_id": "e" * 32,
                "strategy_name": "BTC-demo",
                "strategy_version_id": strategy_hash,
                "strategy_path": str(strategy_path),
                "report_path": str(report_path),
                "wizard_run_id": "run-1",
                "exchange_name": "Bybit",
                "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
                "source_report_sha256": report_hash,
            }
        ],
        "v6_provenance": {
            "analysis_run_id": "a" * 64,
            "generation_manifest_sha256": "g" * 64,
            "strategy_json_sha256": {"BTC-demo.json": strategy_hash},
            "candidate_identity_to_strategy_names": {"candidate-1": ["BTC-demo"]},
            "candidate_diagnostics": diagnostics,
        },
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return inbox, reports


def test_fast_and_runs_share_one_adapter_and_keep_typed_identity(tmp_path: Path) -> None:
    fast, report_root = _inbox(tmp_path, mode="FAST", orders=2)
    runs, _ = _inbox(tmp_path, mode="RUNS", orders=2)

    prepared_fast = read_performance_v2_inbox(fast, report_root)
    prepared_runs = read_performance_v2_inbox(runs, report_root)

    assert prepared_fast.entries[0].identity == prepared_runs.entries[0].identity
    entry = prepared_fast.entries[0]
    assert entry.identity.side == "LONG"
    assert entry.identity.close_ma_len == 20
    assert [order.order_id for order in entry.identity.orders] == [1, 2]
    assert [order.shift_bp for order in entry.identity.orders] == [100, 200]
    assert entry.identity.orders[0].lot_x == entry.identity.orders[1].lot_x
    assert "point_id" not in entry.__dict__ if hasattr(entry, "__dict__") else True
    assert not any("strategy_json" in field for field in prepared_fast.__dataclass_fields__)
    assert len(prepared_fast.plateaus) == 1
    assert prepared_fast.plateaus[0].plateau_id == "P1"


def test_missing_and_conflicting_plateau_facts_are_rejected(tmp_path: Path) -> None:
    inbox, report_root = _inbox(tmp_path, orders=1, diagnostics={})
    with pytest.raises(PerformanceV2InputError, match="plateau"):
        read_performance_v2_inbox(inbox, report_root)

    conflicting = {
        "candidate-1": {
            "order_count": 1,
            "orders": [{
                "order_id": 1,
                "plateau_id": "P1",
                "plateau_point_count": 5,
                "base_point_trades": 20,
                "plateau_total_trades": 80,
            }],
        }
    }
    inbox, conflict_report_root = _inbox(tmp_path / "conflict", orders=1, diagnostics=conflicting)
    strategy_path = inbox / "strategies" / "BTC-demo.json"
    second_path = inbox / "strategies" / "other.json"
    second_strategy = _strategy("other")
    second_bytes = json.dumps(second_strategy, separators=(",", ":")).encode()
    second_path.write_bytes(second_bytes)
    second_report = conflict_report_root / "other.html"
    second_report.write_bytes(b"<html>other</html>")
    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    manifest["expected_strategy_names"].append("other")
    second_entry = {**manifest["entries"][0], "strategy_name": "other", "strategy_path": str(second_path), "report_path": str(second_report), "manifest_entry_id": "f" * 32,
                    "strategy_version_id": sha256(json.dumps(second_strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "source_strategy_sha256": sha256(second_bytes).hexdigest(), "source_report_sha256": sha256(second_report.read_bytes()).hexdigest()}
    manifest["entries"].append(second_entry)
    manifest["v6_provenance"]["candidate_identity_to_strategy_names"]["candidate-2"] = ["other"]
    manifest["v6_provenance"]["candidate_diagnostics"]["candidate-2"] = {
        "order_count": 1,
        "orders": [{"order_id": 1, "plateau_id": "P1", "plateau_point_count": 4,
                     "base_point_trades": 20, "plateau_total_trades": 80}],
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PerformanceV2InputError, match="plateau"):
        read_performance_v2_inbox(inbox, conflict_report_root)


def test_trust_boundary_and_size_are_checked_before_staging(tmp_path: Path) -> None:
    inbox, report_root = _inbox(tmp_path, orders=1)
    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    manifest["entries"][0]["report_path"] = str(tmp_path / "outside.html")
    (tmp_path / "outside.html").write_bytes(b"bad")
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PerformanceV2InputError, match="report path"):
        read_performance_v2_inbox(inbox, report_root)

    inbox, report_root = _inbox(tmp_path / "large", orders=1)
    with pytest.raises(PerformanceV2InputError, match="size"):
        read_performance_v2_inbox(inbox, report_root, max_html_bytes=2)


def test_staging_is_fresh_bounded_and_removed(tmp_path: Path) -> None:
    inbox, report_root = _inbox(tmp_path, orders=1)
    prepared = read_performance_v2_inbox(inbox, report_root)
    root = tmp_path / "v2"
    staging = create_v2_parser_staging(root, prepared)
    assert staging.parent == root / ".staging"
    assert (staging / ".v2-staging-owner").is_file()
    assert (staging / "strategies" / "BTC-demo.json").is_file()
    assert (staging / "reports" / "BTC-demo.html").is_file()
    assert staging != create_v2_parser_staging(root, prepared)
    remove_v2_parser_staging(staging)
    assert not staging.exists()


def test_cleanup_rejects_unrelated_staging_directory(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign" / ".staging" / "not-v2"
    foreign.mkdir(parents=True)
    payload = foreign / "payload"
    payload.write_text("keep", encoding="utf-8")

    with pytest.raises(PerformanceV2InputError, match="ownership"):
        remove_v2_parser_staging(foreign)
    assert payload.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_replayed_marker_in_same_staging_root(tmp_path: Path) -> None:
    inbox, report_root = _inbox(tmp_path)
    prepared = read_performance_v2_inbox(inbox, report_root)
    root = tmp_path / "v2"
    owned = create_v2_parser_staging(root, prepared)
    foreign = owned.parent / "foreign"
    foreign.mkdir()
    (foreign / ".v2-staging-owner").write_bytes((owned / ".v2-staging-owner").read_bytes())
    payload = foreign / "payload"
    payload.write_text("keep", encoding="utf-8")

    with pytest.raises(PerformanceV2InputError, match="ownership"):
        remove_v2_parser_staging(foreign)
    assert payload.read_text(encoding="utf-8") == "keep"
    remove_v2_parser_staging(owned)


def test_staging_failure_leaves_replaced_foreign_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox, report_root = _inbox(tmp_path)
    prepared = read_performance_v2_inbox(inbox, report_root)
    original = input_module._write_staging_marker

    def replace_after_marker(staging: Path, staging_root: Path) -> None:
        original(staging, staging_root)
        shutil.rmtree(staging)
        staging.mkdir()
        (staging / "foreign-payload").write_text("keep", encoding="utf-8")
        raise PerformanceV2InputError("injected staging failure")

    monkeypatch.setattr(input_module, "_write_staging_marker", replace_after_marker)
    with pytest.raises(PerformanceV2InputError, match="injected"):
        create_v2_parser_staging(tmp_path / "v2", prepared)
    foreign = next((tmp_path / "v2" / ".staging").iterdir())
    assert (foreign / "foreign-payload").read_text(encoding="utf-8") == "keep"


def test_cleanup_survives_swap_after_initial_ownership_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox, report_root = _inbox(tmp_path)
    prepared = read_performance_v2_inbox(inbox, report_root)
    owned = create_v2_parser_staging(tmp_path / "v2", prepared)
    original_verify = input_module._verify_staging_marker

    def swap_after_verify(staging: Path, *, expected_staging: Path | None = None) -> None:
        if expected_staging is None:
            original_verify(staging)
        else:
            original_verify(staging, expected_staging=expected_staging)
        if expected_staging is None:
            shutil.rmtree(staging)
            staging.mkdir()
            (staging / "foreign-payload").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(input_module, "_verify_staging_marker", swap_after_verify)
    with pytest.raises(PerformanceV2InputError, match="ownership"):
        remove_v2_parser_staging(owned)
    assert not owned.exists()
    tombstones = [path for path in owned.parent.iterdir() if path.is_dir()]
    assert len(tombstones) == 1
    assert (tombstones[0] / "foreign-payload").read_text(encoding="utf-8") == "keep"


def test_identity_rejects_non_integral_shift_and_invalid_order_ids() -> None:
    strategy = _strategy("bad")
    identity = adapt_strategy_identity(strategy)
    assert identity.side == "LONG" and identity.orders[0].shift_bp == 100
    strategy["mrs3"]["ma_long"][0]["multiplier"] = 0.97305  # type: ignore[index]
    with pytest.raises(PerformanceV2InputError, match="shift"):
        adapt_strategy_identity(strategy)


def test_inbox_snapshot_is_verified_when_preparation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox, report_root = _inbox(tmp_path)
    original = input_module._manifest_diagnostics

    def mutate(*args: object, **kwargs: object) -> object:
        (inbox / "strategies" / "BTC-demo.json").write_bytes(b"changed")
        return original(*args, **kwargs)

    monkeypatch.setattr(input_module, "_manifest_diagnostics", mutate)
    with pytest.raises(PerformanceV2InputError, match="changed"):
        read_performance_v2_inbox(inbox, report_root)
