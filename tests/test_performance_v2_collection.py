from __future__ import annotations
from hashlib import sha256
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.performance_v2_collection import build_single_mode_collection_inbox
from mrs3.performance_v2_input import PerformanceV2InputError, read_performance_v2_inbox
from mrs3.performance_v2_html import ParsedPerformanceV2Report
import mrs3.performance_v2_collection as collection_module
import mrs3.performance_v2_import as import_module


_CONTRACT_FIELDS = {
    "MakerFee": "0.0002",
    "TakerFee": "0.0004",
    "SlippagePercent": "0.01",
    "FundingRate": "0.0001",
    "FundingIntervalHours": "8",
}


def _strategy(name: str) -> dict[str, object]:
    return {
        "name": name,
        "exchange": {"name": "Bybit", "use_upnl": True},
        "basic": {
            "strategy": "mrs3",
            "symbol": "BTCUSDT",
            "time_frame": "1h",
            "use_long": True,
            "use_short": False,
        },
        "mrs3": {
            "ma_long": [{"id": 1, "len": 11, "multiplier": 0.99, "lot_x": 1}],
            "ma_short": [],
            "ma_close_long": {"len": 20},
            "ma_close_short": {"len": 20},
        },
    }


def _member(
    tmp_path: Path,
    name: str,
    *,
    start: str,
    end: str,
    taker: str,
    run_id: str,
    isolated_strategy: bool = False,
    report_name: str | None = None,
    commission: bool = True,
) -> tuple[Path, Path, Path]:
    strategy_root = (tmp_path / "strategies") if isolated_strategy else (tmp_path.parent / "strategies")
    strategy_root.mkdir(parents=True, exist_ok=True)
    strategy = _strategy(name)
    strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
    strategy_path = strategy_root / f"{name}.json"
    strategy_path.write_bytes(strategy_bytes)
    report_root = tmp_path.parent / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"{report_name or name}.html"
    report_path.write_bytes(f"<html>{name}</html>".encode())

    strategy_id = sha256(
        json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    strategy_hash = sha256(strategy_bytes).hexdigest()
    report_hash = sha256(report_path.read_bytes()).hexdigest()
    contract = {**_CONTRACT_FIELDS, "TakerFee": taker}
    contract_id = sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "schema_version": 1,
        "batch_id": f"batch-{name}",
        "expected_strategy_names": [name],
        "tester_config_sha256": run_id.ljust(64, "x")[:64],
        "commission_contract": contract,
        "commission_contract_id": contract_id,
        "source_mode": "metadata_only",
        "run_mode": "SINGLE_MODE",
        "test_start": start,
        "test_end": end,
        "entries": [{
            "manifest_entry_id": run_id.ljust(32, "e")[:32],
            "strategy_name": name,
            "strategy_version_id": strategy_id,
            "strategy_path": str(strategy_path),
            "report_path": report_path.name,
            "wizard_run_id": f"wizard-{name}",
            "exchange_name": "Bybit",
            "source_strategy_sha256": strategy_hash,
            "source_report_sha256": report_hash,
        }],
        "v6_provenance": {
            "analysis_run_id": run_id,
            "generation_manifest_sha256": "g" * 64,
            "strategy_json_sha256": {f"{name}.json": strategy_id},
            "candidate_identity_to_strategy_names": {f"candidate-{name}": [name]},
            "candidate_diagnostics": {f"candidate-{name}": {
                "order_count": 1,
                "orders": [{
                    "order_id": 1,
                    "plateau_id": f"plateau-{name}",
                    "plateau_point_count": 1,
                    "base_point_trades": 1,
                    "plateau_total_trades": 1,
                }],
            }},
        },
    }
    if not commission:
        manifest.pop("commission_contract")
        manifest.pop("commission_contract_id")
    inbox = tmp_path / "inboxes" / name
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return inbox, report_root, strategy_root


def test_collection_reads_heterogeneous_member_context_without_merging_it(tmp_path: Path) -> None:
    first, report_root, strategy_root = _member(
        tmp_path / "first", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    second, _, _ = _member(
        tmp_path / "second", "beta", start="2026-02-01", end="2026-02-08", taker="0.0007", run_id="b" * 64
    )
    collection = build_single_mode_collection_inbox(
        tmp_path / "collections", "collection-1", [first, second],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )

    prepared = read_performance_v2_inbox(collection, report_root, strategy_root=strategy_root)

    assert prepared.run_mode == "SINGLE_MODE_COLLECTION"
    assert [entry.strategy_name for entry in prepared.entries] == ["alpha", "beta"]
    assert [(entry.test_start, entry.test_end) for entry in prepared.entries] == [
        ("2026-01-01", "2026-01-09"), ("2026-02-01", "2026-02-08")
    ]
    assert [entry.commission_contract["TakerFee"] for entry in prepared.entries] == ["0.0004", "0.0007"]
    assert [entry.analysis_run_id for entry in prepared.entries] == ["a" * 64, "b" * 64]


def test_collection_preserves_member_without_commission_pair(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09",
        taker="0.0004", run_id="a" * 64, commission=False,
    )
    collection = build_single_mode_collection_inbox(
        tmp_path / "collections", "collection-1", [member],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )
    manifest = json.loads((collection / "inbox_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert "commission_contract" not in entry
    assert "commission_contract_id" not in entry

    prepared = read_performance_v2_inbox(collection, report_root, strategy_root=strategy_root)
    assert prepared.entries[0].commission_contract == {}
    assert prepared.entries[0].commission_contract_id == ""


def test_collection_builder_binds_registered_names_to_exact_prepared_read(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )

    with pytest.raises(PerformanceV2InputError, match="registered strategy names"):
        build_single_mode_collection_inbox(
            tmp_path / "collections", "collection-1", [member],
            report_root=report_root,
            trusted_strategy_root=strategy_root,
            expected_member_names=[["swapped"]],
        )


def test_collection_manifest_expectations_bind_digest_and_id(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    collection = build_single_mode_collection_inbox(
        tmp_path / "collections", "collection-1", [member],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )
    manifest_path = collection / "inbox_manifest.json"
    digest = sha256(manifest_path.read_bytes()).hexdigest()

    prepared = read_performance_v2_inbox(
        collection, report_root, strategy_root=strategy_root,
        expected_manifest_sha256=digest, expected_collection_id="collection-1",
    )
    assert prepared.run_mode == "SINGLE_MODE_COLLECTION"
    with pytest.raises(PerformanceV2InputError, match="manifest digest"):
        read_performance_v2_inbox(
            collection, report_root, strategy_root=strategy_root,
            expected_manifest_sha256="0" * 64, expected_collection_id="collection-1",
        )
    with pytest.raises(PerformanceV2InputError, match="collection ID"):
        read_performance_v2_inbox(
            collection, report_root, strategy_root=strategy_root,
            expected_manifest_sha256=digest, expected_collection_id="wrong-collection",
        )


def test_collection_uses_each_entry_range_and_commission_for_import_values(tmp_path: Path) -> None:
    first, report_root, strategy_root = _member(
        tmp_path / "first", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    second, _, _ = _member(
        tmp_path / "second", "beta", start="2026-02-01", end="2026-02-08", taker="0.0007", run_id="b" * 64
    )
    collection = build_single_mode_collection_inbox(
        tmp_path / "collections", "collection-1", [first, second],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )
    prepared = read_performance_v2_inbox(collection, report_root, strategy_root=strategy_root)
    for index, entry in enumerate(prepared.entries, start=1):
        report = ParsedPerformanceV2Report(
            settings={"basic": {"symbol": entry.identity.symbol}},
            metrics={
                "Report range": f"{entry.test_start} - {entry.test_end}",
                "Initial balance": str(1000 * index),
            },
            actions=(), wallet_series=(), equity_series=(), inventory=object(),  # type: ignore[arg-type]
        )
        import_module._validate_report(entry, report, prepared)
        values = import_module._result_values(
            entry, report, entry.commission_contract, datetime(2026, 3, 1, tzinfo=timezone.utc)
        )
        assert values["commission_rate"] == Decimal(entry.commission_contract["TakerFee"])
        assert values["initial_balance"] == 1000 * index


def test_collection_digest_is_deterministic_and_does_not_scan_unlisted_inbox(tmp_path: Path) -> None:
    first, report_root, strategy_root = _member(
        tmp_path / "first", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    second, _, _ = _member(
        tmp_path / "second", "beta", start="2026-02-01", end="2026-02-08", taker="0.0007", run_id="b" * 64
    )
    third, _, _ = _member(
        tmp_path / "third", "gamma", start="2026-03-01", end="2026-03-08", taker="0.0008", run_id="d" * 64
    )
    left = build_single_mode_collection_inbox(
        tmp_path / "collections-left", "collection-1", [first, second],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )
    right = build_single_mode_collection_inbox(
        tmp_path / "collections-right", "collection-1", [first, second],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )
    left_doc = json.loads((left / "inbox_manifest.json").read_text(encoding="utf-8"))
    right_doc = json.loads((right / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert left_doc["collection_digest"] == right_doc["collection_digest"]
    assert "gamma" not in [entry["strategy_name"] for entry in left_doc["entries"]]
    assert third.exists()


@pytest.mark.parametrize("case", ["duplicate_name", "duplicate_report", "changed_artifact"])
def test_collection_rejects_unsafe_member_snapshot(tmp_path: Path, case: str) -> None:
    first, report_root, strategy_root = _member(
        tmp_path / "first", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    second, _, _ = _member(
        tmp_path / "second", "beta", start="2026-02-01", end="2026-02-08", taker="0.0007", run_id="b" * 64
    )
    if case == "duplicate_name":
        manifest_path = second / "inbox_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["strategy_name"] = "alpha"
        manifest["expected_strategy_names"] = ["alpha"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "duplicate_report":
        manifest_path = second / "inbox_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["report_path"] = "alpha.html"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        (report_root / "alpha.html").write_bytes(b"changed")

    with pytest.raises(PerformanceV2InputError):
        build_single_mode_collection_inbox(
            tmp_path / "collections", "collection-1", [first, second],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )


def test_collection_rejects_missing_member_report(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    (report_root / "alpha.html").unlink()

    with pytest.raises(PerformanceV2InputError, match="report path|report artifact"):
        build_single_mode_collection_inbox(
            tmp_path / "collections", "collection-1", [member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )


def test_collection_rejects_duplicate_member_path(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )

    with pytest.raises(PerformanceV2InputError, match="duplicate member"):
        build_single_mode_collection_inbox(
            tmp_path / "collections", "collection-1", [member, member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )


@pytest.mark.parametrize("collection_id", ["", ".", "..", "nested/id", "C:collection"])
def test_collection_rejects_unsafe_collection_id(tmp_path: Path, collection_id: str) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )

    with pytest.raises(PerformanceV2InputError, match="collection ID"):
        build_single_mode_collection_inbox(
            tmp_path / "collections", collection_id, [member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )


def test_collection_strategy_names_are_unique_case_insensitively(tmp_path: Path) -> None:
    first, report_root, strategy_root = _member(
        tmp_path / "first", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64,
        isolated_strategy=True,
    )
    second, _, _ = _member(
        tmp_path / "second", "Alpha", start="2026-02-01", end="2026-02-08", taker="0.0007", run_id="b" * 64,
        isolated_strategy=True,
        report_name="alpha-other",
    )

    with pytest.raises(PerformanceV2InputError, match="duplicate strategy"):
        build_single_mode_collection_inbox(
            tmp_path / "collections", "collection-1", [first, second],
            report_root=report_root, trusted_strategy_root=tmp_path,
        )


def test_collection_claim_collision_fails_closed_without_clobbering(tmp_path: Path) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    root = tmp_path / "collections"
    root.mkdir()
    claim = root / ".collection-1.claim"
    claim.mkdir()
    marker = claim / "owner-marker"
    marker.write_text("other-owner", encoding="ascii")

    with pytest.raises(PerformanceV2InputError, match="claim|busy|publication"):
        build_single_mode_collection_inbox(
            root, "collection-1", [member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )
    assert marker.read_text(encoding="ascii") == "other-owner"
    assert not (root / "collection-1").exists()


def test_collection_race_does_not_replace_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    root = tmp_path / "collections"
    target = root / "collection-1"
    original_exists = Path.exists
    injected = False

    def race_exists(path: Path) -> bool:
        nonlocal injected
        result = original_exists(path)
        if path == target and not injected:
            injected = True
            target.mkdir(parents=True)
            (target / "owner-marker").write_text("winner", encoding="ascii")
            return False
        return result

    monkeypatch.setattr(Path, "exists", race_exists)
    with pytest.raises(PerformanceV2InputError):
        build_single_mode_collection_inbox(
            root, "collection-1", [member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )
    assert (target / "owner-marker").read_text(encoding="ascii") == "winner"


def test_collection_empty_target_race_is_rejected_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    root = tmp_path / "collections"
    target = root / "collection-1"
    original_rename = collection_module._rename_noreplace
    injected = False

    def race_rename(source: Path, destination: Path) -> None:
        nonlocal injected
        if destination == target and not injected:
            injected = True
            target.mkdir(parents=True)
        original_rename(source, destination)

    monkeypatch.setattr(collection_module, "_rename_noreplace", race_rename)
    with pytest.raises(PerformanceV2InputError, match="already exists|publication"):
        build_single_mode_collection_inbox(
            root, "collection-1", [member],
            report_root=report_root, trusted_strategy_root=strategy_root,
        )
    assert target.is_dir()
    assert not (target / "inbox_manifest.json").exists()


def test_collection_publishes_complete_staged_directory_with_one_noreplace_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    member, report_root, strategy_root = _member(
        tmp_path / "member", "alpha", start="2026-01-01", end="2026-01-09", taker="0.0004", run_id="a" * 64
    )
    original = collection_module._rename_noreplace
    observed: dict[str, Path] = {}

    def observe(source: Path, target: Path) -> None:
        observed["source"] = source
        observed["target"] = target
        assert (source / "inbox_manifest.json").is_file()
        assert not target.exists()
        original(source, target)

    monkeypatch.setattr(collection_module, "_rename_noreplace", observe)
    published = build_single_mode_collection_inbox(
        tmp_path / "collections", "collection-1", [member],
        report_root=report_root, trusted_strategy_root=strategy_root,
    )

    assert observed["target"] == published
    assert (published / "inbox_manifest.json").is_file()
    assert not observed["source"].exists()


def test_noreplace_helper_preserves_preexisting_empty_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "inbox_manifest.json").write_text("complete", encoding="ascii")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(PerformanceV2InputError, match="already exists"):
        collection_module._rename_noreplace(source, target)
    assert target.is_dir()
    assert not (target / "inbox_manifest.json").exists()
    assert (source / "inbox_manifest.json").read_text(encoding="ascii") == "complete"


def test_noreplace_helper_maps_collision_to_input_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(PerformanceV2InputError, match="already exists"):
        collection_module._rename_noreplace(source, target)


def test_noreplace_helper_fails_closed_when_platform_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    monkeypatch.setattr(collection_module.sys, "platform", "unsupported-os")

    with pytest.raises(PerformanceV2InputError, match="unavailable"):
        collection_module._rename_noreplace(source, target)
    assert source.is_dir()
    assert not target.exists()
