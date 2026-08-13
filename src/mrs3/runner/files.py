from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .config import (
    RunnerConfig,
    UnsafePathError,
    validate_report_directory,
    validate_strategy_directory,
)


class BatchPreparationError(RuntimeError):
    """Raised before a tester batch can be installed safely."""


@dataclass(frozen=True, slots=True)
class BatchFiles:
    source_directory: Path
    installed_directory: Path
    expected_names: tuple[str, ...]
    filenames: tuple[str, ...]
    file_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BatchInspection:
    source_directory: Path
    expected_names: tuple[str, ...]
    filenames: tuple[str, ...]
    file_hashes: tuple[tuple[str, str], ...]


def _inside_bot(path: Path, config: RunnerConfig, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(config.bot_root.resolve())
    except ValueError as error:
        raise UnsafePathError(f"{label} must be inside bot_root: {resolved}") from error
    if resolved == config.bot_root.resolve():
        raise UnsafePathError(f"{label} cannot be bot_root itself")
    return resolved


def validate_runner_paths(config: RunnerConfig) -> tuple[Path, Path, Path, Path]:
    executable = _inside_bot(config.executable_path, config, "executable_path")
    if executable.name.casefold() != "hb_c.exe":
        raise UnsafePathError("executable_path must name hb_c.exe")
    strategy_dir = validate_strategy_directory(config.strategy_dir, config.bot_root)
    report_dir = validate_report_directory(config.report_dir, config.bot_root)
    result = _inside_bot(config.wizard_result, config, "wizard_result")
    progress = _inside_bot(config.wizard_progress, config, "wizard_progress")
    expected_result = (config.bot_root / "tester" / "wizard_result.json").resolve()
    expected_progress = (config.bot_root / "tester" / "wizard_progress.json").resolve()
    if result != expected_result or progress != expected_progress:
        raise UnsafePathError("wizard logs must be the two exact files under bot_root/tester")
    return strategy_dir, report_dir, result, progress


def _validate_source(source: Path) -> tuple[tuple[Path, str, str], ...]:
    resolved = source.resolve()
    if not resolved.is_dir():
        raise BatchPreparationError(f"strategy source is not a directory: {resolved}")
    files = sorted(resolved.glob("*.json"), key=lambda path: path.name.casefold())
    if not files:
        raise BatchPreparationError(f"strategy source contains no JSON files: {resolved}")
    validated: list[tuple[Path, str, str]] = []
    seen: set[str] = set()
    for path in files:
        if not path.is_file():
            raise BatchPreparationError(f"strategy path is not a file: {path}")
        try:
            payload = path.read_bytes()
            document = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BatchPreparationError(f"invalid JSON strategy file: {path.name}") from error
        if not isinstance(document, dict):
            raise BatchPreparationError(f"strategy must be a JSON object: {path.name}")
        name = document.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BatchPreparationError(f"strategy has no non-empty name: {path.name}")
        if path.stem != name:
            raise BatchPreparationError(
                f"strategy filename must equal its name: {path.name} != {name}.json"
            )
        if name in seen:
            raise BatchPreparationError(f"duplicate strategy name: {name}")
        seen.add(name)
        validated.append((path, name, hashlib.sha256(payload).hexdigest()))
    return tuple(validated)


def _file_hashes(
    validated: tuple[tuple[Path, str, str], ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((path.name, digest) for path, _, digest in validated)


def _root_json_files(strategy_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in strategy_dir.iterdir()
                if path.suffix.casefold() == ".json"
                and path.is_file()
                and not path.is_symlink()
            ),
            key=lambda path: path.name.casefold(),
        )
    )


def _protected_root_entry_names(strategy_dir: Path) -> set[str]:
    return {
        path.name
        for path in strategy_dir.iterdir()
        if path.is_symlink()
        or not (path.suffix.casefold() == ".json" and path.is_file())
    }


def _source_is_inside_strategy_dir(source: Path, strategy_dir: Path) -> bool:
    try:
        source.resolve().relative_to(strategy_dir.resolve())
    except ValueError:
        return False
    return True


def inspect_strategy_batch(source_strategies: Path) -> BatchInspection:
    validated = _validate_source(source_strategies)
    return BatchInspection(
        source_directory=source_strategies.resolve(),
        expected_names=tuple(name for _, name, _ in validated),
        filenames=tuple(path.name for path, _, _ in validated),
        file_hashes=_file_hashes(validated),
    )


def _remove_raw_artifacts(report_dir: Path, result: Path, progress: Path) -> None:
    if report_dir.exists():
        if not report_dir.is_dir():
            raise BatchPreparationError(f"report_dir is not a directory: {report_dir}")
        shutil.rmtree(report_dir)
    result.unlink(missing_ok=True)
    progress.unlink(missing_ok=True)


def _restore_root_json(
    strategy_dir: Path, backup: Path, installed: tuple[Path, ...]
) -> None:
    for path in installed:
        path.unlink(missing_ok=True)
    for path in _root_json_files(backup):
        path.replace(strategy_dir / path.name)
    backup.rmdir()


def prepare_batch_files(
    config: RunnerConfig,
    source_strategies: Path,
    *,
    expected_file_hashes: tuple[tuple[str, str], ...] | None = None,
    selected_names: tuple[str, ...] | None = None,
    preserve_raw_artifacts: bool = False,
) -> BatchFiles:
    strategy_dir, report_dir, result, progress = validate_runner_paths(config)
    if _source_is_inside_strategy_dir(source_strategies, strategy_dir):
        raise BatchPreparationError(
            f"strategy source cannot be inside strategy_dir: {source_strategies.resolve()}"
        )
    validated = _validate_source(source_strategies)
    source_hashes = _file_hashes(validated)
    if expected_file_hashes is not None and source_hashes != expected_file_hashes:
        raise BatchPreparationError(
            "strategy batch content changed after the read-only preflight"
        )
    if selected_names is not None:
        selected = set(selected_names)
        if len(selected) != len(selected_names):
            raise BatchPreparationError("selected strategy names must be unique")
        validated = tuple(item for item in validated if item[1] in selected)
        if tuple(item[1] for item in validated) != selected_names:
            raise BatchPreparationError("selected strategy names are not in the source batch")
    strategy_dir.mkdir(parents=True, exist_ok=True)
    backup = strategy_dir.with_name(f".{strategy_dir.name}.mrs3-backup")
    if backup.exists():
        raise BatchPreparationError(
            f"strategy backup already exists and requires recovery: {backup}"
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{strategy_dir.name}.mrs3-stage-", dir=strategy_dir.parent
        )
    )
    installed: list[Path] = []
    backup_created = False
    try:
        for source, _, _ in validated:
            shutil.copy2(source, staging / source.name)
        staged = _validate_source(staging)
        staged_hashes = _file_hashes(staged)
        if staged_hashes != _file_hashes(validated):
            raise BatchPreparationError(
                "strategy batch content changed while staged copies were being created"
            )
        protected_collisions = sorted(
            {path.name for path, _, _ in staged}.intersection(
                _protected_root_entry_names(strategy_dir)
            )
        )
        if protected_collisions:
            raise BatchPreparationError(
                "staged strategy collides with protected root entry: "
                + ", ".join(protected_collisions)
            )
        if not preserve_raw_artifacts:
            _remove_raw_artifacts(report_dir, result, progress)
        backup.mkdir()
        backup_created = True
        for existing in _root_json_files(strategy_dir):
            existing.replace(backup / existing.name)
        for source, _, _ in staged:
            destination = strategy_dir / source.name
            shutil.copy2(source, destination)
            installed.append(destination)
        if _file_hashes(_validate_source(strategy_dir)) != staged_hashes:
            raise BatchPreparationError("installed strategy batch does not match staging")
        shutil.rmtree(backup)
        backup_created = False
    except BaseException as error:
        if backup_created:
            try:
                _restore_root_json(strategy_dir, backup, tuple(installed))
            except Exception as rollback_error:
                raise BatchPreparationError(
                    f"root JSON rollback failed; recovery required at {backup}"
                ) from rollback_error
        if not isinstance(error, Exception):
            raise
        if isinstance(error, BatchPreparationError):
            raise
        raise BatchPreparationError("could not install tester strategy batch") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    names = tuple(name for _, name, _ in staged)
    filenames = tuple(source.name for source, _, _ in staged)
    return BatchFiles(
        source_directory=source_strategies.resolve(),
        installed_directory=strategy_dir,
        expected_names=names,
        filenames=filenames,
        file_hashes=staged_hashes,
    )


def cleanup_completed_batch(config: RunnerConfig) -> None:
    _, report_dir, result, progress = validate_runner_paths(config)
    _remove_raw_artifacts(report_dir, result, progress)
