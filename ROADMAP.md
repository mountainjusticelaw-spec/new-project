# Roadmap and acceptance gates

## Phase 0 — Foundation (this commit)
Versioned architecture, standards, policies, directory ownership and synthetic workflow specification. No executable services, credentials, infrastructure or connections.

## Phase 1 — Local synthetic vertical slice
Implement Python contracts, deterministic matter resolver, policy gate, in-memory or local persistence and mock adapters; add ADK/model adapter only after an approved test configuration. No external API calls by default. Acceptance: one synthetic correspondence event yields a source-linked draft review packet, no official record changes; duplicate events do not duplicate jobs, ambiguous identity quarantines, missing sources abstain, denial prevents writes, failures are recoverable.

## Phase 2 — Authorized development cloud
Review actual Google Cloud account, budget, regions and service availability; propose IaC and IAM plan. After separate approval, provision only dev with synthetic data, authenticate services, verify retry/outbox, backup restore, logs and cost alerts. No production credentials.

## Phase 3 — Limited read-only pilot
Obtain separate approval for specific integrations/scopes and data classes; begin read-only with approved test accounts or an explicitly authorized pilot matter. Reconcile missed events, verify source mapping, extraction and evidence coverage. Independent confidentiality and legal review required.

## Phase 4 — Specialized agents and review UI
COO, evidence, docket, practice division, research and senior reviewer; attorney review queue and source previews. Evaluate representative and adversarial fixtures, monitor cost/latency. Approval executor remains simulated until a separate release gate.

## Phase 5 — Controlled writes and production
Approve exact action categories, named owners, staging and production environments, monitored release, rollback and restore drill. Test stale approval, tampering, revoked permissions, retries and uncertain provider response. No unattended filing, conflict clearance or engagement.

## Phase 6 — Agent/workflow builder
Versioned specifications, schema validation, test/evaluation before activation, permission changes requiring separate review, rollback to previous definitions.

## Success measures
For synthetic MVP: zero unauthorized writes/cross-matter retrievals in test set, zero duplicate effects, every material assertion source-linked or marked unsupported, every uncertain deadline labeled unverified, every run terminal or visibly actionable. Measure human intervention rate, processing latency and cost per successful packet; set thresholds from pilot evidence. Never treat a passing small test set as proof of safety. Managing attorney signs acceptance; technical operator records tests and unresolved risks.
