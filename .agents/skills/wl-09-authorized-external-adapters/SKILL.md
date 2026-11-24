---
name: wl-09-authorized-external-adapters
description: >
  Defines isolated Python Protocol boundaries and synthetic local test doubles for Clio, Dropbox, Gmail, Calendar, and primary law connectors with email-attachment family preservation. Use when designing external integration interfaces, test doubles, or effect_journal write gates. Do not wire live credentials or unapproved external network calls during baseline development.
---

# WL-09: Authorized External Adapters (Clio, Dropbox, Gmail, Primary Law)

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## External Adapter Isolation Rules

1. **Strict Adapter Isolation:**
   - Build clean interface boundaries (`Protocol` / ABCs) backed by **synthetic local test doubles** in v0.1 so zero live credentials or network calls are required during baseline development.
   - Preserve parent-attachment document families (`family_id` linking incoming email envelope and attachments) on correspondence ingress.
   - Read adapters inherit source ACLs; write adapters accept calls ONLY from the Policy & Approval Executor with a verified `effect_journal` lease.
