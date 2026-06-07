# piracy and licensing

A licens is what proves you bought an app. We mint one per app per order
in the fulfilment service. They are perpetual which means once minted they
never expire. Which is a problem if the user refunds becuase the licens stays
valid.

There is no DRM in this system. The download URLs are signed but the binarys
themselves don't check the licens, that is the OS's job. If somebody pulls a
URL out of the receipt and shares it with a freind, the freind can install too.
That's a known gap, the URL has expires_at but its not enforced anywhere yet.

For PearCare replacements, we mint a *second* licens for the same user, same
app. The user technically has two now. Some folks think this is a bug, its not.
The data model says one user can hold many licenses for the same app and that
makes the family share story easier later.

If we wanted to actually fight piracy, the steps would be:

- enforce expires_at on the CDN
- have a license check API that the running app pings
- revoke licenses on refund

None of that is built. The system mostly assumes good actors. Anyone trying
to pirate apps via this store is going to have a great time.
