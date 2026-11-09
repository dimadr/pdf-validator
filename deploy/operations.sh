#!/bin/sh
# Sourcing this file performs no deployment. Every mutating command needs the gate.
PROJECT1_OPS_DIR=$(CDPATH= cd -- "$(dirname -- "${PROJECT1_OPS_FILE:?Set the absolute path to operations.sh}")" && pwd)
prod() {
    python3 -B "$PROJECT1_OPS_DIR/deployment_guard.py" --approval "${PROJECT1_PRODUCTION_APPROVAL:-}" || return 2
    "$@"
}
