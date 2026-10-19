"""Git sync of the pair registry between machines (registry stage 5).

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 5 contract»; ADR-0081. Every test works on bare and local
git repositories in its own temporary folder: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import subprocess

import pytest

from mrs3.pair_registry.records import (
    current_decision, current_finalists, effective_ready_rule, record_decision, record_finalist, record_setting,
)
from mrs3.pair_registry.store import connect
from mrs3.pair_registry.sync import (
    APPEND_ONLY, SyncError, collect_tables, has_remote, import_tables, init_repo, is_enabled, repo_lock,
    stamp_local_rows, sync_pull, sync_push, write_tables,
)
from tests.test_pair_registry_records import NOW, _registry


def _machine(tmp_path: Path, name: str, remote: Path | None):
    con = _registry(tmp_path / name)
    repo = tmp_path / name / "sync"
    init_repo(repo, name, str(remote) if remote else None)
    return con, repo


def _remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    return remote


def test_without_a_repository_sync_is_off(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    assert sync_push(con, tmp_path / "sync", "pc1") == {"enabled": False}
    assert sync_pull(con, tmp_path / "sync", "pc1") == {"enabled": False}
    assert not (tmp_path / "sync").exists()


def test_a_local_repository_commits_without_a_remote(tmp_path: Path) -> None:
    con, repo = _machine(tmp_path, "pc1", None)
    assert is_enabled(repo) and not has_remote(repo)
    record_decision(con, "bybit", "GOODUSDT", "ALL", "WATCH", "thin", "user", now_ms=NOW)
    first = sync_push(con, repo, "pc1")
    assert first["committed"] is True and first["pushed"] is False and "decisions" in first["changed"]
    lines = (repo / "decisions" / "pc1.jsonl").read_text(encoding="utf-8").split("\n")
    assert json.loads(lines[0])[:3] == ["row_id", "origin", "exchange"] and json.loads(lines[1])[1] == "pc1"
    again = sync_push(con, repo, "pc1")                                   # nothing changed: no commit
    assert again["committed"] is False and again["changed"] == []


def test_decisions_settings_marks_and_tests_reach_the_other_machine(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    a, repo_a = _machine(tmp_path, "pc1", remote)
    b, repo_b = _machine(tmp_path, "pc2", remote)
    record_decision(a, "bybit", "GOODUSDT", "ALL", "WATCH", "thin", "user", now_ms=NOW)
    record_setting(a, "bybit", "GOODUSDT", "ALL", "ready_v4", "admit_score", 25, "volatile", "user", now_ms=NOW)
    record_finalist(a, "bybit", "GOODUSDT", "LONG", "S_A", "FINALIST", "panel", user_rank=1, now_ms=NOW)
    a.execute("insert into pair_tests (source, exchange, symbol, side, kind, created_ms) values "
              "('perfdb:11', 'bybit', 'GOODUSDT', 'LONG', 'mrs3_tick', ?)", [NOW])
    pushed = sync_push(a, repo_a, "pc1")
    assert pushed["pushed"] is True

    pulled = sync_pull(b, repo_b, "pc2")
    assert pulled["imported"]["decisions"] == 1 and pulled["imported"]["pair_settings"] == 1
    assert pulled["imported"]["finalists"] == 1 and pulled["imported"]["pair_tests"] == 1
    assert current_decision(b, "bybit", "GOODUSDT") == "WATCH"
    assert effective_ready_rule(b, "bybit", "GOODUSDT")[0].admit_score == 25
    assert current_finalists(b, "bybit")[0]["status"] == "FINALIST"
    assert b.execute("select source, origin from pair_tests").fetchall() == [("pc1:perfdb:11", "pc1")]
    again = sync_pull(b, repo_b, "pc2")                                   # idempotent
    assert all(count == 0 for count in again["imported"].values())
    sync_push(b, repo_b, "pc2")                                          # imported rows are never re-exported
    assert not (repo_b / "decisions" / "pc2.jsonl").read_text(encoding="utf-8").split("\n")[1].strip()


def test_concurrent_writes_on_both_machines_merge_without_conflict(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    a, repo_a = _machine(tmp_path, "pc1", remote)
    b, repo_b = _machine(tmp_path, "pc2", remote)
    record_decision(a, "bybit", "GOODUSDT", "ALL", "WATCH", "a", "user", now_ms=NOW)
    record_decision(b, "bybit", "GOODUSDT", "ALL", "EXCLUDE", "b", "user", now_ms=NOW + 10)
    sync_push(a, repo_a, "pc1")
    result = sync_push(b, repo_b, "pc2")                                 # rejected first: pull, then push again
    assert result["pushed"] is True
    sync_pull(a, repo_a, "pc1")
    sync_pull(b, repo_b, "pc2")
    for con in (a, b):                                                   # in force by time on both machines
        assert current_decision(con, "bybit", "GOODUSDT") == "EXCLUDE"
        assert con.execute("select count(*) from decisions").fetchone()[0] == 2


def test_the_newer_pipeline_row_wins(tmp_path: Path) -> None:
    from mrs3.pair_registry.pipeline import compute_pipeline
    from tests.test_ready_store import _store_with_periods

    remote = _remote(tmp_path)
    a, repo_a = _machine(tmp_path, "pc1", remote)
    b, repo_b = _machine(tmp_path, "pc2", remote)
    store = _store_with_periods(tmp_path / "store", "GOODUSDT", 3, 35.0)
    compute_pipeline(a, store, "bybit", date(2026, 10, 19), "pc1", now_ms=NOW + 100, track=False)
    compute_pipeline(b, store, "bybit", date(2026, 10, 19), "pc2", now_ms=NOW, track=False)
    sync_push(a, repo_a, "pc1")
    sync_pull(b, repo_b, "pc2")
    assert b.execute("select machine from pipeline where symbol = 'GOODUSDT'").fetchone() == ("pc1",)
    sync_push(b, repo_b, "pc2")
    sync_pull(a, repo_a, "pc1")                                         # pc2's older row never replaces it
    assert a.execute("select machine, computed_ms from pipeline where symbol = 'GOODUSDT'").fetchone() == ("pc1", NOW + 100)


def test_stamping_keeps_existing_rows_and_their_order(tmp_path: Path) -> None:
    con = _registry(tmp_path)
    record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "FINALIST", "panel", now_ms=NOW)
    record_finalist(con, "bybit", "GOODUSDT", "LONG", "S_A", "RESERVE", "panel", now_ms=NOW + 1)
    assert stamp_local_rows(con, "pc1") == 2
    assert stamp_local_rows(con, "pc1") == 0
    ids = [row[0] for row in con.execute("select row_id from finalists").fetchall()]
    assert len(set(ids)) == 2 and all(len(value) == 64 for value in ids)
    assert current_finalists(con, "bybit")[0]["status"] == "RESERVE"


def test_init_repo_is_idempotent_and_adds_a_remote(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.sync as sync

    remote = _remote(tmp_path)
    monkeypatch.setattr(sync.shutil, "which", lambda name: "C:/Tools/gh.exe" if name == "gh" else None)
    assert init_repo(tmp_path / "sync", "pc1")["created"] is True
    assert init_repo(tmp_path / "sync", "pc1", str(remote)) == {"created": False, "remote": True}
    init_repo(tmp_path / "sync", "pc1", str(remote))                      # credentials stay one helper, not two
    helpers = subprocess.run(["git", "-C", str(tmp_path / "sync"), "config", "--local", "--get-all",
                              "credential.helper"], capture_output=True, text=True, check=True).stdout.splitlines()
    assert helpers == ["", "!'C:/Tools/gh.exe' auth git-credential"]   # the gh login, never the system manager
    with pytest.raises(SyncError):
        from mrs3.pair_registry.sync import _git
        _git(tmp_path / "sync", "no-such-command")


def test_a_registry_from_before_stage_5_gains_the_sync_columns(tmp_path: Path) -> None:
    import duckdb

    root = tmp_path / "registry"
    con = connect(root)
    for table, column in (("decisions", "origin"), ("decisions", "row_id"), ("pair_tests", "origin")):
        con.execute(f"alter table {table} drop column {column}")
    con.close()
    con = connect(root)                                                  # the late columns come back
    columns = {row[0] for row in con.execute(
        "select column_name from information_schema.columns where table_name = 'decisions'").fetchall()}
    assert {"origin", "row_id"} <= columns
    assert isinstance(con, duckdb.DuckDBPyConnection)


def test_a_machine_name_from_the_system_is_made_safe(tmp_path: Path, monkeypatch) -> None:
    from mrs3.pair_registry.store import RegistryError, load_registry_settings

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": "registry"}}), encoding="utf-8")
    monkeypatch.setenv("COMPUTERNAME", "ПК Ани")                         # a Cyrillic name with a space
    name = load_registry_settings(config).machine
    assert name.startswith("______-") and len(name) == 15                    # never stops the registry
    monkeypatch.setenv("COMPUTERNAME", "ПК Бор")
    assert load_registry_settings(config).machine != name                   # two such names never collide
    monkeypatch.setenv("COMPUTERNAME", "PC-1")
    assert load_registry_settings(config).machine == "PC-1"
    config.write_text(json.dumps({"pair_registry": {"root": "registry", "machine": "my pc"}}), encoding="utf-8")
    with pytest.raises(RegistryError):                                      # a configured name must be safe
        load_registry_settings(config)


def test_one_sync_at_a_time_across_processes(tmp_path: Path) -> None:
    _con, repo = _machine(tmp_path, "pc1", None)
    with repo_lock(repo):
        with pytest.raises(SyncError, match="busy"):
            with repo_lock(repo, timeout_s=0.5):
                pass
    with repo_lock(repo, timeout_s=0.5):                                    # released after use
        pass
    assert "mrs3-sync.lock" not in subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                                  capture_output=True, text=True, check=True).stdout


def test_an_older_snapshot_never_drops_an_exported_row(tmp_path: Path) -> None:
    con, repo = _machine(tmp_path, "pc1", None)
    record_decision(con, "bybit", "GOODUSDT", "ALL", "WATCH", "a", "user", now_ms=NOW)
    stamp_local_rows(con, "pc1")
    older = collect_tables(con, "pc1", APPEND_ONLY)                        # taken before the second decision
    record_decision(con, "bybit", "GOODUSDT", "ALL", "EXCLUDE", "b", "user", now_ms=NOW + 1)
    sync_push(con, repo, "pc1")
    assert write_tables(repo, "pc1", older) == []                           # nothing removed, nothing to write
    assert len((repo / "decisions" / "pc1.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert not list((repo / ".git").glob("export-*.tmp"))                   # temporary files are renamed away


def test_a_broken_sync_file_stops_the_import_and_changes_nothing(tmp_path: Path) -> None:
    con, repo = _machine(tmp_path, "pc1", None)
    (repo / "decisions").mkdir()
    (repo / "decisions" / "pc2.jsonl").write_text('["row_id","origin"]\n["x", \n', encoding="utf-8")
    with pytest.raises(SyncError, match="decisions/pc2.jsonl"):
        sync_pull(con, repo, "pc1")
    assert con.execute("select count(*) from decisions").fetchone()[0] == 0


def test_two_machines_with_one_name_stop_with_a_clear_error(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    a, repo_a = _machine(tmp_path, "a", remote)
    b, repo_b = _machine(tmp_path, "b", remote)
    record_decision(a, "bybit", "GOODUSDT", "ALL", "WATCH", "a", "user", now_ms=NOW)
    record_decision(b, "bybit", "GOODUSDT", "ALL", "EXCLUDE", "b", "user", now_ms=NOW + 1)
    sync_push(a, repo_a, "same")
    with pytest.raises(SyncError, match="pair_registry.machine"):           # both wrote decisions/same.jsonl
        sync_push(b, repo_b, "same")
    assert not (repo_b / ".git" / "MERGE_HEAD").exists()                    # the merge is aborted, not left half done


def test_cli_sync_init_and_a_decision_commit_to_the_sync_repository(tmp_path: Path, capsys) -> None:
    from mrs3.pair_registry import cli

    remote = _remote(tmp_path)
    _registry(tmp_path).close()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"pair_registry": {"root": str(tmp_path / "registry"), "machine": "pc1"}}),
                      encoding="utf-8")
    assert cli.main(["sync-init", "--config", str(config), "--remote", str(remote)]) == 0
    assert json.loads(capsys.readouterr().out) == {"sync": {"created": True, "remote": True}, "machine": "pc1"}
    assert cli.main(["decide", "--config", str(config), "--symbol", "GOODUSDT", "--decision", "WATCH",
                     "--reason", "check", "--author", "me"]) == 0
    sync = json.loads(capsys.readouterr().out)["sync"]
    assert sync["committed"] is True and sync["pushed"] is True and "decisions" in sync["changed"]
    heads = subprocess.run(["git", "-C", str(remote), "log", "--format=%s", "main"], capture_output=True, text=True,
                           check=True).stdout
    assert heads.startswith("decide (pc1)")
