"""Safe rendering of the tester's JSONC-style configuration template."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Callable, Protocol

from .locking import TesterTargetLock
from .runner.config import RunnerConfig
from .runner.files import (
    TesterSettingsSnapshot,
    capture_tester_settings,
    prepare_batch_files,
    restore_tester_settings,
    validate_runner_paths,
)
from .runner.process import start_bot, stop_bot
from .runner.http import TesterHttpClient
from .runner.workflow import validate_runtime_preflight


class PanelTestingError(ValueError):
    pass


class TesterRunClient(Protocol):
    def run_tester(self) -> None: ...
    def tester_status(self) -> str: ...
    def close(self) -> None: ...


_SYMBOL = re.compile(r"^[A-Z0-9]{2,32}$")
_TEMPLATES = {
    "LONG": ("templates/tester/mrs2/config_tester_long.json", "templates/strategies/source-v6-mrs2/long.json"),
    "SHORT": ("templates/tester/mrs2/config_tester_short.json", "templates/strategies/source-v6-mrs2/short.json"),
}


def mrs3_tester_config_template(repo_root: Path | None = None) -> Path:
    root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    return root / "templates/tester/mrs3/config_tester.json"


def _without_trailing_commas(text: str) -> str:
    output: list[str] = []
    quoted = escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quoted:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
        if character == ",":
            next_index = index + 1
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            if next_index < len(text) and text[next_index] in "]}":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def render_tester_config(
    template: str,
    symbols: tuple[str, ...],
    start: str,
    end: str,
    *,
    max_parallel_runs: int | None = None,
) -> str:
    if not isinstance(template, str) or not isinstance(symbols, tuple) or not symbols:
        raise PanelTestingError("invalid tester configuration")
    if max_parallel_runs is not None and (type(max_parallel_runs) is not int or max_parallel_runs <= 0):
        raise PanelTestingError("invalid tester configuration")
    clean_symbols = tuple(symbol.strip().upper() for symbol in symbols)
    if len(set(clean_symbols)) != len(clean_symbols) or any(not _SYMBOL.fullmatch(symbol) for symbol in clean_symbols):
        raise PanelTestingError("invalid tester configuration")
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        document = json.loads(_without_trailing_commas(template))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise PanelTestingError("invalid tester configuration") from None
    if not isinstance(document, dict) or start_date > end_date:
        raise PanelTestingError("invalid tester configuration")
    mining = document.get("parameter_mining")
    if not isinstance(mining, list):
        raise PanelTestingError("invalid tester configuration")
    targets = [entry for entry in mining if isinstance(entry, dict) and entry.get("name") == "settings[*].basic.symbol"]
    if len(targets) != 1:
        raise PanelTestingError("invalid tester configuration")
    document["StartDate"] = f"{start_date.isoformat()}T00:00:00"
    document["EndDate"] = f"{end_date.isoformat()}T00:00:00"
    if max_parallel_runs is not None:
        document["max_parallel_runs"] = max_parallel_runs
    targets[0]["values"] = list(clean_symbols)
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def render_strategy(template: str, symbol: str, side: str) -> tuple[str, dict]:
    if not isinstance(template, str) or not isinstance(symbol, str) or side not in {"LONG", "SHORT"}:
        raise PanelTestingError("invalid strategy template")
    symbol = symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise PanelTestingError("invalid strategy template")
    try:
        document = json.loads(template)
    except (TypeError, json.JSONDecodeError):
        raise PanelTestingError("invalid strategy template") from None
    basic = document.get("basic") if isinstance(document, dict) else None
    name = document.get("name") if isinstance(document, dict) else None
    if not isinstance(basic, dict) or not isinstance(name, str) or not name.strip():
        raise PanelTestingError("invalid strategy template")
    basic["symbol"] = symbol
    basic["use_long"] = side == "LONG"
    basic["use_short"] = side == "SHORT"
    return f"{name}.json", document


@dataclass(frozen=True, slots=True)
class LocalTestingPreparation:
    """Files staged for a future local tester run; no bot files are touched."""

    tester_config: Path
    strategy_source: Path
    strategy_name: str
    side: str
    symbols: tuple[str, ...]
    start: str
    end: str

    def as_dict(self) -> dict[str, object]:
        return {
            "tester_config": self.tester_config.name,
            "strategy_source": self.strategy_source.name,
            "strategy_name": self.strategy_name,
            "side": self.side,
            "symbols": list(self.symbols),
            "start": self.start,
            "end": self.end,
        }


class LocalTestingService:
    """Read-only local preflight and staging adapter for the control panel."""

    def __init__(
        self,
        config: RunnerConfig,
        repo_root: Path,
        *,
        install_batch: Callable[..., object] = prepare_batch_files,
        start_bot: Callable[[RunnerConfig], object] = start_bot,
        stop_bot: Callable[[RunnerConfig], object] = stop_bot,
        client_factory: Callable[[RunnerConfig], TesterRunClient] = lambda config: TesterHttpClient(
            config.base_url, timeout=config.request_timeout_seconds
        ),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.repo_root = repo_root.resolve()
        self._install_batch = install_batch
        self._start_bot = start_bot
        self._stop_bot = stop_bot
        self._client_factory = client_factory
        self._sleep = sleep
        self._target_owner: TesterTargetLock | None = None
        self._target_snapshot: TesterSettingsSnapshot | None = None

    def status(self) -> dict[str, object]:
        try:
            validate_runtime_preflight(self.config)
        except Exception:
            preflight_ok = False
        else:
            preflight_ok = True

        bot_root = self.config.bot_root
        disk_root = bot_root if bot_root.exists() else self.config.inbox_root
        if not disk_root.exists():
            disk_root = self.repo_root
        try:
            disk_free_bytes = int(shutil.disk_usage(disk_root).free)
        except OSError:
            disk_free_bytes = 0
        return {
            "preflight_ok": preflight_ok,
            "bot": {
                "exists": bot_root.is_dir(),
                "executable": self.config.executable_path.is_file(),
            },
            "report": {"exists": self.config.report_dir.is_dir()},
            "strategy": {"exists": self.config.strategy_dir.is_dir()},
            "disk_free_bytes": disk_free_bytes,
        }

    def prepare(
        self,
        *,
        side: str,
        symbols: tuple[str, ...] | list[str],
        start: str,
        end: str,
        output_dir: Path | None = None,
    ) -> LocalTestingPreparation:
        validate_runtime_preflight(self.config)
        side = side.strip().upper() if isinstance(side, str) else ""
        if side not in _TEMPLATES:
            raise PanelTestingError("invalid testing side")
        selected = tuple(symbols)
        if not selected:
            raise PanelTestingError("at least one symbol is required")

        config_name, strategy_name = _TEMPLATES[side]
        config_template = (self.repo_root / config_name).read_text(encoding="utf-8")
        strategy_template = (self.repo_root / strategy_name).read_text(encoding="utf-8")
        rendered_config = render_tester_config(
            config_template,
            selected,
            start,
            end,
            max_parallel_runs=self.config.max_parallel_submissions,
        )
        filename, strategy = render_strategy(strategy_template, selected[0], side)

        workspace = (self.config.inbox_root / "panel-testing").resolve()
        destination = (output_dir or workspace).resolve()
        if destination != workspace:
            raise PanelTestingError("staging output must be the panel workspace")
        try:
            destination.relative_to(self.config.bot_root.resolve())
        except ValueError:
            pass
        else:
            raise PanelTestingError("staging output must be outside bot_root")
        destination.mkdir(parents=True, exist_ok=True)
        destination = Path(
            tempfile.mkdtemp(prefix="mrs3-testing-", dir=destination)
        )
        strategy_source = destination / "strategies"
        strategy_source.mkdir()
        tester_config = destination / "tester_config.json"
        _atomic_write(tester_config, rendered_config)
        _atomic_write(
            strategy_source / filename,
            json.dumps(strategy, ensure_ascii=False, indent=2) + "\n",
        )
        return LocalTestingPreparation(
            tester_config=tester_config,
            strategy_source=strategy_source,
            strategy_name=filename.removesuffix(".json"),
            side=side,
            symbols=tuple(symbol.strip().upper() for symbol in selected),
            start=date.fromisoformat(start).isoformat(),
            end=date.fromisoformat(end).isoformat(),
        )

    def fill(
        self,
        *,
        side: str,
        symbols: tuple[str, ...] | list[str],
        start: str,
        end: str,
        delete_old_reports: bool = False,
    ) -> dict[str, object]:
        """Install the requested single strategy and rendered tester config."""
        if not isinstance(delete_old_reports, bool):
            raise PanelTestingError("delete_old_reports must be a boolean")
        prepared = self.prepare(side=side, symbols=symbols, start=start, end=end)
        owner: TesterTargetLock | None = None
        target_quiesced = True
        try:
            if self._target_owner is not None:
                raise PanelTestingError("tester target is already owned")
            owner = TesterTargetLock(self.config.bot_root).acquire()
            try:
                self._stop_bot(self.config)
            except BaseException:
                target_quiesced = False
                raise
            self._target_snapshot = capture_tester_settings(self.config)
            if delete_old_reports:
                _clear_report_contents(self.config)
            _atomic_write(
                self.config.tester_config,
                prepared.tester_config.read_text(encoding="utf-8"),
            )
            self._install_batch(
                self.config,
                prepared.strategy_source,
                selected_names=(prepared.strategy_name,),
                preserve_raw_artifacts=True,
            )
            self._target_owner = owner
            result = prepared.as_dict()
            result["reports_cleared"] = delete_old_reports
            return result
        except BaseException:
            if owner is not None:
                self._target_owner = owner
                if target_quiesced:
                    if self._target_snapshot is not None:
                        restore_tester_settings(self.config, self._target_snapshot)
                        self._target_snapshot = None
                    owner.release()
                    self._target_owner = None
            raise
        finally:
            shutil.rmtree(prepared.tester_config.parent, ignore_errors=True)

    def start(self) -> dict[str, str]:
        validate_runtime_preflight(self.config)
        owner = self._target_owner or TesterTargetLock(self.config.bot_root).acquire()
        bot_started = False
        client: TesterRunClient | None = None
        try:
            self._start_bot(self.config)
            bot_started = True
            self._sleep(self.config.request_timeout_seconds)
            client = self._client_factory(self.config)
            client.run_tester()
            tester_status = _safe_tester_status(client.tester_status())
        except BaseException:
            if bot_started:
                try:
                    self._stop_bot(self.config)
                except BaseException:
                    pass
            self._target_owner = owner
            raise
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._target_owner = owner
        return {"state": "STARTED", "tester_status": tester_status}

    def stop(self) -> dict[str, str]:
        validate_runtime_preflight(self.config)
        owner = self._target_owner
        temporary = owner is None
        if temporary:
            owner = TesterTargetLock(self.config.bot_root).acquire()
        try:
            self._stop_bot(self.config)
        except BaseException:
            self._target_owner = owner
            raise
        if self._target_snapshot is not None:
            restore_tester_settings(self.config, self._target_snapshot)
            self._target_snapshot = None
        owner.release()
        self._target_owner = None
        return {"state": "STOPPED"}


def _atomic_write(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _clear_report_contents(config: RunnerConfig) -> None:
    """Empty only the validated report directory without following links."""
    _, report_dir, _, _ = validate_runner_paths(config)
    if not report_dir.is_dir() or report_dir.is_symlink():
        raise PanelTestingError("tester report directory is not a regular directory")
    entries = tuple(report_dir.iterdir())
    unsafe = [
        entry
        for entry in entries
        if entry.is_symlink()
        or getattr(entry, "is_junction", lambda: False)()
        or not (entry.is_file() or entry.is_dir())
    ]
    if unsafe:
        raise PanelTestingError("tester report directory contains an unsupported entry")
    for entry in entries:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _safe_tester_status(value: str) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip().casefold()
    for status in ("idle", "running", "starting", "stopping"):
        if re.search(rf"\b{status}\b", text):
            return status.upper()
    return "UNKNOWN"
