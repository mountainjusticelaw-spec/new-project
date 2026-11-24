-- Integration assertions; local disposable cluster only, no production DB.
\set ON_ERROR_STOP on
BEGIN;
DO $$ BEGIN
 IF current_database() <> 'weaverlaw_pr1_test' OR (inet_server_addr() IS NOT NULL AND inet_server_addr() NOT IN ('127.0.0.1'::inet,'::1'::inet)) THEN
  RAISE EXCEPTION 'local disposable weaverlaw_pr1_test required';
 END IF;
 IF (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='weaverlaw' AND c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity) <> 12 THEN
  RAISE EXCEPTION 'all twelve tables must force RLS';
 END IF;
END $$;
-- Bound adapter signal validation is identical to the Python contract.
DO $$ BEGIN
 IF weaverlaw.canonical_signals('["clio-001","clio-001"]'::jsonb)<>'["clio-001"]'::jsonb THEN
  RAISE EXCEPTION 'signal canonicalization changed identity';
 END IF;
 BEGIN
  PERFORM weaverlaw.canonical_signals('["clio bad"]'::jsonb);
  RAISE EXCEPTION 'signal whitespace accepted';
 EXCEPTION WHEN check_violation THEN NULL;
 END;
 BEGIN
  PERFORM weaverlaw.canonical_signals('{}'::jsonb);
  RAISE EXCEPTION 'non-array signals accepted';
 EXCEPTION WHEN check_violation THEN NULL;
 END;
END $$;
-- Local superuser seeds only synthetic rows. Production mapping writes not in scope.
INSERT INTO weaverlaw.matter_mapping VALUES
 ('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002','clio-001','OPEN','v1'),
 ('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000003','clio-002','OPEN','v1');
SET LOCAL ROLE wl_intake;
SELECT set_config('app.verified_firm_id','00000000-0000-0000-0000-000000000001',true),set_config('app.verified_matter_id','',true);
INSERT INTO weaverlaw.event_inbox(inbox_id,firm_id,event_id,source,event_type,schema_version,occurred_at,received_at,correlation_id,source_ref,source_version,sensitivity,dedup_key,matter_signals,envelope_sha256,evidence_sha256)
SELECT ('00000000-0000-0000-0000-00000000001'||n)::uuid,'00000000-0000-0000-0000-000000000001',('00000000-0000-0000-0000-00000000002'||n)::uuid,'synthetic.gmail','correspondence.received',1,now(),now(),'00000000-0000-0000-0000-000000000004','synthetic-msg-'||n,'v1','synthetic',repeat(n::text,64),pg_catalog.jsonb_build_array(CASE n WHEN 1 THEN 'clio-001' WHEN 2 THEN 'clio-002' ELSE 'unmapped-signal' END),repeat(n::text,64),repeat(n::text,64) FROM generate_series(1,3) n;
INSERT INTO weaverlaw.workflow_run(run_id,firm_id,inbox_id)
SELECT ('00000000-0000-0000-0000-00000000003'||n)::uuid,'00000000-0000-0000-0000-000000000001',('00000000-0000-0000-0000-00000000001'||n)::uuid FROM generate_series(1,3) n;
INSERT INTO weaverlaw.outbox(outbox_id,firm_id,run_id,topic,idempotency_key)
SELECT ('00000000-0000-0000-0000-00000000004'||n)::uuid,'00000000-0000-0000-0000-000000000001',('00000000-0000-0000-0000-00000000003'||n)::uuid,'RESOLUTION_PENDING','synthetic-resolve-'||n FROM generate_series(1,3) n;
RESET ROLE;
SET LOCAL ROLE wl_resolver;
SELECT set_config('app.verified_matter_id','00000000-0000-0000-0000-000000000002',true);
UPDATE weaverlaw.workflow_run SET status='VALIDATED',scope_mode='MATTER_VERIFIED',verified_matter_id='00000000-0000-0000-0000-000000000002',reason_code='MATTER_VERIFIED' WHERE run_id='00000000-0000-0000-0000-000000000031' AND status='RECEIVED';
SELECT set_config('app.verified_matter_id','00000000-0000-0000-0000-000000000003',true);
UPDATE weaverlaw.workflow_run SET status='VALIDATED',scope_mode='MATTER_VERIFIED',verified_matter_id='00000000-0000-0000-0000-000000000003',reason_code='MATTER_VERIFIED' WHERE run_id='00000000-0000-0000-0000-000000000032' AND status='RECEIVED';
SELECT set_config('app.verified_matter_id','',true);
UPDATE weaverlaw.workflow_run SET status='QUARANTINED',reason_code='UNMAPPED_MATTER_SIGNAL' WHERE run_id='00000000-0000-0000-0000-000000000033' AND status='RECEIVED';
DO $$ BEGIN
 IF (SELECT count(*) FROM weaverlaw.workflow_run) <> 1 OR (SELECT status FROM weaverlaw.workflow_run) <> 'QUARANTINED' THEN RAISE EXCEPTION 'intake scope leak'; END IF;
END $$;
RESET ROLE;
-- Use the local superuser to create Tier B fixtures and verify cross-scope FKs.
INSERT INTO weaverlaw.workflow_step(step_id,firm_id,run_id,verified_matter_id,step_name,idempotency_key,status) VALUES
 ('00000000-0000-0000-0000-000000000051','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000031','00000000-0000-0000-0000-000000000002','synthetic-step','key1','PENDING'),
 ('00000000-0000-0000-0000-000000000052','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000032','00000000-0000-0000-0000-000000000003','synthetic-step','key2','PENDING');
DO $$ BEGIN
 BEGIN
  INSERT INTO weaverlaw.workflow_step(step_id,firm_id,run_id,verified_matter_id,step_name,idempotency_key,status) VALUES ('00000000-0000-0000-0000-000000000053','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000033','00000000-0000-0000-0000-000000000002','invalid-step','key3','PENDING');
  RAISE EXCEPTION 'quarantine accepted Tier B write';
 EXCEPTION WHEN foreign_key_violation THEN NULL;
 END;
END $$;
SET LOCAL ROLE wl_worker;
SELECT set_config('app.verified_matter_id','00000000-0000-0000-0000-000000000002',true);
DO $$ DECLARE n integer; BEGIN
 IF (SELECT count(*) FROM weaverlaw.workflow_step) <> 1 OR (SELECT count(*) FROM weaverlaw.workflow_run) <> 1 THEN RAISE EXCEPTION 'cross-matter leak'; END IF;
 IF (SELECT count(*) FROM weaverlaw.audit_event) <> 1 THEN RAISE EXCEPTION 'missing verified resolution audit'; END IF;
 BEGIN
  UPDATE weaverlaw.workflow_run SET status='EXECUTING',reason_code='UNAUTHORIZED' WHERE status='VALIDATED';
  RAISE EXCEPTION 'illegal transition accepted';
 EXCEPTION WHEN check_violation THEN NULL;
 END;
 IF (SELECT count(*) FROM weaverlaw.audit_event) <> 1 THEN RAISE EXCEPTION 'illegal transition changed audit'; END IF;
 UPDATE weaverlaw.workflow_run SET status='RUNNING',reason_code='START' WHERE status='VALIDATED';
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>1 OR (SELECT count(*) FROM weaverlaw.audit_event)<>2 THEN RAISE EXCEPTION 'atomic transition or audit missing'; END IF;
 UPDATE weaverlaw.workflow_run SET status='RUNNING',reason_code='STALE' WHERE status='VALIDATED';
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>0 OR (SELECT count(*) FROM weaverlaw.audit_event)<>2 THEN RAISE EXCEPTION 'stale compare and set changed state'; END IF;
END $$;
SELECT set_config('app.verified_matter_id','',true);
DO $$ BEGIN IF EXISTS(SELECT 1 FROM weaverlaw.workflow_step) OR EXISTS(SELECT 1 FROM weaverlaw.workflow_run) THEN RAISE EXCEPTION 'missing scope allowed reads'; END IF; END $$;
RESET ROLE;
-- Force the audit insert to fail; the enclosing transaction must roll back the update.
REVOKE INSERT ON weaverlaw.audit_event FROM wl_audit_writer;
SET LOCAL ROLE wl_worker;
SELECT set_config('app.verified_matter_id','00000000-0000-0000-0000-000000000003',true);
DO $$ BEGIN
 BEGIN
  UPDATE weaverlaw.workflow_run SET status='RUNNING',reason_code='START' WHERE status='VALIDATED';
  RAISE EXCEPTION 'missing audit privilege did not abort';
 EXCEPTION WHEN insufficient_privilege THEN NULL;
 END;
 IF (SELECT status FROM weaverlaw.workflow_run) <> 'VALIDATED' THEN RAISE EXCEPTION 'audit failure failed to roll back state'; END IF;
END $$;
RESET ROLE;
-- Provider reassignment must revoke access even while the original internal matter stays OPEN.
GRANT INSERT ON weaverlaw.audit_event TO wl_audit_writer;
UPDATE weaverlaw.matter_mapping SET clio_matter_id='clio-001-retired' WHERE verified_matter_id='00000000-0000-0000-0000-000000000002';
UPDATE weaverlaw.matter_mapping SET clio_matter_id='clio-001' WHERE verified_matter_id='00000000-0000-0000-0000-000000000003';
SET LOCAL ROLE wl_worker;
SELECT set_config('app.verified_matter_id','00000000-0000-0000-0000-000000000002',true);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM weaverlaw.workflow_run) OR EXISTS(SELECT 1 FROM weaverlaw.workflow_step) THEN
  RAISE EXCEPTION 'provider reassignment retained prior matter visibility';
 END IF;
 IF weaverlaw.revalidate_run('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000031','00000000-0000-0000-0000-000000000002') THEN
  RAISE EXCEPTION 'provider reassignment passed revalidation';
 END IF;
END $$;
RESET ROLE;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM weaverlaw.workflow_run WHERE run_id='00000000-0000-0000-0000-000000000031'
  AND status='QUARANTINED' AND reason_code='MATTER_INELIGIBLE' AND verified_matter_id IS NULL
  AND resolved_matter_id='00000000-0000-0000-0000-000000000002'
  AND resolution_evidence->>'decision'='VALIDATED') THEN
  RAISE EXCEPTION 'reassignment quarantine lost original evidence';
 END IF;
END $$;
ROLLBACK;
