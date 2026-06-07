# Agentic AI Capstone project

## Overview

I'm proposing to implement a Research Agent that can help our leadership, developers, and product managers have a complete view of the architecture, dependencies, progress, and known gaps of our systems.

## Documents and links

* [Project documentation](PROJECT.md)
* [Implementation](implementation)
* [Synthetic data for testing the project](synthetic-data)
* [Playground](playground)

## Implementation & documentation gap (synthetic data)

`synthetic-data/` contains a small Pear Store storefront — eight
backend microservices plus a sibling PearCare warranty system — used
as input for the Research Agent's gap-detection workflows. Both the
**implementation** and the **documentation** are intentionally uneven
so the agent has gaps to find:

| Surface       | Where                                       | Uneven dimensions                                                                  |
| ------------- | ------------------------------------------- | ---------------------------------------------------------------------------------- |
| Service code  | `synthetic-data/implementation/services/`   | docstrings range from thorough to one-liner-with-typos; some hooks are stubs        |
| Service docs  | `synthetic-data/documentation/services/`    | HIGH / MEDIUM / LOW / VERY LOW — `documentation/README.md` is the gap map           |
| DB docs       | `synthetic-data/documentation/database/`    | three docs MISSING outright (`order`, `fulfillment`, `pearcare-plan`)               |
| Telemetry     | `synthetic-data/telemetry/` + `documentation/telemetry/` | five services instrumented at different tiers (HIGH/MEDIUM/LOW), five emit nothing  |
| Runbooks      | `synthetic-data/documentation/runbooks/`    | `pearcare-fraud.md` is a placeholder; `incident-response.md` is one-liner thin      |
| Business docs | `synthetic-data/documentation/business-cases/` | refunds / piracy / cancellations are deliberately under-developed                 |

### Telemetry coverage tiers

Five backend services are wired to a small dependency-free OpenTelemetry
shim (`synthetic-data/implementation/shared/otel.py`) that emits
OTLP-shaped JSONL into `synthetic-data/telemetry/<service>/` while
the services run:

- **HIGH** — `order`, `payment`: root spans, child spans for every
  downstream call, success/failure counters, latency histograms, and
  business metrics.
- **MEDIUM** — `fulfillment`, `pearcare-claim`: root spans + standard
  metrics + one business counter, but the inner hook calls aren't
  spanned (deliberate gap).
- **LOW** — `catalog`: counters and a latency histogram, **no traces**.
- **NONE** — `account`, `cart`, `review`, `search`, `pearcare-plan`:
  uninstrumented (deliberate gap; the doc gap agent should flag the
  five missing telemetry docs).

The doc tree mirrors the same shape: `documentation/telemetry/order.md`
is the HIGH-quality template, and `documentation/telemetry/search.md`
is intentionally wrong (claims metrics that don't exist) so the
verifier has something concrete to reconcile with code.

To produce telemetry, boot the stack with
`synthetic-data/implementation/start_all.sh`, drive some traffic, and
read the JSONL files at `synthetic-data/telemetry/<service>/`. Stop
the stack with `synthetic-data/implementation/stop_all.sh`.
