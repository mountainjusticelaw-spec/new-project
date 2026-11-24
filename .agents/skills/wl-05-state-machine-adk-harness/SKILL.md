---
name: wl-05-state-machine-adk-harness
description: >
  Implements the deterministic Python workflow state machine, atomic inbox/outbox deduplication, at-least-once Pub/Sub safety, NEEDS_RECONCILIATION handling, and bounded Google ADK specialists. Use when building workflow coordinators, state transitions, retries, or Google ADK + Gemini workers. Do not allow direct transitions from model output to external execution.
---

# WL-05: Python State Machine, Transactional Outbox & ADK Specialist Harness

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Orchestration & ADK Specialist Rules

1. **Explicit Run State Machine (Python First):**
   - Enforce legal transitions strictly in deterministic Python and Cloud SQL transactions:
     `RECEIVED -> VALIDATED -> RUNNING -> REVIEW_REQUIRED -> APPROVED -> EXECUTING -> COMPLETED`
     plus exception/terminal states: `REJECTED`, `FAILED`, `QUARANTINED`, `CANCELLED`, `NEEDS_RECONCILIATION`.
   - Persist every transition with timestamp, actor/component, and `reason_code`. Disallow any direct transition from `agent_run` to `EXECUTING`.

2. **Transactional Inbox & Outbox (At-Least-Once Safe):**
   - Deduplicate ingress via `UNIQUE (source, dedup_key)` on `event_inbox`. Write `event_inbox` and `outbox` atomically in one DB transaction before acknowledging ingress.
   - Assume Pub/Sub delivers at least once and out of order; every `workflow_step` and `effect_journal` entry must enforce idempotency keys, per-run time/token budgets, and bounded jittered retries.
   - If an external adapter call times out with ambiguous completion, transition to `NEEDS_RECONCILIATION` rather than blindly re-sending external communications or duplicating actions.

3. **Bounded Google ADK Specialists:**
   - Keep ADK agents narrow, stateless per invocation, and version-pinned (`model_version`, `prompt_version`, `schema_version`, `tool_version` recorded on `agent_run`).
   - Structure multi-rule or multi-document evaluations with canonical shared prefixes for Vertex AI context/prefix caching where approved.
