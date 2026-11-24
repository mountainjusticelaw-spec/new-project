---
name: wl-07-attorney-command-center-ui
description: >
  Guides development of the Next.js/TypeScript Attorney Command Center for REVIEW_REQUIRED, QUARANTINED, and NEEDS_RECONCILIATION queues, coordinate-highlighted source previews, and digest-bound approvals. Use when building frontend views, review queues, PDF highlight overlays, or approval submission forms.
---

# WL-07: Attorney Command Center UI (Next.js / TypeScript)

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Command Center UI Rules

1. **Queue-First Review Surface:**
   - Center the UI on `workflow_run` review queues (`REVIEW_REQUIRED`, `QUARANTINED`, `NEEDS_RECONCILIATION`), matter timelines, `work_product` inspection, and `action_proposal` approval cards.

2. **Side-by-Side Source Verification:**
   - Clicking any finding or proposed deadline/reply highlights the exact `page_number` and `bbox` inside the permitted preview of the incoming correspondence or attachment.

3. **Digest-Bound Approval Submission:**
   - When an attorney clicks Approve, submit the exact displayed `action_digest` and `source_version` list so backend stale-state checks reject approvals if underlying facts changed while the screen was open.
