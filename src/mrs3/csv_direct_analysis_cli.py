"""Standalone direct CSV -> analysis-v6 DuckDB builder (upstream-only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import AlgorithmConfig
from .csv_direct_input import TRADE_CONTRACT, load_csv_direct
from .csv_direct_analysis import prepare_csv_direct_analysis, publish_csv_direct_analysis
from .loader import load_listing_dates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, action='append', required=True, help='CSV/ZIP/directory; repeatable')
    parser.add_argument('--listing-dates', type=Path, required=True, help='Listing registry .xlsx/.csv or JSON mapping')
    parser.add_argument('--analysis-config', type=Path, help='Original AlgorithmConfig JSON')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--filename', default='csv-direct.analysis-v6.duckdb')
    parser.add_argument('--shared-run-template', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Prepare scopes and frames without publishing a database')
    args = parser.parse_args()

    if args.listing_dates.suffix.lower() == '.json':
        listing = json.loads(args.listing_dates.read_text(encoding='utf-8-sig'))
        if not isinstance(listing, dict):
            parser.error('listing JSON must be a symbol -> UTC date object')
    else:
        listing = load_listing_dates(args.listing_dates)
    config = AlgorithmConfig.from_json(args.analysis_config) if args.analysis_config else AlgorithmConfig()
    source = load_csv_direct(
        args.source, listing_dates=listing, allow_shared_snapshot_template=args.shared_run_template,
    )
    prepared = prepare_csv_direct_analysis(source, config, listing_dates=listing)
    summary = {
        'analysis_id': prepared.analysis_id,
        'fingerprint': prepared.identity['fingerprint'],
        'trade_count_contract': TRADE_CONTRACT,
        'source_point_count': prepared.point_count,
        'source_total_completed_trades': source.total_trades,
        'source_total_closing_fills': source.total_fills,
        'source_scope_count': len(prepared.results),
        'tables': {
            table: sum(len(item['frames'][table]) for item in prepared.results)
            for table in ('points','refine_requests','plateaus','close_profiles','base_one_order','structures','structure_diagnostics')
        },
        'analysis_path': None,
        'dry_run': args.dry_run,
    }
    if not args.dry_run:
        summary['analysis_path'] = str(publish_csv_direct_analysis(prepared,args.output_dir,filename=args.filename))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
