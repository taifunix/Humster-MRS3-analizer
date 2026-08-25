from __future__ import annotations

import argparse
import json
from pathlib import Path

from mrs3.performance_import import (
    PerformanceImportRequest,
    allocate_performance_database,
    import_performance_batch,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    database = allocate_performance_database(args.root, (args.pair,), args.start, args.end)
    result = import_performance_batch(
        PerformanceImportRequest(args.inbox, database, audit_dir=database.parent / database.stem, allow_quarantine=True)
    )
    print(json.dumps({"database": str(database), "import_id": result.import_id, "imported": result.imported_count, "quarantined": result.quarantined_count}))


if __name__ == "__main__":
    main()
