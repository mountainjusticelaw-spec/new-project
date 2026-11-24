---
name: wl-06-citation-provenance-auditor
description: >
  Builds the mandatory Independent Review gate verifying statutory/case citations (eyecite), matter-scoped source_reference versions, and verbatim quote alignment (rapidfuzz) before REVIEW_REQUIRED. Use when auditing work_product or action_proposal records for hallucinations or mismatched provenance. Do not let generative models self-certify their own citations.
---

# WL-06: Deterministic Citation, Quote & Provenance Auditor (Independent Review)

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Independent Review Gate (`F -> H -> I`)

Implement the architecture's Independent Review step as a mandatory deterministic + verifier gate before a run enters `REVIEW_REQUIRED`:

1. Parse every statutory/case citation (`eyecite`) and every `source_reference` pointer in `work_product` and `action_proposal`.
2. Verify every `source_reference` belongs to the run's verified `matter_id` and matches the pinned `source_version`.
3. Verify verbatim quotes against `raw_excerpt` (`rapidfuzz` >= 95 alignment) and resolve exact page/coordinate spans.
4. Flag any unverified authority, mismatched quote, or unsupported inference explicitly so the Attorney Command Center highlights exceptions before approval.
