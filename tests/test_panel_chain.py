"""The step chain of one Panel action (Panel rework stage 3).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract».
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

CHAIN = Path(__file__).resolve().parents[1] / "scripts" / "panel_chain.py"


def _chain(tmp_path: Path, steps: list[tuple[str, str]], *extra: str) -> tuple[int, list[dict], list[str]]:
    folder = tmp_path / "mrs3-chain-test"
    folder.mkdir(exist_ok=True)
    steps_file = folder / "steps.json"
    steps_file.write_text(json.dumps({"steps": [{"name": name, "argv": [sys.executable, "-c", code]}
                                                for name, code in steps]}, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run([sys.executable, str(CHAIN), str(steps_file), *extra], capture_output=True,
                               encoding="utf-8", errors="strict", timeout=60)
    lines = completed.stdout.splitlines()
    events = [json.loads(line) for line in lines if line.startswith("{")]
    return completed.returncode, events, lines


def test_steps_run_in_order_with_step_progress_and_utf8_output(tmp_path: Path) -> None:
    code, events, lines = _chain(tmp_path, [("История", "print('шаг один')"), ("Конвейер", "print('ok')")])
    assert code == 0
    assert [event["progress"]["step"] for event in events if "progress" in event] == ["История", "Конвейер", "done"]
    assert "шаг один" in lines                                   # a non-ASCII child line passes through intact


def test_a_failing_step_ends_the_chain_with_an_error(tmp_path: Path) -> None:
    code, events, _lines = _chain(tmp_path, [("plan", "raise SystemExit(3)"), ("run", "print('never')")])
    assert code == 3
    assert events[-1] == {"error": "step plan ended with exit code 3", "step": "plan"}
    assert not any(event.get("progress", {}).get("step") == "run" for event in events)


def test_a_stop_file_ends_the_chain_before_the_next_step(tmp_path: Path) -> None:
    stop = tmp_path / ".stop"
    code, events, _lines = _chain(tmp_path, [("plan", f"open(r'{stop}', 'w').write('stop')"), ("run", "print('never')")],
                                  "--stop-file", str(stop))
    assert code == 0 and events[-1] == {"stopped_before": "run"}


def test_a_cooperative_stop_reported_by_a_step_ends_the_chain(tmp_path: Path) -> None:
    # review: tester_jobs run deletes jobs/.stop itself when it stops, so the chain also honours its report
    stop = tmp_path / ".stop"
    stopped = "import json; print(json.dumps({'stopped': True, 'done': 1, 'failed': 0}))"
    code, events, _lines = _chain(tmp_path, [("run", stopped), ("import", "print('never')")], "--stop-file", str(stop))
    assert code == 0 and events[-1] == {"stopped_before": "import"}
    summary = "import json; print(json.dumps({'summary': {'stopped': True, 'done': 0, 'failed': 0}}))"
    code, events, _lines = _chain(tmp_path, [("run", summary), ("import", "print('never')")], "--stop-file", str(stop))
    assert code == 0 and events[-1] == {"stopped_before": "import"}


def test_cleanup_removes_the_steps_file_and_its_folder(tmp_path: Path) -> None:
    code, _events, _lines = _chain(tmp_path, [("one", "print(1)")], "--cleanup")
    assert code == 0 and not (tmp_path / "mrs3-chain-test").exists()
