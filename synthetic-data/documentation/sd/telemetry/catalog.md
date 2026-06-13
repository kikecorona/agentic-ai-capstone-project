# catalog — telemetry (LOW)

catalog has metrics. probably enough for now.

emitted:
- `catalog_requests_total`
- `catalog_request_duration_ms`

both are tagged with the route. there's a 404 path on
`/apps/<app_id>` when the id doesn't exist; that one shows up as
`status=failure`.

no traces. it's read-mostly so we didn't bother. might revisit.

todo: add a hit/miss counter for the seed cache? not sure.
