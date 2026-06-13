# payment

Charges money. Three providers. POST /authorize, POST /refund.

There are some hooks under hooks/. Stripe is the default. PayPal and Pearcard
also exist.

If the card is declined you get a 402 back. The payment id format is `pay_xxx`.
