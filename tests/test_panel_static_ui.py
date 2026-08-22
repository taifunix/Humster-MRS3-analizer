from pathlib import Path
import re


PANEL_WEB = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web"


def _read(name: str) -> str:
    return (PANEL_WEB / name).read_text(encoding="utf-8")


def test_static_shell_starts_with_all_accordions_collapsed_and_status_in_header() -> None:
    html = _read("index.html")

    assert not re.search(r"<details\b[^>]*\bopen(?:\s|>)", html)
    assert "details.open = true" not in _read("app.js")
    assert "disk_free_bytes" in _read("app.js")
    header = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert 'id="status"' in header
    assert 'class="global-status topbar-status"' in header


def test_static_shell_has_approved_navigation_and_exclusions() -> None:
    html = _read("index.html")

    for href, label in (
        ("#testing", "Тестирование"),
        ("#source-db", "Source DB"),
        ("#surfaces", "Поверхности"),
        ("#strategies-dd5", "Стратегии и DD5"),
        ("#settings", "Настройки"),
    ):
        assert f'href="{href}"' in html
        assert label in html
    assert ">Portfolio" in html
    assert 'disabled' in html
    assert 'aria-disabled="true"' in html
    assert 'tabindex="-1"' in html
    assert 'id="artefacts"' not in html
    assert "CSV" not in html
    assert "DUCKDB_DIRECT" not in html


def test_static_shell_does_not_claim_unverified_artifacts() -> None:
    html = _read("index.html")

    assert "18 READY" not in html
    assert "2 / 2 scopes" not in html
    assert "selected-set.surface-v6.duckdb · READY scopes" not in html
    assert "CXUSDT · SHORT</th><td>1h" not in html


def test_testing_screen_has_two_independent_runner_cards_without_ssh_fields() -> None:
    html = _read("index.html")

    assert 'id="runner-local"' in html
    assert 'id="runner-remote"' in html
    for runner in ("local", "remote"):
        assert f'id="{runner}-pair"' in html
        assert f'id="{runner}-side"' in html
        assert f'id="{runner}-start-date"' in html
        assert f'id="{runner}-end-date"' in html
        assert f'id="{runner}-paths"' in html
    assert "Проверить runner и диск" in html
    assert "Сохранить пути" in html
    assert "Запуск ожидает backend" in html
    assert 'name="host"' not in html
    assert 'name="user"' not in html
    assert "ssh" not in html.lower()


def test_source_surfaces_and_strategies_screens_have_approved_workflow_cards() -> None:
    html = _read("index.html")

    source = html.split('id="source-db"', 1)[1].split('id="surfaces"', 1)[0]
    assert source.count("<details") == 3
    for label in ("Локальный импорт", "Удалённый импорт", "Локальный merge"):
        assert label in source

    surfaces = html.split('id="surfaces"', 1)[1].split('id="strategies-dd5"', 1)[0]
    for label in ("Источник", "Preflight", "READY", "Публикация"):
        assert label in surfaces
    assert 'href="#gaps"' in surfaces
    assert "n/r - Check gaps" in surfaces

    strategies = html.split('id="strategies-dd5"', 1)[1].split('id="settings"', 1)[0]
    for label in (
        "Опубликованная surface",
        "Анализ",
        "Shortlist",
        "Tester",
        "DD5",
        "CALCULATION_ONLY",
    ):
        assert label in strategies
    assert "Source PnL" in strategies


def test_settings_semantic_ids_and_static_js_use_v2_testing_endpoints() -> None:
    html = _read("index.html")
    js = _read("app.js")

    assert '<section id="settings"' in html
    assert '<form' in html
    assert 'for="settings-default-root"' in html
    assert 'id="settings-default-root"' in html
    assert 'aria-live="polite"' in html
    assert 'value="legacy"' in html
    assert 'value="static"' in html
    assert 'fetch("/api/v2/bootstrap")' in js
    assert 'fetch("/api/v2/testing/local/status")' in js
    assert 'fetch("/api/v2/testing/remote/status")' in js
    assert 'fetch("/api/v2/testing/local/fill"' in js
    assert '`/api/v2/testing/local/${action}`' in js
    assert "/api/ui/" not in js
    assert "duckdb-direct" not in js
    assert "title.tabIndex = -1" in js
    assert "let jobTarget = ''" in js
    assert "algorithm_version: document.querySelector('#settings-algorithm')" in js
    assert "fetch('/api/v2/settings/reload')" in js
