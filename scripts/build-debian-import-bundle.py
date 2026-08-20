#!/usr/bin/env python3
"""Build the self-contained headless Debian HTML importer bundle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import uuid


CANONICAL_SHIFTS_BP = [
    30,
    40,
    50,
    60,
    70,
    90,
    110,
    140,
    170,
    200,
    230,
    270,
    310,
    350,
    390,
    430,
    470,
    510,
    550,
]

REQUIREMENTS = """duckdb>=1.5,<2
pandas>=2.2,<3
lxml>=5,<7
"""

README = """# Debian-бандл импортёра MRS3

Минимальный headless-контур `HTML -> Source DuckDB` без панели и surface-логики.

Полная инструкция на русском: [IMPORT_INSTRUCTIONS.md](IMPORT_INSTRUCTIONS.md).
"""

IMPORT_INSTRUCTIONS = """# Инструкция по импорту HTML в Source DuckDB

Этот бандл предназначен только для headless-импорта сырых HTML-отчётов в новую
Source DuckDB. Панель не нужна и не входит в бандл. Канонические значения в
`config.local.json` (ADR-0009) записаны как метаданные для последующей Phase 1;
сырой импорт не выполняет surface materialization и не применяет coverage
filtering.

## Предварительные требования

Нужны Debian/Linux, `python3` с модулем `venv`, доступное место на диске для
HTML, временных файлов и Source DuckDB, а также права чтения HTML и записи в
каталог бандла или в пути из конфига.

## Распаковка, окружение и конфигурация

Скопируйте архив на Debian-хост, распакуйте его и перейдите в каталог бандла:

```sh
tar -xzf debian-duckdb-importer.tar.gz
cd debian-duckdb-importer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Можно оставить `config.local.json` в корне бандла: его относительные пути
разрешаются от этого корня. По умолчанию Source DuckDB создаётся в
`data/mrs3_source_v5.duckdb`, HTML читается из `data/html`, а manifest,
checklist и audit пишутся в `data/import_audit`. Другой конфиг можно передать
абсолютным или относительным путём через `--config`; значения
`source_duckdb_path` и `audit_root` должны быть настроены.

Установите права запуска и выполните точную команду:

```sh
chmod +x scripts/import-html-duckdb-debian.sh
scripts/import-html-duckdb-debian.sh --html-root data/html --config config.local.json
```

Для внешнего каталога HTML используйте, например,
`scripts/import-html-duckdb-debian.sh --html-root /data/reports --config /path/config.local.json`.

## Успех и результаты

Вывод — NDJSON, по одному событию в строке. Успех означает, что последняя
строка имеет `"event":"summary"`, `"final_state":"COMMITTED"`,
`counts.quarantined=0` и `"safe_to_delete":"YES"`. Ненулевой код или
quarantine означает, что импорт нельзя считать успешным.

Source DuckDB находится по `source_duckdb_path`, а manifest, checklist и
прочие audit-артефакты — в `audit_root`; проверяйте фактические
`manifest_path` и `checklist_path` в summary.

## Остановка, повтор и место на диске

При прерывании нажмите `Ctrl-C`. Повторите ту же команду после проверки
свободного места: уже зафиксированные идентичные отчёты будут пропущены, а
не завершённая работа продолжится безопасно. Заранее оставьте запас места
для Source DuckDB, временных данных и audit; ошибка `No space left on device`
делает импорт неуспешным и требует устранить причину перед повтором.

Импортёр сам не удаляет HTML. Удаляйте исходные файлы только после проверки
финального summary с `safe_to_delete=YES` и `quarantine=0`, а также сохранения
необходимых manifest/checklist и резервных копий по вашим правилам.

В бандле нет панели, surface materialization и coverage filtering: этот запуск
делает только raw HTML -> Source DuckDB import.
"""

IMPORT_RUNTIME_FILES = (
    Path("src/mrs3/__init__.py"),
    Path("src/mrs3/config.py"),
    Path("src/mrs3/duckdb_import.py"),
    Path("src/mrs3/duckdb_events.py"),
    Path("src/mrs3/duckdb_source_schema.py"),
    Path("src/mrs3/source_packs.py"),
    Path("src/mrs3/locking.py"),
    # config.py imports Side from this module directly.
    Path("src/mrs3/models.py"),
)

CODEC_RELATIVE = Path(
    Path("programs")
    / "Обработчик HTML-DuckDB"
    / "mrs3_html_compact_importer_v3.py"
)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _copy_required_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"required source file is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _bundle_config() -> dict[str, object]:
    return {
        "duckdb_import": {
            "source_duckdb_path": "data/mrs3_source_v5.duckdb",
            "analysis_duckdb_path": None,
            "default_html_root": "data/html",
            "audit_root": "data/import_audit",
            "workers": 4,
            "transaction_batch_size": 250,
        },
        "canonical_shifts_bp": CANONICAL_SHIFTS_BP,
        "shift_domain": {"min_bp": 30, "max_bp": 550},
        "direct_materialization": {
            "workers": 15,
            "fetch_batch_size": 256,
            "worker_chunk_size": 16,
            "max_in_flight_chunks": 30,
        },
        "close_support": {"core_min": 0.90, "supported_min": 0.60},
        "canonical_metadata_note": (
            "Recorded metadata only; raw HTML import does not perform surface "
            "materialization or coverage filtering."
        ),
    }


def _replace_destination(staging: Path, destination: Path) -> None:
    """Atomically publish staging, replacing only the requested destination."""
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"bundle destination is not a directory: {destination}")

    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        backup = destination.parent / f".{destination.name}.old-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None and not destination.exists() and not destination.is_symlink():
            os.replace(backup, destination)
        raise
    if backup is not None:
        if backup.is_symlink() or not backup.is_dir():
            backup.unlink()
        else:
            shutil.rmtree(backup)


def _new_staging_directory(parent: Path, bundle_name: str) -> Path:
    """Create a randomized sibling directory with the parent's permissions."""
    for _ in range(10):
        candidate = parent / f".{bundle_name}.tmp-{uuid.uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not create a unique bundle staging directory")


def build_bundle(
    destination: Path | str | None = None,
    *,
    repo_root: Path | str | None = None,
) -> Path:
    """Build and atomically publish the Debian bundle, returning its path."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent.parent
    root = root.resolve()
    bundle = (
        Path(destination).absolute()
        if destination is not None
        else root / "debian-duckdb-importer"
    )
    if bundle == bundle.parent or bundle == root:
        raise ValueError("bundle destination must be a child path")
    bundle.parent.mkdir(parents=True, exist_ok=True)

    staging = _new_staging_directory(bundle.parent, bundle.name)
    try:
        for relative_path in IMPORT_RUNTIME_FILES:
            _copy_required_file(root / relative_path, staging / relative_path)
        _copy_required_file(
            root / "scripts" / "import-html-duckdb-debian.sh",
            staging / "scripts" / "import-html-duckdb-debian.sh",
        )
        _copy_required_file(
            root / "scripts" / "import_html_duckdb_debian.py",
            staging / "scripts" / "import_html_duckdb_debian.py",
        )
        _copy_required_file(root / CODEC_RELATIVE, staging / CODEC_RELATIVE)
        _write_text(staging / "README.md", README)
        _write_text(staging / "IMPORT_INSTRUCTIONS.md", IMPORT_INSTRUCTIONS)
        _write_text(staging / "requirements.txt", REQUIREMENTS)
        _write_text(
            staging / "config.local.json",
            json.dumps(_bundle_config(), ensure_ascii=False, indent=2) + "\n",
        )
        (staging / "data" / "html").mkdir(parents=True, exist_ok=True)
        _write_text(staging / "data" / "html" / ".gitkeep", "")
        (staging / "scripts" / "import-html-duckdb-debian.sh").chmod(0o755)
        (staging / "scripts" / "import_html_duckdb_debian.py").chmod(0o755)
        _replace_destination(staging, bundle)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="bundle directory (default: debian-duckdb-importer at repository root)",
    )
    args = parser.parse_args(argv)
    print(build_bundle(args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
