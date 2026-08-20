#!/bin/sh
# Debian convenience runner for the recursive HTML -> Source DuckDB import.
# Uses the platform-independent Python runner and never starts the web panel.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(dirname -- "$SCRIPT_DIR")

# Resolve relative config.local.json paths from the repository root,
# not from the caller's current directory.
cd "$REPO_ROOT"

RUNNER="$SCRIPT_DIR/import_html_duckdb_debian.py"
if [ ! -f "$RUNNER" ]; then
    echo "import-html-duckdb: python runner not found: $RUNNER" >&2
    exit 2
fi

PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "${PYTHON:-python3}" "$RUNNER" "$@"
