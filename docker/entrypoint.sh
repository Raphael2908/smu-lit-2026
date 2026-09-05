#!/usr/bin/env bash
# One image, many roles. This is what makes the single-box -> scaled split mechanical later.
set -euo pipefail
ROLE="${ROLE:-api}"

case "$ROLE" in
  api)
    exec uvicorn verifier.api.app:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
    ;;
  worker)
    exec celery -A verifier.worker.celery_app.celery_app worker \
      --loglevel="${LOG_LEVEL:-info}" -Q default,maintenance --concurrency=8
    ;;
  judgeworker)
    # Isolated so a 15s Opus call can never sit in front of the 0.6s fabrication check.
    exec celery -A verifier.worker.celery_app.celery_app worker \
      --loglevel="${LOG_LEVEL:-info}" -Q judge --concurrency=4
    ;;
  browserworker)
    # Browsers are heavy; keep concurrency low and off the fast path.
    exec celery -A verifier.worker.celery_app.celery_app worker \
      --loglevel="${LOG_LEVEL:-info}" -Q browser --concurrency=2
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "Unknown ROLE: $ROLE (expected api|worker|judgeworker|browserworker|migrate)" >&2
    exit 1
    ;;
esac
