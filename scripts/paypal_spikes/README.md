# PayPal sandbox spikes

The spikes are the opt-in live tests in `tests/test_paypal_live.py`, so they use
exactly the client and settings the product uses. They never run by default.

```bash
# one sandbox *personal* account email to receive the $1.00 test payouts/invoices
set PAYPAL_SPIKE_RECEIVER=sb-xxxx@personal.example.com      # PowerShell: $env:PAYPAL_SPIKE_RECEIVER="..."
pytest -m paypal_live tests/test_paypal_live.py -rA -s
```

Record each outcome in [`reports/paypal/spikes.md`](../../reports/paypal/spikes.md).
