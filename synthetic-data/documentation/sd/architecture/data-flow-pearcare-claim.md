# Data flow: a PearCare claim

User has an active enrollment and something has gone wrong.

## Sequence

```mermaid
sequenceDiagram
    autonumber
    participant FE as frontend
    participant CL as pearcare-claim
    participant PL as pearcare-plan
    participant RV as repair-vendor-hook
    participant FUL as fulfillment-svc

    FE->>CL: POST /claims (user_id, enrollment_id, issue)
    CL->>PL: GET /enrollments/{id}
    PL-->>CL: 200 (tier, app_id, status)
    CL-->>FE: 201 (claim: filed)

    Note over CL: Operator or _auto_triage picks one of: support, repair, replacement, deny

    alt resolution = repair
        CL->>RV: dispatch(claim_id, app_id, issue)
        RV-->>CL: vendor, ticket, eta_days
    else resolution = replacement
        CL->>FUL: POST /fulfill (order_id: replacement-CLAIMID)
        FUL-->>CL: 200 entitlements
    else resolution = support
        CL->>CL: no hook fires, mark approved
    else resolution = deny
        CL->>CL: mark denied
    end
```

## Triage rules (default, `_auto_triage` in `claim/app.py`)

This is intentionally simple — real eligibility logic should live here.

| Issue keywords                              | Required tier      | Result        |
| ------------------------------------------- | ------------------ | ------------- |
| "lost", "stolen", "theft"                   | pearcare_loss      | replacement   |
| "lost" / "stolen" without loss tier         | any                | deny          |
| "broken", "damage", "cracked", "crash"      | any                | repair        |
| "question", "how do", "help", "setup"       | any                | support       |
| anything else                               | any                | support       |
