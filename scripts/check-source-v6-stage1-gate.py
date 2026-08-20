#!/usr/bin/env python3
"""Fail closed unless Stage 1 has one root acceptance token."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re


TOKEN = re.compile(
    r"^STAGE_1_GATE=ACCEPTED_BY_ROOT; date=(\d{4}-\d{2}-\d{2}); reviewer=([^;\r\n]+); evidence=([^\r\n]+)$",
    re.MULTILINE,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--progress", type=Path, default=Path(__file__).resolve().parents[1] / "progress.md")
    progress = parser.parse_args().progress
    matches = TOKEN.findall(progress.read_text(encoding="utf-8"))
    if len(matches) != 1:
        print("REJECTED: expected exactly one well-formed STAGE_1_GATE token")
        return 1
    if not matches[0][1].strip() or not matches[0][2].strip():
        print("REJECTED: Stage 1 gate reviewer and evidence must be non-empty")
        return 1
    try:
        date.fromisoformat(matches[0][0])
    except ValueError:
        print("REJECTED: invalid Stage 1 gate date")
        return 1
    print("ACCEPTED: Stage 1 root gate is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
