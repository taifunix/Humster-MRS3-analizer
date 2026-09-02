"""Small shared helpers for removing local file details from client errors."""

from __future__ import annotations

import re


_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/][^\s\"')]*"),
    re.compile(r"\\\\[^\s\"')]+"),
    re.compile(r"(?<!\w)[\\/][^\s\"')]+"),
    re.compile(r"\b(?:data|var|tmp|config|surfaces|cache)(?:[\\/][^\s\"')]+)+", re.IGNORECASE),
    re.compile(r"\b[\w.-]+\.(?:duckdb|json|ya?ml|csv|xlsx|html|txt|log)\b", re.IGNORECASE),
)


def has_local_path(message: str) -> bool:
    return any(pattern.search(message) for pattern in _PATH_PATTERNS)


def redact_local_paths(message: str, replacement: str = "<local-path>") -> str:
    for pattern in _PATH_PATTERNS:
        message = pattern.sub(replacement, message)
    return message
