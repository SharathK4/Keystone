#!/usr/bin/env bash
# Start the Keystone API on Render.
#
# The analysis serves from committed artifacts and needs no database. Execution
# records do: if the database is missing or unreachable the API still starts,
# and the execution routes answer 503 with the reason.
set -u

# Render hands out postgresql://...; SQLAlchemy must be told to use psycopg 3.
case "${DATABASE_URL:-}" in
  postgres://*)   DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgres://}" ;;
  postgresql://*) DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgresql://}" ;;
esac
export DATABASE_URL

if [ -n "${DATABASE_URL:-}" ] && alembic upgrade head; then
  echo "database ready: execution records enabled"
else
  echo "database unavailable: analysis serves; execution routes answer 503"
fi

exec uvicorn lce.api.app:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*'
