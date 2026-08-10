from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class OutputDirectoryBusyError(RuntimeError):
    """Raised when another selection process owns the output directory."""


class OutputDirectoryLock:
    """Cross-platform advisory lock held for one complete selection run."""

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory.resolve()
        self.path = self.output_directory / ".mrs3-selection.lock"
        self._handle: BinaryIO | None = None

    def __enter__(self) -> OutputDirectoryLock:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise OutputDirectoryBusyError(
                f"output directory is already being written: {self.output_directory}"
            ) from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
