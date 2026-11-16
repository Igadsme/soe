#!/bin/sh
set -eu

# Validate before migrations or custom worker commands can touch production data.
python -c 'from app.config import settings; settings.validate_production()'
case "${SOE_RUN_MIGRATIONS:-true}" in
  [Tt][Rr][Uu][Ee]) alembic upgrade head ;;
esac
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${SOE_WEB_CONCURRENCY:-2}" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --timeout 120 --graceful-timeout 30 \
  --forwarded-allow-ips "" --error-logfile -
