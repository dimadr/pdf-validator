#!/bin/bash
set -e

/usr/local/bin/docker-entrypoint.sh postgres &
POSTGRES_PID=$!

for i in $(seq 1 30); do
  if pg_isready -U postgres -h localhost 2>/dev/null; then
    break
  fi
  sleep 1
done

psql -U postgres -d postgres -c "ALTER USER postgres PASSWORD '${POSTGRES_PASSWORD:-postgres}';" 2>/dev/null || true

wait $POSTGRES_PID
