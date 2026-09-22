#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/dev/ceo-dashboard/back/internal_portal_ceo_dashboard_backend
export APP_ENV=development
exec ./venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8005 --reload --timeout-graceful-shutdown 2
