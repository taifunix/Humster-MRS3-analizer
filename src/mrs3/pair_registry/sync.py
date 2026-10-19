"""Git sync of the synced registry tables between machines (registry stage 5).

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 5 contract»; decision ADR-0081. One file per synced table
and machine (`<table>/<machine>.jsonl`): a machine writes only its own files, so git merges never conflict. Pull and
push run only when the sync repository has a remote `origin` (created by the user); a commit is always local.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Iterable, Iterator, Sequence

import duckdb

from .store import RegistryError

# append-only tables keyed by row_id: (columns in file order)
APPEND_ONLY: dict[str, tuple[str, ...]] = {
    "decisions": ("row_id", "origin", "exchange", "symbol", "side", "decision", "reason", "author", "created_ms"),
    "pair_settings": ("row_id", "origin", "exchange", "symbol", "side", "filter", "parameter", "value", "reason",
                      "author", "created_ms"),
    "finalists": ("row_id", "origin", "exchange", "symbol", "side", "strategy", "status", "user_rank", "source",
                  "created_ms"),
}
TESTS_COLUMNS = ("source", "origin", "exchange", "symbol", "side", "kind", "window_start", "window_end", "structure",
                 "pnl_pct", "maxdd_pct", "trades", "created_ms")
PIPELINE_KEY = ("exchange", "symbol", "side", "as_of_day")
TABLES = (*APPEND_ONLY, "pair_tests", "pipeline")
IDENTITY = "mrs3-registry"
LOCK_NAME = "mrs3-sync.lock"                   # inside .git: never committed
LOCK_TIMEOUT_S = 300.0


class SyncError(RegistryError):
    """The sync repository cannot be used (message safe to show; no local path)."""


# -- git ---------------------------------------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True, config: Sequence[str] = ()) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["git", *config, *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
                                errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError(f"git {args[0]}: {type(error).__name__}") from None
    if check and result.returncode != 0:
        raise SyncError(f"git {args[0]} failed (exit {result.returncode})")
    return result


def is_enabled(repo: Path) -> bool:
    return (Path(repo) / ".git").is_dir()


def has_remote(repo: Path) -> bool:
    return is_enabled(repo) and _git(repo, "remote", check=False).stdout.split().count("origin") == 1


def _gh_credentials() -> list[str]:
    """The GitHub CLI as the only credential helper, when it is installed: the private repository belongs to the
    account `gh` is logged in to, which may differ from the one stored in the system credential manager."""
    gh = shutil.which("gh")
    if not gh or "'" in gh:                    # the path goes inside a shell single-quoted string
        return []
    return ["credential.helper=", f"credential.helper=!'{Path(gh).as_posix()}' auth git-credential"]


def _use_gh_credentials(repo: Path) -> None:
    helpers = _gh_credentials()
    if helpers:
        _git(repo, "config", "--local", "--replace-all", "credential.helper", "")
        _git(repo, "config", "--local", "--add", "credential.helper", helpers[1].split("=", 1)[1])


def init_repo(repo: Path, machine: str, remote: str | None = None) -> dict:
    """Create the local sync repository (clone when the remote has history, else init). Idempotent."""
    repo = Path(repo)
    config = [part for helper in _gh_credentials() for part in ("-c", helper)]
    if is_enabled(repo):
        if remote and not has_remote(repo):
            _git(repo, "remote", "add", "origin", remote)
        if has_remote(repo):
            _use_gh_credentials(repo)
        return {"created": False, "remote": has_remote(repo)}
    repo.mkdir(parents=True, exist_ok=True)
    if remote and _git(repo, "ls-remote", "--heads", remote, check=False, config=config).stdout.strip():
        _git(repo.parent, "clone", remote, str(repo), config=config)
    else:
        _git(repo, "init", "-b", "main")
        if remote:
            _git(repo, "remote", "add", "origin", remote)
    _git(repo, "config", "user.name", f"{IDENTITY} {machine}")
    _git(repo, "config", "user.email", f"{IDENTITY}@{machine}.local")
    _git(repo, "config", "core.autocrlf", "false")
    if remote:
        _use_gh_credentials(repo)
    return {"created": True, "remote": has_remote(repo)}


def _branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"


def _has_commits(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def pull(repo: Path) -> bool:
    """Fetch and merge the remote branch when there is one; True when something new arrived."""
    if not has_remote(repo):
        return False
    _git(repo, "fetch", "origin")
    branch = _branch(repo) if _has_commits(repo) else "main"
    if not _git(repo, "rev-parse", "--verify", f"origin/{branch}", check=False).returncode == 0:
        return False
    before = _git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    if not _has_commits(repo):
        _git(repo, "checkout", "-B", branch, f"origin/{branch}")
    else:
        # each machine writes only its own files, so two independently started histories merge cleanly
        merged = _git(repo, "merge", "--no-edit", "--allow-unrelated-histories", f"origin/{branch}", check=False)
        if merged.returncode != 0:             # e.g. two machines with one name: never leave a half-done merge
            _git(repo, "merge", "--abort", check=False)
            raise SyncError(f"git merge failed (exit {merged.returncode}); check that every machine has its own "
                            "pair_registry.machine")
    return _git(repo, "rev-parse", "HEAD").stdout.strip() != before


def push(repo: Path) -> bool:
    """Push the local commits when a remote is set (a rejected push gets one pull and one retry)."""
    if not has_remote(repo) or not _has_commits(repo):
        return False
    branch = _branch(repo)
    if _git(repo, "push", "origin", branch, check=False).returncode != 0:
        pull(repo)
        _git(repo, "push", "origin", branch)
    return True


def commit_and_push(repo: Path, message: str) -> dict:
    """Commit the changed files; push when a remote is set."""
    _git(repo, "add", "-A")
    changed = bool(_git(repo, "status", "--porcelain").stdout.strip())
    if changed:
        _git(repo, "commit", "-m", message)
    return {"committed": changed, "pushed": push(repo)}


@contextmanager
def repo_lock(repo: Path, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """One sync at a time on this machine, across processes (Panel, CLI) and threads: an OS lock on a file in .git."""
    path = Path(repo) / ".git" / LOCK_NAME
    handle = open(path, "a+b")
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                _lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SyncError("the sync repository is busy with another sync") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# -- files ------------------------------------------------------------------------------------------------------

def _plain(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write(path: Path, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> bool:
    """Write the file when its text changes; atomically (a temporary file in .git, then a rename), so a killed
    process never leaves half a file to be committed."""
    lines = [json.dumps(list(columns), ensure_ascii=False)]
    lines += [json.dumps([_plain(value) for value in row], ensure_ascii=False, separators=(",", ":")) for row in rows]
    text = "\n".join(lines) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    git_dir = path.parent.parent / ".git"
    with tempfile.NamedTemporaryFile("wb", dir=git_dir if git_dir.is_dir() else path.parent, prefix="export-",
                                     suffix=".tmp", delete=False) as handle:
        handle.write(text.encode("utf-8"))
    os.replace(handle.name, path)
    return True


def _read(path: Path) -> tuple[list[str], list[list[object]]]:
    """A sync file; a broken line fails the import with the file named (relative to the repository), never a path."""
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    if not lines:
        return [], []
    try:
        columns = json.loads(lines[0])
        rows = [json.loads(line) for line in lines[1:]]
    except json.JSONDecodeError as error:
        raise SyncError(f"{path.parent.name}/{path.name}: not a valid sync file ({error.msg})") from None
    if not isinstance(columns, list) or not all(isinstance(row, list) for row in rows):
        raise SyncError(f"{path.parent.name}/{path.name}: not a valid sync file")
    return columns, rows


# -- export -----------------------------------------------------------------------------------------------------

def stamp_local_rows(con: duckdb.DuckDBPyConnection, machine: str) -> int:
    """Give every local row without one its origin (this machine) and row_id (sha256 of origin, seq and content)."""
    stamped = 0
    for table, columns in APPEND_ONLY.items():
        content = [column for column in columns if column not in ("row_id", "origin")]
        rows = con.execute(f"select seq, {', '.join(content)} from {table} where row_id is null").fetchall()
        for seq, *values in rows:
            digest = hashlib.sha256(json.dumps([machine, seq, *[_plain(v) for v in values]], ensure_ascii=False,
                                               default=str).encode("utf-8")).hexdigest()
            con.execute(f"update {table} set origin = coalesce(origin, ?), row_id = ? where seq = ?",
                        [machine, digest, seq])
            stamped += 1
    con.execute("update pair_tests set origin = ? where origin is null", [machine])
    return stamped


Snapshot = dict[str, tuple[Sequence[str], list[Sequence[object]]]]


def collect_tables(con: duckdb.DuckDBPyConnection, machine: str, tables: Iterable[str] = TABLES) -> Snapshot:
    """This machine's rows of the synced tables, read into memory (columns, rows) per table."""
    snapshot: Snapshot = {}
    for table in tables:
        if table in APPEND_ONLY:
            columns: Sequence[str] = APPEND_ONLY[table]
            rows = con.execute(f"select {', '.join(columns)} from {table} where origin = ? order by row_id",
                               [machine]).fetchall()
        elif table == "pair_tests":
            columns = TESTS_COLUMNS
            rows = con.execute(f"select {', '.join(columns)} from pair_tests where origin = ? order by source",
                               [machine]).fetchall()
        else:
            columns = [row[0] for row in con.execute(
                "select column_name from information_schema.columns where table_name = 'pipeline' "
                "order by ordinal_position").fetchall()]
            rows = con.execute(
                f"select {', '.join(columns)} from (select *, row_number() over (partition by exchange, symbol, side "
                "order by as_of_day desc, computed_ms desc) as newest from pipeline where machine = ?) where newest = 1 "
                "order by exchange, symbol, side", [machine]).fetchall()
        snapshot[table] = (columns, rows)
    return snapshot


def write_tables(repo: Path, machine: str, snapshot: Snapshot) -> list[str]:
    """Write this machine's files from a snapshot; returns the tables whose file changed. Append-only rows already in
    the file are kept (union by row_id), so a snapshot taken before another process exported never drops a row."""
    changed = []
    for table, (columns, rows) in snapshot.items():
        path = Path(repo) / table / f"{machine}.jsonl"
        if table in APPEND_ONLY and path.is_file():
            file_columns, file_rows = _read(path)
            if list(file_columns) == list(columns):
                merged = {row[0]: list(row) for row in file_rows}
                merged.update({row[0]: [_plain(value) for value in row] for row in rows})
                rows = [merged[key] for key in sorted(merged)]
        if _write(path, columns, rows):
            changed.append(table)
    return changed


def export_tables(con: duckdb.DuckDBPyConnection, repo: Path, machine: str) -> list[str]:
    """Write this machine's files; returns the tables whose file changed."""
    return write_tables(repo, machine, collect_tables(con, machine))


# -- import -----------------------------------------------------------------------------------------------------

def _other_files(repo: Path, table: str, machine: str) -> list[Path]:
    folder = Path(repo) / table
    return sorted(path for path in folder.glob("*.jsonl") if path.stem != machine) if folder.is_dir() else []


def import_tables(con: duckdb.DuckDBPyConnection, repo: Path, machine: str) -> dict[str, int]:
    """Add the other machines' rows (idempotent, one transaction)."""
    counts = {table: 0 for table in TABLES}
    con.begin()
    try:
        for table in APPEND_ONLY:
            known = {row[0] for row in con.execute(f"select row_id from {table} where row_id is not null").fetchall()}
            for path in _other_files(repo, table, machine):
                columns, rows = _read(path)
                for values in rows:
                    record = dict(zip(columns, values))
                    if not record.get("row_id") or record["row_id"] in known:
                        continue
                    names = [column for column in APPEND_ONLY[table] if column in record]
                    con.execute(f"insert into {table} ({', '.join(names)}) values ({', '.join('?' * len(names))})",
                                [record[name] for name in names])
                    known.add(record["row_id"])
                    counts[table] += 1
        known_sources = {row[0] for row in con.execute("select source from pair_tests").fetchall()}
        for path in _other_files(repo, "pair_tests", machine):
            columns, rows = _read(path)
            for values in rows:
                record = dict(zip(columns, values))
                origin = record.get("origin") or path.stem
                source = record["source"] if str(record["source"]).startswith(f"{origin}:") else f"{origin}:{record['source']}"
                if source in known_sources:
                    continue
                record.update(source=source, origin=origin)
                names = [column for column in TESTS_COLUMNS if column in record]
                con.execute(f"insert into pair_tests ({', '.join(names)}) values ({', '.join('?' * len(names))})",
                            [record[name] for name in names])
                known_sources.add(source)
                counts["pair_tests"] += 1
        local_columns = {row[0] for row in con.execute(
            "select column_name from information_schema.columns where table_name = 'pipeline'").fetchall()}
        for path in _other_files(repo, "pipeline", machine):
            columns, rows = _read(path)
            for values in rows:
                record = {key: value for key, value in zip(columns, values) if key in local_columns}
                key = [record[name] for name in PIPELINE_KEY]
                current = con.execute("select computed_ms from pipeline where exchange = ? and symbol = ? and side = ? "
                                      "and as_of_day = ?", key).fetchone()
                if current is not None and current[0] >= record["computed_ms"]:
                    continue
                if current is not None:
                    con.execute("delete from pipeline where exchange = ? and symbol = ? and side = ? and as_of_day = ?", key)
                names = list(record)
                con.execute(f"insert into pipeline ({', '.join(names)}) values ({', '.join('?' * len(names))})",
                            [record[name] for name in names])
                counts["pipeline"] += 1
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return counts


# -- the two operations -----------------------------------------------------------------------------------------

def sync_pull(con: duckdb.DuckDBPyConnection, repo: Path, machine: str) -> dict:
    """Pull (when a remote is set) and import the other machines' rows; {} when sync is off."""
    if not is_enabled(repo):
        return {"enabled": False}
    with repo_lock(repo):
        pulled = pull(repo)
        return {"enabled": True, "pulled": pulled, "imported": import_tables(con, repo, machine)}


def commit_message(message: str, machine: str, changed: Sequence[str]) -> str:
    return f"{message} ({machine}): {', '.join(changed) or 'no change'}"


def sync_push(con: duckdb.DuckDBPyConnection, repo: Path, machine: str, message: str = "registry export") -> dict:
    """Stamp, export this machine's files, commit and (with a remote) push; off without a repo."""
    if not is_enabled(repo):
        return {"enabled": False}
    with repo_lock(repo):
        stamp_local_rows(con, machine)
        changed = export_tables(con, repo, machine)
        result = commit_and_push(repo, commit_message(message, machine, changed))
    return {"enabled": True, "changed": changed, **result}
