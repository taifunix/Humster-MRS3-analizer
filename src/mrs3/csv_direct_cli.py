"""CLI inspection of normalized MRS3 tester CSV exports.

Validates tester CSV exports and prints per-scope counts without writing
anything; ``mrs3.csv_direct_analysis_cli`` publishes the Analysis DuckDB.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .csv_direct_input import INPUT_CONTRACT, TRADE_CONTRACT, DirectCsvError, load_csv_direct
from .loader import load_listing_dates


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate MRS3 direct tester CSV inputs")
    parser.add_argument("--source", action="append", required=True, help="Report .zip, .csv or directory; repeatable")
    parser.add_argument("--listing-dates", required=True, type=Path, help="Listing registry .xlsx/.csv or JSON symbol -> UTC date")
    parser.add_argument("--shared-run-template", action="store_true", help="Reuse one unique snapshot MA identity for other symbols in the same optimizer run")
    parser.add_argument("--json-out", type=Path, help="Write scope summary JSON")
    args = parser.parse_args()
    try:
        if args.listing_dates.suffix.casefold() == ".json":
            dates = json.loads(args.listing_dates.read_text(encoding="utf-8-sig"))
            if not isinstance(dates, dict):
                raise DirectCsvError("listing dates JSON must be an object")
        else:
            dates = load_listing_dates(args.listing_dates)
        parsed = load_csv_direct(
            args.source,
            listing_dates=dates,
            allow_shared_snapshot_template=args.shared_run_template,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    result = {
        "source_contract": INPUT_CONTRACT,
        "trade_count_contract": TRADE_CONTRACT,
        "points": len(parsed.frame),
        "total_completed_trades": parsed.total_trades,
        "total_closing_fills": parsed.total_fills,
        "csv_sha256": dict(parsed.csv_sha256),
        "normalized_input_digest": parsed.input_digest,
        "report_start": parsed.window_start.isoformat(),
        "report_end": parsed.window_end.isoformat(),
        "scopes": [
            {"scope_key": key, "points": len(group), "trades": int(group["trades"].sum()),
             "closing_fills": int(group["fills"].sum()),
             "activity_30d_sum": format(sum(group["activity_30d"]), "f")}
            for key, group in parsed.scopes().items()
        ],
        "source_v6_created": False,
        "materialized_surface_created": False,
        "analysis_db_created": False,
    }
    formatted = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(formatted + "\n", encoding="utf-8")
    print(formatted)


if __name__ == "__main__":
    main()
