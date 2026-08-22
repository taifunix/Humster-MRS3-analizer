#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname -- "$SCRIPT_DIR")
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then PYTHON=python3; fi
exec "$PYTHON" "$SCRIPT_DIR/import_source_v6_debian.py" "$@"
