from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

import mrs3.bybit_collector.config as config_module
from mrs3.bybit_collector.config import ConfigManager, ConfigError, load_config


def _write_config(path: Path, *, root: str = "data", symbols: tuple[str, ...] = ("AAOIUSDT", "AEHRUSDT"), level: str = "INFO") -> bytes:
    text = (
        "[storage]\n"
        f'root = "{root}"\n'
        "[symbols]\n"
        f"items = [{', '.join(repr(symbol) for symbol in symbols)}]\n"
        "[logging]\n"
        f'level = "{level}"\n'
    )
    payload = text.encode("utf-8")
    path.write_bytes(payload)
    return payload


def test_loads_exact_toml_and_hashes_exact_bytes(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    payload = _write_config(config_path, root="data/bybit")
    (tmp_path / "data" / "bybit").mkdir(parents=True)

    config = load_config(config_path)

    assert config.config_path == config_path.resolve()
    assert config.storage_root == (tmp_path / "data" / "bybit").resolve()
    assert config.symbols == ("AAOIUSDT", "AEHRUSDT")
    assert config.logging_level == "INFO"
    assert config.config_revision == sha256(payload).hexdigest()


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_initial_load_rejects_unavailable_or_non_directory_root(tmp_path: Path, root_kind: str) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, root="missing" if root_kind == "missing" else "not-a-directory")
    if root_kind == "file":
        (tmp_path / "not-a-directory").write_text("file", encoding="utf-8")

    with pytest.raises(ConfigError, match="storage.root"):
        load_config(config_path)


def test_initial_load_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    config_path.write_bytes(b"[storage]\nroot = '\xff'\n")

    with pytest.raises(ConfigError, match="valid UTF-8"):
        load_config(config_path)


@pytest.mark.parametrize(
    "document",
    [
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n[extra]\nvalue = 1\n',
        '[storage]\nroot = "data"\nunknown = true\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\nunknown = true\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\nunknown = true\n',
        '[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n',
        '[storage]\nroot = 1\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = "BTCUSDT"\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = [1]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = 1\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT", "BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["btcusdt"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = [""]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "verbose"\n',
        '[storage]\nroot = "data"\nroot = "other"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        '[storage]\nroot = "data"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n[storage]\n',
    ],
)
def test_rejects_non_contract_toml(tmp_path: Path, document: str) -> None:
    config_path = tmp_path / "collector.toml"
    config_path.write_text(document, encoding="utf-8")
    (tmp_path / "data").mkdir()

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_reload_rejects_candidate_without_changing_active_config(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path)
    (tmp_path / "data").mkdir()
    manager = ConfigManager(config_path)
    prior = manager.active

    config_path.write_text("[storage]\nroot = \"data\"\n[symbols]\nitems = [\"bad\"]\n", encoding="utf-8")
    result = manager.reload()

    assert result.accepted is False
    assert result.error
    assert manager.active is prior


@pytest.mark.parametrize(
    "candidate",
    [
        b"[storage]\nroot = 'data'\n[symbols]\nitems = ['BTCUSDT']\n[logging]\nlevel = 'INFO'\n\xff",
        b"[storage]\nroot = 'data'\n[symbols]\nitems = ['BTCUSDT']\n[logging]\nlevel = \"INFO\"\nnot toml",
    ],
)
def test_reload_rejects_invalid_encoding_or_toml_without_changing_active_config(
    tmp_path: Path, candidate: bytes
) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path)
    (tmp_path / "data").mkdir()
    manager = ConfigManager(config_path)
    prior = manager.active
    config_path.write_bytes(candidate)

    result = manager.reload()

    assert result.accepted is False
    assert result.error
    assert manager.active is prior


def test_initial_load_rejects_storage_root_when_write_probe_fails(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path)
    (tmp_path / "data").mkdir()

    def fail_write_probe(_root: Path) -> None:
        raise OSError("denied")

    monkeypatch.setattr(config_module, "_probe_storage_root", fail_write_probe, raising=False)

    with pytest.raises(ConfigError, match="storage.root is not accessible"):
        load_config(config_path)


@pytest.mark.parametrize("root", [" data", "data ", "C:relative"])
def test_initial_load_rejects_ambiguous_storage_root(tmp_path: Path, root: str) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, root=root)
    (tmp_path / "data").mkdir()

    with pytest.raises(ConfigError, match="storage.root"):
        load_config(config_path)


@pytest.mark.parametrize(
    "candidate",
    [
        '[storage]\nroot = "a\\u0000b"\n[symbols]\nitems = ["BTCUSDT"]\n[logging]\nlevel = "INFO"\n',
        None,
    ],
)
def test_reload_rejects_unusable_candidate_without_raising(tmp_path: Path, candidate: str | None) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path)
    (tmp_path / "data").mkdir()
    manager = ConfigManager(config_path)
    prior = manager.active
    if candidate is None:
        config_path.unlink()
    else:
        config_path.write_text(candidate, encoding="utf-8")

    result = manager.reload()

    assert result.accepted is False
    assert result.error
    assert manager.active is prior


def test_reload_applies_symbols_and_logging_atomically_and_keeps_root_restart_only(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, root="old", symbols=("AAOIUSDT", "AEHRUSDT"))
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    manager = ConfigManager(config_path)

    _write_config(config_path, root="new", symbols=("AEHRUSDT", "BTCUSDT"), level="DEBUG")
    result = manager.reload()

    assert result.accepted is True
    assert result.restart_required is True
    assert result.logging_level_changed is True
    assert result.added_symbols == ("BTCUSDT",)
    assert result.removed_symbols == ("AAOIUSDT",)
    assert result.unchanged_symbols == ("AEHRUSDT",)
    assert manager.active.storage_root == (tmp_path / "old").resolve()
    assert manager.active.pending_storage_root == (tmp_path / "new").resolve()
    assert manager.active.symbols == ("AEHRUSDT", "BTCUSDT")
    assert manager.active.logging_level == "DEBUG"


def test_reload_applies_diffs_without_restart_when_root_is_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, symbols=("AAOIUSDT", "AEHRUSDT"))
    (tmp_path / "data").mkdir()
    manager = ConfigManager(config_path)

    payload = _write_config(config_path, symbols=("AEHRUSDT", "BTCUSDT"), level="DEBUG")
    result = manager.reload()

    assert result.accepted is True
    assert result.restart_required is False
    assert result.logging_level_changed is True
    assert result.added_symbols == ("BTCUSDT",)
    assert result.removed_symbols == ("AAOIUSDT",)
    assert result.unchanged_symbols == ("AEHRUSDT",)
    assert manager.active.storage_root == (tmp_path / "data").resolve()
    assert manager.active.pending_storage_root is None
    assert manager.active.config_revision == sha256(payload).hexdigest()


def test_identical_reload_is_noop_with_deterministic_unchanged_symbols_and_revision(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    payload = _write_config(config_path, symbols=("BTCUSDT", "AAOIUSDT"))
    (tmp_path / "data").mkdir()
    manager = ConfigManager(config_path)
    prior = manager.active

    result = manager.reload()

    assert result.accepted is True
    assert result.restart_required is False
    assert result.added_symbols == ()
    assert result.removed_symbols == ()
    assert result.unchanged_symbols == ("AAOIUSDT", "BTCUSDT")
    assert result.candidate is not None
    assert result.candidate.config_revision == sha256(payload).hexdigest()
    assert manager.active.config_revision == prior.config_revision == sha256(payload).hexdigest()


def test_storage_probe_leaves_no_temporary_files_after_load_and_reload(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path)
    root = tmp_path / "data"
    root.mkdir()

    load_config(config_path)
    manager = ConfigManager(config_path)
    manager.reload()

    assert tuple(root.glob(".bybit-write-probe-*")) == ()


def test_invalid_reload_preserves_pending_restart_requirement(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, root="old")
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    manager = ConfigManager(config_path)

    _write_config(config_path, root="new")
    accepted = manager.reload()
    prior = manager.active
    config_path.write_text("[storage]\nroot = \"new\"\n[symbols]\nitems = [1]\n[logging]\nlevel = \"INFO\"\n", encoding="utf-8")

    result = manager.reload()

    assert accepted.restart_required is True
    assert result.accepted is False
    assert result.restart_required is True
    assert manager.active is prior
    assert manager.active.pending_storage_root == (tmp_path / "new").resolve()


def test_reload_tracks_pending_root_reversion_and_a_later_root_change(tmp_path: Path) -> None:
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, root="old")
    for root in ("old", "new", "third"):
        (tmp_path / root).mkdir()
    manager = ConfigManager(config_path)

    new_payload = _write_config(config_path, root="new")
    first = manager.reload()
    assert first.restart_required is True
    assert first.candidate is not None
    assert first.candidate.config_revision == sha256(new_payload).hexdigest()
    assert manager.active.storage_root == (tmp_path / "old").resolve()
    assert manager.active.pending_storage_root == (tmp_path / "new").resolve()

    second = manager.reload()
    assert second.restart_required is True
    assert second.candidate is not None
    assert manager.active.pending_storage_root == (tmp_path / "new").resolve()

    old_payload = _write_config(config_path, root="old")
    reverted = manager.reload()
    assert reverted.restart_required is False
    assert reverted.candidate is not None
    assert reverted.candidate.config_revision == sha256(old_payload).hexdigest()
    assert manager.active.pending_storage_root is None

    third_payload = _write_config(config_path, root="third")
    changed_again = manager.reload()
    assert changed_again.restart_required is True
    assert changed_again.candidate is not None
    assert changed_again.candidate.config_revision == sha256(third_payload).hexdigest()
    assert manager.active.pending_storage_root == (tmp_path / "third").resolve()
