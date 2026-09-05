"""Strict TOML configuration and atomic hot reload for the collector."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path
import threading
import tempfile
import tomllib
from typing import Any


class ConfigError(ValueError):
    """The configuration file is unreadable or violates the collector contract."""


_SECTIONS = frozenset({"storage", "symbols", "logging"})
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Validated file state; a non-None pending root requires process restart."""

    config_path: Path
    storage_root: Path
    symbols: tuple[str, ...]
    logging_level: str
    config_revision: str
    pending_storage_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ConfigReloadResult:
    accepted: bool
    config: CollectorConfig
    candidate: CollectorConfig | None = None
    restart_required: bool = False
    added_symbols: tuple[str, ...] = ()
    removed_symbols: tuple[str, ...] = ()
    unchanged_symbols: tuple[str, ...] = ()
    logging_level_changed: bool = False
    error: str | None = None

def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _only_keys(table: dict[str, Any], expected: frozenset[str], name: str) -> None:
    unknown = sorted(set(table).difference(expected))
    missing = sorted(expected.difference(table))
    if unknown:
        raise ConfigError(f"{name} contains unknown keys: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"{name} is missing keys: {', '.join(missing)}")


def _probe_storage_root(root: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=root, prefix=".bybit-write-probe-", delete=True):
        pass


def _resolve_storage_root(config_path: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("storage.root must be a non-empty string")
    if value != value.strip():
        raise ConfigError("storage.root must not have leading or trailing whitespace")
    try:
        root = Path(value)
        if not root.is_absolute() and (root.drive or root.root):
            raise ConfigError("storage.root must not be drive-relative or rooted")
        if not root.is_absolute():
            root = config_path.parent / root
        root = root.resolve()
        if not root.exists():
            raise ConfigError(f"storage.root is unavailable: {root}")
        if not root.is_dir():
            raise ConfigError(f"storage.root is not a directory: {root}")
        try:
            _probe_storage_root(root)
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigError(f"storage.root is not accessible: {root}: {exc}") from exc
        return root
    except ConfigError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"storage.root is not usable: {exc}") from exc


def _symbols(value: Any) -> tuple[str, ...]:
    """Validate non-empty uppercase ASCII alphanumeric symbol identifiers."""

    if not isinstance(value, list) or not value:
        raise ConfigError("symbols.items must be a non-empty array")
    result: list[str] = []
    for symbol in value:
        if not isinstance(symbol, str) or not symbol or not symbol.strip() or symbol != symbol.strip():
            raise ConfigError("symbols.items must contain non-empty symbol strings")
        if not all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for char in symbol):
            raise ConfigError(f"symbol is not uppercase: {symbol}")
        result.append(symbol)
    if len(result) != len(set(result)):
        raise ConfigError("symbols.items must not contain duplicates")
    return tuple(result)


def _logging_level(value: Any) -> str:
    if not isinstance(value, str) or value not in _LEVELS:
        raise ConfigError("logging.level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
    return value


def load_config(path: str | os.PathLike[str]) -> CollectorConfig:
    """Read and validate one exact UTF-8 collector TOML document."""

    try:
        config_path = Path(path).resolve()
        payload = config_path.read_bytes()
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("configuration must be a TOML table")
    unknown = sorted(set(document).difference(_SECTIONS))
    missing = sorted(_SECTIONS.difference(document))
    if unknown:
        raise ConfigError(f"configuration contains unknown tables: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"configuration is missing tables: {', '.join(missing)}")

    storage = _mapping(document["storage"], "storage")
    symbols = _mapping(document["symbols"], "symbols")
    logging = _mapping(document["logging"], "logging")
    _only_keys(storage, frozenset({"root"}), "storage")
    _only_keys(symbols, frozenset({"items"}), "symbols")
    _only_keys(logging, frozenset({"level"}), "logging")
    return CollectorConfig(
        config_path=config_path,
        storage_root=_resolve_storage_root(config_path, storage["root"]),
        symbols=_symbols(symbols["items"]),
        logging_level=_logging_level(logging["level"]),
        config_revision=sha256(payload).hexdigest(),
    )


class ConfigManager:
    """Own the last accepted config and apply valid candidates as one update."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._lock = threading.RLock()
        try:
            self.path = Path(path).resolve()
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigError(f"cannot read config: {exc}") from exc
        self._active = load_config(self.path)

    @property
    def active(self) -> CollectorConfig:
        with self._lock:
            return self._active

    def restore(self, config: CollectorConfig) -> None:
        """Restore the last applied state when a caller cannot apply a candidate."""
        with self._lock:
            self._active = config

    def reload(self) -> ConfigReloadResult:
        with self._lock:
            current = self._active
            try:
                candidate = load_config(self.path)
            except ConfigError as exc:
                return ConfigReloadResult(
                    accepted=False,
                    config=current,
                    restart_required=current.pending_storage_root is not None,
                    error=str(exc),
                )

            added = tuple(sorted(set(candidate.symbols).difference(current.symbols)))
            removed = tuple(sorted(set(current.symbols).difference(candidate.symbols)))
            unchanged = tuple(sorted(set(candidate.symbols).intersection(current.symbols)))
            logging_changed = candidate.logging_level != current.logging_level
            restart_required = candidate.storage_root != current.storage_root
            active = candidate if not restart_required else replace(
                candidate,
                storage_root=current.storage_root,
                pending_storage_root=candidate.storage_root,
            )
            self._active = active
            return ConfigReloadResult(
                accepted=True,
                config=active,
                candidate=candidate,
                restart_required=active.pending_storage_root is not None,
                added_symbols=added,
                removed_symbols=removed,
                unchanged_symbols=unchanged,
                logging_level_changed=logging_changed,
            )
