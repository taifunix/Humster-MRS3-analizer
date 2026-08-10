from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import pytest

from mrs3.audit import write_audit_csvs, write_audit_workbook


def test_workbook_core_timestamps_are_fixed(tmp_path: Path) -> None:
    target = tmp_path / "audit.xlsx"

    write_audit_workbook({"Sheet": pd.DataFrame({"value": [1]})}, target)

    with ZipFile(target) as archive:
        core = archive.read("docProps/core.xml").decode("utf-8")
    assert "2000-01-01T00:00:00Z" in core
    assert "dcterms:modified" in core
    assert core.count("2000-01-01T00:00:00Z") == 2


def test_csv_export_keeps_previous_file_if_serialization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "audit_csv"
    directory.mkdir()
    target = directory / "Sheet.csv"
    target.write_text("complete-old-file\n", encoding="utf-8")

    def fail_after_partial_write(
        self: pd.DataFrame, path: object, *args: object, **kwargs: object
    ) -> None:
        Path(path).write_text("partial-new-file\n", encoding="utf-8")
        raise OSError("simulated interrupted export")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail_after_partial_write)

    with pytest.raises(OSError, match="interrupted"):
        write_audit_csvs({"Sheet": pd.DataFrame({"value": [1, 2, 3]})}, directory)

    assert target.read_text(encoding="utf-8") == "complete-old-file\n"
    assert sorted(path.name for path in directory.iterdir()) == ["Sheet.csv"]


def test_csv_export_replaces_directory_without_stale_or_temporary_files(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "audit_csv"
    directory.mkdir()
    (directory / "Stale.csv").write_text("stale\n", encoding="utf-8")
    (directory / ".orphan.csv").write_text("partial\n", encoding="utf-8")

    write_audit_csvs(
        {"Current": pd.DataFrame({"value": [1]})},
        directory,
    )

    assert sorted(path.name for path in directory.iterdir()) == ["Current.csv"]


def test_csv_export_preserves_round_trip_float_precision(tmp_path: Path) -> None:
    directory = tmp_path / "audit_csv"
    values = [12.7 / 3.79, 11.43 / 3.79]

    write_audit_csvs({"Metrics": pd.DataFrame({"efficiency": values})}, directory)

    serialized = (directory / "Metrics.csv").read_text(encoding="utf-8").splitlines()
    assert [float(value) for value in serialized[1:]] == values
