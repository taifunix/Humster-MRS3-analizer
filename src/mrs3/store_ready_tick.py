"""MRS3 tick-test batches from trade-store READY folders.

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 1b contract». A READY folder written by
`ready_from_store` is re-rendered with the template of the active PerformanceDB, protocol v2 is applied, and the
strategies are published with the generation manifest that `validate_strategy_manifest` and the SINGLE_MODE tick
test accept (per-order diagnostics are the zone facts of the READY structures, not shortlist plateaus).
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping

from .config import AlgorithmConfig
from .evaluation_protocol import EvaluationTarget, apply_protocol_v2
from .lots import LotMethod, allocate_lots
from .panel_strategy_batch import StrategyBatchValidationError, _canonical, _strategy_digest, validate_strategy_manifest
from .ready_store import structure_document
from .strategy_json import generate_strategy

ADAPTER_VERSION = "store-ready-tick-1"   # part of every batch identity: bump it when the rendering code changes
EVENT_MODE = "real_independent_events"   # the only mode the generation-manifest validator accepts
MANIFEST = "ready_manifest.json"
ZONE_KEYS = ("zone_points", "trades", "zone_trades")
REBUILD = "READY собран до фактов зоны — пересоберите READY"
DAMAGED = "ready_manifest.json повреждён"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class StoreReadyError(ValueError):
    """A READY folder cannot be listed, resolved or prepared for the tick test."""


def _read(ready_dir: Path) -> tuple[bytes, dict]:
    try:
        data = (ready_dir / MANIFEST).read_bytes()
        document = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoreReadyError(f"{MANIFEST} не читается") from error
    if not isinstance(document, dict) or not isinstance(document.get("pairs"), list):
        raise StoreReadyError(f"{MANIFEST} повреждён")
    return data, document


def _items(document: Mapping) -> list[tuple[dict, dict]]:
    """Admitted structures of the READY pairs; a malformed entry refuses the folder instead of failing later."""
    items = []
    for pair in document["pairs"]:
        if not isinstance(pair, dict) or pair.get("status") != "READY":
            continue
        structures = pair.get("structures") or []
        if not isinstance(pair.get("pair"), str) or not isinstance(structures, list):
            raise StoreReadyError(DAMAGED)
        for item in structures:
            orders = item.get("orders") if isinstance(item, dict) else None
            if (not isinstance(orders, list) or not orders or not isinstance(item.get("timeframe"), str)
                    or not isinstance(item.get("strategy"), str) or "close_ma" not in item
                    or any(not isinstance(order, dict) or any(key not in order for key in ("shift_bp", "open_ma", "point_id"))
                           for order in orders)):
                raise StoreReadyError(DAMAGED)
            items.append((pair, item))
    return items


def _zone_problem(items: list[tuple[dict, dict]]) -> str | None:
    for _pair, item in items:
        for order in item.get("orders") or ():
            if any(type(order.get(key)) is not int or order[key] < 0 for key in ZONE_KEYS):
                return REBUILD
    return None


def list_ready_folders(store_root: Path) -> list[dict[str, object]]:
    """READY folders of a trade store (newest first by name) with their counts and whether they can be tested."""
    root = Path(store_root) / "ready"
    if not root.is_dir():
        return []
    entries = []
    for child in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if not _NAME.fullmatch(child.name) or child.is_symlink() or not child.is_dir() or not (child / MANIFEST).is_file():
            continue
        entry: dict[str, object] = {"name": child.name}
        try:
            _data, document = _read(child)
        except StoreReadyError as error:
            entries.append({**entry, "testable": False, "reason": str(error)})
            continue
        try:
            items = _items(document)
        except StoreReadyError as error:
            entries.append({**entry, "testable": False, "reason": str(error)})
            continue
        problem = _zone_problem(items) or (None if items else "в папке READY нет стратегий для теста")
        entries.append({
            **entry,
            "rule_version": document.get("rule_version"), "exchange": document.get("exchange"),
            "latest_source": document.get("latest_source"), "pairs": len(document["pairs"]),
            "ready_pairs": sum(1 for pair in document["pairs"] if isinstance(pair, dict) and pair.get("status") == "READY"),
            "strategies": len(items), "testable": problem is None, "reason": problem,
        })
    return entries


def resolve_ready_folder(store_root: Path, name: object) -> Path:
    """The READY folder `<store>/ready/<name>`; the name is a plain child name, never a client path."""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {".", ".."}:
        raise StoreReadyError("недопустимое имя папки READY")
    root = Path(store_root) / "ready"
    candidate = root / name
    if candidate.is_symlink() or not candidate.is_dir() or not (candidate / MANIFEST).is_file():
        raise StoreReadyError("папка READY не найдена")
    resolved = candidate.resolve()
    if resolved.parent != root.resolve():
        raise StoreReadyError("недопустимое имя папки READY")
    return resolved


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def prepare_tick_batch(ready_dir: Path, out_root: Path, template_path: Path, target: EvaluationTarget) -> dict[str, object]:
    """Publish `<out_root>/<analysis_run_id>/` (strategies + generation manifest) for one READY folder and target;
    the same folder, template and target reuse the existing batch."""
    ready_dir = Path(ready_dir).resolve()
    data, document = _read(ready_dir)
    items = _items(document)
    problem = _zone_problem(items)
    if problem:
        raise StoreReadyError(problem)
    if not items:
        raise StoreReadyError("в папке READY нет стратегий для теста")
    try:
        template_bytes = Path(template_path).read_bytes()
        template = json.loads(template_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoreReadyError("шаблон стратегии MRS3 не читается") from error
    exchange = str(document.get("exchange") or "bybit")
    identity = {
        "adapter": ADAPTER_VERSION, "ready_manifest_sha256": _digest(data), "template_sha256": _digest(template_bytes),
        "base": str(target.base), "exchange": target.exchange,
        "strategy_exchange": [list(pair) for pair in target.strategy_exchange],
    }
    run_id = _digest(_canonical(identity))
    out_root = Path(out_root)
    final = out_root / run_id
    summary = {"analysis_run_id": run_id, "manifest_path": str(final / "strategy_manifest.json"),
               "strategy_count": len(items), "pairs": len(document["pairs"]),
               "ready_pairs": len({pair["pair"] for pair, _item in items})}
    if final.exists():
        try:
            validated = validate_strategy_manifest(final / "strategy_manifest.json")
        except StrategyBatchValidationError as error:
            raise StoreReadyError(f"подготовленный пакет {run_id} damaged: удалите его и подготовьте заново") from error
        if validated.analysis_run_id != run_id:
            raise StoreReadyError(f"подготовленный пакет {run_id} damaged: удалите его и подготовьте заново")
        return {**summary, "reused": True}

    config = AlgorithmConfig()
    strategies: dict[str, dict] = {}
    names: dict[str, list[str]] = {}
    diagnostics: dict[str, dict] = {}
    for pair, item in items:
        symbol = str(pair["pair"])
        structure = structure_document(symbol, item)
        strategy = generate_strategy(template, structure, allocate_lots(structure["orders"], LotMethod.EQUAL, config),
                                     LotMethod.EQUAL, config)
        apply_protocol_v2(strategy, target)
        name = str(strategy["name"])
        if name != item.get("strategy"):
            raise StoreReadyError(f"имя стратегии {name} не совпадает с папкой READY")
        if name in strategies:
            raise StoreReadyError(f"стратегия {name} повторяется")
        strategies[name] = strategy
        candidate = f"{exchange}/{symbol}/{item['timeframe']}/{structure['structure_id']}"
        names[candidate] = [name]
        diagnostics[candidate] = {"order_count": len(item["orders"]), "orders": [
            {"order_id": index, "plateau_id": f"zone:{symbol}:{order['point_id']}",
             "plateau_point_count": order["zone_points"] + 1, "base_point_trades": order["trades"],
             "plateau_total_trades": order["zone_trades"]}
            for index, order in enumerate(item["orders"], 1)]}
    unsigned = {
        "format_version": 1, "analysis_run_id": run_id, "event_mode": EVENT_MODE, "source": "trade-store-ready",
        "adapter_version": ADAPTER_VERSION, "ready_folder": ready_dir.name,
        "ready_manifest_sha256": identity["ready_manifest_sha256"], "rule_version": document.get("rule_version"),
        "exchange": exchange, "template_sha256": identity["template_sha256"],
        "candidate_identities": sorted(names),
        "candidate_identity_to_strategy_names": dict(sorted(names.items())),
        "candidate_diagnostics": dict(sorted(diagnostics.items())),
        "strategy_json_sha256": {f"{name}.json": _strategy_digest(strategy) for name, strategy in sorted(strategies.items())},
        "strategy_count": len(strategies),
    }
    manifest = {**unsigned, "generation_manifest_sha256": _digest(_canonical(unsigned))}
    out_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=out_root))
    try:
        (staging / "strategies").mkdir()
        for name, strategy in strategies.items():
            (staging / "strategies" / f"{name}.json").write_text(
                json.dumps(strategy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (staging / "strategy_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        validate_strategy_manifest(staging / "strategy_manifest.json")       # self-check before publishing
        staging.replace(final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {**summary, "reused": False}
