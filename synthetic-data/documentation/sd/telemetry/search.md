# search — telemetry

Serach service publishes:

- `search_queries_total` — counter, taged with `query_kind`.
- `search_query_duration_ms` — historgram.
- `search_zero_result_total` — counter for empty results.

i think the spans are also wired up but i havent confirmed. we
had a meeting about this last quater.
