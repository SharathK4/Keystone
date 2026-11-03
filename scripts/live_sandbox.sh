#!/usr/bin/env bash
# Local API against the real PayPal sandbox, for the owner's live runs.
#
#   sh scripts/live_sandbox.sh                       # serve on :8000
#   sh scripts/live_sandbox.sh paypal status --probe # any `lce` command, same env
#   sh scripts/live_sandbox.sh paypal map m0005 --email <sandbox personal account>
#
# Unlike scripts/dev_api.sh, the PayPal credentials are NOT blanked: settings
# read them from .env. Its own SQLite file keeps mapped identities, approvals and
# the ledger between runs, apart from the throwaway dev database. Live mode stays
# refused in code; this only stops hiding the sandbox keys.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export DATABASE_URL="sqlite+pysqlite:///${KEYSTONE_LIVE_DB:-artifacts/runs/live.sqlite}"
export LCE_EXECUTION_PROVIDER=paypal_sandbox
# Keep every verified delivery as received, so `lce walk record <event id>` can
# turn the walk's failure into the committed fixture.
export LCE_RECORD_DELIVERIES_DIR="${LCE_RECORD_DELIVERIES_DIR:-artifacts/paypal/deliveries}"
export LCE_LOG_LEVEL="${LCE_LOG_LEVEL:-WARNING}"
mkdir -p artifacts/runs
python -m alembic upgrade head >/dev/null 2>&1 || echo "migration failed: execution routes will answer 503"
if [ "$#" -gt 0 ]; then
  exec python -m lce.cli "$@"
fi
exec python -m lce.cli serve --host 127.0.0.1 --port "${PORT:-8000}"
