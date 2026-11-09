#!/bin/sh
# Changes production ONLY after the owner's dated deployment GO. Never runs at import.
set -eu
umask 077
QUEUE_TOOLS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PROJECT1_OPS_FILE="$QUEUE_TOOLS/../operations.sh"
. "$PROJECT1_OPS_FILE"
prod true
: "${LEGACY_PLAN:?Set the reviewed plan JSON path}"
: "${LEGACY_WORK_DIR:?Set a NEW artifact directory outside production}"
python3 -B - "$LEGACY_WORK_DIR" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve();production=pathlib.Path('/root/project1')
if p==production or production in p.parents or p.exists():raise SystemExit('STOP: work directory must be new and outside production')
p.mkdir(mode=0o700,parents=True)
PY
snapshot() {
    python3 -B "$QUEUE_TOOLS/inspect_legacy_queue.py" --plan "$LEGACY_PLAN" --output "$LEGACY_WORK_DIR/inventory.json"
}
check() {
    python3 -B "$QUEUE_TOOLS/verify_cutover_ready.py" --inventory "$LEGACY_WORK_DIR/inventory.json" --plan "$LEGACY_PLAN" --mode "$1" --output "$LEGACY_WORK_DIR/$1.json"
}
python3 -B "$QUEUE_TOOLS/inspect_legacy_queue.py" --plan "$LEGACY_PLAN" --output "$LEGACY_WORK_DIR/inventory-before-drain.json" --escrow "$LEGACY_WORK_DIR/pre-drain-transport.json"
cp "$LEGACY_WORK_DIR/inventory-before-drain.json" "$LEGACY_WORK_DIR/inventory.json"
check drain
# Only safe final retries may run naturally. No purge/revoke/ACK/republication.
prod docker stop --time 60 project1-celery-beat
snapshot
check drain
deadline=$(($(date +%s) + 900))
while :; do
    snapshot
    if check drained; then break; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo 'STOP: drain timed out; old worker remains, old producer is stopped. No V2 was started. Use the documented abort procedure.' >&2
        exit 2
    fi
    sleep 5
done
sh "$QUEUE_TOOLS/../preflight.sh" --phase prepare --legacy-drain-proof "$LEGACY_WORK_DIR/drained.json" > "$LEGACY_WORK_DIR/pre-stop.json"
prod docker stop --time -1 project1-worker
python3 -B "$QUEUE_TOOLS/inspect_legacy_queue.py" --plan "$LEGACY_PLAN" --output "$LEGACY_WORK_DIR/fenced.json" --escrow "$LEGACY_WORK_DIR/escrow.json"
echo 'AWAIT_ESCROW_COPY: copy escrow.json to the owner PC, create and return its receipt, refresh the fenced inventory and run the fenced gate. No V2 was started.'
