# Security and confidentiality policy

Required design controls; not evidence of implementation, legal compliance or production readiness. Owner: managing attorney; technical/security operator to be assigned before any deployment.

## Classification and prohibited repository content
Public reference material; firm-internal software/policies; confidential matter records; privileged/restricted evidence; credentials. GitHub permits only approved non-client-specific software, templates and wholly synthetic examples. Never commit live matter identifiers, evidence, medical or financial records, communications, model traces, database dumps, secrets, private keys or OAuth tokens. Private visibility alone is insufficient. Use MFA, least-privilege collaboration, reviewed branches and secret scanning where supported; settings require separate authorization.

## Authorization and isolation
Authenticate users and service identities; enforce role, matter membership, action and environment at every API, tool, retrieval, storage and executor boundary. Deny by default. Scope all queries including vector retrieval, caches, bulk exports and indirect identifiers. No cross-matter memory or training corpus. Use per-service identities; no shared owner credentials. Short-lived identity federation for CI; Secret Manager for credentials, rotation and revocation. No persistent service-account keys in GitHub. Verify negative and revoked-access tests.

## External processing
Review vendor contracts, retention, training use, region, subprocessors, grounding/search data flows and legal database licensing before any real-data transfer. Minimize payloads; use approved endpoints and data classifications; prohibit unapproved egress. Redaction is not automatically sufficient. Encrypt transport and storage; define keys, retention, legal holds, export, disposal and backup lifecycle. Never destroy originals or held records automatically.

## Untrusted input and AI
Emails, attachments, websites, retrieved snippets, tools, prompts, memory and outputs are untrusted. Separate data from instructions; ignore embedded instructions; allowlist tools and validate arguments independently. Restrict network destinations, file types, sizes, parser runtime and archive expansion; quarantine suspicious content. No secrets in prompts or model-visible tools. Validate output schemas; require sources, uncertainty and abstention. Model Armor or similar controls may be defense in depth, never sole authorization.

## Authority and external writes
Read-only by default. Require verified matter and policy before reads. Proposals remain internal until human review. Exact-action approval by an authorized human is required for Clio writes, calendar changes, external communications and document moves. Do not automate conflict clearance, engagements, filing, final legal opinions, deadline confirmation or original evidence deletion. Explicit attorney authority and separate processes apply. Independent executor enforces approvals, expiry, source freshness, idempotency, revocation and kill switch. Deny on any missing or stale condition.

## Observability and incidents
Audit event IDs, actor/service identity, scope, policy and agent versions, source references/hashes, action digest, decision, outcome and timestamps without logging raw sensitive content or credentials. Protect audit integrity and access; restrict sensitive work-product stores. Alerts for failed authentication, scope violations, unexpected egress, queue lag, missed schedules, abnormal cost and approval failures. On suspected compromise: stop affected jobs, preserve evidence, revoke/rotate credentials, assess scope and notify designated attorney/security owner; do not destroy evidence. A committed secret must be treated as exposed, rotated first, then investigate history cleanup separately.

## Release gate
Before real data: approve vendor terms, data inventory/retention, access matrix, threat model, penetration/access tests, backup/restore evidence, incident and rollback runbooks, monitoring and budgets, independent review and written attorney approval. Confirm no deployment or live data in this foundation. Controls must be implemented and verified, not assumed from documentation.
