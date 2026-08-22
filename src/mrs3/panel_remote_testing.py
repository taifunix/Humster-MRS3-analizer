"""Safe remote testing boundary for the control panel.

It keeps connection material backend-local and limits remote actions to vetted
argv transport, path checks, lifecycle control, and transactional fill.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from typing import Any, Callable

from .panel_testing import render_strategy, render_tester_config


_INVALID = "invalid remote runner configuration"
_INVALID_REQUEST = "invalid remote testing request"
_REMOTE_PATHS = (
    "bot_root",
    "debian_runner_root",
    "reports_root",
    "source_db_root",
    "reports_archive_root",
)
_ALLOWED_CONFIG = frozenset(
    {"host", "user", "port", "password", "private_key_path", "enabled", *_REMOTE_PATHS}
)
_SYMBOL = re.compile(r"^[A-Z0-9]{2,32}$", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STRATEGY_FILENAME = re.compile(r"^[A-Z0-9]{2,32}\.json$", re.ASCII)
_PROGRESS_MARKER = re.compile(
    r"(?<!\d)(\d{1,9})\s*(?:из|of)\s*(\d{1,9})(?!\d)", re.IGNORECASE
)


class RemoteRunnerConfigError(ValueError):
    """A stable, client-safe remote configuration error."""


class RemoteTestingError(ValueError):
    """A stable, client-safe remote operation error."""


def _config_error() -> RemoteRunnerConfigError:
    return RemoteRunnerConfigError(_INVALID)


def _request_error() -> ValueError:
    return ValueError(_INVALID_REQUEST)


def _clean_text(value: object, *, required: bool) -> str:
    if not isinstance(value, str) or _CONTROL.search(value):
        raise _config_error()
    value = value.strip()
    if required and not value:
        raise _config_error()
    return value


def _remote_path(value: object) -> str:
    value = _clean_text(value, required=True)
    if not value.startswith("/") or "\\" in value:
        raise _config_error()
    if any(part in {".", ".."} for part in value.split("/")):
        raise _config_error()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class RemoteRunnerConfig:
    """Validated remote profile; its repr intentionally contains no secrets."""

    host: str
    user: str
    port: int
    password: str
    private_key_path: str
    bot_root: str
    debian_runner_root: str
    reports_root: str
    source_db_root: str
    reports_archive_root: str
    enabled: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RemoteRunnerConfig":
        if not isinstance(raw, Mapping) or not _ALLOWED_CONFIG.issuperset(raw):
            raise _config_error()
        host = _clean_text(raw.get("host"), required=True)
        user = _clean_text(raw.get("user"), required=True)
        port = raw.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise _config_error()
        password = _clean_text(raw.get("password", ""), required=False)
        private_key_path = _clean_text(raw.get("private_key_path", ""), required=False)
        if password and private_key_path:
            raise _config_error()
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _config_error()
        return cls(
            host=host,
            user=user,
            port=port,
            password=password,
            private_key_path=private_key_path,
            bot_root=_remote_path(raw.get("bot_root")),
            debian_runner_root=_remote_path(raw.get("debian_runner_root")),
            reports_root=_remote_path(raw.get("reports_root")),
            source_db_root=_remote_path(raw.get("source_db_root")),
            reports_archive_root=_remote_path(raw.get("reports_archive_root")),
            enabled=enabled,
        )

    @property
    def auth_method(self) -> str:
        if self.password:
            return "password"
        if self.private_key_path:
            return "private_key"
        return "none"

    @property
    def configured(self) -> bool:
        return self.enabled

    def __repr__(self) -> str:
        return (
            f"RemoteRunnerConfig(configured={self.configured!r}, "
            f"auth_method={self.auth_method!r})"
        )


def load_remote_runner_config(raw: Mapping[str, Any]) -> RemoteRunnerConfig:
    """Load either the remote profile itself or a document containing it."""

    if not isinstance(raw, Mapping):
        raise _config_error()
    if "remote_runner" in raw:
        raw = raw.get("remote_runner")
    if not isinstance(raw, Mapping):
        raise _config_error()
    return RemoteRunnerConfig.from_mapping(raw)


def _status(config: RemoteRunnerConfig) -> dict[str, object]:
    return {
        "configured": config.configured,
        "auth_method": config.auth_method,
        "paths": {name: "configured" for name in _REMOTE_PATHS},
        # No remote I/O is permitted in this slice.
        "source_db_root_exists": None,
    }


def remote_testing_status(config: RemoteRunnerConfig | Mapping[str, Any]) -> dict[str, object]:
    """Return a redacted status, failing closed for malformed profiles."""

    try:
        profile = config if isinstance(config, RemoteRunnerConfig) else load_remote_runner_config(config)
    except (RemoteRunnerConfigError, TypeError):
        return {
            "configured": False,
            "auth_method": "none",
            "paths": {name: "unknown" for name in _REMOTE_PATHS},
            "source_db_root_exists": None,
        }
    return _status(profile)


def remote_testing_preflight(config: RemoteRunnerConfig | Mapping[str, Any]) -> dict[str, object]:
    """Validate a profile without contacting the remote runner."""

    status = remote_testing_status(config)
    return {"preflight_ok": status["configured"], **status}


def _normalise_symbols(symbols: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    try:
        selected = tuple(symbols)
    except TypeError:
        raise _request_error() from None
    if not selected:
        raise _request_error()
    normalised: list[str] = []
    for symbol in selected:
        if not isinstance(symbol, str):
            raise _request_error()
        symbol = symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise _request_error()
        normalised.append(symbol)
    if len(set(normalised)) != len(normalised):
        raise _request_error()
    return tuple(normalised)


def _normalise_dates(start: object, end: object) -> tuple[str, str]:
    if not isinstance(start, str) or not isinstance(end, str):
        raise _request_error()
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise _request_error() from None
    if start_date > end_date:
        raise _request_error()
    return start_date.isoformat(), end_date.isoformat()


def report_archive_folder(symbols: Iterable[str], start: str, end: str) -> str:
    """Build a path-component-only archive name from an already-valid request."""

    trimmed = [symbol.removesuffix("USDT") for symbol in symbols]
    if any(not symbol for symbol in trimmed):
        raise _request_error()
    return f"{'_'.join(trimmed)}_{start}_{end}"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _remote_binary_paths(config: RemoteRunnerConfig) -> tuple[str, str]:
    root = config.bot_root.rstrip("/") or "/"
    return f"{root}/hb_c", f"{root}/.mrs3-panel-tester.log"


def _check_paths_script(config: RemoteRunnerConfig) -> str:
    lines = ["set -u"]
    for name in _REMOTE_PATHS:
        lines.append(
            f"test -d {_shell_quote(getattr(config, name))} "
            "&& printf '1\\n' || printf '0\\n'"
        )
    return "\n".join(lines)


def _lifecycle_script(config: RemoteRunnerConfig, *, action: str) -> str:
    binary, log = _remote_binary_paths(config)
    quoted_binary = _shell_quote(binary)
    lines = [
        "set -u",
        f"bin={quoted_binary}",
        "if ! command -v pgrep >/dev/null 2>&1 || ! command -v readlink >/dev/null 2>&1; then",
        "  printf 'VERIFY_FAILED\\n'; exit 0",
        "fi",
        "if ! resolved=$(readlink -f -- \"$bin\" 2>/dev/null); then",
        "  printf 'VERIFY_FAILED\\n'; exit 0",
        "fi",
        "if [ \"$resolved\" != \"$bin\" ] || [ ! -x \"$bin\" ]; then",
        "  printf 'VERIFY_FAILED\\n'; exit 0",
        "fi",
        "pids=$(pgrep -x hb_c 2>/dev/null); pgrep_status=$?",
        "if [ \"$pgrep_status\" -gt 1 ]; then",
        "  printf 'VERIFY_FAILED\\n'; exit 0",
        "fi",
        "matches=",
        "for pid in $pids; do",
        "  if ! exe=$(readlink -f -- \"/proc/$pid/exe\" 2>/dev/null); then",
        "    printf 'VERIFY_FAILED\\n'; exit 0",
        "  fi",
        "  if [ \"$exe\" = \"$bin\" ]; then matches=\"$matches $pid\"; fi",
        "done",
    ]
    if action == "start":
        lines.extend(
            [
                "if [ -n \"$matches\" ]; then printf 'RUNNING\\n'; exit 0; fi",
                f"nohup \"$bin\" >{_shell_quote(log)} 2>&1 </dev/null &",
                "printf 'STARTED\\n'",
            ]
        )
    else:
        lines.extend(
            [
                "if [ -z \"$matches\" ]; then printf 'NOT_RUNNING\\n'; exit 0; fi",
                "for pid in $matches; do",
                "  if ! kill -TERM \"$pid\" 2>/dev/null; then printf 'STOP_FAILED\\n'; exit 0; fi",
                "done",
                "printf 'STOPPED\\n'",
            ]
        )
    return "\n".join(lines)


def _plink_argv(config: RemoteRunnerConfig, script: str) -> tuple[str, ...]:
    argv = ["plink", "-batch", "-ssh", "-P", str(config.port)]
    if config.password:
        argv.extend(("-pw", config.password))
    elif config.private_key_path:
        argv.extend(("-i", config.private_key_path))
    argv.extend((f"{config.user}@{config.host}", script))
    return tuple(argv)


def _default_command_runner(argv: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except Exception:
        raise RemoteTestingError("remote command failed") from None
    if completed.returncode != 0:
        raise RemoteTestingError("remote command failed")
    return completed.stdout


def _default_file_uploader(
    local_path: Path, remote_path: str, config: RemoteRunnerConfig
) -> None:
    argv = ["pscp", "-batch", "-P", str(config.port)]
    if config.password:
        argv.extend(("-pw", config.password))
    elif config.private_key_path:
        argv.extend(("-i", config.private_key_path))
    argv.extend((str(local_path), f"{config.user}@{config.host}:{remote_path}"))
    try:
        completed = subprocess.run(
            tuple(argv),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except Exception:
        raise RemoteTestingError("remote upload failed") from None
    if completed.returncode != 0:
        raise RemoteTestingError("remote upload failed")


def _remote_child(config: RemoteRunnerConfig, name: str) -> str:
    root = config.bot_root.rstrip("/") or "/"
    return f"{root}/{name}"


def _fill_script(
    config: RemoteRunnerConfig, token: str, strategy_filename: str
) -> str:
    bot_root = _shell_quote(config.bot_root)
    config_path = _shell_quote(_remote_child(config, "config_tester.json"))
    strategy_dir = _shell_quote(_remote_child(config, "settings_strategy"))
    strategy_path = _shell_quote(
        _remote_child(config, f"settings_strategy/{strategy_filename}")
    )
    upload_config = _shell_quote(
        _remote_child(config, f".mrs3-panel-upload-{token}.config")
    )
    upload_strategy = _shell_quote(
        _remote_child(config, f".mrs3-panel-upload-{token}.strategy")
    )
    stage = _shell_quote(_remote_child(config, f".mrs3-panel-stage-{token}"))
    backup = _shell_quote(_remote_child(config, f".mrs3-panel-backup-{token}"))
    backup_strategy_dir = _shell_quote(
        _remote_child(config, f".mrs3-panel-backup-{token}/strategies")
    )
    stage_config = _shell_quote(
        _remote_child(config, f".mrs3-panel-stage-{token}/config_tester.json")
    )
    stage_strategy = _shell_quote(
        _remote_child(config, f".mrs3-panel-stage-{token}/{strategy_filename}")
    )
    lines = [
        "set -u",
        f"bot={bot_root}",
        f"config={config_path}",
        f"strategy_dir={strategy_dir}",
        f"strategy_path={strategy_path}",
        f"upload_config={upload_config}",
        f"upload_strategy={upload_strategy}",
        f"stage={stage}",
        f"backup={backup}",
        f"backup_strategy_dir={backup_strategy_dir}",
        f"stage_config={stage_config}",
        f"stage_strategy={stage_strategy}",
        "stage_created=0",
        "backup_created=0",
        "config_moved=0",
        "config_installed=0",
        "strategy_installed=0",
        "cleanup_uploads() { rm -f \"$upload_config\" \"$upload_strategy\"; }",
        "restore() {",
        "  if [ \"$config_installed\" -eq 1 ]; then rm -f \"$config\"; fi",
        "  if [ \"$config_moved\" -eq 1 ] && [ -f \"$backup/config_tester.json\" ]; then mv \"$backup/config_tester.json\" \"$config\" || true; fi",
        "  if [ \"$strategy_installed\" -eq 1 ]; then rm -f \"$strategy_path\"; fi",
        "  for old in \"$backup_strategy_dir\"/*.json; do [ -e \"$old\" ] || continue; mv \"$old\" \"$strategy_dir/\" || true; done",
        "  if [ \"$stage_created\" -eq 1 ]; then rm -rf \"$stage\"; fi",
        "  if [ \"$backup_created\" -eq 1 ]; then rm -rf \"$backup\"; fi",
        "  cleanup_uploads",
        "}",
        "fail() { restore; printf 'FAILED\\n'; exit 0; }",
        "if ! command -v readlink >/dev/null 2>&1 || ! command -v find >/dev/null 2>&1; then cleanup_uploads; printf 'FAILED\\n'; exit 0; fi",
        "[ -d \"$bot\" ] || fail",
        "if ! bot_resolved=$(readlink -f -- \"$bot\" 2>/dev/null); then fail; fi",
        "[ \"$bot_resolved\" = \"$bot\" ] || fail",
        "[ -d \"$strategy_dir\" ] || fail",
        "if ! strategy_resolved=$(readlink -f -- \"$strategy_dir\" 2>/dev/null); then fail; fi",
        "[ \"$strategy_resolved\" = \"$bot/settings_strategy\" ] || fail",
        "[ -f \"$upload_config\" ] && [ ! -L \"$upload_config\" ] || fail",
        "[ -f \"$upload_strategy\" ] && [ ! -L \"$upload_strategy\" ] || fail",
        "for old in \"$strategy_dir\"/*.json; do",
        "  [ -e \"$old\" ] || continue",
        "  [ -f \"$old\" ] && [ ! -L \"$old\" ] || fail",
        "done",
        "mkdir \"$stage\" || fail",
        "stage_created=1",
        "mkdir \"$backup\" || fail",
        "backup_created=1",
        "mkdir \"$backup_strategy_dir\" || fail",
        "if [ -e \"$config\" ]; then [ -f \"$config\" ] && [ ! -L \"$config\" ] || fail; mv \"$config\" \"$backup/config_tester.json\" || fail; config_moved=1; fi",
        "mv \"$upload_config\" \"$stage_config\" || fail",
        "mv \"$upload_strategy\" \"$stage_strategy\" || fail",
        "for old in \"$strategy_dir\"/*.json; do [ -e \"$old\" ] || continue; mv \"$old\" \"$backup_strategy_dir/\" || fail; done",
        "mv \"$stage_strategy\" \"$strategy_path\" || fail",
        "strategy_installed=1",
        "mv \"$stage_config\" \"$config\" || fail",
        "config_installed=1",
        "count=$(find \"$strategy_dir\" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l)",
        "[ \"$count\" -eq 1 ] && [ -f \"$strategy_path\" ] && [ ! -L \"$strategy_path\" ] || fail",
        "rm -rf \"$stage\" \"$backup\" || { printf 'FAILED\\n'; exit 0; }",
        "cleanup_uploads",
        "printf 'FILLED\\n'",
    ]
    return "\n".join(lines)


def _progress_script(config: RemoteRunnerConfig) -> str:
    _, log = _remote_binary_paths(config)
    return f"tail -n 200 -- {_shell_quote(log)} 2>/dev/null || true"


def _progress_document(output: str) -> dict[str, object]:
    for match in reversed(tuple(_PROGRESS_MARKER.finditer(output[-65536:]))):
        current, total = int(match.group(1)), int(match.group(2))
        percent = round(current * 100 / total, 2) if total else None
        return {
            "current": current,
            "total": total,
            "percent": percent,
            "elapsed": None,
            "message": f"{current}/{total}",
        }
    return {"current": 0, "total": 0, "percent": None, "elapsed": None, "message": None}


def prepare_request(
    *,
    symbols: str | Iterable[str],
    side: str,
    start: str,
    end: str,
) -> dict[str, object]:
    """Validate and normalize a remote request without embedding remote data."""

    selected = _normalise_symbols(symbols)
    if not isinstance(side, str):
        raise _request_error()
    side = side.strip().upper()
    if side not in {"LONG", "SHORT"}:
        raise _request_error()
    start, end = _normalise_dates(start, end)
    return {
        "symbols": list(selected),
        "side": side,
        "start": start,
        "end": end,
        "report_archive_folder": report_archive_folder(selected, start, end),
    }


class RemoteTestingService:
    """Validated remote panel boundary with injectable, argv-only execution."""

    def __init__(
        self,
        config: RemoteRunnerConfig | Mapping[str, Any],
        command_runner: Callable[[tuple[str, ...]], str] | None = None,
        file_uploader: Callable[[Path, str, RemoteRunnerConfig], None] | None = None,
    ) -> None:
        self.config = config if isinstance(config, RemoteRunnerConfig) else load_remote_runner_config(config)
        if command_runner is not None and not callable(command_runner):
            raise RemoteTestingError("remote command runner unavailable")
        if file_uploader is not None and not callable(file_uploader):
            raise RemoteTestingError("remote uploader unavailable")
        self._command_runner = command_runner or _default_command_runner
        self._file_uploader = file_uploader or _default_file_uploader

    def status(self) -> dict[str, object]:
        return _status(self.config)

    def preflight(self) -> dict[str, object]:
        return {"preflight_ok": self.config.configured, **self.status()}

    def _run(self, script: str) -> str:
        try:
            output = self._command_runner(_plink_argv(self.config, script))
        except Exception:
            raise RemoteTestingError("remote command failed") from None
        if not isinstance(output, str):
            raise RemoteTestingError("remote command failed")
        return output

    def check_paths(self) -> dict[str, object]:
        """Check all configured remote directories with one read-only command."""

        output = self._run(_check_paths_script(self.config))
        values = output.split()
        if len(values) != len(_REMOTE_PATHS) or any(value not in {"0", "1"} for value in values):
            raise RemoteTestingError("remote command failed")
        paths = dict(zip(_REMOTE_PATHS, (value == "1" for value in values)))
        return {"paths": paths, "source_db_root_exists": paths["source_db_root"]}

    def _lifecycle(self, action: str) -> dict[str, object]:
        output = self._run(_lifecycle_script(self.config, action=action)).strip()
        if output in {"RUNNING", "STARTED", "NOT_RUNNING", "STOPPED"}:
            return {"state": output}
        if output in {"VERIFY_FAILED", "STOP_FAILED"}:
            return {"state": "FAILED"}
        raise RemoteTestingError("remote command failed")

    def start(self) -> dict[str, object]:
        return self._lifecycle("start")

    def stop(self) -> dict[str, object]:
        return self._lifecycle("stop")

    def fill(
        self,
        request: Mapping[str, object],
        tester_template: str | None = None,
        strategy_template: str | None = None,
        file_uploader: Callable[[Path, str, RemoteRunnerConfig], None] | None = None,
        *,
        config_template: str | None = None,
    ) -> dict[str, object]:
        """Render, upload, and atomically install one remote testing batch."""

        if not isinstance(request, Mapping):
            raise RemoteTestingError(_INVALID_REQUEST)
        if tester_template is None:
            tester_template = config_template
        if not isinstance(tester_template, str) or not isinstance(strategy_template, str):
            raise RemoteTestingError("invalid remote testing template")
        try:
            normalized = prepare_request(
                symbols=request.get("symbols"),
                side=request.get("side"),
                start=request.get("start"),
                end=request.get("end"),
            )
        except Exception:
            raise RemoteTestingError(_INVALID_REQUEST) from None
        try:
            symbols = tuple(normalized["symbols"])
            side = str(normalized["side"])
            start = str(normalized["start"])
            end = str(normalized["end"])
            rendered_config = render_tester_config(tester_template, symbols, start, end)
            strategy_filename, strategy_document = render_strategy(
                strategy_template, symbols[0], side
            )
            if not _STRATEGY_FILENAME.fullmatch(strategy_filename):
                raise ValueError
            strategy_text = json.dumps(strategy_document, ensure_ascii=False, indent=2) + "\n"
        except Exception:
            raise RemoteTestingError("invalid remote testing template") from None

        uploader = file_uploader or self._file_uploader
        if not callable(uploader):
            raise RemoteTestingError("remote uploader unavailable")
        token = secrets.token_hex(8)
        remote_config = _remote_child(self.config, f".mrs3-panel-upload-{token}.config")
        remote_strategy = _remote_child(self.config, f".mrs3-panel-upload-{token}.strategy")
        try:
            with tempfile.TemporaryDirectory(prefix="mrs3-remote-fill-") as temporary:
                root = Path(temporary)
                local_config = root / "config_tester.json"
                local_strategy = root / strategy_filename
                local_config.write_text(rendered_config, encoding="utf-8")
                local_strategy.write_text(strategy_text, encoding="utf-8")
                try:
                    uploader(local_config, remote_config, self.config)
                    uploader(local_strategy, remote_strategy, self.config)
                except Exception:
                    self._cleanup_uploads(remote_config, remote_strategy)
                    raise RemoteTestingError("remote upload failed") from None
                output = self._run(_fill_script(self.config, token, strategy_filename)).strip()
        except RemoteTestingError:
            raise
        except Exception:
            raise RemoteTestingError("remote fill failed") from None
        if output != "FILLED":
            raise RemoteTestingError("remote fill failed")
        return {
            "state": "FILLED",
            "strategy_name": strategy_filename.removesuffix(".json"),
            "symbols": list(symbols),
            "side": side,
            "report_archive_folder": normalized["report_archive_folder"],
        }

    def _cleanup_uploads(self, remote_config: str, remote_strategy: str) -> None:
        script = "rm -f -- " + _shell_quote(remote_config) + " " + _shell_quote(remote_strategy)
        try:
            self._run(script)
        except RemoteTestingError:
            pass

    def read_progress(self) -> dict[str, object]:
        """Read only bounded progress markers from the fixed tester log."""

        return _progress_document(self._run(_progress_script(self.config)))

    @staticmethod
    def prepare_request(**kwargs: object) -> dict[str, object]:
        return prepare_request(**kwargs)
