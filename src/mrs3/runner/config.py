from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlparse


class RunnerConfigError(ValueError):
    """Raised when the tester runner configuration is incomplete or unsafe."""


class UnsafePathError(RunnerConfigError):
    """Raised before a destructive operation can target an unexpected path."""


def _resolve_inside(path: Path, root: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise UnsafePathError(f"{label} must be inside bot_root: {resolved}") from error
    if resolved == resolved_root:
        raise UnsafePathError(f"{label} cannot be bot_root itself")
    return resolved


def _resolve_outside(path: Path, root: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return resolved
    raise UnsafePathError(f"{label} must be outside bot_root: {resolved}")


def validate_report_directory(path: Path, bot_root: Path) -> Path:
    resolved = _resolve_inside(path, bot_root, "report_dir")
    suffix = tuple(part.casefold() for part in resolved.parts[-3:])
    if suffix != ("tester", "report", "my_test"):
        raise UnsafePathError(
            "report_dir must end exactly with tester/report/my_test"
        )
    return resolved


def validate_strategy_directory(path: Path, bot_root: Path) -> Path:
    resolved = _resolve_inside(path, bot_root, "strategy_dir")
    expected = (bot_root.resolve() / "settings_strategy").resolve()
    if resolved != expected:
        raise UnsafePathError(
            f"strategy_dir must be the exact bot_root/settings_strategy directory: {expected}"
        )
    return resolved


def _positive_float(raw: dict[str, object], key: str, default: float) -> float:
    value = float(raw.get(key, default))
    if value <= 0:
        raise RunnerConfigError(f"{key} must be greater than zero")
    return value


def _positive_int(raw: dict[str, object], key: str, default: int) -> int:
    value = int(raw.get(key, default))
    if value <= 0:
        raise RunnerConfigError(f"{key} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    bot_root: Path
    executable_path: Path
    base_url: str
    port: int
    strategy_dir: Path
    report_dir: Path
    wizard_result: Path
    wizard_progress: Path
    bot_args: tuple[str, ...] = ()
    request_timeout_seconds: float = 15.0
    startup_timeout_seconds: float = 60.0
    shutdown_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    batch_timeout_seconds: float = 86400.0
    stall_timeout_seconds: float = 1800.0
    max_parallel_submissions: int = 10
    strategy_batch_size: int = 50
    max_strategy_attempts: int = 4
    max_bot_restarts: int = 30
    submission_delay_seconds: float = 0.2
    report_stability_polls: int = 2
    result_report_grace_seconds: float = 15.0
    metric_tolerance: Decimal = Decimal("0.01")
    tester_config: Path = Path()
    inbox_root: Path = Path()

    @classmethod
    def from_json(cls, path: Path) -> RunnerConfig:
        config_path = path.resolve()
        document = json.loads(config_path.read_text(encoding="utf-8"))
        raw = document.get("tester_runner")
        if not isinstance(raw, dict):
            raise RunnerConfigError("config must contain a tester_runner object")

        required = {
            "bot_root",
            "executable",
            "base_url",
            "port",
            "strategy_dir",
            "report_dir",
            "wizard_result",
            "wizard_progress",
            "tester_config",
            "inbox_root",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise RunnerConfigError(f"missing tester_runner fields: {', '.join(missing)}")

        configured_root = Path(str(raw["bot_root"]))
        if not configured_root.is_absolute():
            configured_root = config_path.parent / configured_root
        bot_root = configured_root.resolve()

        def bot_path(key: str) -> Path:
            candidate = Path(str(raw[key]))
            if not candidate.is_absolute():
                candidate = bot_root / candidate
            return _resolve_inside(candidate, bot_root, key)

        executable_path = bot_path("executable")
        strategy_dir = validate_strategy_directory(bot_path("strategy_dir"), bot_root)
        report_dir = validate_report_directory(bot_path("report_dir"), bot_root)
        wizard_result = bot_path("wizard_result")
        wizard_progress = bot_path("wizard_progress")
        tester_config = bot_path("tester_config")
        configured_inbox = Path(str(raw["inbox_root"]))
        if not configured_inbox.is_absolute():
            configured_inbox = config_path.parent / configured_inbox
        inbox_root = _resolve_outside(configured_inbox, bot_root, "inbox_root")
        if executable_path.name.casefold() != "hb_c.exe":
            raise RunnerConfigError("executable must name hb_c.exe")

        port = int(raw["port"])
        if not 1 <= port <= 65535:
            raise RunnerConfigError("port must be between 1 and 65535")
        base_url = str(raw["base_url"]).rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise RunnerConfigError("base_url must be a local http endpoint")
        if parsed.port != port:
            raise RunnerConfigError("base_url port must match configured port")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise RunnerConfigError("base_url must not contain a path, query, or fragment")

        stability_polls = int(raw.get("report_stability_polls", 2))
        if stability_polls < 2:
            raise RunnerConfigError("report_stability_polls must be at least 2")
        metric_tolerance = Decimal(str(raw.get("metric_tolerance", "0.01")))
        if metric_tolerance < 0:
            raise RunnerConfigError("metric_tolerance cannot be negative")

        return cls(
            bot_root=bot_root,
            executable_path=executable_path,
            base_url=base_url,
            port=port,
            strategy_dir=strategy_dir,
            report_dir=report_dir,
            wizard_result=wizard_result,
            wizard_progress=wizard_progress,
            bot_args=tuple(str(value) for value in raw.get("bot_args", ())),
            request_timeout_seconds=_positive_float(raw, "request_timeout_seconds", 15.0),
            startup_timeout_seconds=_positive_float(raw, "startup_timeout_seconds", 60.0),
            shutdown_timeout_seconds=_positive_float(raw, "shutdown_timeout_seconds", 30.0),
            poll_interval_seconds=_positive_float(raw, "poll_interval_seconds", 1.0),
            batch_timeout_seconds=_positive_float(raw, "batch_timeout_seconds", 86400.0),
            stall_timeout_seconds=_positive_float(raw, "stall_timeout_seconds", 1800.0),
            max_parallel_submissions=_positive_int(raw, "max_parallel_submissions", 10),
            strategy_batch_size=_positive_int(raw, "strategy_batch_size", 50),
            max_strategy_attempts=_positive_int(raw, "max_strategy_attempts", 4),
            max_bot_restarts=_positive_int(raw, "max_bot_restarts", 30),
            submission_delay_seconds=_positive_float(raw, "submission_delay_seconds", 0.2),
            report_stability_polls=stability_polls,
            result_report_grace_seconds=_positive_float(raw, "result_report_grace_seconds", 15.0),
            metric_tolerance=metric_tolerance,
            tester_config=tester_config,
            inbox_root=inbox_root,
        )
