#!/bin/sh
# Observation only: no migrations, starts/stops, application tasks, ACK, purge or writes.
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'STOP: python3 is required' >&2; exit 2; }
PREFLIGHT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -r "$PREFLIGHT_DIR/preflight.py" ] || { echo 'STOP: preflight.py companion is missing' >&2; exit 2; }
exec python3 -B "$PREFLIGHT_DIR/preflight.py" "$@"
