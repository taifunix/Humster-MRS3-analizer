from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile
from typing import Mapping

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows


FIXED_EXCEL_TIME = datetime(2000, 1, 1, 0, 0, 0)


def serialize_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list, dict, set)):
        serializable = sorted(value) if isinstance(value, set) else value
        return json.dumps(serializable, ensure_ascii=False, sort_keys=True, default=str)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def serializable_frame(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in output.columns:
        output[column] = output[column].map(serialize_value)
    return output


def canonical_frame_json(frame: pd.DataFrame) -> str:
    serializable = serializable_frame(frame)
    return json.dumps(
        serializable.to_dict(orient="records"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _normalize_xlsx_archive(path: Path) -> None:
    normalized = path.with_suffix(".normalized.xlsx")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
        normalized, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for name in sorted(source.namelist()):
            original = source.getinfo(name)
            info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = original.external_attr
            info.create_system = original.create_system
            contents = source.read(name)
            if name == "docProps/core.xml":
                text = contents.decode("utf-8")
                text, replacements = re.subn(
                    r"(<dcterms:modified\b[^>]*>)[^<]*(</dcterms:modified>)",
                    r"\g<1>2000-01-01T00:00:00Z\g<2>",
                    text,
                    count=1,
                )
                if replacements != 1:
                    raise ValueError("workbook core properties do not contain modified timestamp")
                contents = text.encode("utf-8")
            target.writestr(info, contents)
    normalized.replace(path)


def write_audit_workbook(tables: Mapping[str, pd.DataFrame], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = "MRS3 v0.6"
    workbook.properties.created = FIXED_EXCEL_TIME
    workbook.properties.modified = FIXED_EXCEL_TIME
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)

    for sheet_name, raw_frame in tables.items():
        worksheet = workbook.create_sheet(sheet_name)
        frame = serializable_frame(raw_frame)
        for row in dataframe_to_rows(frame, index=False, header=True):
            worksheet.append(row)
        if len(frame.columns):
            for cell in worksheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for index, column in enumerate(frame.columns, start=1):
                values = [str(column)] + [
                    "" if value is None else str(value)
                    for value in frame[column].head(2000)
                ]
                width = min(70, max(10, max(len(value) for value in values) + 2))
                worksheet.column_dimensions[get_column_letter(index)].width = width
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}.", suffix=".xlsx", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        workbook.save(temporary)
        _normalize_xlsx_archive(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_audit_csvs(tables: Mapping[str, pd.DataFrame], directory: Path) -> None:
    target_directory = directory.resolve()
    target_directory.parent.mkdir(parents=True, exist_ok=True)
    backup = target_directory.with_name(f".{target_directory.name}.mrs3-backup")
    if backup.exists():
        raise RuntimeError(f"audit CSV backup requires recovery: {backup}")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target_directory.name}.mrs3-stage-",
            dir=target_directory.parent,
        )
    )
    moved_existing = False
    installed = False
    try:
        for sheet_name, frame in tables.items():
            serializable_frame(frame).to_csv(
                staging / f"{sheet_name}.csv",
                index=False,
                float_format="%.17g",
                lineterminator="\n",
            )
        if target_directory.exists():
            if not target_directory.is_dir():
                raise RuntimeError(
                    f"audit CSV target is not a directory: {target_directory}"
                )
            target_directory.replace(backup)
            moved_existing = True
        staging.replace(target_directory)
        installed = True
        if moved_existing:
            try:
                shutil.rmtree(backup)
            except Exception:
                failed = target_directory.with_name(
                    f".{target_directory.name}.mrs3-failed"
                )
                if failed.exists():
                    raise RuntimeError(
                        f"audit CSV failed directory requires recovery: {failed}"
                    )
                target_directory.replace(failed)
                backup.replace(target_directory)
                shutil.rmtree(failed)
                installed = False
                raise
    except Exception:
        if moved_existing and backup.exists() and not target_directory.exists():
            backup.replace(target_directory)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if installed and backup.exists():
            shutil.rmtree(backup)
