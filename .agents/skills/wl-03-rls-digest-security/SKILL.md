---
name: wl-03-rls-digest-security
description: >
  Enforces PostgreSQL FORCE ROW LEVEL SECURITY, restricted NOBYPASSRLS roles, intake vs. verified matter isolation, SHA-256 action-digest approvals, and untrusted content sandboxing. Use when writing SQL migrations, database adapters, authorization checks, approval executors, or telemetry. Do not use fictional placeholder matter IDs for unresolved intake records.
---

# WL-03: Cloud SQL RLS, Action-Digest Approvals & Security Controls

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Security & Isolation Controls

1. **Schema & Verified Matter Scope:**
   - Enforce verified `matter_id` (mapped via `matter_mapping` to Clio) across `workflow_step`, `agent_run`, `source_reference`, `work_product`, `action_proposal`, `approval`, and `effect_journal`.
   - Keep pre-resolution intake tables (`event_inbox`, `workflow_run`, `outbox`, `audit_event`) under explicit scope consistency constraints (`INTAKE_RESTRICTED` vs. `MATTER_VERIFIED`).
   - Enforce both application object-level checks AND PostgreSQL `ENABLE` + `FORCE ROW LEVEL SECURITY` (`NOBYPASSRLS` roles) bound via `SET LOCAL` transaction-scoped variables.
   - Missing, unmapped, or conflicting matter signals MUST route `workflow_run` directly to `QUARANTINED` with a recorded reason code.

2. **Cryptographic Action-Digest Approval Gate:**
   - Compute a canonical SHA-256 `action_digest` over `(action_type, destination, normalized_payload, matter_id, source_reference_versions[], policy_version)`.
   - `approval` binds `action_digest`, `matter_id`, `reviewer_id`, `policy_version`, `expires_at`, and `destination`.
   - Deterministic Policy Executor re-verifies identity, permission, source version freshness (any upstream document/email version change invalidates approval), expiry, idempotency key in `effect_journal`, and global/workflow kill switch before invoking any external adapter.

3. **Untrusted Content & Redacted Telemetry:**
   - Wrap all incoming correspondence, attachments, and retrieved excerpts in `<untrusted_source_content ref_id="...">` delimiters.
   - Never place raw correspondence bodies or PII in Pub/Sub payloads or Cloud Logging; pass scoped IDs and log redacted operational metadata.
