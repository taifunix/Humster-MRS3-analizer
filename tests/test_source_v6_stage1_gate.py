from __future__ import annotations

from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "check-source-v6-stage1-gate.py"


def _run(progress: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--progress", str(progress)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_gate_accepts_exactly_one_well_formed_root_token(tmp_path: Path) -> None:
    progress = tmp_path / "progress.md"
    progress.write_text(
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=CODE_REVIEW_PASS; evidence=.codex/ledger.md\n",
        encoding="utf-8",
    )
    result = _run(progress)
    assert result.returncode == 0
    assert "ACCEPTED" in result.stdout


def test_gate_rejects_absent_malformed_or_duplicate_token(tmp_path: Path) -> None:
    progress = tmp_path / "progress.md"
    for content in (
        "no gate\n",
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=bad; reviewer=x; evidence=y\n",
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-02-30; reviewer=x; evidence=y\n",
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=x; evidence=   \n",
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=x; evidence=y\n"
        "STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=x; evidence=y\n",
    ):
        progress.write_text(content, encoding="utf-8")
        assert _run(progress).returncode == 1
