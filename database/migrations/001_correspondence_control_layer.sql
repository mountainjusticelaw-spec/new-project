-- v0.1 / PR1. Apply ONLY to an empty, disposable PostgreSQL database after review.
-- No Cloud SQL provisioning, live identities, grants to LOGIN roles, or external calls.
BEGIN;
DO $$ BEGIN
 IF pg_catalog.current_setting('server_version_num')::integer < 160000 THEN
  RAISE EXCEPTION 'PostgreSQL 16 or newer is required' USING ERRCODE='0A000';
 END IF;
END $$;
CREATE SCHEMA weaverlaw;
SET LOCAL search_path = pg_catalog;
CREATE ROLE wl_intake NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_resolver NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_reviewer NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_attorney NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_executor NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_audit_writer NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_scope_guard NOLOGIN NOSUPERUSER NOBYPASSRLS;
REVOKE ALL ON SCHEMA weaverlaw FROM PUBLIC;
GRANT USAGE ON SCHEMA weaverlaw TO wl_intake,wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor,wl_audit_writer,wl_scope_guard;
CREATE TYPE weaverlaw.scope_mode AS ENUM ('INTAKE_RESTRICTED','MATTER_VERIFIED');
CREATE TYPE weaverlaw.run_status AS ENUM ('RECEIVED','VALIDATED','RUNNING','REVIEW_REQUIRED','APPROVED','EXECUTING','COMPLETED','REJECTED','FAILED','QUARANTINED','CANCELLED','NEEDS_RECONCILIATION');
CREATE DOMAIN weaverlaw.hex_digest AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN weaverlaw.opaque_id AS text CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$');
CREATE FUNCTION weaverlaw.context_firm() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
 SELECT NULLIF(pg_catalog.current_setting('app.verified_firm_id',true),'')::pg_catalog.uuid
$$;
CREATE FUNCTION weaverlaw.context_matter() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
 SELECT NULLIF(pg_catalog.current_setting('app.verified_matter_id',true),'')::pg_catalog.uuid
$$;
CREATE FUNCTION weaverlaw.scope_valid(mode weaverlaw.scope_mode, matter uuid) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
 SELECT (mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND matter IS NULL) OR (mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND matter IS NOT NULL)
$$;
CREATE TABLE weaverlaw.matter_mapping (
 firm_id uuid NOT NULL, verified_matter_id uuid NOT NULL, clio_matter_id weaverlaw.opaque_id NOT NULL,
 eligibility text NOT NULL CHECK (eligibility IN ('OPEN','CLOSED','RESTRICTED')),
 mapping_version weaverlaw.opaque_id NOT NULL, PRIMARY KEY (firm_id,verified_matter_id), UNIQUE(firm_id,clio_matter_id)
);
-- Canonical immutable adapter evidence: exact strings, sorted and deduplicated.
CREATE FUNCTION weaverlaw.canonical_signals(signals jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE result jsonb; item jsonb; v_value text; code integer;
BEGIN
 IF signals IS NULL OR pg_catalog.jsonb_typeof(signals) <> 'array' THEN
  RAISE EXCEPTION 'signal array required' USING ERRCODE='23514';
 END IF;
 IF pg_catalog.jsonb_array_length(signals)>1024 THEN RAISE EXCEPTION 'too many input signals' USING ERRCODE='23514'; END IF;
 FOR item IN SELECT * FROM pg_catalog.jsonb_array_elements(signals) LOOP
  IF pg_catalog.jsonb_typeof(item)<>'string' THEN RAISE EXCEPTION 'signal strings required' USING ERRCODE='23514'; END IF;
  v_value:=item#>>'{}';
  IF pg_catalog.char_length(v_value) NOT BETWEEN 1 AND 128 THEN RAISE EXCEPTION 'signal length out of bounds' USING ERRCODE='23514'; END IF;
  FOR code IN SELECT pg_catalog.ascii(pg_catalog.substr(v_value,n,1)) FROM pg_catalog.generate_series(1,pg_catalog.char_length(v_value)) n LOOP
   -- Exactly Python str.isspace(), plus C0/C1 controls. No Unicode normalization.
   IF code<=32 OR code BETWEEN 127 AND 160 OR code=5760 OR code BETWEEN 8192 AND 8202
    OR code IN (8232,8233,8239,8287,12288) THEN
    RAISE EXCEPTION 'signal whitespace or control character denied' USING ERRCODE='23514';
   END IF;
  END LOOP;
 END LOOP;
 SELECT COALESCE(pg_catalog.jsonb_agg(items.value ORDER BY items.value COLLATE "C"), '[]'::pg_catalog.jsonb) INTO result
 FROM (SELECT DISTINCT vals.value COLLATE "C" AS value FROM pg_catalog.jsonb_array_elements_text(signals) vals(value)) items;
 IF pg_catalog.jsonb_array_length(result)>32 THEN RAISE EXCEPTION 'too many unique signals' USING ERRCODE='23514'; END IF;
 RETURN result;
END $$;
CREATE TABLE weaverlaw.event_inbox (
 inbox_id uuid PRIMARY KEY, firm_id uuid NOT NULL, event_id uuid NOT NULL, source text NOT NULL CHECK(source='synthetic.gmail'),
 event_type text NOT NULL CHECK(event_type='correspondence.received'), schema_version integer NOT NULL CHECK(schema_version=1),
 occurred_at timestamptz NOT NULL, received_at timestamptz NOT NULL CHECK(received_at>=occurred_at), correlation_id uuid NOT NULL,
 source_ref weaverlaw.opaque_id NOT NULL CHECK(source_ref LIKE 'synthetic-%'), source_version weaverlaw.opaque_id NOT NULL, sensitivity text NOT NULL CHECK(sensitivity='synthetic'), dedup_key weaverlaw.hex_digest NOT NULL,
 matter_signals jsonb NOT NULL DEFAULT '[]'::pg_catalog.jsonb CHECK(matter_signals=weaverlaw.canonical_signals(matter_signals)),
 envelope_sha256 weaverlaw.hex_digest NOT NULL,
 payload_sha256 weaverlaw.hex_digest,
 evidence_sha256 weaverlaw.hex_digest NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)), UNIQUE(source,dedup_key), UNIQUE(inbox_id,firm_id),
 FOREIGN KEY(firm_id,verified_matter_id) REFERENCES weaverlaw.matter_mapping(firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.workflow_run (
 run_id uuid PRIMARY KEY, firm_id uuid NOT NULL, inbox_id uuid NOT NULL UNIQUE,
 replay_conflict_count bigint NOT NULL DEFAULT 0 CHECK(replay_conflict_count>=0),
 replay_conflict_digests weaverlaw.hex_digest[] NOT NULL DEFAULT ARRAY[]::weaverlaw.hex_digest[] CHECK(pg_catalog.cardinality(replay_conflict_digests)<=8 AND pg_catalog.array_position(replay_conflict_digests,NULL) IS NULL),
 resolved_matter_id uuid, resolution_evidence jsonb CHECK(resolution_evidence IS NULL OR pg_catalog.jsonb_typeof(resolution_evidence)='object'),
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 status weaverlaw.run_status NOT NULL DEFAULT 'RECEIVED'::weaverlaw.run_status, reason_code weaverlaw.opaque_id NOT NULL DEFAULT 'ACCEPTED'::weaverlaw.opaque_id,
 created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)),
 CHECK(status NOT IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status,'COMPLETED'::weaverlaw.run_status,'REJECTED'::weaverlaw.run_status,'FAILED'::weaverlaw.run_status,'CANCELLED'::weaverlaw.run_status,'NEEDS_RECONCILIATION'::weaverlaw.run_status) OR (scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id IS NOT NULL)),
 CHECK(verified_matter_id IS NULL OR (resolved_matter_id IS NOT NULL AND verified_matter_id=resolved_matter_id)),
 CHECK(status NOT IN ('RECEIVED'::weaverlaw.run_status,'QUARANTINED'::weaverlaw.run_status) OR scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode),
 CHECK(status <> 'QUARANTINED'::weaverlaw.run_status OR reason_code IN ('UNMAPPED_MATTER_SIGNAL','CONFLICTING_MATTER_SIGNALS','MATTER_INELIGIBLE','CONFLICTING_REPLAY_SIGNALS')),
 UNIQUE(run_id,resolved_matter_id), UNIQUE(run_id,firm_id), UNIQUE(run_id,firm_id,resolved_matter_id),
 FOREIGN KEY(inbox_id,firm_id) REFERENCES weaverlaw.event_inbox(inbox_id,firm_id),
 FOREIGN KEY(firm_id,verified_matter_id) REFERENCES weaverlaw.matter_mapping(firm_id,verified_matter_id),
 FOREIGN KEY(firm_id,resolved_matter_id) REFERENCES weaverlaw.matter_mapping(firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.outbox (
 outbox_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 topic text NOT NULL CHECK(topic='RESOLUTION_PENDING'), idempotency_key weaverlaw.opaque_id NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)), UNIQUE(run_id,topic),
 FOREIGN KEY(run_id,firm_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id)
);
CREATE TABLE weaverlaw.audit_event (
 audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL, verified_matter_id uuid,
 old_status weaverlaw.run_status, new_status weaverlaw.run_status NOT NULL, actor text NOT NULL, reason_code weaverlaw.opaque_id NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)),
 FOREIGN KEY(run_id,firm_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id)
);
-- Tier B: all seven tables have nonnullable verified scope and BOTH composite FKs.
CREATE TABLE weaverlaw.workflow_step (
 step_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 step_name weaverlaw.opaque_id NOT NULL, idempotency_key weaverlaw.opaque_id NOT NULL, attempt integer NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 3),
 status text NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
 UNIQUE(run_id,idempotency_key), UNIQUE(step_id,run_id,firm_id,verified_matter_id),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id)
);
CREATE TABLE weaverlaw.agent_run (
 agent_run_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL, step_id uuid NOT NULL,
 model_version weaverlaw.opaque_id NOT NULL, prompt_version weaverlaw.opaque_id NOT NULL, schema_version weaverlaw.opaque_id NOT NULL, tool_version weaverlaw.opaque_id NOT NULL,
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id),
 FOREIGN KEY(step_id,run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_step(step_id,run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.source_reference (
 source_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 source_system text NOT NULL CHECK(source_system IN ('synthetic.gmail','synthetic.dropbox')), external_id weaverlaw.opaque_id NOT NULL,
 source_version weaverlaw.opaque_id NOT NULL, sha256 weaverlaw.hex_digest NOT NULL, family_id uuid, page_number integer CHECK(page_number>0),
 UNIQUE(run_id,source_system,external_id,source_version), UNIQUE(source_id,run_id,firm_id,verified_matter_id),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id)
);
CREATE TABLE weaverlaw.work_product (
 work_product_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 source_id uuid NOT NULL, version integer NOT NULL CHECK(version>0),
 review_banner text NOT NULL DEFAULT 'PROPOSED WORK PRODUCT — REQUIRES GEORGIA ATTORNEY REVIEW' CHECK(review_banner='PROPOSED WORK PRODUCT — REQUIRES GEORGIA ATTORNEY REVIEW'),
 content jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(content)='object'),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id),
 FOREIGN KEY(source_id,run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.source_reference(source_id,run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.action_proposal (
 proposal_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 action_type weaverlaw.opaque_id NOT NULL, destination weaverlaw.opaque_id NOT NULL, normalized_payload jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(normalized_payload)='object'),
 source_reference_versions jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(source_reference_versions)='array' AND pg_catalog.jsonb_array_length(source_reference_versions)>0),
 policy_version weaverlaw.opaque_id NOT NULL, action_digest weaverlaw.hex_digest NOT NULL,
 UNIQUE(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id)
);
CREATE TABLE weaverlaw.approval (
 approval_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 proposal_id uuid NOT NULL, action_digest weaverlaw.hex_digest NOT NULL, policy_version weaverlaw.opaque_id NOT NULL, destination weaverlaw.opaque_id NOT NULL,
 reviewer_id uuid NOT NULL, approved_at timestamptz NOT NULL, expires_at timestamptz NOT NULL CHECK(expires_at>approved_at), revoked_at timestamptz,
 UNIQUE(approval_id,run_id,firm_id,verified_matter_id,action_digest),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id),
 FOREIGN KEY(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination)
 REFERENCES weaverlaw.action_proposal(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination)
);
CREATE TABLE weaverlaw.effect_journal (
 effect_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 approval_id uuid NOT NULL, action_digest weaverlaw.hex_digest NOT NULL, idempotency_key weaverlaw.hex_digest NOT NULL,
 status text NOT NULL CHECK(status IN ('LEASED','CONFIRMED','NEEDS_RECONCILIATION')), lease_id uuid NOT NULL,
 UNIQUE(firm_id,verified_matter_id,idempotency_key),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,resolved_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,resolved_matter_id),
 FOREIGN KEY(approval_id,run_id,firm_id,verified_matter_id,action_digest) REFERENCES weaverlaw.approval(approval_id,run_id,firm_id,verified_matter_id,action_digest)
);
CREATE INDEX workflow_run_firm_status_updated_idx ON weaverlaw.workflow_run(firm_id,status,updated_at);
CREATE INDEX workflow_run_firm_resolved_idx ON weaverlaw.workflow_run(firm_id,resolved_matter_id);
CREATE INDEX audit_event_firm_run_audit_idx ON weaverlaw.audit_event(firm_id,run_id,audit_id);
CREATE INDEX outbox_firm_created_idx ON weaverlaw.outbox(firm_id,created_at);

-- Derive initial and current bindings from the same immutable inbox, without changing evidence.
CREATE FUNCTION weaverlaw.derive_resolution_evidence(p_firm uuid,p_inbox uuid,p_run uuid)
 RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
 v_inbox weaverlaw.event_inbox%ROWTYPE; v_matches jsonb; v_versions jsonb;
 v_count integer; v_matter uuid; v_reason text; v_target weaverlaw.run_status;
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() THEN RETURN NULL; END IF;
 SELECT * INTO v_inbox FROM weaverlaw.event_inbox i WHERE i.inbox_id=p_inbox AND i.firm_id=p_firm;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT COALESCE(pg_catalog.jsonb_agg(m.clio_matter_id::pg_catalog.text ORDER BY m.clio_matter_id::pg_catalog.text COLLATE "C"),'[]'::pg_catalog.jsonb),
  COALESCE(pg_catalog.jsonb_object_agg(m.clio_matter_id::pg_catalog.text,m.mapping_version::pg_catalog.text),'{}'::pg_catalog.jsonb),
  pg_catalog.count(*)::integer INTO v_matches,v_versions,v_count
  FROM weaverlaw.matter_mapping m WHERE m.firm_id=p_firm
   AND m.clio_matter_id::pg_catalog.text IN (SELECT value FROM pg_catalog.jsonb_array_elements_text(v_inbox.matter_signals));
 IF pg_catalog.jsonb_array_length(v_inbox.matter_signals)=0 OR v_count=0 THEN v_reason:='UNMAPPED_MATTER_SIGNAL';
 ELSIF v_count<>pg_catalog.jsonb_array_length(v_inbox.matter_signals) OR v_count<>1 THEN v_reason:='CONFLICTING_MATTER_SIGNALS';
 ELSE
  SELECT m.verified_matter_id INTO v_matter FROM weaverlaw.matter_mapping m WHERE m.firm_id=p_firm
   AND m.clio_matter_id::pg_catalog.text=(v_matches->>0) AND m.eligibility='OPEN';
  IF v_matter IS NULL THEN v_reason:='MATTER_INELIGIBLE'; ELSE v_reason:='MATTER_VERIFIED'; END IF;
 END IF;
 v_target:=CASE WHEN v_matter IS NULL THEN 'QUARANTINED'::weaverlaw.run_status ELSE 'VALIDATED'::weaverlaw.run_status END;
 RETURN pg_catalog.jsonb_build_object(
  'run_id',p_run,'inbox_id',p_inbox,'source_reference',v_inbox.source_ref,'source_version',v_inbox.source_version,
  'extracted_signals',v_inbox.matter_signals,'matched_clio_ids',v_matches,'mapping_version',v_versions,
  'decision',v_target,'verified_matter_id',v_matter,'reason_code',v_reason,
  'envelope_sha256',v_inbox.envelope_sha256,'payload_sha256',v_inbox.payload_sha256,'evidence_sha256',v_inbox.evidence_sha256);
END $$;
ALTER FUNCTION weaverlaw.derive_resolution_evidence(uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.derive_resolution_evidence(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.derive_resolution_evidence(uuid,uuid,uuid) TO wl_resolver;

-- Lock all original signal matches and the historical anchor in UUID order before run locks.
CREATE FUNCTION weaverlaw.lock_resolution_inputs(p_firm uuid,p_inbox uuid,p_expected uuid)
 RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_signals jsonb;
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() THEN RETURN false; END IF;
 SELECT i.matter_signals INTO v_signals FROM weaverlaw.event_inbox i WHERE i.inbox_id=p_inbox AND i.firm_id=p_firm;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM m.verified_matter_id FROM weaverlaw.matter_mapping m
  WHERE m.firm_id=p_firm AND (m.verified_matter_id=p_expected
   OR m.clio_matter_id::pg_catalog.text IN (SELECT value FROM pg_catalog.jsonb_array_elements_text(v_signals)))
  ORDER BY m.verified_matter_id FOR SHARE;
 RETURN true;
END $$;
ALTER FUNCTION weaverlaw.lock_resolution_inputs(uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.lock_resolution_inputs(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.lock_resolution_inputs(uuid,uuid,uuid) TO wl_resolver;

CREATE FUNCTION weaverlaw.mapping_is_open(p_firm uuid,p_matter uuid) RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
 SELECT p_firm=weaverlaw.context_firm() AND EXISTS(
  SELECT 1 FROM weaverlaw.matter_mapping m WHERE m.firm_id=p_firm AND m.verified_matter_id=p_matter AND m.eligibility='OPEN'
 )
$$;
ALTER FUNCTION weaverlaw.mapping_is_open(uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.mapping_is_open(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.mapping_is_open(uuid,uuid) TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor;

-- Evaluated from the candidate row fields, including UPDATE RETURNING during initial promotion.
CREATE FUNCTION weaverlaw.origin_binds(p_firm uuid,p_inbox uuid,p_run uuid,p_matter uuid)
 RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_evidence jsonb;
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() OR p_matter IS NULL THEN RETURN false; END IF;
 v_evidence:=weaverlaw.derive_resolution_evidence(p_firm,p_inbox,p_run);
 RETURN COALESCE(v_evidence->>'decision'='VALIDATED' AND (v_evidence->>'verified_matter_id')::pg_catalog.uuid=p_matter
  AND weaverlaw.mapping_is_open(p_firm,p_matter),false);
END $$;
ALTER FUNCTION weaverlaw.origin_binds(uuid,uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.origin_binds(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.origin_binds(uuid,uuid,uuid,uuid) TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor;
-- Nonlocking SELECT authorization also verifies provider identity still binds to the original matter.
CREATE FUNCTION weaverlaw.scope_is_active(p_run uuid,p_firm uuid,p_matter uuid) RETURNS boolean
 LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_run weaverlaw.workflow_run%ROWTYPE; v_evidence jsonb;
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() OR p_matter IS DISTINCT FROM weaverlaw.context_matter() THEN RETURN false; END IF;
 SELECT * INTO v_run FROM weaverlaw.workflow_run r WHERE r.run_id=p_run AND r.firm_id=p_firm;
 IF NOT FOUND OR v_run.scope_mode<>'MATTER_VERIFIED'::weaverlaw.scope_mode
  OR v_run.verified_matter_id IS DISTINCT FROM p_matter OR v_run.resolved_matter_id IS DISTINCT FROM p_matter THEN RETURN false; END IF;
 v_evidence:=weaverlaw.derive_resolution_evidence(p_firm,v_run.inbox_id,p_run);
 RETURN COALESCE(v_evidence->>'decision'='VALIDATED' AND (v_evidence->>'verified_matter_id')::pg_catalog.uuid=p_matter
  AND weaverlaw.mapping_is_open(p_firm,p_matter),false);
END $$;
ALTER FUNCTION weaverlaw.scope_is_active(uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.scope_is_active(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.scope_is_active(uuid,uuid,uuid) TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor;

-- UPDATE USING executes this before tuple locking; the trigger repeats the check defensively.
CREATE FUNCTION weaverlaw.lock_mapping_is_open(p_firm uuid,p_matter uuid,p_run uuid) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_run weaverlaw.workflow_run%ROWTYPE; v_evidence jsonb;
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() OR p_matter IS DISTINCT FROM weaverlaw.context_matter() THEN RETURN false; END IF;
 SELECT * INTO v_run FROM weaverlaw.workflow_run r WHERE r.run_id=p_run AND r.firm_id=p_firm;
 IF NOT FOUND OR v_run.resolved_matter_id IS DISTINCT FROM p_matter THEN RETURN false; END IF;
 IF NOT weaverlaw.lock_resolution_inputs(p_firm,v_run.inbox_id,p_matter) THEN RETURN false; END IF;
 v_evidence:=weaverlaw.derive_resolution_evidence(p_firm,v_run.inbox_id,p_run);
 RETURN COALESCE(v_evidence->>'decision'='VALIDATED' AND (v_evidence->>'verified_matter_id')::pg_catalog.uuid=p_matter
  AND weaverlaw.mapping_is_open(p_firm,p_matter),false);
END $$;
ALTER FUNCTION weaverlaw.lock_mapping_is_open(uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.lock_mapping_is_open(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.lock_mapping_is_open(uuid,uuid,uuid) TO wl_resolver,wl_worker,wl_reviewer,wl_attorney;
-- All tables force RLS. No public policies; absent scope is deny-by-default.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['matter_mapping','event_inbox','workflow_run','outbox','audit_event','workflow_step','agent_run','source_reference','work_product','action_proposal','approval','effect_journal'] LOOP
  EXECUTE pg_catalog.format('ALTER TABLE weaverlaw.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE pg_catalog.format('ALTER TABLE weaverlaw.%I FORCE ROW LEVEL SECURITY',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['event_inbox','workflow_run','outbox','audit_event'] LOOP
  EXECUTE pg_catalog.format('CREATE POLICY intake_read ON weaverlaw.%I FOR SELECT TO wl_intake,wl_resolver USING (firm_id=weaverlaw.context_firm() AND scope_mode=''INTAKE_RESTRICTED''::weaverlaw.scope_mode)',t);
  IF t='event_inbox' THEN
   EXECUTE pg_catalog.format('CREATE POLICY matter_read ON weaverlaw.%I FOR SELECT TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND scope_mode=''MATTER_VERIFIED''::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter() AND weaverlaw.mapping_is_open(firm_id,verified_matter_id))',t);
  ELSIF t='workflow_run' THEN
   EXECUTE 'CREATE POLICY matter_read ON weaverlaw.workflow_run FOR SELECT TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND scope_mode=''MATTER_VERIFIED''::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter() AND weaverlaw.origin_binds(firm_id,inbox_id,run_id,resolved_matter_id))';
  ELSE
   EXECUTE pg_catalog.format('CREATE POLICY matter_read ON weaverlaw.%I FOR SELECT TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND scope_mode=''MATTER_VERIFIED''::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter() AND weaverlaw.scope_is_active(run_id,firm_id,verified_matter_id))',t);
  END IF;
 END LOOP;
 FOREACH t IN ARRAY ARRAY['workflow_step','agent_run','source_reference','work_product','action_proposal','approval','effect_journal'] LOOP
  EXECUTE pg_catalog.format('CREATE POLICY matter_read ON weaverlaw.%I FOR SELECT TO wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND verified_matter_id=weaverlaw.context_matter() AND weaverlaw.scope_is_active(run_id,firm_id,verified_matter_id))',t);
 END LOOP;
END $$;
CREATE POLICY mapping_resolve ON weaverlaw.matter_mapping FOR SELECT TO wl_resolver,wl_scope_guard USING(firm_id=weaverlaw.context_firm());
-- Permit FOR SHARE row locking during resolution while denying any actual UPDATE via WITH CHECK (false) and trigger.
CREATE POLICY mapping_lock_only ON weaverlaw.matter_mapping FOR UPDATE TO wl_resolver,wl_scope_guard USING(firm_id=weaverlaw.context_firm()) WITH CHECK(false);
-- Seed only as the migration owner via explicitly reviewed fixture grants/policy; no app mutation.
CREATE POLICY inbox_insert ON weaverlaw.event_inbox FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY run_insert ON weaverlaw.workflow_run FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND status='RECEIVED'::weaverlaw.run_status AND reason_code='ACCEPTED' AND resolved_matter_id IS NULL AND resolution_evidence IS NULL AND replay_conflict_count=0 AND pg_catalog.cardinality(replay_conflict_digests)=0);
CREATE POLICY outbox_insert ON weaverlaw.outbox FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY resolve_update ON weaverlaw.workflow_run FOR UPDATE TO wl_resolver USING(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND weaverlaw.lock_resolution_inputs(firm_id,inbox_id,resolved_matter_id)) WITH CHECK(firm_id=weaverlaw.context_firm() AND (scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode OR (scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter())));
CREATE POLICY worker_update ON weaverlaw.workflow_run FOR UPDATE TO wl_worker,wl_reviewer,wl_attorney USING(firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter() AND weaverlaw.lock_mapping_is_open(firm_id,verified_matter_id,run_id)) WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter());
CREATE POLICY guard_run_read ON weaverlaw.workflow_run FOR SELECT TO wl_scope_guard USING(firm_id=weaverlaw.context_firm());
CREATE POLICY guard_run_update ON weaverlaw.workflow_run FOR UPDATE TO wl_scope_guard USING(firm_id=weaverlaw.context_firm()) WITH CHECK(firm_id=weaverlaw.context_firm());
CREATE POLICY guard_inbox_read ON weaverlaw.event_inbox FOR SELECT TO wl_scope_guard USING(firm_id=weaverlaw.context_firm());
CREATE POLICY audit_insert_intake ON weaverlaw.audit_event FOR INSERT TO wl_audit_writer WITH CHECK(current_user='wl_audit_writer' AND pg_catalog.pg_trigger_depth()>0 AND firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY audit_insert_matter ON weaverlaw.audit_event FOR INSERT TO wl_audit_writer WITH CHECK(current_user='wl_audit_writer' AND pg_catalog.pg_trigger_depth()>0 AND firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter());
CREATE POLICY audit_insert_replay_anomaly ON weaverlaw.audit_event FOR INSERT TO wl_audit_writer WITH CHECK(current_user='wl_audit_writer' AND firm_id=weaverlaw.context_firm() AND reason_code='CONFLICTING_REPLAY_SIGNALS' AND old_status=new_status);
-- PR1 deliberately grants no Tier B writes, approval grants, or executor mutations.
GRANT SELECT,INSERT ON weaverlaw.event_inbox,weaverlaw.workflow_run,weaverlaw.outbox TO wl_intake;
GRANT SELECT ON weaverlaw.matter_mapping,weaverlaw.event_inbox,weaverlaw.workflow_run,weaverlaw.outbox,weaverlaw.audit_event TO wl_resolver;
GRANT UPDATE(eligibility) ON weaverlaw.matter_mapping TO wl_resolver,wl_scope_guard;
GRANT SELECT ON weaverlaw.matter_mapping,weaverlaw.event_inbox,weaverlaw.workflow_run TO wl_scope_guard;
GRANT UPDATE(status,reason_code,scope_mode,verified_matter_id,replay_conflict_count,replay_conflict_digests) ON weaverlaw.workflow_run TO wl_scope_guard;
GRANT UPDATE(status,reason_code,scope_mode,verified_matter_id) ON weaverlaw.workflow_run TO wl_resolver,wl_worker,wl_reviewer,wl_attorney;
GRANT SELECT ON weaverlaw.workflow_run,weaverlaw.audit_event,weaverlaw.workflow_step,weaverlaw.agent_run,weaverlaw.source_reference,weaverlaw.work_product,weaverlaw.action_proposal,weaverlaw.approval,weaverlaw.effect_journal TO wl_worker,wl_reviewer,wl_attorney;
GRANT INSERT ON weaverlaw.audit_event TO wl_audit_writer;
GRANT USAGE ON SEQUENCE weaverlaw.audit_event_audit_id_seq TO wl_audit_writer;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON weaverlaw.audit_event FROM PUBLIC,wl_intake,wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor;
REVOKE UPDATE,DELETE,TRUNCATE ON weaverlaw.audit_event FROM wl_audit_writer;
CREATE FUNCTION weaverlaw.deny_app_mapping_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
 IF current_user IN ('wl_intake','wl_resolver','wl_worker','wl_reviewer','wl_attorney','wl_executor','wl_audit_writer','wl_scope_guard') THEN
  RAISE EXCEPTION 'matter_mapping is read-only for application roles' USING ERRCODE='42501';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER mapping_immutable_for_app BEFORE INSERT OR UPDATE OR DELETE ON weaverlaw.matter_mapping FOR EACH ROW EXECUTE FUNCTION weaverlaw.deny_app_mapping_mutation();
CREATE FUNCTION weaverlaw.deny_inbox_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'immutable intake evidence' USING ERRCODE='23514'; END $$;
CREATE TRIGGER inbox_immutable BEFORE UPDATE OR DELETE ON weaverlaw.event_inbox FOR EACH ROW EXECUTE FUNCTION weaverlaw.deny_inbox_mutation();
-- Defense-in-depth: even a direct UPDATE by a permitted service role must obey edges.
CREATE FUNCTION weaverlaw.enforce_run_edge() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
 v_actor text := COALESCE(NULLIF(NULLIF(pg_catalog.current_setting('role',true),''),'none'),session_user);
 v_evidence jsonb; v_matter uuid; v_reason text; v_target weaverlaw.run_status;
BEGIN
 IF NEW.run_id<>OLD.run_id OR NEW.firm_id<>OLD.firm_id OR NEW.inbox_id<>OLD.inbox_id OR NEW.created_at<>OLD.created_at THEN
  RAISE EXCEPTION 'immutable run identity' USING ERRCODE='23514';
 END IF;
 NEW.updated_at:=pg_catalog.clock_timestamp();
 IF OLD.resolution_evidence IS NOT NULL AND NEW.resolution_evidence IS DISTINCT FROM OLD.resolution_evidence
  OR OLD.resolved_matter_id IS NOT NULL AND NEW.resolved_matter_id IS DISTINCT FROM OLD.resolved_matter_id THEN
  RAISE EXCEPTION 'immutable resolution evidence' USING ERRCODE='23514';
 END IF;
 IF current_user<>'wl_scope_guard' AND (NEW.replay_conflict_count<>OLD.replay_conflict_count OR NEW.replay_conflict_digests<>OLD.replay_conflict_digests) THEN
  RAISE EXCEPTION 'replay counters require internal guard' USING ERRCODE='42501';
 END IF;
 -- Internal counter-only updates preserve terminal states and do not manufacture audit rows.
 IF current_user='wl_scope_guard' AND NEW.status=OLD.status AND NEW.reason_code=OLD.reason_code
  AND NEW.scope_mode=OLD.scope_mode AND NEW.verified_matter_id IS NOT DISTINCT FROM OLD.verified_matter_id
  AND NEW.resolved_matter_id IS NOT DISTINCT FROM OLD.resolved_matter_id AND NEW.resolution_evidence IS NOT DISTINCT FROM OLD.resolution_evidence
  AND NEW.replay_conflict_count=(CASE WHEN OLD.replay_conflict_count=9223372036854775807 THEN OLD.replay_conflict_count ELSE OLD.replay_conflict_count+1 END)
  AND (NEW.replay_conflict_digests=OLD.replay_conflict_digests
   OR (pg_catalog.cardinality(NEW.replay_conflict_digests)=pg_catalog.cardinality(OLD.replay_conflict_digests)+1
    AND NEW.replay_conflict_digests[1:pg_catalog.cardinality(OLD.replay_conflict_digests)]=OLD.replay_conflict_digests)) THEN
  RETURN NEW;
 END IF;
 IF OLD.status='RECEIVED'::weaverlaw.run_status
  AND (v_actor='wl_resolver' OR (current_user='wl_scope_guard' AND v_actor='wl_intake'))
  AND NEW.status IN ('VALIDATED'::weaverlaw.run_status,'QUARANTINED'::weaverlaw.run_status) THEN
  IF NOT weaverlaw.lock_resolution_inputs(OLD.firm_id,OLD.inbox_id,OLD.resolved_matter_id) THEN RAISE EXCEPTION 'missing immutable inbox' USING ERRCODE='23514'; END IF;
  v_evidence:=weaverlaw.derive_resolution_evidence(OLD.firm_id,OLD.inbox_id,OLD.run_id);
  v_target:=(v_evidence->>'decision')::weaverlaw.run_status;
  v_matter:=(v_evidence->>'verified_matter_id')::pg_catalog.uuid;
  v_reason:=v_evidence->>'reason_code';
  -- Conflicting replays quarantine without replacing the original unresolved evidence.
  IF current_user='wl_scope_guard' AND NEW.reason_code='CONFLICTING_REPLAY_SIGNALS' THEN
   IF NEW.status<>'QUARANTINED'::weaverlaw.run_status OR NEW.verified_matter_id IS NOT NULL
    OR NEW.scope_mode<>'INTAKE_RESTRICTED'::weaverlaw.scope_mode THEN RAISE EXCEPTION 'invalid replay quarantine' USING ERRCODE='23514'; END IF;
   RETURN NEW;
  END IF;
  IF NEW.status<>v_target OR NEW.verified_matter_id IS DISTINCT FROM v_matter OR NEW.reason_code::pg_catalog.text<>v_reason
   OR NEW.scope_mode<>(CASE WHEN v_matter IS NULL THEN 'INTAKE_RESTRICTED'::weaverlaw.scope_mode ELSE 'MATTER_VERIFIED'::weaverlaw.scope_mode END) THEN
   RAISE EXCEPTION 'resolution differs from immutable inbox evidence' USING ERRCODE='23514';
  END IF;
  NEW.resolved_matter_id:=v_matter;
  NEW.resolution_evidence:=v_evidence;
  RETURN NEW;
 END IF;
 IF current_user='wl_scope_guard' AND NEW.status='QUARANTINED'::weaverlaw.run_status
  AND OLD.status IN ('RECEIVED'::weaverlaw.run_status,'VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status)
  AND NEW.reason_code IN ('MATTER_INELIGIBLE','CONFLICTING_REPLAY_SIGNALS')
  AND NEW.scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND NEW.verified_matter_id IS NULL THEN RETURN NEW; END IF;
 IF NEW.verified_matter_id IS DISTINCT FROM OLD.verified_matter_id OR NEW.scope_mode<>OLD.scope_mode
  OR NEW.resolved_matter_id IS DISTINCT FROM OLD.resolved_matter_id OR NEW.resolution_evidence IS DISTINCT FROM OLD.resolution_evidence THEN
  RAISE EXCEPTION 'scope change denied' USING ERRCODE='23514';
 END IF;
 IF (v_actor='wl_worker' AND ((OLD.status='VALIDATED'::weaverlaw.run_status AND NEW.status='RUNNING'::weaverlaw.run_status) OR (OLD.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status) AND NEW.status='FAILED'::weaverlaw.run_status) OR (OLD.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status) AND NEW.status='CANCELLED'::weaverlaw.run_status)))
 OR (v_actor='wl_reviewer' AND OLD.status='RUNNING'::weaverlaw.run_status AND NEW.status='REVIEW_REQUIRED'::weaverlaw.run_status)
 OR (v_actor='wl_attorney' AND OLD.status='REVIEW_REQUIRED'::weaverlaw.run_status AND NEW.status='REJECTED'::weaverlaw.run_status) THEN
  IF NOT weaverlaw.lock_mapping_is_open(OLD.firm_id,OLD.verified_matter_id,OLD.run_id) THEN RAISE EXCEPTION 'mapping no longer eligible' USING ERRCODE='42501'; END IF;
  RETURN NEW;
 END IF;
 RAISE EXCEPTION 'illegal or disabled transition' USING ERRCODE='23514';
END $$;
CREATE TRIGGER run_edge BEFORE UPDATE ON weaverlaw.workflow_run FOR EACH ROW EXECUTE FUNCTION weaverlaw.enforce_run_edge();
-- Audit write is isolated to SECURITY DEFINER wl_audit_writer in the same transaction; failure aborts the state mutation.
CREATE FUNCTION weaverlaw.audit_run_edge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
 v_actor text := COALESCE(NULLIF(NULLIF(pg_catalog.current_setting('role',true),''),'none'),session_user);
BEGIN
 IF TG_RELID <> 'weaverlaw.workflow_run'::pg_catalog.regclass OR v_actor = 'wl_audit_writer' THEN
  RAISE EXCEPTION 'unauthorized audit trigger source' USING ERRCODE='42501';
 END IF;
 IF TG_OP='UPDATE' AND NEW.status=OLD.status AND NEW.reason_code=OLD.reason_code
  AND NEW.scope_mode=OLD.scope_mode AND NEW.verified_matter_id IS NOT DISTINCT FROM OLD.verified_matter_id THEN RETURN NEW; END IF;
 INSERT INTO weaverlaw.audit_event(firm_id,run_id,scope_mode,verified_matter_id,old_status,new_status,actor,reason_code)
 VALUES(NEW.firm_id,NEW.run_id,NEW.scope_mode,NEW.verified_matter_id,
 CASE WHEN TG_OP='INSERT' THEN NULL ELSE OLD.status END,NEW.status,v_actor,NEW.reason_code);
 RETURN NEW;
END $$;
CREATE TRIGGER run_audit AFTER INSERT OR UPDATE ON weaverlaw.workflow_run FOR EACH ROW EXECUTE FUNCTION weaverlaw.audit_run_edge();
ALTER FUNCTION weaverlaw.audit_run_edge() OWNER TO wl_audit_writer;
REVOKE ALL ON FUNCTION weaverlaw.audit_run_edge() FROM PUBLIC;
-- Terminal conflicting deliveries are audit anomalies, never terminal-state rewrites.
CREATE FUNCTION weaverlaw.append_replay_anomaly(p_firm uuid,p_run uuid,p_scope weaverlaw.scope_mode,p_matter uuid,p_status weaverlaw.run_status)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE v_actor text := COALESCE(NULLIF(NULLIF(pg_catalog.current_setting('role',true),''),'none'),session_user);
BEGIN
 IF p_firm IS DISTINCT FROM weaverlaw.context_firm() OR v_actor<>'wl_intake' THEN
  RAISE EXCEPTION 'unauthorized replay audit' USING ERRCODE='42501';
 END IF;
 INSERT INTO weaverlaw.audit_event(firm_id,run_id,scope_mode,verified_matter_id,old_status,new_status,actor,reason_code)
 VALUES(p_firm,p_run,p_scope,p_matter,p_status,p_status,v_actor,'CONFLICTING_REPLAY_SIGNALS');
END $$;
ALTER FUNCTION weaverlaw.append_replay_anomaly(uuid,uuid,weaverlaw.scope_mode,uuid,weaverlaw.run_status) OWNER TO wl_audit_writer;
REVOKE ALL ON FUNCTION weaverlaw.append_replay_anomaly(uuid,uuid,weaverlaw.scope_mode,uuid,weaverlaw.run_status) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.append_replay_anomaly(uuid,uuid,weaverlaw.scope_mode,uuid,weaverlaw.run_status) TO wl_scope_guard;

-- Only immutable intake evidence is compared; this helper never exposes promoted rows.
CREATE FUNCTION weaverlaw.check_replay(p_firm uuid,p_inbox uuid,p_signals jsonb,p_envelope_sha256 text,p_payload_sha256 text,p_evidence_sha256 text)
 RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
 v_actor text := COALESCE(NULLIF(NULLIF(pg_catalog.current_setting('role',true),''),'none'),session_user);
 v_inbox weaverlaw.event_inbox%ROWTYPE; v_run weaverlaw.workflow_run%ROWTYPE;
 v_signals jsonb; v_evidence jsonb; v_conflict boolean; v_emit boolean; v_digests weaverlaw.hex_digest[];
 v_digest weaverlaw.hex_digest; v_count bigint; v_prior_matter text;
BEGIN
 IF v_actor<>'wl_intake' OR p_firm IS DISTINCT FROM weaverlaw.context_firm() THEN
  RAISE EXCEPTION 'unauthorized replay check' USING ERRCODE='42501';
 END IF;
 v_signals:=weaverlaw.canonical_signals(p_signals);
 IF p_envelope_sha256 IS NULL OR p_evidence_sha256 IS NULL THEN RAISE EXCEPTION 'bound hashes required' USING ERRCODE='23514'; END IF;
 PERFORM p_envelope_sha256::weaverlaw.hex_digest,p_payload_sha256::weaverlaw.hex_digest;
 v_digest:=p_evidence_sha256::weaverlaw.hex_digest;
 SELECT * INTO v_inbox FROM weaverlaw.event_inbox i WHERE i.inbox_id=p_inbox AND i.firm_id=p_firm;
 IF NOT FOUND THEN RAISE EXCEPTION 'missing or unauthorized inbox' USING ERRCODE='42501'; END IF;
 SELECT * INTO v_run FROM weaverlaw.workflow_run r WHERE r.inbox_id=p_inbox AND r.firm_id=p_firm;
 IF NOT FOUND THEN RAISE EXCEPTION 'missing or unauthorized run' USING ERRCODE='42501'; END IF;
 -- All current original signal rows and the historical anchor precede the run lock.
 PERFORM weaverlaw.lock_resolution_inputs(p_firm,p_inbox,v_run.resolved_matter_id);
 SELECT * INTO STRICT v_run FROM weaverlaw.workflow_run r WHERE r.run_id=v_run.run_id AND r.firm_id=p_firm FOR UPDATE;
 v_conflict:=v_inbox.matter_signals IS DISTINCT FROM v_signals
  OR v_inbox.envelope_sha256::pg_catalog.text IS DISTINCT FROM p_envelope_sha256
  OR v_inbox.payload_sha256::pg_catalog.text IS DISTINCT FROM p_payload_sha256
  OR v_inbox.evidence_sha256::pg_catalog.text IS DISTINCT FROM p_evidence_sha256;
 IF v_conflict THEN
  v_emit:=NOT (v_digest=ANY(v_run.replay_conflict_digests)) AND pg_catalog.cardinality(v_run.replay_conflict_digests)<8;
  v_digests:=v_run.replay_conflict_digests;
  IF v_emit THEN v_digests:=pg_catalog.array_append(v_digests,v_digest); END IF;
  v_count:=CASE WHEN v_run.replay_conflict_count=9223372036854775807 THEN v_run.replay_conflict_count ELSE v_run.replay_conflict_count+1 END;
  IF v_run.status IN ('RECEIVED'::weaverlaw.run_status,'VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status) THEN
   UPDATE weaverlaw.workflow_run SET status='QUARANTINED'::weaverlaw.run_status,reason_code='CONFLICTING_REPLAY_SIGNALS',
    scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode,verified_matter_id=NULL,
    replay_conflict_count=v_count,replay_conflict_digests=v_digests WHERE run_id=v_run.run_id AND firm_id=p_firm;
  ELSE
   UPDATE weaverlaw.workflow_run SET replay_conflict_count=v_count,replay_conflict_digests=v_digests
    WHERE run_id=v_run.run_id AND firm_id=p_firm;
   IF v_emit THEN PERFORM weaverlaw.append_replay_anomaly(p_firm,v_run.run_id,v_run.scope_mode,v_run.verified_matter_id,v_run.status); END IF;
  END IF;
 ELSIF v_run.status='RECEIVED'::weaverlaw.run_status THEN
  -- Recover a process loss between durable acceptance and initial resolution using bound evidence only.
  v_evidence:=weaverlaw.derive_resolution_evidence(p_firm,p_inbox,v_run.run_id);
  v_prior_matter:=pg_catalog.current_setting('app.verified_matter_id',true);
  PERFORM pg_catalog.set_config('app.verified_matter_id',COALESCE(v_evidence->>'verified_matter_id',''),true);
  UPDATE weaverlaw.workflow_run SET status=(v_evidence->>'decision')::weaverlaw.run_status,
   reason_code=(v_evidence->>'reason_code')::weaverlaw.opaque_id,
   verified_matter_id=(v_evidence->>'verified_matter_id')::pg_catalog.uuid,
   scope_mode=CASE WHEN v_evidence->>'decision'='VALIDATED' THEN 'MATTER_VERIFIED'::weaverlaw.scope_mode ELSE 'INTAKE_RESTRICTED'::weaverlaw.scope_mode END
   WHERE run_id=v_run.run_id AND firm_id=p_firm;
  PERFORM pg_catalog.set_config('app.verified_matter_id',COALESCE(v_prior_matter,''),true);
 ELSIF v_run.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status) THEN
  v_evidence:=weaverlaw.derive_resolution_evidence(p_firm,p_inbox,v_run.run_id);
  IF v_evidence->>'decision'<>'VALIDATED' OR (v_evidence->>'verified_matter_id')::pg_catalog.uuid IS DISTINCT FROM v_run.resolved_matter_id
   OR NOT weaverlaw.mapping_is_open(p_firm,v_run.resolved_matter_id) THEN
   UPDATE weaverlaw.workflow_run SET status='QUARANTINED'::weaverlaw.run_status,reason_code='MATTER_INELIGIBLE',
    scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode,verified_matter_id=NULL WHERE run_id=v_run.run_id AND firm_id=p_firm;
  END IF;
 END IF;
 RETURN v_run.run_id;
END $$;
ALTER FUNCTION weaverlaw.check_replay(uuid,uuid,jsonb,text,text,text) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.check_replay(uuid,uuid,jsonb,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.check_replay(uuid,uuid,jsonb,text,text,text) TO wl_intake;

-- Caller commits a false result, then surfaces Denied; quarantine must survive denial.
CREATE FUNCTION weaverlaw.revalidate_run(p_firm uuid,p_run uuid,p_matter uuid)
 RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
 v_actor text := COALESCE(NULLIF(NULLIF(pg_catalog.current_setting('role',true),''),'none'),session_user);
 v_run weaverlaw.workflow_run%ROWTYPE; v_open boolean := false;
BEGIN
 IF v_actor NOT IN ('wl_resolver','wl_worker','wl_reviewer','wl_attorney')
  OR p_firm IS DISTINCT FROM weaverlaw.context_firm() OR p_matter IS NULL
  OR p_matter IS DISTINCT FROM weaverlaw.context_matter() THEN
  RAISE EXCEPTION 'unauthorized revalidation' USING ERRCODE='42501';
 END IF;
 SELECT * INTO v_run FROM weaverlaw.workflow_run r WHERE r.run_id=p_run AND r.firm_id=p_firm;
 IF NOT FOUND OR v_run.resolved_matter_id IS DISTINCT FROM p_matter THEN
  RAISE EXCEPTION 'missing or unauthorized run' USING ERRCODE='42501';
 END IF;
 v_open:=weaverlaw.lock_mapping_is_open(p_firm,p_matter,p_run);
 SELECT * INTO STRICT v_run FROM weaverlaw.workflow_run r WHERE r.run_id=p_run AND r.firm_id=p_firm FOR UPDATE;
 IF v_run.status NOT IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status) THEN RETURN false; END IF;
 IF v_run.scope_mode<>'MATTER_VERIFIED'::weaverlaw.scope_mode OR v_run.verified_matter_id IS DISTINCT FROM p_matter THEN RETURN false; END IF;
 IF COALESCE(v_open,false) THEN RETURN true; END IF;
 IF v_run.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status) THEN
  UPDATE weaverlaw.workflow_run SET status='QUARANTINED'::weaverlaw.run_status,reason_code='MATTER_INELIGIBLE',
   scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode,verified_matter_id=NULL WHERE run_id=p_run AND firm_id=p_firm;
 END IF;
 RETURN false;
END $$;
ALTER FUNCTION weaverlaw.revalidate_run(uuid,uuid,uuid) OWNER TO wl_scope_guard;
REVOKE ALL ON FUNCTION weaverlaw.revalidate_run(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weaverlaw.revalidate_run(uuid,uuid,uuid) TO wl_resolver,wl_worker,wl_reviewer,wl_attorney;
COMMIT;
