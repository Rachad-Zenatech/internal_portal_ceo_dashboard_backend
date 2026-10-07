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

echo "================================================================"
echo " Pull Production Data -> Local Docker Database"
echo "================================================================"

PROD_URL=""
if [ -f ".env" ]; then
  PROD_URL=$(grep -E "^DATABASE_URL=" .env | cut -d '=' -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true)
fi

if [ -z "$PROD_URL" ] || [[ "$PROD_URL" == *"localhost"* ]] || [[ "$PROD_URL" == *"127.0.0.1"* ]]; then
  echo "? Error: Could not find remote DATABASE_URL in .env"
  exit 1
fi

echo "Target Local : ceo_db_local (localhost:3001 / zenatech_ceo_dashboard)"

echo "[1/3] Ensuring local Docker database is running..."
"$DOCKER_CMD" compose up -d db > /dev/null 2>&1 || "$DOCKER_CMD" compose up -d db

echo "[2/3] Fetching live data dump from remote database..."
"$DOCKER_CMD" exec ceo_db_local sh -c "pg_dump '$PROD_URL' --no-owner --no-acl -Fc -f /tmp/prod.dump"

echo "[3/3] Restoring live data into local database..."
"$DOCKER_CMD" exec ceo_db_local sh -c "pg_restore -U postgres -d zenatech_ceo_dashboard --clean --if-exists --no-owner /tmp/prod.dump" > /dev/null 2>&1 || true
"$DOCKER_CMD" exec ceo_db_local rm -f /tmp/prod.dump

echo ""
echo "? Local Docker database successfully updated with live data!"
echo "================================================================"
