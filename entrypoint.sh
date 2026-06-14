#!/usr/bin/env bash
set -e

# Virtual display for headful Chrome (domain discovery). Search engines load
# results only with a real viewport, so Chrome renders into Xvfb.
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &

cd /app/backend
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8585}"
