"""PayPal sandbox integration.

Three concerns, one module each, none of which the analysis imports:

* :mod:`lce.paypal.money` - ``Decimal`` money and the declared demo scale that
  turns an analytic rupee amount into a sandbox amount.
* :mod:`lce.paypal.client` - a minimal REST client: OAuth client credentials,
  Payouts create/get, and read-only probes.
* :mod:`lce.paypal.webhooks` - signature verification for inbound webhooks.

Sandbox only, by construction: :class:`lce.config.PayPalSettings` refuses live
mode and any API host that is not a PayPal sandbox host, and the client checks
again before its first request.
"""
