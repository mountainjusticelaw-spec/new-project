# PR #1 — Synthetic incoming correspondence control layer (locked)

This contract covers the local deterministic slice and the F-01, F-02, and F-05 control repairs. [Architecture decisions](../../docs/DECISIONS.md) lock the future stack to Google ADK + Gemini + Cloud SQL for PostgreSQL. PR1 does not invoke ADK or Gemini, connect live provider adapters, provision resources, enable billing or IAM, deploy, send communications, alter official records, or process client data. [Governance](../../docs/GOVERNANCE.md), [security policy](../../SECURITY.md), and [AGENTS.md](../../AGENTS.md) retain their authority.

The ten checked-in [workspace skills](../../skills/README.md) (`WL-01..WL-10`) record the WeaverLawOS skill controls across the roadmap. `WL-01` (`wl-01-scope-vertical-slice`), `WL-03` (`wl-03-rls-digest-security`), `WL-05` (`wl-05-state-machine-adk-harness`), and `WL-10` (`wl-10-synthetic-fixtures-gates`) govern PR1 scoping, RLS/action-digest/evidence security, deterministic state/replay orchestration, and synthetic plus live-PostgreSQL verification gates; `WL-02`, `WL-04`, `WL-06`, `WL-07`, `WL-08`, and `WL-09` define downstream legal, extraction, citation-audit, UI, OOXML, and adapter controls that remain inactive in PR1.

## Acceptance contract

Accept one wholly fictional Georgia opposing-counsel correspondence through the trusted synthetic boundary. Derive the metadata-only envelope, source-event dedup key, and immutable evidence digests. Atomically accept the inbox, received run, intake outbox, and initial audit. Resolve exact signals from the originating inbox; never replace its evidence with later caller claims. A unique eligible match permits scoped processing. No match, unmatched additional or conflicting signals, or a closed/restricted mapping quarantines without specialist invocation or Tier B writes.

Replays retain exactly one inbox/run/outbox. Matching evidence returns the existing run idempotently. Contradictory evidence preserves the original inbox, quarantines a nonterminal run, and records an anomaly. Before matter-scoped progress, recheck current mapping eligibility; revocation withdraws active scope and blocks further work. Reject stale status, illegal actors/edges, mismatched resolution input, and unauthorized mutations without partial state changes. Revocation and contradictory replay are deliberate audited quarantine operations, not ordinary validation failures.

Only the local mock policy executor simulates an approved effect. SQL approval/execution edges remain disabled; the schema grants no application Tier B writes or production execution capability.

## Envelope, evidence, and identity

`Envelope` uses Pydantic v2 with unknown fields forbidden, fixed schema version 1, `synthetic.gmail` / `correspondence.received`, nonzero firm/event/correlation UUIDs, UTC-aware ordered timestamps, synthetic opaque `source_ref` and `source_version`, and synthetic classification. Reject body, instructions, user-supplied dedup key, and asserted matter ID. Metadata syntax alone does not prove absence of real information: the ingress boundary must receive synthetic values only.

The dedup key is SHA-256 of canonical compact JSON `[schema_version, firm_id, source, event_type, source_ref, source_version]`. Event ID, correlation ID, and receipt timestamp are delivery metadata and do not change this identity. A new source version or firm creates a new key. Source identifiers and provider signals are exact and case-sensitive; no fuzzy matching.

At `ingest_event` / `receive`, freeze canonical `matter_signals`, `envelope_sha256`, optional `payload_sha256`, and combined `evidence_sha256` on `event_inbox`, alongside the source reference/version. Signals are sorted, unique, exact strings obtained from the trusted synthetic adapter boundary; an email or agent assertion is not trusted extraction. The canonical envelope digest excludes `event_id`, `correlation_id`, `received_at`, and the derived `dedup_key`; it includes intrinsic source metadata and `occurred_at`. Hashes use deterministic compact JSON serialization. Payload digest input is a digest, not permission to persist a body or retrieve live correspondence. These evidence columns are immutable after insertion.

`resolve_run` loads the run's original inbox evidence and uses its extracted signals. An optional supplied signal list must canonically match the bound list; a mismatch is `Denied` and cannot rebind the run. Record immutable `resolution_evidence` JSONB with `run_id`, `inbox_id`, `source_reference`, `source_version`, `extracted_signals`, `matched_clio_ids`, `mapping_version`, `decision`, and `reason_code`. `mapping_version` records the matched provider ID/version mapping. The SQL resolution guard independently derives/checks the evidence against the originating inbox and current mapping; adapters cannot authorize an arbitrary matter by providing a JSON claim.

Provider `clio_matter_id` is text and is distinct from internal UUID `verified_matter_id`. Match extracted provider signals to `clio_matter_id`; recheck resolved scope by `(firm_id, verified_matter_id)`. Never compare an internal UUID with a provider text identifier.

## Scope, immutability, and database authority

All 12 tables enable and force RLS. Four Tier A tables enforce scope-mode/nullability consistency. Inbox, intake outbox, and historical intake audit remain restricted. A run's current `verified_matter_id` is active scope. Its immutable `resolved_matter_id` anchors the original resolved matter and historical composite references, including audit/Tier B records, after active scope is withdrawn. Quarantine clears current scope without deleting or rewriting historical evidence. Matter-specific records carry nonnull historical verified matter and composite run/matter plus run/firm/matter FKs. Active access must also satisfy current eligibility and active scope; retained historical references do not permit continued processing.

Application roles are dedicated NOLOGIN/NOSUPERUSER/NOBYPASSRLS identities: intake, resolver, worker, independent reviewer, attorney, and executor. They have no production login configuration, privilege-bearing membership, table ownership, or Tier B write grants. `wl_audit_writer` is a separate restricted NOLOGIN/NOSUPERUSER/NOBYPASSRLS role that owns the audit trigger function and has only the audit insertion/sequence privileges it needs. Application roles cannot directly insert, update, delete, or truncate audit events. The audit trigger validates its originating relation and records the calling service role while running under its isolated writer identity.

The restricted `wl_scope_guard` role owns narrowly granted SECURITY DEFINER scope/replay/revalidation helpers. It has no members or login. Helpers validate trusted firm scope and allowed caller roles, use pinned `pg_catalog` search paths and schema-qualified relations, obey their RLS policies, and provide only the required operation. They do not expose general audit insertion, matter mutation, Tier B writes, or SQL execution. Temporary trigger forgery and temporary-table shadowing must not redirect these operations.

Trusted services set transaction-local firm/matter GUCs; missing context denies reads. GUCs are scope context, not authentication or a defense against a compromised credential. Production authentication, credential isolation, pool security, tamper-evident audit storage, and operational ownership require separate verification before live use. SQL special constructs such as `NULLIF` and `COALESCE` use standard syntax; schema qualification applies to ordinary functions and relations, not those constructs.

## Transactions, continuous eligibility, and replays

`receive` / `ingest_event` writes inbox, run, initial audit, and outbox in one transaction. A unique `(source, dedup_key)` arbitrates concurrent acceptance. A matching duplicate returns the existing run ID with `False`, not a second run or outbox. The trusted firm-scoped intake service may receive that opaque identifier; this is not a general matter-data read path.

On duplicate delivery, compare canonical envelope, signals, and payload evidence with the original inbox under the same replay-handling boundary. A contradiction never overwrites evidence. Any nonterminal run becomes `QUARANTINED`, `scope_mode='INTAKE_RESTRICTED'`, `verified_matter_id=NULL`, `reason_code='CONFLICTING_REPLAY_SIGNALS'`; append an audit anomaly. A terminal run retains its status and receives an anomaly without automatic reopening or retry. The helper serializes with run progress so a contradictory replay cannot race past quarantine.

`resolve_run` reads only its bound inbox/run and firm-scoped mapping, locks relevant mapping rows `FOR SHARE`, records resolution evidence, and performs CAS. `transition` / `transition_run` requires a caller-owned idle psycopg 3 connection, trusted actor, expected state, and verified scope. Reject missing/non-string actors and undefined edges before comparing the required role. Atomic updates include run ID, firm, expected status, and expected current matter. Zero rows is `Denied`.

Before any allowed `MATTER_VERIFIED` progress, the SQL guard and service layer revalidate OPEN eligibility using the internal firm/matter mapping with `FOR SHARE`. This includes `VALIDATED -> RUNNING` and `RUNNING -> REVIEW_REQUIRED`; mock-only approval/execution also rechecks eligibility. Mapping locks serialize concurrent eligibility revocation with progress. There is no unguarded specialist or simulated-effect path after revocation.

`revalidate_matter_eligibility` returns whether the current mapping remains eligible. A revoked mapping on an active `VALIDATED`, `RUNNING`, `REVIEW_REQUIRED`, or mock `APPROVED` run causes an audited `MATTER_INELIGIBLE` quarantine, clears current scope, and preserves resolution evidence/historical scope. A requested progress operation commits this safety quarantine before raising `Denied`, so callers must not assume every denial leaves status unchanged. The blocked requested progress itself never occurs. A missing mapping is ineligible and cannot supply scope.

SQL before-triggers independently enforce role/state, evidence, and scope rules. Audit and state changes share a transaction; audit failure rolls back the mutation. Source/step/approval/effect tables remain schema-only for application writes. The memory fake is single-threaded and is not proof of concurrency; the live gate exercises actual PostgreSQL locking, RLS, and rollback.

## State edges and stopping rules

The ordinary edges are `RECEIVED -> VALIDATED/QUARANTINED` (resolver), `VALIDATED -> RUNNING` (coordinator), `RUNNING -> REVIEW_REQUIRED` (independent review), `REVIEW_REQUIRED -> REJECTED` (attorney), `VALIDATED/RUNNING -> FAILED` (coordinator), and `VALIDATED/RUNNING/REVIEW_REQUIRED/APPROVED -> CANCELLED` (coordinator). Eligibility revocation and contradictory replay add the restricted safety-quarantine paths described above; they do not grant ordinary processing transitions to intake or agents.

Only the in-memory policy executor exercises `REVIEW_REQUIRED -> APPROVED -> EXECUTING -> COMPLETED/NEEDS_RECONCILIATION`. It checks accepted approval binding, digest, expiry, source/policy versions, current eligibility, permissions, revocation, destination, kill switch, and effect deduplication. It is not production authentication or durable execution. SQL approval/execution edges remain disabled until a separately scoped persistent policy executor exists.

Quarantine, failure, rejection, cancellation, completion, and reconciliation stop automatic processing. Reconciliation cannot be blindly retried. No autonomous legal decisions, deadlines, client acceptance, filing, or external effects are authorized.

## Exact local verification gate

Run from the repository root as a non-root local user with write access to `/tmp` and read access to the repository. PostgreSQL16+ tools `initdb`, `pg_ctl`, `psql`, and `createdb` must be available on `PATH`. The live suite always creates a fresh isolated temporary cluster on `127.0.0.1` plus a temporary Unix socket, creates `weaverlaw_pr1_test`, applies the migration, runs SQL assertions, and stops the cluster. It does not accept an existing database DSN. Roles are cluster-wide and the migration is intentionally non-idempotent; never run it against an existing or production database.

Dependency setup in a reviewed local virtual environment (Python >=3.11):

```sh
python -m pip install 'pydantic>=2.10,<3' 'pytest>=8,<10' 'psycopg[binary]>=3.2,<4'
```

Verify prerequisites and run the required gate:

```sh
initdb --version
pg_ctl --version
psql --version
createdb --version
python -c "import psycopg, psycopg_binary; print(psycopg.__version__, psycopg_binary.__version__)"
WEAVERLAW_REQUIRE_LIVE_PG=1 python -m pytest -v
python -m compileall -q services tests
git diff --check
```

The isolated pool regression can also be run independently:

```sh
WEAVERLAW_REQUIRE_LIVE_PG=1 python -m pytest tests/test_postgres_live.py -k pool -v
```

The required live gate must have zero skipped tests and zero errors. Report actual test counts and the exact pytest summary, environment versions, compilation/diff-check results, and working-tree state; do not hard-code a stale count into the contract. Coverage includes post-validation restriction, bound/mismatched resolution evidence, immutable evidence writes, contradictory duplicate replay, duplicate/CAS concurrency, two-session mapping revocation, forced RLS, temporary-trigger forgery, table shadowing, audit rollback, and pool cleanup. No real provider credentials or client records are needed.

Require independent code/SQL review and owner acceptance before merge. No Cloud SQL migration or deployment is performed by this gate. Rollback for this slice is branch/PR review and disposal of the fresh local test cluster; do not drop real data or roles.

## Phase 2 boundary

ADK/Gemini runtime, real extraction and independent citation verification, live Clio/Gmail/Dropbox adapters, authenticated production approvals, persistent effect leases, Pub/Sub publishing/reconciliation, command center, Cloud SQL provisioning, IAM, deployment, and production data remain outside PR1. Local hardening of evidence, eligibility, replay, isolation, and fixtures does not activate those capabilities.
