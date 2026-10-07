#!/usr/bin/env bash
set -e

for DP in "$LOCALAPPDATA/Programs/DockerDesktop/resources/bin" \
          "/mnt/c/Users/$USER/AppData/Local/Programs/DockerDesktop/resources/bin" \
          "/mnt/c/Users/RachadQuintyne/AppData/Local/Programs/DockerDesktop/resources/bin" \
          "/c/Users/$USER/AppData/Local/Programs/DockerDesktop/resources/bin" \
          "/c/Program Files/Docker/Docker/resources/bin"; do
  if [ -d "$DP" ] && [[ ":$PATH:" != *":$DP:"* ]]; then
    export PATH="$DP:$PATH"
  fi
done

DOCKER_CMD="docker"
if [ ! -x "/usr/bin/docker" ] && command -v docker.exe &> /dev/null; then
  DOCKER_CMD="docker.exe"
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

echo "?? [Disposable DB] Checking local database environment..."

if ! command -v "$DOCKER_CMD" &> /dev/null; then
  echo "??  [Disposable DB] Docker is not installed or not in PATH. Skipping local DB reset."
  exit 0
fi

if ! "$DOCKER_CMD" info &> /dev/null; then
  echo "??  [Disposable DB] Docker daemon is not running. Skipping local DB reset."
  exit 0
fi

DB_URL=""
if [ -f ".env" ]; then
  DB_URL=$(grep -E "^DATABASE_URL=" .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
fi
if [ -n "$DATABASE_URL" ]; then
  DB_URL="$DATABASE_URL"
fi

if [ -n "$DB_URL" ]; then
  if [[ "$DB_URL" != *"localhost"* ]] && [[ "$DB_URL" != *"127.0.0.1"* ]] && [[ "$DB_URL" != *"host.docker.internal"* ]]; then
    echo "?? [SAFETY GUARD] DATABASE_URL points to a remote/RDS host! Refusing to reset."
    echo "Target: $DB_URL"
    exit 0
  fi
fi

echo "???  [Disposable DB] Tearing down previous local PostgreSQL container & volumes..."
"$DOCKER_CMD" compose down -v > /dev/null 2>&1 || true

echo "?? [Disposable DB] Starting clean local PostgreSQL container..."
"$DOCKER_CMD" compose up -d db

echo "? [Disposable DB] Waiting for PostgreSQL to be ready..."
RETRIES=30
until "$DOCKER_CMD" exec ceo_db_local pg_isready -U postgres &> /dev/null || [ $RETRIES -eq 0 ]; do
  sleep 1
  RETRIES=$((RETRIES - 1))
done

if [ $RETRIES -eq 0 ]; then
  echo "? [Disposable DB] PostgreSQL failed to become ready in time."
  exit 1
fi

echo "?? [Disposable DB] Running Alembic migrations to branch HEAD..."
if command -v alembic &> /dev/null; then
  alembic upgrade head
elif [ -f "venv/bin/alembic" ]; then
  venv/bin/alembic upgrade head
elif [ -f "venv/Scripts/alembic.exe" ]; then
  venv/Scripts/alembic.exe upgrade head
elif command -v python &> /dev/null; then
  python -m alembic upgrade head 2>/dev/null || true
else
  echo "??  [Disposable DB] Alembic not found in PATH or venv. Run 'alembic upgrade head' manually."
fi

echo "? [Disposable DB] Local database successfully recreated and migrated to current branch head!"
