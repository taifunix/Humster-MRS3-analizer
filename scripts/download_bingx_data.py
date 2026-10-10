"""Manual BingX 1m data top-up using the ``bingx`` block of config.local.json."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:  # Allow ``python scripts/download_bingx_data.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.bingx_downloader import (  # noqa: E402
    BingXClient,
    BingXDownloadError,
    download_pairs,
    load_account_keys,
    load_bingx_settings,
)

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.local.json"


def main(argv: Sequence[str] | None = None, *, client: BingXClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config.local.json")
    args = parser.parse_args(argv)
    try:
        settings = load_bingx_settings(args.config)
        if settings.start_date is None or settings.end_date is None or not settings.pairs:
            raise BingXDownloadError("bingx.start_date, bingx.end_date and bingx.pairs are required for a manual run")
        if client is None:
            client = BingXClient(credentials=load_account_keys(settings.bot_root))
        report = download_pairs(
            bot_root=settings.bot_root,
            pairs=settings.pairs,
            start=settings.start_date,
            end=settings.end_date,
            mode=settings.download_mode,
            client=client,
            log=print,
        )
    except BingXDownloadError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    print()
    for line in report.summary_lines():
        print(line)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
