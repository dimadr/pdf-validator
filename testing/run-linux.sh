#!/bin/sh
# Host orchestrator: approved isolated namespace only; no daemon/global cleanup.
set -eu
[ "${PROJECT1_TEST_HOST_APPROVED:-}" = 1 ] || { echo 'NOT RUN: approved isolated Docker host required' >&2; exit 77; }
command -v docker >/dev/null; command -v curl >/dev/null; command -v awk >/dev/null
[ "$(docker info --format '{{.OSType}}')" = linux ] || { echo 'Linux Docker required' >&2; exit 2; }
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECKOUT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUN_ID=${PROJECT1_TEST_RUN_ID:-$(date -u +%Y%m%dt%H%M%sz)-$$}
case "$RUN_ID" in *[!a-z0-9_-]*|'') echo 'Lowercase unique test run id required' >&2; exit 2;; esac
export COMPOSE_PROJECT_NAME="project1-rc-acceptance-$RUN_ID"
export PROJECT1_TEST_IMAGE_PREFIX="$COMPOSE_PROJECT_NAME"
[ -z "$(docker image ls -q --filter "label=io.project1.acceptance.run=$COMPOSE_PROJECT_NAME")" ] || { echo 'Fresh test image namespace required' >&2; exit 2; }
[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] || { echo 'Fresh test namespace required' >&2; exit 2; }
[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] || { echo 'Fresh test volumes required' >&2; exit 2; }
[ -z "$(docker network ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] || { echo 'Fresh test network required' >&2; exit 2; }
PROJECT1_TEST_POSTGRES_PASSWORD=${PROJECT1_TEST_POSTGRES_PASSWORD:-$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')}
case "$PROJECT1_TEST_POSTGRES_PASSWORD" in *[!0-9a-fA-F]*|'') echo 'Hexadecimal test password required' >&2; exit 2;; esac
export PROJECT1_TEST_POSTGRES_PASSWORD
PROJECT1_TEST_JWT_SECRET=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
export PROJECT1_TEST_JWT_SECRET
export COMPOSE_PARALLEL_LIMIT=1
PROJECT1_PYTHON_IMAGE=${PROJECT1_PYTHON_IMAGE:-python:3.11-slim-bookworm}
PROJECT1_TEST_POSTGRES_IMAGE=${PROJECT1_TEST_POSTGRES_IMAGE:-postgres:15-bookworm}
export PROJECT1_PYTHON_IMAGE PROJECT1_TEST_POSTGRES_IMAGE
BASE_FILE="$SCRIPT_DIR/compose.yml"; PDF_FILE=''
if [ "${PROJECT1_TEST_PDF_FIXTURES:-0}" = 1 ]; then
  : "${PDF_FIXTURE_DIR:?Set separately approved copied fixture directory}"
  : "${PDF_CAPTURE_FILE:?Set separately approved copied capture JSON}"
  PDF_FILE="$SCRIPT_DIR/compose.pdf.yml"
fi
dc() {
  if [ -n "$PDF_FILE" ]; then docker compose --project-name "$COMPOSE_PROJECT_NAME" -f "$BASE_FILE" -f "$PDF_FILE" "$@";
  else docker compose --project-name "$COMPOSE_PROJECT_NAME" -f "$BASE_FILE" "$@"; fi
}
dc config --quiet
ARTIFACTS="$CHECKOUT/.testing-artifacts/$RUN_ID"
[ ! -e "$ARTIFACTS" ] || { echo 'Fresh evidence directory required' >&2; exit 2; }
mkdir -p "$ARTIFACTS"
for image in "$PROJECT1_PYTHON_IMAGE" "$PROJECT1_TEST_POSTGRES_IMAGE" redis:7-alpine; do
  id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
  printf '%s|%s\n' "$image" "$id" >> "$ARTIFACTS/base-images-before.txt"
done
PROTECTED_IDS=$(docker ps -aq)
FORMAT='{{.Id}}|{{.Name}}|{{.State.StartedAt}}|{{.State.Status}}|{{if index .State "Health"}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.OOMKilled}}'
protected_snapshot() {
  if [ -n "$PROTECTED_IDS" ]; then docker inspect --format "$FORMAT" $PROTECTED_IDS | sort; fi
}
protected_snapshot > "$ARTIFACTS/protected-before.txt"
: "${PROJECT1_PROTECTED_HTTP_URLS:?Set read-only health URLs to protect before approving shared-host tests}"
# Health URLs may contain credentials: never echo them or their response bodies.
index=0
for url in $PROJECT1_PROTECTED_HTTP_URLS; do
  index=$((index+1))
  sample=$(curl --silent --show-error --output /dev/null --max-time 5 --write-out '%{http_code} %{time_total}' "$url")
  code=$(printf '%s' "$sample" | awk '{print $1}')
  case "$code" in 2??|3??) :;; *) echo 'Protected HTTP baseline is not available' >&2; exit 2;; esac
  printf '%s\n' "$sample" > "$ARTIFACTS/http-$index-before.txt"
done
CORES=$(getconf _NPROCESSORS_ONLN)
[ "$CORES" -ge 4 ] || { echo 'At least 4 host CPUs required to reserve capacity for existing services' >&2; exit 2; }
AVAILABLE_MB=$(awk '/MemAvailable:/{print int($2/1024)}' /proc/meminfo)
[ "$AVAILABLE_MB" -ge 8192 ] || { echo 'At least 8GiB available RAM required before tests' >&2; exit 2; }
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}')
FREE_KB=$(df -Pk "$DOCKER_ROOT" | awk 'END{print $4}')
[ "$FREE_KB" -ge 20971520 ] || { echo 'At least 20GiB free Docker storage required' >&2; exit 2; }
LOAD=$(awk '{print $1}' /proc/loadavg)
awk -v load="$LOAD" -v cpus="$CORES" 'BEGIN{exit !(load<=cpus/2)}' || { echo 'Host load already too high' >&2; exit 2; }
MIN_AVAILABLE_MB=$((AVAILABLE_MB-3000)); [ "$MIN_AVAILABLE_MB" -ge 6144 ] || MIN_AVAILABLE_MB=6144
printf '%s\n' "project=$COMPOSE_PROJECT_NAME cores=$CORES available_mb=$AVAILABLE_MB free_kb=$FREE_KB load=$LOAD" > "$ARTIFACTS/preflight.txt"
printf 'Isolated project: %s; runtime cap2CPU/2432MiB; serial build RUN cap1CPU/1GiB\n' "$COMPOSE_PROJECT_NAME"
RUNNING="$ARTIFACTS/watchdog-running"; ABORTED="$ARTIFACTS/watchdog-aborted"; ACTIVE_PID="$ARTIFACTS/active-client-pid"
: > "$RUNNING"
MAIN_PID=$$
owned_container_ids() { docker ps -q --filter "label=io.project1.acceptance.run=$COMPOSE_PROJECT_NAME"; }
stop_active_client() {
  if [ -s "$ACTIVE_PID" ]; then pid=$(cat "$ACTIVE_PID"); case "$pid" in *[!0-9]*|'') :;; *) kill -TERM "$pid" 2>/dev/null || true;; esac; fi
}
cleanup() {
  if ! mkdir "$ARTIFACTS/cleanup-lock" 2>/dev/null; then
    waits=0
    while [ ! -f "$ARTIFACTS/cleanup-done" ] && [ "$waits" -lt 90 ]; do sleep 1; waits=$((waits+1)); done
    return 0
  fi
  rm -f "$RUNNING"
  if [ -n "${WATCHDOG_PID:-}" ]; then kill -TERM "$WATCHDOG_PID" 2>/dev/null || true; fi
  stop_active_client
  # Labels are tied to a namespace proven absent at preflight. Never force-remove images.
  ids=$(owned_container_ids)
  [ -z "$ids" ] || docker stop --time 5 $ids >/dev/null 2>&1 || true
  dc down --volumes --remove-orphans --rmi local > "$ARTIFACTS/cleanup.log" 2>&1 || true
  for image in "$PROJECT1_TEST_IMAGE_PREFIX-test-runner" "$PROJECT1_TEST_IMAGE_PREFIX-smtp"; do
    owner=$(docker image inspect --format '{{index .Config.Labels "io.project1.acceptance.run"}}' "$image" 2>/dev/null || true)
    [ "$owner" != "$COMPOSE_PROJECT_NAME" ] || docker image rm "$image" >> "$ARTIFACTS/cleanup.log" 2>&1 || true
  done
  pass=0
  while [ "$pass" -lt 20 ]; do
    images=$(docker image ls -q --filter "label=io.project1.acceptance.run=$COMPOSE_PROJECT_NAME")
    [ -n "$images" ] || break
    docker image rm $images >> "$ARTIFACTS/cleanup.log" 2>&1 || true
    pass=$((pass+1))
  done
  while IFS='|' read -r image old_id; do
    [ -z "$old_id" ] || continue
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm "$image" >> "$ARTIFACTS/cleanup.log" 2>&1 || printf '%s\n' "$image" >> "$ARTIFACTS/base-images-retained.txt"
    fi
  done < "$ARTIFACTS/base-images-before.txt"
  if [ -s "$ARTIFACTS/base-images-retained.txt" ]; then : > "$ABORTED"; echo 'FAIL: new base image still referenced; never force-remove' >&2; fi
  protected_snapshot > "$ARTIFACTS/protected-after.txt" 2>/dev/null || true
  if ! cmp -s "$ARTIFACTS/protected-before.txt" "$ARTIFACTS/protected-after.txt"; then
    : > "$ABORTED"; echo 'FAIL: protected container state changed' >&2
  fi
  remaining=$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME"; docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME"; docker network ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME"; docker image ls -q --filter "label=io.project1.acceptance.run=$COMPOSE_PROJECT_NAME")
  if [ -n "$remaining" ]; then : > "$ABORTED"; echo 'FAIL: isolated resources remain; inspect cleanup.log' >&2; fi
  : > "$ARTIFACTS/cleanup-done"
}
trap 'cleanup' EXIT
trap 'cleanup; exit 130' INT TERM
abort_test() {
  printf '%s\n' "$1" > "$ABORTED"
  echo 'ABORT: test stopped to protect existing services; inspect watchdog-aborted' >&2
  stop_active_client
  cleanup
  kill -TERM "$MAIN_PID" 2>/dev/null || true
}
watchdog() {
  load_strikes=0; cpu_strikes=0
  previous=$(awk '/^cpu /{total=0;for(i=2;i<=9;i++)total+=$i; print total,$5+$6;exit}' /proc/stat)
  while [ -f "$RUNNING" ]; do
    sleep 5
    [ -f "$RUNNING" ] || break
    protected_snapshot > "$ARTIFACTS/protected-current.txt" 2>/dev/null || { abort_test protected_inspect_failed; return; }
    cmp -s "$ARTIFACTS/protected-before.txt" "$ARTIFACTS/protected-current.txt" || { abort_test protected_container_changed; return; }
    available=$(awk '/MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    [ "$available" -ge "$MIN_AVAILABLE_MB" ] || { abort_test available_ram_degraded; return; }
    free=$(df -Pk "$DOCKER_ROOT" | awk 'END{print $4}')
    [ "$free" -ge 10485760 ] || { abort_test docker_storage_low; return; }
    load=$(awk '{print $1}' /proc/loadavg)
    if awk -v load="$load" -v cpus="$CORES" 'BEGIN{exit !(load>cpus)}'; then load_strikes=$((load_strikes+1)); else load_strikes=0; fi
    [ "$load_strikes" -lt 3 ] || { abort_test sustained_host_load; return; }
    current=$(awk '/^cpu /{total=0;for(i=2;i<=9;i++)total+=$i; print total,$5+$6;exit}' /proc/stat)
    if awk -v old="$previous" -v new="$current" 'BEGIN{split(old,o);split(new,n);dt=n[1]-o[1];busy=dt>0?100*(1-(n[2]-o[2])/dt):0;exit !(busy>80)}'; then cpu_strikes=$((cpu_strikes+1)); else cpu_strikes=0; fi
    previous="$current"
    [ "$cpu_strikes" -lt 2 ] || { abort_test sustained_cpu_pressure; return; }
    index=0
    for url in $PROJECT1_PROTECTED_HTTP_URLS; do
      index=$((index+1))
      sample=$(curl --silent --output /dev/null --max-time 5 --write-out '%{http_code} %{time_total}' "$url") || { abort_test protected_http_unavailable; return; }
      before=$(cat "$ARTIFACTS/http-$index-before.txt")
      awk -v old="$before" -v new="$sample" 'BEGIN{split(old,o);split(new,n);limit=3*o[2]+0.75;exit !(n[1]==o[1] && n[2]<=limit && n[2]<=3)}' || { abort_test protected_http_degraded; return; }
    done
  done
}
watchdog & WATCHDOG_PID=$!
run_checked() {
  [ ! -e "$ABORTED" ] || return 99
  "$@" & child=$!
  printf '%s\n' "$child" > "$ACTIVE_PID"
  status=0; wait "$child" || status=$?
  rm -f "$ACTIVE_PID"
  [ ! -e "$ABORTED" ] || return 99
  return "$status"
}
run_dc() {
  if [ -n "$PDF_FILE" ]; then run_checked docker compose --project-name "$COMPOSE_PROJECT_NAME" -f "$BASE_FILE" -f "$PDF_FILE" "$@";
  else run_checked docker compose --project-name "$COMPOSE_PROJECT_NAME" -f "$BASE_FILE" "$@"; fi
}
bounded_build() {
  # Never fall back to unbounded BuildKit. Unsupported legacy builder is a hard NOT RUN.
  run_checked env DOCKER_BUILDKIT=0 docker build --force-rm --memory=1g --memory-swap=1g --cpu-period=100000 --cpu-quota=100000 --build-arg "PROJECT1_PYTHON_IMAGE=$PROJECT1_PYTHON_IMAGE" --build-arg "PROJECT1_ACCEPTANCE_RUN_ID=$COMPOSE_PROJECT_NAME" --tag "$1" --file "$2" "$CHECKOUT"
}
for image in "$PROJECT1_PYTHON_IMAGE" "$PROJECT1_TEST_POSTGRES_IMAGE" redis:7-alpine; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then run_checked docker pull "$image"; fi
done
bounded_build "$PROJECT1_TEST_IMAGE_PREFIX-smtp" "$SCRIPT_DIR/smtp/Dockerfile"
bounded_build "$PROJECT1_TEST_IMAGE_PREFIX-test-runner" "$SCRIPT_DIR/Dockerfile"
run_dc up -d --wait --no-build postgres redis smtp
run_dc run --rm --no-deps --pull never --workdir /app/backend test-runner python -m alembic upgrade head
run_dc run --rm --no-deps --pull never test-runner python /app/testing/probe_services.py baseline
run_dc run --rm --no-deps --pull never test-runner python /app/testing/probe_services.py smtp-wire
run_dc run --rm --no-deps --pull never test-runner python /app/testing/probe_services.py redis-before-restart
run_dc run --rm --no-deps --pull never test-runner python /app/testing/probe_services.py redis-reserve-and-crash
run_dc restart redis
count=0
until dc exec -T redis redis-cli ping 2>/dev/null | tr -d '\r' | grep -qx PONG; do count=$((count+1)); [ "$count" -lt 30 ] || exit 3; sleep 1; done
run_dc run --rm --no-deps --pull never test-runner python /app/testing/probe_services.py redis-after-restart
TEST_STATUS=0
if [ "${PROJECT1_STAGING:-0}" = 1 ]; then
  run_dc run -d --no-deps --pull never --name "$COMPOSE_PROJECT_NAME-staging" test-runner python /app/testing/staging.py
  docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$COMPOSE_PROJECT_NAME-staging" > "$ARTIFACTS/staging-ip.txt"
  while [ ! -f "$ARTIFACTS/staging-stop" ] && [ ! -e "$ABORTED" ]; do sleep 2; done
  cleanup
  exit 0
fi
run_dc run --rm --no-deps --pull never test-runner python -m pytest tests testing/decision/test_preflight_policy.py testing/decision/test_legacy_queue_policy.py -q -p no:cacheprovider --junitxml=/results/pytest.xml -o junit_logging=all || TEST_STATUS=$?
run_dc run --rm --no-deps --pull never test-runner python -c 'import pathlib; print(pathlib.Path("/results/pytest.xml").read_text())' > "$ARTIFACTS/pytest.xml" || true
run_dc run --rm --no-deps --pull never test-runner python -c 'import pathlib,json; print(json.dumps({p.name:p.read_text(errors="replace") for p in pathlib.Path("/results").glob("*.log")}))' > "$ARTIFACTS/worker-logs.json" || true
cleanup
[ ! -e "$ABORTED" ] || exit 99
[ "$TEST_STATUS" -eq 0 ] || exit "$TEST_STATUS"
printf 'PASS: isolated run finished; own containers/volumes/networks/images removed; protected snapshot unchanged.\n'
