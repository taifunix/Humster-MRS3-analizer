from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path

from openpyxl import load_workbook

from mrs3.config import AlgorithmConfig
from mrs3.models import Side
from mrs3.pipeline import SelectionInputs, run_selection
from tests.factories import write_selection_inputs


def _digest_files(directory: Path, pattern: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_pipeline_builds_two_plateaus_and_validated_json(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    config = AlgorithmConfig.from_json(paths["config"])
    inputs = SelectionInputs(
        csv_path=paths["csv"],
        dates_path=paths["dates"],
        template_path=paths["template"],
        side=Side.LONG,
        output_dir=tmp_path / "output",
    )

    result = run_selection(inputs, config)

    assert len(result.plateaus) == 2
    assert set(result.structures["order_count"]) == {2}
    assert len(result.generated_strategies) == 5
    assert result.manifest["ready_json_count"] == 5
    assert result.manifest["base_json_count"] == 1
    assert result.manifest["mrs3_json_count"] == 4
    assert result.manifest["ready_structure_count_by_orders"] == {
        "2ORD": 2,
        "3ORD": 0,
        "4ORD": 0,
    }
    assert result.manifest["ready_json_count"] == (
        result.manifest["base_json_count"]
        + result.manifest["mrs3_json_count"]
    )
    assert result.manifest["mrs3_json_count"] == 2 * result.manifest[
        "ready_structure_count"
    ]
    assert (inputs.output_dir / "audit.xlsx").exists()
    workbook = load_workbook(inputs.output_dir / "audit.xlsx", read_only=True)
    assert workbook.sheetnames == [
        "00_Input_Audit",
        "01_Pair_History",
        "02_Filtering",
        "03_Refine_Required",
        "04_Plateau_Points",
        "05_Plateau_Library",
        "06_CloseMA_Profile",
        "07_Isolated_Peaks",
        "08_1ORD",
        "09_CloseMA_Families",
        "10_MRS3_Structures",
        "11_Lot_Variants",
        "12_Ready_JSON",
        "13_Deep_Gap_Research",
        "14_Recalibration",
        "15_Config",
    ]


def test_same_inputs_produce_identical_digest_rows_and_json(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    config = AlgorithmConfig.from_json(paths["config"])
    first_inputs = SelectionInputs(paths["csv"], paths["dates"], paths["template"], Side.LONG, tmp_path / "a")
    second_inputs = SelectionInputs(paths["csv"], paths["dates"], paths["template"], Side.LONG, tmp_path / "b")

    first = run_selection(first_inputs, config)
    second = run_selection(second_inputs, config)

    assert first.manifest["deterministic_digest"] == second.manifest["deterministic_digest"]
    assert _digest_files(tmp_path / "a", "strategies/*.json") == _digest_files(
        tmp_path / "b", "strategies/*.json"
    )
    assert (tmp_path / "a" / "audit.xlsx").read_bytes() == (
        tmp_path / "b" / "audit.xlsx"
    ).read_bytes()


def test_coarse_domain_emits_refine_requests_without_fabricated_points(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    config = replace(
        AlgorithmConfig.from_json(paths["config"]),
        shift_domain_min_bp=30,
        shift_domain_max_bp=270,
    )
    inputs = SelectionInputs(paths["csv"], paths["dates"], paths["template"], Side.LONG, tmp_path / "output")

    result = run_selection(inputs, config)

    assert not result.refine_requests.empty
    exported_point_ids = {
        order["point_id"]
        for structure in result.structures.get("orders", [])
        for order in structure
    }
    assert exported_point_ids.issubset(set(result.points["point_id"]))


def test_pipeline_finishes_with_audit_when_no_plateau_is_ready(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    config = replace(
        AlgorithmConfig.from_json(paths["config"]),
        economic_min_win_rate_pct=Decimal("100"),
    )
    inputs = SelectionInputs(
        paths["csv"],
        paths["dates"],
        paths["template"],
        Side.LONG,
        tmp_path / "output",
    )

    result = run_selection(inputs, config)

    assert result.plateaus.empty
    assert result.structures.empty
    assert result.generated_strategies == []
    assert result.manifest["ready_plateau_count"] == 0
    assert result.manifest["ready_json_count"] == 0
    assert (inputs.output_dir / "audit.xlsx").exists()


def test_rerun_replaces_strategy_directory_without_stale_json(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    config = AlgorithmConfig.from_json(paths["config"])
    inputs = SelectionInputs(
        paths["csv"],
        paths["dates"],
        paths["template"],
        Side.LONG,
        tmp_path / "output",
    )
    first = run_selection(inputs, config)
    stale = inputs.output_dir / "strategies" / "STALE.json"
    stale.write_text('{"name":"STALE"}', encoding="utf-8")

    second = run_selection(inputs, config)

    expected = sorted(second.lot_variants["json_filename"])
    actual = sorted(path.name for path in (inputs.output_dir / "strategies").glob("*.json"))
    assert len(first.generated_strategies) == len(second.generated_strategies)
    assert actual == expected
    assert not stale.exists()
