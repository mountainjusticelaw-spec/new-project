-- v0.1 / PR1. Apply ONLY to an empty, disposable PostgreSQL database after review.
-- No Cloud SQL provisioning, live identities, grants to LOGIN roles, or external calls.
BEGIN;
CREATE SCHEMA weaverlaw;
SET LOCAL search_path = pg_catalog;
CREATE ROLE wl_intake NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_resolver NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_reviewer NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_attorney NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_executor NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE wl_audit_writer NOLOGIN NOSUPERUSER NOBYPASSRLS;
REVOKE ALL ON SCHEMA weaverlaw FROM PUBLIC;
GRANT USAGE ON SCHEMA weaverlaw TO wl_intake,wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor,wl_audit_writer;
CREATE TYPE weaverlaw.scope_mode AS ENUM ('INTAKE_RESTRICTED','MATTER_VERIFIED');
CREATE TYPE weaverlaw.run_status AS ENUM ('RECEIVED','VALIDATED','RUNNING','REVIEW_REQUIRED','APPROVED','EXECUTING','COMPLETED','REJECTED','FAILED','QUARANTINED','CANCELLED','NEEDS_RECONCILIATION');
CREATE DOMAIN weaverlaw.hex_digest AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN weaverlaw.opaque_id AS text CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$');
CREATE FUNCTION weaverlaw.context_firm() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
 SELECT pg_catalog.nullif(pg_catalog.current_setting('app.verified_firm_id',true),'')::pg_catalog.uuid
$$;
CREATE FUNCTION weaverlaw.context_matter() RETURNS uuid LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
 SELECT pg_catalog.nullif(pg_catalog.current_setting('app.verified_matter_id',true),'')::pg_catalog.uuid
$$;
CREATE FUNCTION weaverlaw.scope_valid(mode weaverlaw.scope_mode, matter uuid) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
 SELECT (mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND matter IS NULL) OR (mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND matter IS NOT NULL)
$$;
CREATE TABLE weaverlaw.matter_mapping (
 firm_id uuid NOT NULL, verified_matter_id uuid NOT NULL, clio_matter_id weaverlaw.opaque_id NOT NULL,
 eligibility text NOT NULL CHECK (eligibility IN ('OPEN','CLOSED','RESTRICTED')),
 mapping_version weaverlaw.opaque_id NOT NULL, PRIMARY KEY (firm_id,verified_matter_id), UNIQUE(firm_id,clio_matter_id)
);
CREATE TABLE weaverlaw.event_inbox (
 inbox_id uuid PRIMARY KEY, firm_id uuid NOT NULL, event_id uuid NOT NULL, source text NOT NULL CHECK(source='synthetic.gmail'),
 event_type text NOT NULL CHECK(event_type='correspondence.received'), schema_version integer NOT NULL CHECK(schema_version=1),
 occurred_at timestamptz NOT NULL, received_at timestamptz NOT NULL CHECK(received_at>=occurred_at), correlation_id uuid NOT NULL,
 source_ref weaverlaw.opaque_id NOT NULL CHECK(source_ref LIKE 'synthetic-%'), source_version weaverlaw.opaque_id NOT NULL, sensitivity text NOT NULL CHECK(sensitivity='synthetic'), dedup_key weaverlaw.hex_digest NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)), UNIQUE(source,dedup_key), UNIQUE(inbox_id,firm_id),
 FOREIGN KEY(firm_id,verified_matter_id) REFERENCES weaverlaw.matter_mapping(firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.workflow_run (
 run_id uuid PRIMARY KEY, firm_id uuid NOT NULL, inbox_id uuid NOT NULL UNIQUE,
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 status weaverlaw.run_status NOT NULL DEFAULT 'RECEIVED'::weaverlaw.run_status, reason_code weaverlaw.opaque_id NOT NULL DEFAULT 'ACCEPTED'::weaverlaw.opaque_id,
 created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)),
 CHECK(status NOT IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status,'EXECUTING'::weaverlaw.run_status,'COMPLETED'::weaverlaw.run_status,'REJECTED'::weaverlaw.run_status,'FAILED'::weaverlaw.run_status,'CANCELLED'::weaverlaw.run_status,'NEEDS_RECONCILIATION'::weaverlaw.run_status) OR (scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id IS NOT NULL)),
 CHECK(status NOT IN ('RECEIVED'::weaverlaw.run_status,'QUARANTINED'::weaverlaw.run_status) OR scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode),
 CHECK(status <> 'QUARANTINED'::weaverlaw.run_status OR reason_code IN ('UNMAPPED_MATTER_SIGNAL','CONFLICTING_MATTER_SIGNALS','MATTER_INELIGIBLE')),
 UNIQUE(run_id,verified_matter_id), UNIQUE(run_id,firm_id), UNIQUE(run_id,firm_id,verified_matter_id),
 FOREIGN KEY(inbox_id,firm_id) REFERENCES weaverlaw.event_inbox(inbox_id,firm_id),
 FOREIGN KEY(firm_id,verified_matter_id) REFERENCES weaverlaw.matter_mapping(firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.outbox (
 outbox_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL DEFAULT 'INTAKE_RESTRICTED'::weaverlaw.scope_mode, verified_matter_id uuid,
 topic text NOT NULL CHECK(topic='RESOLUTION_PENDING'), idempotency_key weaverlaw.opaque_id NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)), UNIQUE(run_id,topic),
 FOREIGN KEY(run_id,firm_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.audit_event (
 audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL,
 scope_mode weaverlaw.scope_mode NOT NULL, verified_matter_id uuid,
 old_status weaverlaw.run_status, new_status weaverlaw.run_status NOT NULL, actor text NOT NULL, reason_code weaverlaw.opaque_id NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
 CHECK(weaverlaw.scope_valid(scope_mode,verified_matter_id)),
 FOREIGN KEY(run_id,firm_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id)
);
-- Tier B: all seven tables have nonnullable verified scope and BOTH composite FKs.
CREATE TABLE weaverlaw.workflow_step (
 step_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 step_name weaverlaw.opaque_id NOT NULL, idempotency_key weaverlaw.opaque_id NOT NULL, attempt integer NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 3),
 status text NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
 UNIQUE(run_id,idempotency_key), UNIQUE(step_id,run_id,firm_id,verified_matter_id),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.agent_run (
 agent_run_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL, step_id uuid NOT NULL,
 model_version weaverlaw.opaque_id NOT NULL, prompt_version weaverlaw.opaque_id NOT NULL, schema_version weaverlaw.opaque_id NOT NULL, tool_version weaverlaw.opaque_id NOT NULL,
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id),
 FOREIGN KEY(step_id,run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_step(step_id,run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.source_reference (
 source_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 source_system text NOT NULL CHECK(source_system IN ('synthetic.gmail','synthetic.dropbox')), external_id weaverlaw.opaque_id NOT NULL,
 source_version weaverlaw.opaque_id NOT NULL, sha256 weaverlaw.hex_digest NOT NULL, family_id uuid, page_number integer CHECK(page_number>0),
 UNIQUE(run_id,source_system,external_id,source_version), UNIQUE(source_id,run_id,firm_id,verified_matter_id),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.work_product (
 work_product_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 source_id uuid NOT NULL, version integer NOT NULL CHECK(version>0),
 review_banner text NOT NULL DEFAULT 'PROPOSED WORK PRODUCT — REQUIRES GEORGIA ATTORNEY REVIEW' CHECK(review_banner='PROPOSED WORK PRODUCT — REQUIRES GEORGIA ATTORNEY REVIEW'),
 content jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(content)='object'),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id),
 FOREIGN KEY(source_id,run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.source_reference(source_id,run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.action_proposal (
 proposal_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 action_type weaverlaw.opaque_id NOT NULL, destination weaverlaw.opaque_id NOT NULL, normalized_payload jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(normalized_payload)='object'),
 source_reference_versions jsonb NOT NULL CHECK(pg_catalog.jsonb_typeof(source_reference_versions)='array' AND pg_catalog.jsonb_array_length(source_reference_versions)>0),
 policy_version weaverlaw.opaque_id NOT NULL, action_digest weaverlaw.hex_digest NOT NULL,
 UNIQUE(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id)
);
CREATE TABLE weaverlaw.approval (
 approval_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 proposal_id uuid NOT NULL, action_digest weaverlaw.hex_digest NOT NULL, policy_version weaverlaw.opaque_id NOT NULL, destination weaverlaw.opaque_id NOT NULL,
 reviewer_id uuid NOT NULL, approved_at timestamptz NOT NULL, expires_at timestamptz NOT NULL CHECK(expires_at>approved_at), revoked_at timestamptz,
 UNIQUE(approval_id,run_id,firm_id,verified_matter_id,action_digest),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id),
 FOREIGN KEY(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination)
 REFERENCES weaverlaw.action_proposal(proposal_id,run_id,firm_id,verified_matter_id,action_digest,policy_version,destination)
);
CREATE TABLE weaverlaw.effect_journal (
 effect_id uuid PRIMARY KEY, firm_id uuid NOT NULL, run_id uuid NOT NULL, verified_matter_id uuid NOT NULL,
 approval_id uuid NOT NULL, action_digest weaverlaw.hex_digest NOT NULL, idempotency_key weaverlaw.hex_digest NOT NULL,
 status text NOT NULL CHECK(status IN ('LEASED','CONFIRMED','NEEDS_RECONCILIATION')), lease_id uuid NOT NULL,
 UNIQUE(firm_id,verified_matter_id,idempotency_key),
 FOREIGN KEY(run_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,verified_matter_id),
 FOREIGN KEY(run_id,firm_id,verified_matter_id) REFERENCES weaverlaw.workflow_run(run_id,firm_id,verified_matter_id),
 FOREIGN KEY(approval_id,run_id,firm_id,verified_matter_id,action_digest) REFERENCES weaverlaw.approval(approval_id,run_id,firm_id,verified_matter_id,action_digest)
);
-- All tables force RLS. No public policies; absent scope is deny-by-default.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['matter_mapping','event_inbox','workflow_run','outbox','audit_event','workflow_step','agent_run','source_reference','work_product','action_proposal','approval','effect_journal'] LOOP
  EXECUTE pg_catalog.format('ALTER TABLE weaverlaw.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE pg_catalog.format('ALTER TABLE weaverlaw.%I FORCE ROW LEVEL SECURITY',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['event_inbox','workflow_run','outbox','audit_event'] LOOP
  EXECUTE pg_catalog.format('CREATE POLICY intake_read ON weaverlaw.%I FOR SELECT TO wl_intake,wl_resolver USING (firm_id=weaverlaw.context_firm() AND scope_mode=''INTAKE_RESTRICTED''::weaverlaw.scope_mode)',t);
  EXECUTE pg_catalog.format('CREATE POLICY matter_read ON weaverlaw.%I FOR SELECT TO wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND scope_mode=''MATTER_VERIFIED''::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter())',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['workflow_step','agent_run','source_reference','work_product','action_proposal','approval','effect_journal'] LOOP
  EXECUTE pg_catalog.format('CREATE POLICY matter_read ON weaverlaw.%I FOR SELECT TO wl_worker,wl_reviewer,wl_attorney,wl_executor USING (firm_id=weaverlaw.context_firm() AND verified_matter_id=weaverlaw.context_matter())',t);
 END LOOP;
END $$;
CREATE POLICY mapping_resolve ON weaverlaw.matter_mapping FOR SELECT TO wl_resolver USING(firm_id=weaverlaw.context_firm());
-- Permit FOR SHARE row locking during resolution while denying any actual UPDATE via WITH CHECK (false) and trigger.
CREATE POLICY mapping_lock_only ON weaverlaw.matter_mapping FOR UPDATE TO wl_resolver USING(firm_id=weaverlaw.context_firm()) WITH CHECK(false);
-- Seed only as the migration owner via explicitly reviewed fixture grants/policy; no app mutation.
CREATE POLICY inbox_insert ON weaverlaw.event_inbox FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY run_insert ON weaverlaw.workflow_run FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode AND status='RECEIVED'::weaverlaw.run_status AND reason_code='ACCEPTED');
CREATE POLICY outbox_insert ON weaverlaw.outbox FOR INSERT TO wl_intake WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY resolve_update ON weaverlaw.workflow_run FOR UPDATE TO wl_resolver USING(firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode) WITH CHECK(firm_id=weaverlaw.context_firm() AND (scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode OR (scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter())));
CREATE POLICY worker_update ON weaverlaw.workflow_run FOR UPDATE TO wl_worker,wl_reviewer,wl_attorney USING(firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter()) WITH CHECK(firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter());
CREATE POLICY audit_insert_intake ON weaverlaw.audit_event FOR INSERT TO wl_audit_writer WITH CHECK(current_user='wl_audit_writer' AND pg_catalog.pg_trigger_depth()>0 AND firm_id=weaverlaw.context_firm() AND scope_mode='INTAKE_RESTRICTED'::weaverlaw.scope_mode);
CREATE POLICY audit_insert_matter ON weaverlaw.audit_event FOR INSERT TO wl_audit_writer WITH CHECK(current_user='wl_audit_writer' AND pg_catalog.pg_trigger_depth()>0 AND firm_id=weaverlaw.context_firm() AND scope_mode='MATTER_VERIFIED'::weaverlaw.scope_mode AND verified_matter_id=weaverlaw.context_matter());
-- PR1 deliberately grants no Tier B writes, approval grants, or executor mutations.
GRANT SELECT,INSERT ON weaverlaw.event_inbox,weaverlaw.workflow_run,weaverlaw.outbox TO wl_intake;
GRANT SELECT ON weaverlaw.matter_mapping,weaverlaw.event_inbox,weaverlaw.workflow_run,weaverlaw.outbox,weaverlaw.audit_event TO wl_resolver;
GRANT UPDATE(eligibility) ON weaverlaw.matter_mapping TO wl_resolver;
GRANT UPDATE(status,reason_code,scope_mode,verified_matter_id) ON weaverlaw.workflow_run TO wl_resolver,wl_worker,wl_reviewer,wl_attorney;
GRANT SELECT ON weaverlaw.workflow_run,weaverlaw.audit_event,weaverlaw.workflow_step,weaverlaw.agent_run,weaverlaw.source_reference,weaverlaw.work_product,weaverlaw.action_proposal,weaverlaw.approval,weaverlaw.effect_journal TO wl_worker,wl_reviewer,wl_attorney;
GRANT INSERT ON weaverlaw.audit_event TO wl_audit_writer;
GRANT USAGE ON SEQUENCE weaverlaw.audit_event_audit_id_seq TO wl_audit_writer;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON weaverlaw.audit_event FROM PUBLIC,wl_intake,wl_resolver,wl_worker,wl_reviewer,wl_attorney,wl_executor;
REVOKE UPDATE,DELETE,TRUNCATE ON weaverlaw.audit_event FROM wl_audit_writer;
CREATE FUNCTION weaverlaw.deny_app_mapping_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
 IF current_user IN ('wl_intake','wl_resolver','wl_worker','wl_reviewer','wl_attorney','wl_executor','wl_audit_writer') THEN
  RAISE EXCEPTION 'matter_mapping is read-only for application roles' USING ERRCODE='42501';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER mapping_immutable_for_app BEFORE INSERT OR UPDATE OR DELETE ON weaverlaw.matter_mapping FOR EACH ROW EXECUTE FUNCTION weaverlaw.deny_app_mapping_mutation();
-- Defense-in-depth: even a direct UPDATE by a permitted service role must obey edges.
CREATE FUNCTION weaverlaw.enforce_run_edge() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
 IF NEW.run_id<>OLD.run_id OR NEW.firm_id<>OLD.firm_id OR NEW.inbox_id<>OLD.inbox_id THEN RAISE EXCEPTION 'immutable run identity'; END IF;
 IF OLD.status='RECEIVED'::weaverlaw.run_status AND current_user='wl_resolver' AND NEW.status IN ('VALIDATED'::weaverlaw.run_status,'QUARANTINED'::weaverlaw.run_status) THEN
  IF NEW.status='VALIDATED'::weaverlaw.run_status AND NOT EXISTS(SELECT 1 FROM weaverlaw.matter_mapping m WHERE m.firm_id=NEW.firm_id AND m.verified_matter_id=NEW.verified_matter_id AND m.eligibility='OPEN') THEN RAISE EXCEPTION 'matter not eligible'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.verified_matter_id IS DISTINCT FROM OLD.verified_matter_id OR NEW.scope_mode<>OLD.scope_mode THEN RAISE EXCEPTION 'scope change denied'; END IF;
 IF (current_user='wl_worker' AND ((OLD.status='VALIDATED'::weaverlaw.run_status AND NEW.status='RUNNING'::weaverlaw.run_status) OR (OLD.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status) AND NEW.status='FAILED'::weaverlaw.run_status) OR (OLD.status IN ('VALIDATED'::weaverlaw.run_status,'RUNNING'::weaverlaw.run_status,'REVIEW_REQUIRED'::weaverlaw.run_status,'APPROVED'::weaverlaw.run_status) AND NEW.status='CANCELLED'::weaverlaw.run_status)))
 OR (current_user='wl_reviewer' AND OLD.status='RUNNING'::weaverlaw.run_status AND NEW.status='REVIEW_REQUIRED'::weaverlaw.run_status)
 OR (current_user='wl_attorney' AND OLD.status='REVIEW_REQUIRED'::weaverlaw.run_status AND NEW.status='REJECTED'::weaverlaw.run_status) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'illegal or disabled transition' USING ERRCODE='23514';
END $$;
CREATE TRIGGER run_edge BEFORE UPDATE ON weaverlaw.workflow_run FOR EACH ROW EXECUTE FUNCTION weaverlaw.enforce_run_edge();
-- Audit write is isolated to SECURITY DEFINER wl_audit_writer in the same transaction; failure aborts the state mutation.
CREATE FUNCTION weaverlaw.audit_run_edge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
 v_actor text := COALESCE(pg_catalog.nullif(pg_catalog.nullif(pg_catalog.current_setting('role',true),''),'none'),session_user);
BEGIN
 IF TG_RELID <> 'weaverlaw.workflow_run'::pg_catalog.regclass OR v_actor = 'wl_audit_writer' THEN
  RAISE EXCEPTION 'unauthorized audit trigger source' USING ERRCODE='42501';
 END IF;
 INSERT INTO weaverlaw.audit_event(firm_id,run_id,scope_mode,verified_matter_id,old_status,new_status,actor,reason_code)
 VALUES(NEW.firm_id,NEW.run_id,NEW.scope_mode,NEW.verified_matter_id,
 CASE WHEN TG_OP='INSERT' THEN NULL ELSE OLD.status END,NEW.status,v_actor,NEW.reason_code);
 RETURN NEW;
END $$;
CREATE TRIGGER run_audit AFTER INSERT OR UPDATE ON weaverlaw.workflow_run FOR EACH ROW EXECUTE FUNCTION weaverlaw.audit_run_edge();
ALTER FUNCTION weaverlaw.audit_run_edge() OWNER TO wl_audit_writer;
REVOKE ALL ON FUNCTION weaverlaw.audit_run_edge() FROM PUBLIC;
COMMIT;
