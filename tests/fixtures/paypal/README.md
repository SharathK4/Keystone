# Vendored PayPal API specification

`payments_payouts_batch_v1.json` is PayPal's published OpenAPI 3.0.3 description
of the Payouts API (v1.9), copied unmodified from
<https://github.com/paypal/paypal-rest-api-specifications/blob/90e8041ffe02d80c452d2b476bedd59a8d219bdc/openapi/payments_payouts_batch_v1.json>
(commit `90e8041ffe02d80c452d2b476bedd59a8d219bdc`, fetched 2026-10-21).

It is used only by `tests/test_paypal_contract.py` to validate every request
body Keystone would send to Payouts against PayPal's own schema.

Copyright PayPal, Inc. Licensed under the Apache License, Version 2.0
(<https://github.com/paypal/paypal-rest-api-specifications/blob/main/LICENSE>),
the same licence as this repository.

## `canonical_extract.json`

Three scenarios (`single_missed_inflow`, `multi_node_shock`,
`supplier_failure`) and the merchant list, extracted **unmodified** from the
canonical snapshot `snap-a4c99d5a49a64ffd` (content hash `668c4a7b6d6fb71c`).
The execution tests build plans from these instead of from the local artifact
directory, which is not in version control. Regenerate with:

```python
from lce.snapshot.store import get_store
st = get_store()
# dump st.snapshot_id, the three scenarios' model_dump(mode="json"),
# and [m.model_dump(mode="json") for m in st.merchants()]
```
