#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
python3 scripts/init_local.py
set -a
source .env.local
set +a

if [[ ! -x .venv/bin/python ]]; then
  echo 'Create the environment first: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock' >&2
  exit 1
fi
if [[ "${APP_ENV:-}" != development || "${DATABASE_URL:-}" != sqlite:* ]]; then
  echo 'run-local.sh is for a development SQLite installation only; use Compose for PostgreSQL.' >&2
  exit 1
fi

# SQLite supports exactly one transport worker in this local launch.
export WORKER_SHARD_COUNT=1
export WORKER_SHARDS=0
export WORKER_HEARTBEAT_PATH="${TMPDIR:-/tmp}/omnicrm-local-worker-$$.heartbeat"
child_pids=()
cleanup() {
  trap - EXIT INT TERM
  for child_pid in "${child_pids[@]}"; do kill "$child_pid" 2>/dev/null || true; done
  for child_pid in "${child_pids[@]}"; do wait "$child_pid" 2>/dev/null || true; done
  rm -f -- "$WORKER_HEARTBEAT_PATH"
}
trap cleanup EXIT INT TERM

# Create development tables before either process can accept work.
.venv/bin/python -c 'from app.config import settings; from app.db import Base,engine; settings.validate(); Base.metadata.create_all(engine)'
.venv/bin/python -m app.worker &
child_pids+=("$!")
.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port "${API_PORT:-8080}" --no-access-log &
child_pids+=("$!")
echo "Local inbox: http://127.0.0.1:${API_PORT:-8080} — Ctrl+C stops API and worker."
wait -n "${child_pids[@]}"
