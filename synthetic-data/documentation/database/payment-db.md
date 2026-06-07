# payment db

`data/payment.db`. There's a payments table somewhere in here.

Has fields like id and amount and provider. Refunds set a refund_id column.

We dont keep history of state changes, just the latest status.
