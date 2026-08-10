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
    cleanup_completed_batch,
    inspect_strategy_batch,
    prepare_batch_files,
)


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
        metric_tolerance=Decimal("0.01"),
    )


def _strategy(path: Path, name: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "settings": []}), encoding="utf-8")
    return path


def test_preparation_removes_reports_and_logs_then_installs_exact_batch(
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

    assert not config.report_dir.exists()
    assert not config.wizard_result.exists()
    assert not config.wizard_progress.exists()
    assert sorted(path.name for path in config.strategy_dir.glob("*.json")) == [
        "A.json",
        "B.json",
    ]
    assert batch.expected_names == ("A", "B")


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


def test_success_cleanup_removes_only_report_tree_and_two_logs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "result.html").write_text("result", encoding="utf-8")
    config.wizard_result.write_text("{}", encoding="utf-8")
    config.wizard_progress.write_text("{}", encoding="utf-8")
    keep = config.bot_root / "tester" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    cleanup_completed_batch(config)

    assert not config.report_dir.exists()
    assert not config.wizard_result.exists()
    assert not config.wizard_progress.exists()
    assert keep.exists()
