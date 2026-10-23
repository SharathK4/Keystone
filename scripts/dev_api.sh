#!/usr/bin/env bash
# Local API for frontend work: a throwaway SQLite execution database, migrated
# on start, and PayPal deliberately unconfigured so no page can call PayPal.
# Live sandbox calls happen only through the opt-in paypal_live tests.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export DATABASE_URL="sqlite+pysqlite:///${KEYSTONE_DEV_DB:-artifacts/runs/dev.sqlite}"
export PAYPAL_CLIENT_ID= PAYPAL_CLIENT_SECRET= PAYPAL_WEBHOOK_ID=
export LCE_EXECUTION_PROVIDER=paypal_sandbox
export LCE_LOG_LEVEL="${LCE_LOG_LEVEL:-WARNING}"
mkdir -p artifacts/runs
python -m alembic upgrade head >/dev/null 2>&1 || echo "migration failed: execution routes will answer 503"
exec python -m lce.cli serve --host 127.0.0.1 --port "${PORT:-8000}"
