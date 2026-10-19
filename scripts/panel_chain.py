"""Run the steps of one Panel action in order (Panel rework stage 3), as one task of the task area.

Usage: python scripts/panel_chain.py <steps.json> [--stop-file PATH] [--cleanup]
`steps.json`: {"steps": [{"name": "...", "argv": ["python", "..."]}, ...]}. Every child line is passed through
(children report `progress` / `error` JSON lines themselves); the chain adds a `progress` line at each step boundary
and an `error` line when a step fails. A stop file present before a step, or a step reporting a cooperative stop
(`{"stopped": true}` or a `summary` with it, as `tester_jobs run` does after deleting the stop file), ends the
chain. `--cleanup` deletes the steps file and its folder once read. Output is UTF-8 in both directions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


def _reports_stop(line: str) -> bool:
    try:
        event = json.loads(line)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    summary = event.get("summary")
    return event.get("stopped") is True or (isinstance(summary, dict) and summary.get("stopped") is True)


def main(argv: Sequence[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("steps", type=Path)
    parser.add_argument("--stop-file", type=Path, default=None)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    steps = json.loads(args.steps.read_text(encoding="utf-8"))["steps"]
    if args.cleanup:
        args.steps.unlink(missing_ok=True)
        try:
            args.steps.parent.rmdir()
        except OSError:
            pass
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    total = len(steps)
    stopped = False
    for index, step in enumerate(steps):
        if stopped or (args.stop_file is not None and args.stop_file.exists()):
            print(json.dumps({"stopped_before": step["name"]}, ensure_ascii=False), flush=True)
            return 0
        print(json.dumps({"progress": {"done": index, "total": total, "step": step["name"], "pair": None}},
                         ensure_ascii=False), flush=True)
        process = subprocess.Popen(step["argv"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            stopped = stopped or _reports_stop(line)
        code = process.wait()
        if code != 0:
            print(json.dumps({"error": f"step {step['name']} ended with exit code {code}", "step": step["name"]},
                             ensure_ascii=False), flush=True)
            return code
    print(json.dumps({"progress": {"done": total, "total": total, "step": "done", "pair": None}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
