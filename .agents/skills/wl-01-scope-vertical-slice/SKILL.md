---
name: wl-01-scope-vertical-slice
description: >
  Enforces staged vertical-slice scoping, single-question ambiguity gates, synthetic acceptance tests, and honest verification reporting for WeaverLawOS. Use when planning features, scoping PRs, starting branches, or reporting verification status. Do not use to bypass repository governance in AGENTS.md.
---

# WL-01: Scope & Staged Vertical-Slice Execution

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Core Rules

1. **Inspect Before Editing:** Read applicable schemas, standards (`README.md`, `ARCHITECTURE.md`, `SECURITY.md`, `AGENTS.md`), and tests first. Surface conflicting instructions instead of guessing.
2. **One-Question Gate:** Ask at most ONE focused question only when ambiguity affects schema, state transitions, or legal framing.
3. **Lock Slice & Synthetic Acceptance Test:** Define one bounded deliverable and a runnable local test using synthetic fixtures before writing feature code. Park future ideas in a visible backlog.
4. **Honest Verification Reporting:** Report exact files changed, tests actually executed (never claim unrun tests passed), residual risks, rollback notes, and the next review gate.
