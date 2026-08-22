from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mrs3.panel_source_db as source_db
from mrs3.panel_source_db import LocalSourceDbService


def _import_preflight(tmp_path: Path) -> SimpleNamespace:
    root = (tmp_path / "reports").resolve()
    target = (tmp_path / "out" / "source.duckdb").resolve()
    return SimpleNamespace(
        token="import-token",
        root_path=root,
        database_path=target,
        snapshots=(
            SimpleNamespace(
                input_ordinal=0,
                path=root / "nested" / "first.html",
                relative_path="nested/first.html",
                source_size=12,
                source_mtime_ns=34,
            ),
        ),
    )


def _merge_preflight(tmp_path: Path) -> SimpleNamespace:
    inputs = tuple((tmp_path / name).resolve() for name in ("one.duckdb", "two.duckdb"))
    target = (tmp_path / "merged.duckdb").resolve()
    return SimpleNamespace(
        token="merge-token",
        input_paths=inputs,
        target_path=target,
        input_identities=((1, 2, "a"), (3, 4, "b")),
    )


def test_import_preflight_is_redacted_and_execute_requires_latest_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _import_preflight(tmp_path)
    monkeypatch.setattr(source_db, "preflight_source_v6", lambda root, target: preflight)
    calls: list[dict[str, object]] = []

    def fake_import(root: Path, target: Path, **kwargs: object) -> object:
        calls.append({"root": root, "target": target, **kwargs})
        return SimpleNamespace(status="COMMITTED", target_path=target)

    monkeypatch.setattr(source_db, "import_source_v6", fake_import)
    service = LocalSourceDbService(workers=3)

    document = service.preflight_import(tmp_path / "reports", tmp_path / "out" / "source.duckdb")

    assert document["phase"] == "PREFLIGHT_READY"
    assert document["token"] == "import-token"
    assert document["total"] == 1
    assert document["snapshots"] == [
        {"ordinal": 0, "relative_path": "nested/first.html", "size": 12, "mtime_ns": 34}
    ]
    assert str(tmp_path) not in json.dumps(document)
    with pytest.raises(ValueError, match="latest Source DB import preflight token"):
        service.execute_import("stale-token")

    result = service.execute_import("import-token")

    assert result.status == "COMMITTED"
    assert calls == [{
        "root": preflight.root_path,
        "target": preflight.database_path,
        "preflight": preflight,
        "workers": 3,
    }]


def test_merge_preflight_is_redacted_and_execute_keeps_inputs_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _merge_preflight(tmp_path)
    monkeypatch.setattr(source_db, "preflight_source_v6_merge", lambda inputs, target: preflight)
    calls: list[dict[str, object]] = []

    def fake_merge(inputs: tuple[Path, ...], target: Path, **kwargs: object) -> object:
        calls.append({"inputs": inputs, "target": target, **kwargs})
        return SimpleNamespace(status="COMMITTED", target_path=target)

    monkeypatch.setattr(source_db, "merge_source_v6", fake_merge)
    service = LocalSourceDbService(workers=2)

    document = service.preflight_merge(
        (tmp_path / "one.duckdb", tmp_path / "two.duckdb"), tmp_path / "merged.duckdb"
    )

    assert document["phase"] == "PREFLIGHT_READY"
    assert document["token"] == "merge-token"
    assert document["total"] == 2
    assert document["inputs"] == ["one.duckdb", "two.duckdb"]
    assert document["target"] == "merged.duckdb"
    assert str(tmp_path) not in json.dumps(document)
    result = service.execute_merge("merge-token")

    assert result.status == "COMMITTED"
    assert calls == [{
        "inputs": preflight.input_paths,
        "target": preflight.target_path,
        "preflight": preflight,
        "workers": 2,
    }]


def test_service_does_not_expose_surface_publication_or_remote_operations() -> None:
    service = LocalSourceDbService()

    assert not hasattr(service, "publish_surface")
    assert not hasattr(service, "remote_import")
