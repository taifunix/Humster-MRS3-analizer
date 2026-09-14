from __future__ import annotations

import argparse
import json
from pathlib import Path

from mrs3.performance_v2_prune import DEFAULT_CUTOFF, PerformanceV2PruneError, prune_performance_v2
from mrs3.performance_v2_store import load_performance_v2_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or apply safe Performance v2 cleanup")
    parser.add_argument("--config", type=Path, default=Path("config.performance.json"))
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="UTC cutoff date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="create a backup and apply deletion")
    args = parser.parse_args()
    try:
        result = prune_performance_v2(
            load_performance_v2_config(args.config), args.cutoff, apply=args.apply
        )
    except (OSError, PerformanceV2PruneError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
