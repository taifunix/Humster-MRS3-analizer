from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Callable, TypeVar

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


@dataclass(frozen=True, slots=True)
class TesterSettingsSnapshot:
    tester_config: bytes | None
    strategies: tuple[tuple[str, bytes], ...]


FileFingerprint = tuple[int, int, str]
_StableValue = TypeVar("_StableValue")


def file_fingerprint(path: Path) -> FileFingerprint | None:
    """Read one exact regular file identity for stale shared-log checks."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, hashlib.sha256(payload).hexdigest()


def file_changed_since(path: Path, baseline: FileFingerprint | None) -> bool:
    current = file_fingerprint(path)
    return current is not None and current != baseline


def read_stable_file(
    path: Path,
    parser: Callable[[Path], _StableValue],
    *,
    baseline: FileFingerprint | None = None,
) -> _StableValue | None:
    """Parse one immutable observation of a shared file.

    The parser receives a private copy of the bytes that were checked before
    and after reading the source.  This avoids parsing a later, separately
    read version after a stale-result or freshness check.
    """
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        verify_payload = path.read_bytes()
        verified = path.stat()
    except (OSError, ValueError):
        return None
    fingerprint = (after.st_mtime_ns, after.st_size, hashlib.sha256(payload).hexdigest())
    verified_fingerprint = (
        verified.st_mtime_ns,
        verified.st_size,
        hashlib.sha256(verify_payload).hexdigest(),
    )
    if (
        before.st_mtime_ns != after.st_mtime_ns
        or before.st_size != after.st_size
        or fingerprint != verified_fingerprint
        or (baseline is not None and fingerprint == baseline)
    ):
        return None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
        return parser(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture_tester_settings(config: RunnerConfig) -> TesterSettingsSnapshot:
    """Capture the two shared tester inputs while target ownership is held."""
    strategy_dir = _inside_bot(config.strategy_dir, config, "strategy_dir")
    strategy_dir.mkdir(parents=True, exist_ok=True)
    protected = [
        path for path in strategy_dir.iterdir()
        if path.suffix.casefold() == ".json" and (path.is_symlink() or not path.is_file())
    ]
    if protected:
        raise BatchPreparationError("tester settings contain a protected JSON entry")
    strategies = tuple((path.name, path.read_bytes()) for path in _root_json_files(strategy_dir))
    tester_config = _inside_bot(config.tester_config, config, "tester_config")
    if tester_config.exists() and (tester_config.is_symlink() or not tester_config.is_file()):
        raise BatchPreparationError("tester config is not a regular file")
    return TesterSettingsSnapshot(
        tester_config.read_bytes() if tester_config.exists() else None,
        strategies,
    )


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_tester_settings(config: RunnerConfig, snapshot: TesterSettingsSnapshot) -> None:
    """Restore captured config and root strategy JSON under the same owner."""
    strategy_dir = _inside_bot(config.strategy_dir, config, "strategy_dir")
    strategy_dir.mkdir(parents=True, exist_ok=True)
    if any(path.suffix.casefold() == ".json" and (path.is_symlink() or not path.is_file()) for path in strategy_dir.iterdir()):
        raise BatchPreparationError("tester settings restore found a protected JSON entry")
    staged: list[tuple[Path, Path]] = []
    try:
        for name, payload in snapshot.strategies:
            with tempfile.NamedTemporaryFile(dir=strategy_dir, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
            staged.append((temporary, strategy_dir / name))
        for path in _root_json_files(strategy_dir):
            path.unlink()
        for temporary, destination in staged:
            temporary.replace(destination)
        tester_config = _inside_bot(config.tester_config, config, "tester_config")
        if snapshot.tester_config is None:
            tester_config.unlink(missing_ok=True)
        else:
            _replace_bytes(tester_config, snapshot.tester_config)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


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
    preserve_raw_artifacts: bool = True,
) -> BatchFiles:
    strategy_dir, _, _, _ = validate_runner_paths(config)
    if preserve_raw_artifacts is not True:
        raise BatchPreparationError("shared tester artifacts cannot be cleared during batch preparation")
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
