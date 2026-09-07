from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

import mrs3.runner.files as runner_files
from mrs3.runner.config import RunnerConfig, UnsafePathError
from mrs3.runner.files import (
    BatchPreparationError,
    capture_tester_settings,
    file_changed_since,
    file_fingerprint,
    inspect_strategy_batch,
    prepare_batch_files,
    read_stable_file,
    restore_tester_settings,
)


def test_shared_log_fingerprint_rejects_retained_bytes_and_accepts_new_bytes(
    tmp_path: Path,
) -> None:
    result = tmp_path / "wizard_result.json"
    progress = tmp_path / "wizard_progress.json"
    result.write_text("old", encoding="utf-8")
    progress.write_text("old", encoding="utf-8")
    result_baseline = file_fingerprint(result)
    progress_baseline = file_fingerprint(progress)
    assert not file_changed_since(result, result_baseline)
    assert not file_changed_since(progress, progress_baseline)
    result.write_text("new", encoding="utf-8")
    progress.write_text("new", encoding="utf-8")
    assert file_changed_since(result, result_baseline)
    assert file_changed_since(progress, progress_baseline)


def test_stable_read_rejects_mutation_between_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = tmp_path / "wizard_result.json"
    result.write_bytes(b"old")
    original_read_bytes = Path.read_bytes
    calls = 0

    def mutate_after_first_read(path: Path) -> bytes:
        nonlocal calls
        payload = original_read_bytes(path)
        if path == result:
            calls += 1
            if calls == 1:
                result.write_bytes(b"new")
        return payload

    monkeypatch.setattr(Path, "read_bytes", mutate_after_first_read)
    assert read_stable_file(result, lambda path: path.read_bytes()) is None


def test_tester_settings_snapshot_restores_config_and_root_strategies(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.strategy_dir.mkdir(parents=True)
    config.tester_config.parent.mkdir(parents=True)
    (config.strategy_dir / "old.json").write_bytes(b'{"name":"old"}')
    config.tester_config.write_bytes(b'{"StartDate":"old"}')
    snapshot = capture_tester_settings(config)
    (config.strategy_dir / "old.json").unlink()
    (config.strategy_dir / "new.json").write_bytes(b'{"name":"new"}')
    config.tester_config.write_bytes(b'{"StartDate":"new"}')
    restore_tester_settings(config, snapshot)
    assert [path.name for path in config.strategy_dir.glob("*.json")] == ["old.json"]
    assert (config.strategy_dir / "old.json").read_bytes() == b'{"name":"old"}'
    assert config.tester_config.read_bytes() == b'{"StartDate":"old"}'


def _config(tmp_path: Path) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
    return RunnerConfig(
        bot_root=bot,
        executable_path=(bot / "hb_c.exe").resolve(),
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=(bot / "settings_strategy").resolve(),
        report_dir=(bot / "tester/report/my_test").resolve(),
        wizard_result=(bot / "tester/wizard_result.json").resolve(),
        wizard_progress=(bot / "tester/wizard_progress.json").resolve(),
        tester_config=(bot / "tester/tester_config.json").resolve(),
        inbox_root=(tmp_path / "tester_inbox").resolve(),
        metric_tolerance=Decimal("0.01"),
    )


def _strategy(path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "settings": []}), encoding="utf-8")
    return path


def test_preparation_retains_reports_and_logs_then_installs_exact_batch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "old.html").write_text("old", encoding="utf-8")
    config.wizard_result.write_text("{}", encoding="utf-8")
    config.wizard_progress.write_text("{}", encoding="utf-8")
    _strategy(config.strategy_dir / "OLD.json", "OLD")
    source = tmp_path / "generated"
    _strategy(source / "B.json", "B")
    _strategy(source / "A.json", "A")

    batch = prepare_batch_files(config, source)

    assert (config.report_dir / "old.html").read_text(encoding="utf-8") == "old"
    assert config.wizard_result.read_text(encoding="utf-8") == "{}"
    assert config.wizard_progress.read_text(encoding="utf-8") == "{}"
    assert sorted(path.name for path in config.strategy_dir.glob("*.json")) == [
        "A.json",
        "B.json",
    ]
    assert batch.expected_names == ("A", "B")


def test_resume_preparation_preserves_reports_and_wizard_logs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / "A.html"
    report.write_text("complete", encoding="utf-8")
    config.wizard_result.write_text("[]", encoding="utf-8")
    config.wizard_progress.write_text("{}", encoding="utf-8")
    source = tmp_path / "generated"
    _strategy(source / "A.json", "A")

    prepare_batch_files(config, source, preserve_raw_artifacts=True)

    assert report.read_text(encoding="utf-8") == "complete"
    assert config.wizard_result.read_text(encoding="utf-8") == "[]"
    assert config.wizard_progress.read_text(encoding="utf-8") == "{}"


def test_invalid_strategy_json_leaves_existing_bot_files_untouched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = _strategy(config.strategy_dir / "original.json", "original")
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / "diagnostic.html"
    report.write_text("keep", encoding="utf-8")
    config.wizard_result.write_text("keep", encoding="utf-8")
    source = tmp_path / "invalid"
    source.mkdir()
    (source / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(BatchPreparationError, match="invalid JSON"):
        prepare_batch_files(config, source)

    assert original.exists()
    assert report.exists()
    assert config.wizard_result.exists()


def test_inspection_records_sha256_for_each_validated_file(tmp_path: Path) -> None:
    source = tmp_path / "generated"
    first = _strategy(source / "A.json", "A")
    second = _strategy(source / "B.json", "B")

    inspection = inspect_strategy_batch(source)

    assert dict(inspection.file_hashes) == {
        "A.json": hashlib.sha256(first.read_bytes()).hexdigest(),
        "B.json": hashlib.sha256(second.read_bytes()).hexdigest(),
    }


def test_content_change_during_copy_is_rejected_before_bot_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = _strategy(config.strategy_dir / "original.json", "original")
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / "diagnostic.html"
    report.write_text("keep", encoding="utf-8")
    source = tmp_path / "generated"
    source_file = _strategy(source / "A.json", "A")
    inspection = inspect_strategy_batch(source)
    real_copy = runner_files.shutil.copy2
    changed = False

    def mutate_then_copy(source_path: Path, destination: Path) -> Path:
        nonlocal changed
        if not changed:
            changed = True
            source_file.write_text(
                json.dumps({"name": "A", "settings": ["changed"]}),
                encoding="utf-8",
            )
        return real_copy(source_path, destination)

    monkeypatch.setattr(runner_files.shutil, "copy2", mutate_then_copy)

    with pytest.raises(BatchPreparationError, match="content changed"):
        prepare_batch_files(
            config,
            source,
            expected_file_hashes=inspection.file_hashes,
        )

    assert original.exists()
    assert report.read_text(encoding="utf-8") == "keep"


def test_filename_must_equal_strategy_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source / "wrong.json", "RIGHT")

    with pytest.raises(BatchPreparationError, match="filename"):
        prepare_batch_files(config, source)


def test_strategy_directory_cannot_replace_protected_tester_tree(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    tester_dir = config.bot_root / "tester"
    protected = tester_dir / "keep.txt"
    protected.parent.mkdir(parents=True)
    protected.write_text("keep", encoding="utf-8")
    unsafe = replace(config, strategy_dir=tester_dir.resolve())
    source = tmp_path / "generated"
    _strategy(source / "A.json", "A")

    with pytest.raises(UnsafePathError, match="settings_strategy"):
        prepare_batch_files(unsafe, source)

    assert protected.read_text(encoding="utf-8") == "keep"


def test_preparation_replaces_only_root_json_and_preserves_nested_tree(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _strategy(config.strategy_dir / "OLD.json", "OLD")
    protected = config.strategy_dir / "Bybit"
    _strategy(protected / "KEEP.json", "KEEP")
    marker = protected / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    note = config.strategy_dir / "note.txt"
    note.write_text("keep", encoding="utf-8")
    source = tmp_path / "generated"
    _strategy(source / "NEW.json", "NEW")

    prepare_batch_files(config, source)

    assert sorted(path.name for path in config.strategy_dir.glob("*.json")) == [
        "NEW.json"
    ]
    assert (protected / "KEEP.json").is_file()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert note.read_text(encoding="utf-8") == "keep"


def test_preparation_rejects_source_inside_strategy_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = config.strategy_dir / "Bybit"
    _strategy(source / "A.json", "A")

    with pytest.raises(BatchPreparationError, match="inside strategy_dir"):
        prepare_batch_files(config, source)


def test_batch_preparation_rejects_shared_cleanup_and_keeps_root_json(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = _strategy(config.strategy_dir / "OLD.json", "OLD")
    source = tmp_path / "generated"
    _strategy(source / "NEW.json", "NEW")

    with pytest.raises(BatchPreparationError, match="cannot be cleared"):
        prepare_batch_files(config, source, preserve_raw_artifacts=False)

    assert original.is_file()
    assert not (config.strategy_dir / "NEW.json").exists()


def test_failed_root_install_restores_root_json_and_preserves_nested_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = _strategy(config.strategy_dir / "OLD.json", "OLD")
    protected = config.strategy_dir / "Bybit" / "KEEP.json"
    _strategy(protected, "KEEP")
    source = tmp_path / "generated"
    _strategy(source / "NEW.json", "NEW")
    real_copy = runner_files.shutil.copy2

    def fail_root_copy(source_path: Path, destination: Path) -> Path:
        if destination.parent == config.strategy_dir:
            raise OSError("root copy failed")
        return real_copy(source_path, destination)

    monkeypatch.setattr(runner_files.shutil, "copy2", fail_root_copy)

    with pytest.raises(BatchPreparationError, match="could not install"):
        prepare_batch_files(config, source)

    assert original.is_file()
    assert protected.is_file()
    assert not (config.strategy_dir / "NEW.json").exists()
    assert not config.strategy_dir.with_name(
        f".{config.strategy_dir.name}.mrs3-backup"
    ).exists()


def test_interrupted_root_install_rolls_back_before_propagating_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = _strategy(config.strategy_dir / "OLD.json", "OLD")
    source = tmp_path / "generated"
    _strategy(source / "NEW.json", "NEW")
    real_copy = runner_files.shutil.copy2

    def interrupt_root_copy(source_path: Path, destination: Path) -> Path:
        if destination.parent == config.strategy_dir:
            raise KeyboardInterrupt
        return real_copy(source_path, destination)

    monkeypatch.setattr(runner_files.shutil, "copy2", interrupt_root_copy)

    with pytest.raises(KeyboardInterrupt):
        prepare_batch_files(config, source)

    assert original.is_file()
    assert not (config.strategy_dir / "NEW.json").exists()
    assert not config.strategy_dir.with_name(
        f".{config.strategy_dir.name}.mrs3-backup"
    ).exists()


def test_preparation_rejects_collision_with_root_json_symlink(tmp_path: Path) -> None:
    config = _config(tmp_path)
    protected_target = tmp_path / "protected.json"
    protected_target.write_text("protected", encoding="utf-8")
    root_link = config.strategy_dir / "NEW.json"
    root_link.parent.mkdir(parents=True)
    try:
        root_link.symlink_to(protected_target)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    source = tmp_path / "generated"
    _strategy(source / "NEW.json", "NEW")

    with pytest.raises(BatchPreparationError, match="protected root entry"):
        prepare_batch_files(config, source)

    assert root_link.is_symlink()
    assert protected_target.read_text(encoding="utf-8") == "protected"
