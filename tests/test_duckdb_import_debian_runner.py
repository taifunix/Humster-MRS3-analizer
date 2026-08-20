from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mrs3.duckdb_import import ImportProgress, SnapshotProgress

ROOT = Path(__file__).parents[1]
RUNNER_PATH = ROOT / "scripts" / "import_html_duckdb_debian.py"
WRAPPER_PATH = ROOT / "scripts" / "import-html-duckdb-debian.sh"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("import_html_duckdb_debian", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_config(tmp_path: Path, *, configured: bool = True) -> Path:
    config = tmp_path / "config.local.json"
    section = (
        {
            "source_duckdb_path": str(tmp_path / "source.duckdb"),
            "audit_root": str(tmp_path / "audit"),
            "workers": 2,
            "transaction_batch_size": 40,
        }
        if configured
        else {}
    )
    config.write_text(json.dumps({"duckdb_import": section}), encoding="utf-8")
    return config


def _committed_result(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values = dict(
        job_id="job-x",
        final_state="COMMITTED",
        discovered=3,
        parsed=3,
        inserted=3,
        replaced=0,
        identical=0,
        ambiguous=0,
        quarantined=0,
        safe_to_delete="YES",
        manifest_path=tmp_path / "audit" / "import_manifest.json",
        checklist_path=tmp_path / "audit" / "html_delete_checklist.json",
        error=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_requires_html_root(runner: object, tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        runner.main(["--config", str(config)])
    assert exc_info.value.code == 2


def test_default_config_resolves_relative_to_repo_root(runner: object) -> None:
    args = SimpleNamespace(config=None)
    assert runner._config_path(args) == runner._REPO_ROOT / "config.local.json"


def test_debian_wrapper_changes_to_repo_root_before_python() -> None:
    script = WRAPPER_PATH.read_text(encoding="utf-8")
    cd_line = 'cd "$REPO_ROOT"'
    exec_line = next(
        line for line in script.splitlines() if line.startswith("exec ")
    )
    assert cd_line in script
    assert script.index(cd_line) < script.index(exec_line)


def test_rejects_missing_config_file(
    runner: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = runner.main(
        [
            "--html-root",
            str(tmp_path / "html"),
            "--config",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == runner.EXIT_USAGE
    assert "config file not found" in capsys.readouterr().out


def test_rejects_missing_source_db_or_audit_before_work(
    runner: object, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path, configured=False)
    (tmp_path / "html").mkdir()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("preflight must not run without configured settings")

    monkeypatch.setattr(runner, "preflight_html_import", unexpected)

    code = runner.main(["--html-root", str(tmp_path / "html"), "--config", str(config)])

    assert code == runner.EXIT_USAGE
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "error"
    assert "source_duckdb_path and duckdb_import.audit_root" in events[-1]["error"]


def test_rejects_missing_html_root(
    runner: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    code = runner.main(
        ["--html-root", str(tmp_path / "missing"), "--config", str(config)]
    )
    assert code == runner.EXIT_USAGE
    assert "html root is not a directory" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--workers", "--batch-size"])
def test_rejects_non_positive_workers_and_batch(
    runner: object, tmp_path: Path, flag: str
) -> None:
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        runner.main(
            [
                "--html-root",
                str(tmp_path / "html"),
                "--config",
                str(config),
                flag,
                "0",
            ]
        )
    assert exc_info.value.code == 2


def test_rejects_unsafe_job_id(
    runner: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    (tmp_path / "html").mkdir()
    code = runner.main(
        [
            "--html-root",
            str(tmp_path / "html"),
            "--config",
            str(config),
            "--job-id",
            "bad id!",
        ]
    )
    assert code == runner.EXIT_USAGE
    assert "job_id must be a safe" in capsys.readouterr().out


def test_binds_exact_preflight_token_and_prints_progress_and_summary(
    runner: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    html_root = tmp_path / "html"
    html_root.mkdir()
    captured: dict[str, object] = {}

    def fake_preflight(request, progress_callback=None):
        assert request.root_path == html_root.resolve()
        if progress_callback is not None:
            progress_callback(SnapshotProgress(3, 1, 10, 2))
        return SimpleNamespace(token="token-abc", discovered=3, source_schema_version=4)

    def fake_import(request, progress_callback=None):
        captured["request"] = request
        if progress_callback is not None:
            progress_callback(ImportProgress("PARSING", 3, 1, 0, 0, 0, 0, 0))
        return _committed_result(tmp_path)

    monkeypatch.setattr(runner, "preflight_html_import", fake_preflight)
    monkeypatch.setattr(runner, "import_html_tree", fake_import)

    code = runner.main(
        [
            "--html-root",
            str(html_root),
            "--config",
            str(config),
            "--workers",
            "5",
            "--batch-size",
            "7",
            "--job-id",
            "job-x",
        ]
    )

    assert code == 0
    request = captured["request"]
    assert request.expected_preflight_token == "token-abc"
    assert request.preflight.token == "token-abc"
    assert request.database_path == tmp_path / "source.duckdb"
    assert request.audit_root == tmp_path / "audit"
    assert request.workers == 5
    assert request.transaction_batch_size == 7
    assert request.job_id == "job-x"

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == [
        "preflight_progress",
        "preflight_ready",
        "import_progress",
        "summary",
    ]
    summary = lines[-1]
    assert summary["final_state"] == "COMMITTED"
    assert summary["safe_to_delete"] == "YES"
    assert summary["counts"]["inserted"] == 3
    assert summary["manifest_path"] == str(tmp_path / "audit" / "import_manifest.json")
    assert summary["checklist_path"] == str(tmp_path / "audit" / "html_delete_checklist.json")
    rendered = "\n".join(line for line in [json.dumps(line, ensure_ascii=False) for line in lines])
    assert str(tmp_path / "source.duckdb") not in rendered
    assert str(config) not in rendered


def test_exits_nonzero_when_final_state_is_not_committed(
    runner: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    html_root = tmp_path / "html"
    html_root.mkdir()

    def fake_preflight(request, progress_callback=None):
        return SimpleNamespace(token="token-x", discovered=1, source_schema_version=None)

    monkeypatch.setattr(runner, "preflight_html_import", fake_preflight)
    monkeypatch.setattr(
        runner,
        "import_html_tree",
        lambda request, progress_callback=None: _committed_result(
            tmp_path,
            final_state="FAILED",
            safe_to_delete="NO",
            error="OSError: [Errno 28] No space left on device",
        ),
    )

    code = runner.main(["--html-root", str(html_root), "--config", str(config)])

    assert code == runner.EXIT_IMPORT_FAILURE
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["final_state"] == "FAILED"
    assert summary["safe_to_delete"] == "NO"
    assert "No space left on device" in (summary["error"] or "")


def test_exits_nonzero_when_quarantined(
    runner: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(tmp_path)
    html_root = tmp_path / "html"
    html_root.mkdir()

    def fake_preflight(request, progress_callback=None):
        return SimpleNamespace(token="token-y", discovered=2, source_schema_version=None)

    monkeypatch.setattr(runner, "preflight_html_import", fake_preflight)
    monkeypatch.setattr(
        runner,
        "import_html_tree",
        lambda request, progress_callback=None: _committed_result(
            tmp_path, quarantined=2, safe_to_delete="NO"
        ),
    )

    code = runner.main(["--html-root", str(html_root), "--config", str(config)])

    assert code == runner.EXIT_IMPORT_FAILURE
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["final_state"] == "COMMITTED"
    assert summary["counts"]["quarantined"] == 2
