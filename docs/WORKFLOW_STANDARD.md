# Workflow and trigger standard

A workflow is a versioned, deterministic control graph around bounded AI judgment. A trigger is a validated event or schedule, not permission to act.

## Required definition
workflow_id/version; owner; goal; event schema and authorized source; dedup key; matter resolution; prerequisites; step graph and data contract; tool and scope permissions; time/cost budgets; retries and dead-letter handling; checkpoint and terminal states; human review and exact-action approval; effect idempotency; audit fields; recovery, cancellation, replay and rollback; acceptance tests.

## Event intake
Verify origin and replay protection appropriate to provider. Persist before acknowledging; deduplicate at source-event level and semantic job/effect level. Treat delivery as possibly duplicated, late or out of order. Use UTC internally; store source timezone and display local timezone explicitly. Schedules must define timezone, missed-run policy and overlap behavior. Reconcile missing notifications independently. Never infer service/filing dates or court deadlines from message receipt.

## Orchestration
Use deterministic validation and state transitions; AI interprets unstructured content only. Persist step checkpoints and versioned artifacts; bound retries and concurrent runs. Missing/ambiguous matter, authority, source, permission or policy causes quarantine or review, not guessed action. Route urgent exceptions visibly to assigned human; avoid duplicate alerts. Cancellation or kill switch prevents future effects; in-flight ambiguous effects require reconciliation.

## Approval and execution
A review packet includes source references, findings, uncertainties, proposed actions, destination, impact and source versions. Approval is authenticated and exact-action bound, expiring and revocable; any change invalidates it. No agent can approve. Executor validates current state, role, scope, digest, idempotency and budget; journal attempts and confirmed effects; unknown result blocks blind retry. Rejections stop affected actions. Read-only analysis can finish with a draft and no external changes.

## Tests
Duplicate/out-of-order events, missing data, forged or stale event, ambiguous matter, prompt injection, wrong matter, tool timeout, malformed output, expired/revoked/altered approval, concurrent retries, failed effect and uncertain result. Verify source-linked work product, zero unauthorized effects, deterministic exception paths and visible recovery.
