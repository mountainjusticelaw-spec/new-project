# Initial threat register

| Threat | Required mitigation / validation |
| --- | --- |
| Cross-matter disclosure | Deny-by-default scoped retrieval and object authorization; test direct, indirect and cached access |
| Prompt injection in pleadings/email | Treat content as data, constrain tools, adversarial tests |
| Forged or duplicated trigger | Authenticate source, validate freshness, durable dedup and replay tests |
| Stale or tampered approval | Exact action digest, expiry, source version, reviewer checks, negative tests |
| Uncertain external write | Effect journal, provider idempotency if supported, reconciliation before retry |
| Credential or privileged log leakage | Secret Manager, short-lived identities, redacted logs, scans and incident drill |
| Citation or deadline hallucination | Provenance, independent review, abstention and attorney verification |
| Unbounded model or parser cost | Quotas, time/size budgets, limits and kill switch |
| Vendor retention or prohibited egress | Contract and data-flow review; allowlist endpoints and minimize data |
| Missed docket event / outage | Reconciliation, alert, source verification, human fallback and restore drill |

Risk owners and acceptance thresholds must be assigned before live pilot. This is not a completed security assessment.
