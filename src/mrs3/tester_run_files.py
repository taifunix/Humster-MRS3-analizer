"""Render small, single-strategy tester run snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import shutil
from typing import Mapping

from .config import AlgorithmConfig
from .lots import LotMethod, allocate_lots
from .strategy_json import generate_strategy


def _timestamp(value: str) -> str:
    try:
        return f"{date.fromisoformat(value):%Y-%m-%d}T00:00:00"
    except ValueError as error:
        raise ValueError("tester run dates must be ISO dates") from error


def _bot_template_json(value: str) -> object:
    """Accept the BOM and trailing commas emitted by the bot's template export."""
    cleaned: list[str] = []
    quoted = escaped = False
    for index, char in enumerate(value):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            next_char = next((item for item in value[index + 1:] if not item.isspace()), "")
            if next_char in "}]":
                continue
        cleaned.append(char)
    return json.loads("".join(cleaned))


def render_run_snapshot(
    template_path: Path | str,
    structure: Mapping[str, object],
    start_date: str,
    end_date: str,
    max_parallel_runs: int,
    config: AlgorithmConfig,
) -> tuple[str, dict[str, object]]:
    """Render one EQUAL-lot snapshot without touching the tester filesystem."""
    if type(max_parallel_runs) is not int or max_parallel_runs <= 0:
        raise ValueError("max_parallel_runs must be a positive integer")
    if _timestamp(start_date) > _timestamp(end_date):
        raise ValueError("tester run start date must not exceed end date")
    try:
        snapshot = _bot_template_json(Path(template_path).read_text(encoding="utf-8-sig"))
        settings = snapshot["settings"]
        tester_config = snapshot["tester_config"]
        if not isinstance(settings, list) or len(settings) != 1 or not isinstance(settings[0], dict):
            raise ValueError
        if not isinstance(tester_config, dict):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError("tester run template is invalid") from error
    orders = tuple(structure.get("orders", ()))
    if not orders:
        raise ValueError("READY structure has no orders")
    strategy = generate_strategy(
        settings[0], structure, allocate_lots(orders, LotMethod.EQUAL, config), LotMethod.EQUAL, config
    )
    name = str(strategy["name"])
    rendered = deepcopy(snapshot)
    rendered["settings"] = [strategy]
    rendered["tester_config"]["StartDate"] = _timestamp(start_date)
    rendered["tester_config"]["EndDate"] = _timestamp(end_date)
    rendered["tester_config"]["max_parallel_runs"] = max_parallel_runs
    return name, rendered


def _inside(path: Path, root: Path, label: str) -> Path:
    root, path = root.absolute(), path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} must be inside bot_root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not use a symbolic link")
    return path


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    rendered = json.dumps(document, ensure_ascii=False, indent=2).replace('"MakerFee": 1e-05', '"MakerFee": 0.00001')
    temporary.write_text(rendered + "\n", encoding="utf-8")
    os.replace(temporary, path)


def publish_run_snapshots(
    template_path: Path | str,
    bot_root: Path | str,
    tester_config_path: Path | str,
    structures: list[Mapping[str, object]],
    start_date: str,
    end_date: str,
    max_parallel_runs: int,
    config: AlgorithmConfig,
) -> dict[str, object]:
    """Replace the exact tester runs directory with one to five snapshots."""
    if not 1 <= len(structures) <= 5:
        raise ValueError("tester run generation requires from one to five candidates")
    root = Path(bot_root).resolve()
    runs = _inside(root / "tester" / "runs", root, "tester runs directory")
    tester_config = _inside(Path(tester_config_path), root, "tester config")
    if not root.is_dir() or not tester_config.is_file():
        raise ValueError("tester runs target is unavailable")
    try:
        config_document = json.loads(tester_config.read_text(encoding="utf-8"))
        if not isinstance(config_document, dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("tester config is invalid") from error
    runs.mkdir(parents=True, exist_ok=True)
    existing = list(runs.iterdir())
    if any(path.is_symlink() for path in existing):
        raise ValueError("tester runs directory contains a symbolic link")
    rendered = [render_run_snapshot(template_path, row, start_date, end_date, max_parallel_runs, config) for row in structures]
    if len({name for name, _ in rendered}) != len(rendered):
        raise ValueError("READY candidates must have unique strategy names")
    for path in existing:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    config_document["use_runs"] = True
    _write_json(tester_config, config_document)
    names: list[str] = []
    for index, (name, snapshot) in enumerate(rendered, start=1):
        _write_json(runs / f"{index:03d}_{name}.json", snapshot)
        names.append(name)
    return {"run_count": len(names), "run_names": names}
