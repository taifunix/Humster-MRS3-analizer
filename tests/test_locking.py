from __future__ import annotations

from pathlib import Path

import pytest

from mrs3.locking import OutputDirectoryBusyError, OutputDirectoryLock


def test_second_writer_to_same_output_directory_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "selection"

    with OutputDirectoryLock(output):
        with pytest.raises(OutputDirectoryBusyError, match="already being written"):
            with OutputDirectoryLock(output):
                pass


def test_lock_is_released_after_writer_finishes(tmp_path: Path) -> None:
    output = tmp_path / "selection"

    with OutputDirectoryLock(output):
        pass
    with OutputDirectoryLock(output):
        pass

    assert (output / ".mrs3-selection.lock").exists()
