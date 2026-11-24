"""Live disposable PostgreSQL 16+ security, RLS, audit, concurrency, and pool tests.

Always spins up a fresh, isolated disposable cluster via `initdb` + `pg_ctl` bound
strictly to `127.0.0.1` and a temporary Unix socket directory, creates disposable
database `weaverlaw_pr1_test`, applies
`database/migrations/001_correspondence_control_layer.sql`, runs the SQL integration
assertions in `database/tests/001_correspondence_control_layer_test.sql`, and verifies
all PR #1A merge blockers with real `psycopg` connections.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from services.correspondence.core import Actor, Denied, Envelope, State
from services.correspondence.postgres import (
    receive,
    resolve_run,
    revalidate_matter_eligibility,
    transition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SQL = REPO_ROOT / 'database' / 'migrations' / '001_correspondence_control_layer.sql'
TEST_SQL = REPO_ROOT / 'database' / 'tests' / '001_correspondence_control_layer_test.sql'

ROLES = ('wl_intake', 'wl_resolver', 'wl_worker', 'wl_reviewer', 'wl_attorney', 'wl_executor')
ALL_RESTRICTED_ROLES = (*ROLES, 'wl_audit_writer', 'wl_scope_guard')
ALL_TABLES = (
    'matter_mapping', 'event_inbox', 'workflow_run', 'outbox', 'audit_event',
    'workflow_step', 'agent_run', 'source_reference', 'work_product',
    'action_proposal', 'approval', 'effect_journal',
)
TIER_B_TABLES = (
    'workflow_step', 'agent_run', 'source_reference', 'work_product',
    'action_proposal', 'approval', 'effect_journal',
)

FIRM_A = UUID('00000000-0000-0000-0000-000000000101')
MATTER_A = UUID('00000000-0000-0000-0000-000000000201')
MATTER_B = UUID('00000000-0000-0000-0000-000000000202')
MATTER_CLOSED = UUID('00000000-0000-0000-0000-000000000203')
MATTER_RACE = UUID('00000000-0000-0000-0000-000000000209')
NOW = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)


def _find_pg_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    pg_root = Path('/usr/lib/postgresql')
    if pg_root.exists():
        for candidate in sorted(pg_root.glob(f'*/bin/{name}'), reverse=True):
            if os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _skip_or_fail(reason: str):
    if os.environ.get('WEAVERLAW_REQUIRE_LIVE_PG') == '1':
        pytest.fail(reason)
    pytest.skip(reason)


def _require_postgresql_16(version: str, *, source: str) -> int:
    if source == 'server':
        major = int(version.strip()) // 10000 if version.strip().isdigit() else 0
    else:
        match = re.search(r'PostgreSQL\)\s+(\d+)(?:\.\d+)*', version)
        major = int(match.group(1)) if match else 0
    if major < 16:
        _skip_or_fail(f'{source} requires PostgreSQL 16+; found {version.strip()!r}')
    return major


def test_postgresql_version_gate_accepts_16_and_rejects_old_or_unknown_versions(monkeypatch):
    for version, source in (
        ('initdb (PostgreSQL) 16.2', 'initdb'),
        ('initdb (PostgreSQL) 17.4 (Debian 17.4-1)', 'initdb'),
        ('160002', 'server'),
        ('170004', 'server'),
    ):
        assert _require_postgresql_16(version, source=source) >= 16
    for required, outcome in (('0', pytest.skip.Exception), ('1', pytest.fail.Exception)):
        monkeypatch.setenv('WEAVERLAW_REQUIRE_LIVE_PG', required)
        for version, source in (
            ('initdb (PostgreSQL) 15.12', 'initdb'),
            ('150012', 'server'),
            ('unknown initdb version', 'initdb'),
            ('not-a-server-version', 'server'),
        ):
            with pytest.raises(outcome, match=r'requires PostgreSQL 16\+'):
                _require_postgresql_16(version, source=source)


@pytest.fixture(scope='module')
def pg_dsn(tmp_path_factory):
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _skip_or_fail('psycopg is not installed; live PostgreSQL suite requires psycopg>=3.2')

    initdb = _find_pg_tool('initdb')
    pg_ctl = _find_pg_tool('pg_ctl')
    psql = _find_pg_tool('psql')
    createdb = _find_pg_tool('createdb')
    if not (initdb and pg_ctl and psql and createdb):
        _skip_or_fail('Local PostgreSQL server/client binaries (initdb, pg_ctl, psql, createdb) unavailable')

    initdb_version = subprocess.run(
        [initdb, '--version'], check=True, capture_output=True, text=True,
    ).stdout
    _require_postgresql_16(initdb_version, source='initdb')

    base_dir = tmp_path_factory.mktemp('weaverlaw_pg16')
    pgdata = base_dir / 'data'
    sock_dir = base_dir / 'sock'
    sock_dir.mkdir(parents=True, exist_ok=True)
    logfile = base_dir / 'postgres.log'
    port = _free_port()

    subprocess.run(
        [initdb, '-D', str(pgdata), '--auth=trust', '--no-locale', '--encoding=UTF8'],
        check=True, capture_output=True, text=True,
    )
    opts = f"-F -h 127.0.0.1 -p {port} -k {sock_dir}"
    subprocess.run(
        [pg_ctl, '-D', str(pgdata), '-l', str(logfile), '-o', opts, 'start'],
        check=True, capture_output=True, text=True,
    )
    try:
        server_version = subprocess.run(
            [psql, '-X', '-A', '-t', '-h', '127.0.0.1', '-p', str(port),
             '-d', 'postgres', '-c', 'SHOW server_version_num'],
            check=True, capture_output=True, text=True,
        ).stdout
        _require_postgresql_16(server_version, source='server')
        subprocess.run(
            [createdb, '-h', '127.0.0.1', '-p', str(port), 'weaverlaw_pr1_test'],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [psql, '-X', '-h', '127.0.0.1', '-p', str(port), '-v', 'ON_ERROR_STOP=1',
             '-d', 'weaverlaw_pr1_test', '-f', str(MIGRATION_SQL)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [psql, '-X', '-h', '127.0.0.1', '-p', str(port), '-v', 'ON_ERROR_STOP=1',
             '-d', 'weaverlaw_pr1_test', '-f', str(TEST_SQL)],
            check=True, capture_output=True, text=True,
        )
        yield f'postgresql://127.0.0.1:{port}/weaverlaw_pr1_test'
    finally:
        subprocess.run(
            [pg_ctl, '-D', str(pgdata), '-m', 'immediate', 'stop'],
            check=False, capture_output=True, text=True,
        )


def _connect(dsn: str, role: str | None = None):
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True)
    if role is not None:
        conn.execute(f'SET ROLE {role}')
    return conn


def _make_envelope(*, source_ref: str, firm_id: UUID = FIRM_A, event_id: UUID | None = None) -> Envelope:
    return Envelope(
        schema_version=1,
        event_id=event_id or uuid4(),
        source='synthetic.gmail',
        event_type='correspondence.received',
        firm_id=firm_id,
        occurred_at=NOW,
        received_at=NOW + timedelta(seconds=1),
        correlation_id=uuid4(),
        source_ref=source_ref,
        source_version='v1',
        sensitivity='synthetic',
    )


@pytest.fixture(scope='module')
def seeded_cluster(pg_dsn):
    with _connect(pg_dsn) as admin, admin.transaction():
        admin.execute(
            """INSERT INTO weaverlaw.matter_mapping
               (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
               VALUES (%s, %s, 'clio-live-a', 'OPEN', 'v1'),
                      (%s, %s, 'clio-live-b', 'OPEN', 'v1'),
                      (%s, %s, 'clio-live-closed', 'CLOSED', 'v1')
               ON CONFLICT (firm_id, verified_matter_id) DO NOTHING""",
            (FIRM_A, MATTER_A, FIRM_A, MATTER_B, FIRM_A, MATTER_CLOSED),
        )
    return pg_dsn


def _seed_tier_b_bundle(admin_conn, *, firm_id: UUID, run_id: UUID, matter_id: UUID, tag: str):
    step_id = uuid4()
    source_id = uuid4()
    proposal_id = uuid4()
    approval_id = uuid4()
    digest_hex = (tag.encode().hex() * 64)[:64]
    idem_hex = ((tag + 'e').encode().hex() * 64)[:64]
    with admin_conn.transaction():
        admin_conn.execute(
            """INSERT INTO weaverlaw.workflow_step
               (step_id, firm_id, run_id, verified_matter_id, step_name, idempotency_key, status)
               VALUES (%s, %s, %s, %s, 'extract', %s, 'SUCCEEDED')""",
            (step_id, firm_id, run_id, matter_id, f'step-{tag}'),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.agent_run
               (agent_run_id, firm_id, run_id, verified_matter_id, step_id,
                model_version, prompt_version, schema_version, tool_version)
               VALUES (%s, %s, %s, %s, %s, 'gemini-2.5-pro', 'p1', 's1', 't1')""",
            (uuid4(), firm_id, run_id, matter_id, step_id),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.source_reference
               (source_id, firm_id, run_id, verified_matter_id, source_system,
                external_id, source_version, sha256, page_number)
               VALUES (%s, %s, %s, %s, 'synthetic.gmail', %s, 'v1', %s, 1)""",
            (source_id, firm_id, run_id, matter_id, f'synthetic-src-{tag}', digest_hex),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.work_product
               (work_product_id, firm_id, run_id, verified_matter_id, source_id, version, content)
               VALUES (%s, %s, %s, %s, %s, 1, %s::jsonb)""",
            (uuid4(), firm_id, run_id, matter_id, source_id, json.dumps({'summary': f'synthetic-{tag}'})),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.action_proposal
               (proposal_id, firm_id, run_id, verified_matter_id, action_type, destination,
                normalized_payload, source_reference_versions, policy_version, action_digest)
               VALUES (%s, %s, %s, %s, 'mock.review', 'synthetic-sink',
                       '{"draft": true}'::jsonb, '[{"ref": "synthetic-src", "ver": "v1"}]'::jsonb,
                       'p1', %s)""",
            (proposal_id, firm_id, run_id, matter_id, digest_hex),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.approval
               (approval_id, firm_id, run_id, verified_matter_id, proposal_id, action_digest,
                policy_version, destination, reviewer_id, approved_at, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'p1', 'synthetic-sink', %s, %s, %s)""",
            (approval_id, firm_id, run_id, matter_id, proposal_id, digest_hex,
             uuid4(), NOW, NOW + timedelta(minutes=10)),
        )
        admin_conn.execute(
            """INSERT INTO weaverlaw.effect_journal
               (effect_id, firm_id, run_id, verified_matter_id, approval_id,
                action_digest, idempotency_key, status, lease_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'CONFIRMED', %s)""",
            (uuid4(), firm_id, run_id, matter_id, approval_id, digest_hex, idem_hex, uuid4()),
        )


def test_all_roles_and_rls_forced_and_cross_matter_isolated(seeded_cluster):
    import psycopg.errors

    with _connect(seeded_cluster) as admin:
        rls_rows = admin.execute(
            """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'weaverlaw' AND c.relkind = 'r'"""
        ).fetchall()
        assert {row[0] for row in rls_rows} == set(ALL_TABLES)
        assert all(row[1] and row[2] for row in rls_rows)

        role_rows = admin.execute(
            """SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
               FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)""",
            (list(ALL_RESTRICTED_ROLES),),
        ).fetchall()
        assert {row[0] for row in role_rows} == set(ALL_RESTRICTED_ROLES)
        assert all((not can_login and not is_super and not bypass_rls)
                   for _, can_login, is_super, bypass_rls in role_rows)

    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster) as admin,
    ):
        run_a, ok_a = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-a'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'])
        run_b, ok_b = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-b'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-b'])
        run_q, ok_q = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-q'), trusted_firm_id=FIRM_A, trusted_signals=['unmapped-signal!'])
        assert ok_a and ok_b and ok_q

        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_a, trusted_signals=['clio-live-a']) == (
            State.VALIDATED, MATTER_A, 'MATTER_VERIFIED',
        )
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_b, trusted_signals=['clio-live-b']) == (
            State.VALIDATED, MATTER_B, 'MATTER_VERIFIED',
        )
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_q, trusted_signals=['unmapped-signal!']) == (
            State.QUARANTINED, None, 'UNMAPPED_MATTER_SIGNAL',
        )

        _seed_tier_b_bundle(admin, firm_id=FIRM_A, run_id=run_a, matter_id=MATTER_A, tag='aa')
        _seed_tier_b_bundle(admin, firm_id=FIRM_A, run_id=run_b, matter_id=MATTER_B, tag='bb')

    # Verify Matter A session sees Matter A rows ONLY (0 Matter B rows, 0 QUARANTINED rows)
    for role in ('wl_worker', 'wl_reviewer', 'wl_attorney'):
        with _connect(seeded_cluster, role) as conn, conn.transaction():
            conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_A)),
            )
            visible_runs = conn.execute(
                'SELECT run_id, verified_matter_id, status FROM weaverlaw.workflow_run '
                'WHERE run_id = ANY(%s)',
                ([run_a, run_b, run_q],),
            ).fetchall()
            assert visible_runs == [(run_a, MATTER_A, 'VALIDATED')]
            for table in TIER_B_TABLES:
                matters = conn.execute(
                    f'SELECT DISTINCT verified_matter_id FROM weaverlaw.{table}'
                ).fetchall()
                assert matters == [(MATTER_A,)], f'{role} leaked rows on {table}: {matters}'

    # wl_intake and wl_resolver cannot read Tier B tables; wl_executor has zero table grants in PR1
    for denied_role in ('wl_intake', 'wl_resolver', 'wl_executor', 'wl_audit_writer', 'wl_scope_guard'):
        with _connect(seeded_cluster, denied_role) as conn:
            for table in TIER_B_TABLES:
                with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                    conn.execute(
                        "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                        "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                        (str(FIRM_A), str(MATTER_A)),
                    )
                    conn.execute(f'SELECT count(*) FROM weaverlaw.{table}').fetchone()

    # wl_intake cannot see MATTER_VERIFIED promoted runs, only INTAKE_RESTRICTED quarantined/received runs
    with _connect(seeded_cluster, 'wl_intake') as intake_conn, intake_conn.transaction():
        intake_conn.execute(
            "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
            "pg_catalog.set_config('app.verified_matter_id', %s, true)",
            (str(FIRM_A), str(MATTER_A)),
        )
        intake_statuses = {
            r[1] for r in intake_conn.execute(
                'SELECT run_id, status FROM weaverlaw.workflow_run'
            ).fetchall()
        }
        assert 'VALIDATED' not in intake_statuses
        assert 'QUARANTINED' in intake_statuses


def test_scope_promotion_and_audit_atomicity_immutability_and_rollback(seeded_cluster):
    import psycopg.errors

    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker_conn,
        _connect(seeded_cluster, 'wl_reviewer') as reviewer_conn,
        _connect(seeded_cluster, 'wl_attorney') as attorney_conn,
        _connect(seeded_cluster) as admin,
    ):
        run_id, created = receive(
            intake_conn, _make_envelope(source_ref='synthetic-audit-chain'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        assert created is True
        target, matter, reason = resolve_run(
            resolver_conn, firm_id=FIRM_A, run_id=run_id, trusted_signals=['clio-live-a'],
        )
        assert (target, matter, reason) == (State.VALIDATED, MATTER_A, 'MATTER_VERIFIED')

        transition(
            worker_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
            expected=State.VALIDATED, target=State.RUNNING,
            actor=Actor.COORDINATOR, reason='START',
        )
        transition(
            reviewer_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
            expected=State.RUNNING, target=State.REVIEW_REQUIRED,
            actor=Actor.INDEPENDENT_REVIEW, reason='INDEPENDENT_AUDIT_OK',
            independently_checked=True,
        )
        transition(
            attorney_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
            expected=State.REVIEW_REQUIRED, target=State.REJECTED,
            actor=Actor.ATTORNEY, reason='ATTORNEY_REJECTED',
        )

        audit_chain = admin.execute(
            """SELECT old_status, new_status, actor, scope_mode, verified_matter_id
               FROM weaverlaw.audit_event WHERE run_id = %s ORDER BY audit_id""",
            (run_id,),
        ).fetchall()
        assert audit_chain == [
            (None, 'RECEIVED', 'wl_intake', 'INTAKE_RESTRICTED', None),
            ('RECEIVED', 'VALIDATED', 'wl_resolver', 'MATTER_VERIFIED', MATTER_A),
            ('VALIDATED', 'RUNNING', 'wl_worker', 'MATTER_VERIFIED', MATTER_A),
            ('RUNNING', 'REVIEW_REQUIRED', 'wl_reviewer', 'MATTER_VERIFIED', MATTER_A),
            ('REVIEW_REQUIRED', 'REJECTED', 'wl_attorney', 'MATTER_VERIFIED', MATTER_A),
        ]

        # Prove UPDATE, DELETE, and TRUNCATE on audit_event fail for all restricted roles (including wl_audit_writer)
        for role in ALL_RESTRICTED_ROLES:
            with _connect(seeded_cluster, role) as role_conn:
                for stmt in (
                    "UPDATE weaverlaw.audit_event SET reason_code = 'TAMPER'",
                    'DELETE FROM weaverlaw.audit_event',
                    'TRUNCATE weaverlaw.audit_event',
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege), role_conn.transaction():
                        role_conn.execute(stmt)

        # Prove direct INSERT into audit_event fails for all 6 application roles (INSERT revoked)
        for role in ROLES:
            with (
                _connect(seeded_cluster, role) as role_conn,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                role_conn.transaction(),
            ):
                role_conn.execute(
                    "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                    "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                    (str(FIRM_A), str(MATTER_A)),
                )
                role_conn.execute(
                    """INSERT INTO weaverlaw.audit_event
                       (firm_id, run_id, scope_mode, verified_matter_id, old_status, new_status, actor, reason_code)
                       VALUES (%s, %s, 'MATTER_VERIFIED', %s, 'REJECTED', 'REJECTED', %s, 'FORGED')""",
                    (FIRM_A, run_id, MATTER_A, role),
                )

        # Prove simulated audit-insert failure (revoking wl_audit_writer INSERT) rolls back workflow_run transition
        run_rb, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-audit-rollback'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_rb, trusted_signals=['clio-live-a'])
        admin.execute('REVOKE INSERT ON weaverlaw.audit_event FROM wl_audit_writer')
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                transition(
                    worker_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_rb,
                    expected=State.VALIDATED, target=State.RUNNING,
                    actor=Actor.COORDINATOR, reason='START',
                )
            persisted_status = admin.execute(
                'SELECT status FROM weaverlaw.workflow_run WHERE run_id = %s', (run_rb,),
            ).fetchone()[0]
            assert persisted_status == 'VALIDATED'
        finally:
            admin.execute('GRANT INSERT ON weaverlaw.audit_event TO wl_audit_writer')


def test_pg_temp_trigger_cannot_forge_audit_event(seeded_cluster):
    import psycopg.errors

    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster) as admin,
    ):
        run_id, created = receive(
            intake_conn, _make_envelope(source_ref='synthetic-temp-trig-001'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        assert created is True and run_id is not None
        assert resolve_run(
            resolver_conn, firm_id=FIRM_A, run_id=run_id, trusted_signals=['clio-live-a'],
        ) == (State.VALIDATED, MATTER_A, 'MATTER_VERIFIED')

    for role in ('wl_intake', 'wl_resolver', 'wl_worker', 'wl_reviewer', 'wl_attorney'):
        with _connect(seeded_cluster, role) as attacker_conn:
            # 1. Custom TEMP TABLE + AFTER INSERT trigger incrementing pg_trigger_depth() > 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege), attacker_conn.transaction():
                attacker_conn.execute(
                    "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                    "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                    (str(FIRM_A), str(MATTER_A)),
                )
                attacker_conn.execute('CREATE TEMP TABLE temp_audit_driver (id integer)')
                attacker_conn.execute(
                    f"""CREATE FUNCTION pg_temp.forge_audit_via_temp_trigger() RETURNS trigger
                        LANGUAGE plpgsql AS $$
                        BEGIN
                         INSERT INTO weaverlaw.audit_event
                          (firm_id, run_id, scope_mode, verified_matter_id, old_status, new_status, actor, reason_code)
                         VALUES
                          ('{FIRM_A}'::pg_catalog.uuid, '{run_id}'::pg_catalog.uuid,
                           'MATTER_VERIFIED'::weaverlaw.scope_mode, '{MATTER_A}'::pg_catalog.uuid,
                           'VALIDATED'::weaverlaw.run_status, 'APPROVED'::weaverlaw.run_status,
                           'wl_attorney', 'FORGED_VIA_TEMP');
                         RETURN NEW;
                        END $$"""
                )
                attacker_conn.execute(
                    """CREATE TRIGGER forge_audit_after_insert
                       AFTER INSERT ON pg_temp.temp_audit_driver
                       FOR EACH ROW EXECUTE FUNCTION pg_temp.forge_audit_via_temp_trigger()"""
                )
                attacker_conn.execute('INSERT INTO pg_temp.temp_audit_driver VALUES (1)')

            # 2. Attaching weaverlaw.audit_run_edge() directly to a TEMP TABLE must also fail
            with pytest.raises(psycopg.errors.InsufficientPrivilege), attacker_conn.transaction():
                attacker_conn.execute('CREATE TEMP TABLE fake_workflow_run (LIKE weaverlaw.workflow_run)')
                attacker_conn.execute(
                    """CREATE TRIGGER hijack_audit_writer
                       AFTER INSERT ON pg_temp.fake_workflow_run
                       FOR EACH ROW EXECUTE FUNCTION weaverlaw.audit_run_edge()"""
                )

    with _connect(seeded_cluster) as admin:
        forged_count = admin.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE reason_code = 'FORGED_VIA_TEMP'"
        ).fetchone()[0]
        assert forged_count == 0


def test_temp_table_shadowing_cannot_hijack_mapping_or_audit(seeded_cluster):
    import psycopg.errors

    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker_conn,
        _connect(seeded_cluster) as admin,
    ):
        shadow_run_id, created = receive(
            intake_conn, _make_envelope(source_ref='synthetic-temp-shadow-001'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-closed'],
        )
        assert created is True and shadow_run_id is not None

        # Verify wl_resolver cannot mutate weaverlaw.matter_mapping despite FOR SHARE lock policy
        with pytest.raises(psycopg.errors.InsufficientPrivilege), resolver_conn.transaction():
            resolver_conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true)",
                (str(FIRM_A),),
            )
            resolver_conn.execute(
                "UPDATE weaverlaw.matter_mapping SET eligibility = 'OPEN' "
                "WHERE clio_matter_id = 'clio-live-closed'"
            )

        # Create shadowing TEMP tables on resolver_conn (persisting across transactions)
        resolver_conn.execute(
            """CREATE TEMP TABLE matter_mapping (
                   firm_id uuid, verified_matter_id uuid,
                   clio_matter_id text, eligibility text, mapping_version text
               )"""
        )
        resolver_conn.execute(
            "INSERT INTO pg_temp.matter_mapping VALUES (%s, %s, 'clio-live-closed', 'OPEN', 'v-shadow')",
            (FIRM_A, MATTER_A),
        )
        resolver_conn.execute(
            """CREATE TEMP TABLE audit_event (
                   audit_id bigint, firm_id uuid, run_id uuid, scope_mode text,
                   verified_matter_id uuid, old_status text, new_status text,
                   actor text, reason_code text, occurred_at timestamptz
               )"""
        )

        # resolve_run must ignore pg_temp.matter_mapping ('OPEN') and enforce weaverlaw.matter_mapping ('CLOSED')
        outcome = resolve_run(
            resolver_conn, firm_id=FIRM_A, run_id=shadow_run_id, trusted_signals=['clio-live-closed'],
        )
        assert outcome == (State.QUARANTINED, None, 'MATTER_INELIGIBLE')
        assert resolver_conn.execute('SELECT count(*) FROM pg_temp.audit_event').fetchone()[0] == 0

        persisted = admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code '
            'FROM weaverlaw.workflow_run WHERE run_id = %s',
            (shadow_run_id,),
        ).fetchone()
        assert persisted == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE')

        # Also verify transition() ignores pg_temp.workflow_run and pg_temp.audit_event
        valid_run_id, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-temp-shadow-002'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        assert resolve_run(
            resolver_conn, firm_id=FIRM_A, run_id=valid_run_id, trusted_signals=['clio-live-a'],
        ) == (State.VALIDATED, MATTER_A, 'MATTER_VERIFIED')

        worker_conn.execute('CREATE TEMP TABLE workflow_run (LIKE weaverlaw.workflow_run)')
        worker_conn.execute('CREATE TEMP TABLE audit_event (LIKE weaverlaw.audit_event)')
        transition(
            worker_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=valid_run_id,
            expected=State.VALIDATED, target=State.RUNNING,
            actor=Actor.COORDINATOR, reason='START',
        )
        assert worker_conn.execute('SELECT count(*) FROM pg_temp.audit_event').fetchone()[0] == 0
        assert admin.execute(
            'SELECT status FROM weaverlaw.workflow_run WHERE run_id = %s', (valid_run_id,),
        ).fetchone()[0] == 'RUNNING'


def test_concurrency_dedup_cas_and_mapping_revocation_races(seeded_cluster):
    import psycopg.errors

    # (a) Simultaneous identical (source, dedup_key) submissions across parallel connections
    workers = 6
    barrier = threading.Barrier(workers)

    def submit_duplicate(_idx: int):
        env = _make_envelope(source_ref='synthetic-concurrent-dedup-001', event_id=uuid4())
        with _connect(seeded_cluster, 'wl_intake') as conn:
            barrier.wait(timeout=10)
            return receive(conn, env, trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(submit_duplicate, range(workers)))

    winners = [run_id for run_id, created in results if created]
    losers = [run_id for run_id, created in results if not created]
    assert len(winners) == 1 and winners[0] is not None
    assert losers == [winners[0]] * (workers - 1)
    dedup_run_id = winners[0]

    with _connect(seeded_cluster) as admin:
        outbox_count = admin.execute(
            'SELECT count(*) FROM weaverlaw.outbox WHERE run_id = %s', (dedup_run_id,),
        ).fetchone()[0]
        assert outbox_count == 1

    # Matching RECEIVED replays complete the original resolution exactly once.
    with _connect(seeded_cluster) as admin:
        assert admin.execute(
            'SELECT status FROM weaverlaw.workflow_run WHERE run_id = %s', (dedup_run_id,),
        ).fetchone()[0] == 'VALIDATED'

    # (b) Simultaneous competing transitions on the same run_id
    race_barrier = threading.Barrier(workers)

    def compete_transition(idx: int):
        target = State.RUNNING if idx % 2 == 0 else State.CANCELLED
        reason = 'START' if idx % 2 == 0 else 'CANCEL_RACE'
        with _connect(seeded_cluster, 'wl_worker') as conn:
            race_barrier.wait(timeout=10)
            try:
                transition(
                    conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=dedup_run_id,
                    expected=State.VALIDATED, target=target,
                    actor=Actor.COORDINATOR, reason=reason,
                )
                return target
            except Denied:
                return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        cas_outcomes = list(pool.map(compete_transition, range(workers)))

    cas_winners = [outcome for outcome in cas_outcomes if outcome is not None]
    assert len(cas_winners) == 1

    with _connect(seeded_cluster) as admin:
        final_status, audit_count = admin.execute(
            """SELECT r.status, (SELECT count(*) FROM weaverlaw.audit_event a WHERE a.run_id = r.run_id)
               FROM weaverlaw.workflow_run r WHERE r.run_id = %s""",
            (dedup_run_id,),
        ).fetchone()
        assert final_status == cas_winners[0].value
        assert audit_count == 3  # RECEIVED + VALIDATED + single winning transition

    # (c) Direct SQL enforce_run_edge check on closed mapping
    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        revoked_run, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-revoked-map-001'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-closed'],
        )
        with pytest.raises(psycopg.errors.CheckViolation, match='resolution differs from immutable inbox evidence'), resolver_conn.transaction():
            resolver_conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_CLOSED)),
            )
            resolver_conn.execute(
                """UPDATE weaverlaw.workflow_run
                   SET status = 'VALIDATED', scope_mode = 'MATTER_VERIFIED',
                       verified_matter_id = %s, reason_code = 'MATTER_VERIFIED'
                   WHERE run_id = %s AND status = 'RECEIVED'""",
                (MATTER_CLOSED, revoked_run),
            )


def test_concurrent_mapping_revocation_two_session_race(seeded_cluster):
    with (
        _connect(seeded_cluster) as admin_seed,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
    ):
        with admin_seed.transaction():
            admin_seed.execute(
                """INSERT INTO weaverlaw.matter_mapping
                   (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
                   VALUES (%s, %s, 'clio-live-race', 'OPEN', 'v1')
                   ON CONFLICT (firm_id, verified_matter_id)
                   DO UPDATE SET eligibility = 'OPEN'""",
                (FIRM_A, MATTER_RACE),
            )
        race_run_id, created = receive(
            intake_conn,
            _make_envelope(source_ref='synthetic-two-session-revoke-001'),
            trusted_firm_id=FIRM_A, trusted_signals=['clio-live-race'],
        )
        assert created is True and race_run_id is not None

    start_barrier = threading.Barrier(2)

    with (
        _connect(seeded_cluster) as session_a,
        _connect(seeded_cluster, 'wl_resolver') as session_b,
        _connect(seeded_cluster) as monitor,
    ):
        resolver_pid = session_b.info.backend_pid

        def run_concurrent_resolver():
            start_barrier.wait(timeout=10)
            return resolve_run(
                session_b,
                firm_id=FIRM_A,
                run_id=race_run_id,
                trusted_signals=['clio-live-race'],
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            with session_a.transaction():
                # Session A acquires row lock revoking eligibility -> CLOSED without committing yet
                session_a.execute(
                    """UPDATE weaverlaw.matter_mapping
                       SET eligibility = 'CLOSED'
                       WHERE firm_id = %s AND clio_matter_id = 'clio-live-race'""",
                    (FIRM_A,),
                )
                future = pool.submit(run_concurrent_resolver)
                start_barrier.wait(timeout=10)

                # Deterministically verify Session B blocked on FOR SHARE Lock before Session A commits
                deadline = time.monotonic() + 5.0
                blocked_on_lock = False
                while time.monotonic() < deadline:
                    wait_row = monitor.execute(
                        'SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = %s',
                        (resolver_pid,),
                    ).fetchone()
                    if wait_row and wait_row[0] == 'Lock':
                        blocked_on_lock = True
                        break
                    time.sleep(0.01)
                assert blocked_on_lock, (
                    'resolve_run did not serialize on FOR SHARE lock against concurrent mapping revocation'
                )
                # Exiting `with session_a.transaction():` commits eligibility = 'CLOSED'

            outcome = future.result(timeout=10)
            assert outcome == (State.QUARANTINED, None, 'MATTER_INELIGIBLE')

        run_row = monitor.execute(
            """SELECT status, scope_mode, verified_matter_id, reason_code
               FROM weaverlaw.workflow_run WHERE run_id = %s""",
            (race_run_id,),
        ).fetchone()
        assert run_row == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE')

        audit_rows = monitor.execute(
            """SELECT old_status, new_status, actor, scope_mode, verified_matter_id, reason_code
               FROM weaverlaw.audit_event WHERE run_id = %s ORDER BY audit_id""",
            (race_run_id,),
        ).fetchall()
        assert audit_rows == [
            (None, 'RECEIVED', 'wl_intake', 'INTAKE_RESTRICTED', None, 'ACCEPTED'),
            ('RECEIVED', 'QUARANTINED', 'wl_resolver', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE'),
        ]


def test_connection_pool_set_local_and_role_never_leak(seeded_cluster):
    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster) as admin,
    ):
        pool_run_id, created = receive(
            intake_conn, _make_envelope(source_ref='synthetic-pool-isolation-001'), trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        assert created is True and pool_run_id is not None
        assert resolve_run(
            resolver_conn, firm_id=FIRM_A, run_id=pool_run_id, trusted_signals=['clio-live-a'],
        ) == (State.VALIDATED, MATTER_A, 'MATTER_VERIFIED')
        _seed_tier_b_bundle(admin, firm_id=FIRM_A, run_id=pool_run_id, matter_id=MATTER_A, tag='pool')

    with _connect(seeded_cluster) as pooled_conn:
        base_user = pooled_conn.execute('SELECT current_user').fetchone()[0]

        with pooled_conn.transaction():
            pooled_conn.execute('SET LOCAL ROLE wl_worker')
            pooled_conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_A)),
            )
            in_tx_user, in_tx_firm, in_tx_matter = pooled_conn.execute(
                "SELECT current_user, pg_catalog.current_setting('app.verified_firm_id', true), "
                "pg_catalog.current_setting('app.verified_matter_id', true)"
            ).fetchone()
            assert in_tx_user == 'wl_worker'
            assert in_tx_firm == str(FIRM_A)
            assert in_tx_matter == str(MATTER_A)
            assert pooled_conn.execute(
                'SELECT count(*) FROM weaverlaw.workflow_run WHERE run_id = %s', (pool_run_id,),
            ).fetchone()[0] == 1
            for table in TIER_B_TABLES:
                assert pooled_conn.execute(
                    f'SELECT count(*) FROM weaverlaw.{table} WHERE run_id = %s', (pool_run_id,),
                ).fetchone()[0] == 1

        # Next transaction on the EXACT SAME physical connection must have clean role & empty GUCs
        with pooled_conn.transaction():
            after_user, after_firm, after_matter = pooled_conn.execute(
                "SELECT current_user, coalesce(pg_catalog.current_setting('app.verified_firm_id', true), ''), "
                "coalesce(pg_catalog.current_setting('app.verified_matter_id', true), '')"
            ).fetchone()
            assert after_user == base_user
            assert after_firm == ''
            assert after_matter == ''

            pooled_conn.execute('SET LOCAL ROLE wl_worker')
            leaked_runs = pooled_conn.execute('SELECT count(*) FROM weaverlaw.workflow_run').fetchone()[0]
            assert leaked_runs == 0
            for table in TIER_B_TABLES:
                assert pooled_conn.execute(f'SELECT count(*) FROM weaverlaw.{table}').fetchone()[0] == 0


@pytest.mark.parametrize('starting_status', [State.VALIDATED, State.RUNNING])
def test_restricted_matter_after_validation_quarantines_before_forward_transition(seeded_cluster, starting_status):
    matter_id = uuid4()
    signal = f'clio-post-validation-{matter_id.hex}'
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker_conn,
        _connect(seeded_cluster, 'wl_reviewer') as reviewer_conn,
    ):
        admin.execute(
            """INSERT INTO weaverlaw.matter_mapping
               (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
               VALUES (%s, %s, %s, 'OPEN', 'v1')""",
            (FIRM_A, matter_id, signal),
        )
        run_id, created = receive(
            intake_conn, _make_envelope(source_ref=f'synthetic-post-validation-{matter_id.hex}'),
            trusted_firm_id=FIRM_A, trusted_signals=[signal],
        )
        assert created is True
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, matter_id, 'MATTER_VERIFIED',
        )
        if starting_status == State.RUNNING:
            transition(
                worker_conn, firm_id=FIRM_A, matter_id=matter_id, run_id=run_id,
                expected=State.VALIDATED, target=State.RUNNING,
                actor=Actor.COORDINATOR, reason='START',
            )
        original_evidence = admin.execute(
            'SELECT resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        # Preserve historical matter records while removing the run's active scope.
        _seed_tier_b_bundle(admin, firm_id=FIRM_A, run_id=run_id, matter_id=matter_id, tag=matter_id.hex)
        admin.execute(
            "UPDATE weaverlaw.matter_mapping SET eligibility = 'RESTRICTED', mapping_version = 'v2' "
            'WHERE firm_id = %s AND verified_matter_id = %s',
            (FIRM_A, matter_id),
        )
        target = State.RUNNING if starting_status == State.VALIDATED else State.REVIEW_REQUIRED
        conn = worker_conn if starting_status == State.VALIDATED else reviewer_conn
        actor = Actor.COORDINATOR if starting_status == State.VALIDATED else Actor.INDEPENDENT_REVIEW
        # RLS hides revoked scope before a direct SQL role can reach the edge trigger.
        audit_before_direct = admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        with conn.transaction():
            conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(matter_id)),
            )
            assert conn.execute(
                'UPDATE weaverlaw.workflow_run SET status = %s, reason_code = %s WHERE run_id = %s',
                (target.value, 'DIRECT_POST_REVOCATION', run_id),
            ).rowcount == 0
        assert admin.execute(
            'SELECT status FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == starting_status.value
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == audit_before_direct
        with pytest.raises(Denied):
            transition(
                conn, firm_id=FIRM_A, matter_id=matter_id, run_id=run_id,
                expected=starting_status, target=target, actor=actor,
                reason='POST_REVOCATION_ATTEMPT',
                independently_checked=starting_status == State.RUNNING,
            )
        row = admin.execute(
            """SELECT status, scope_mode, verified_matter_id, reason_code, resolution_evidence
               FROM weaverlaw.workflow_run WHERE run_id = %s""", (run_id,),
        ).fetchone()
        assert row == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE', original_evidence)
        audit = admin.execute(
            'SELECT old_status, new_status, reason_code FROM weaverlaw.audit_event '
            'WHERE run_id = %s ORDER BY audit_id DESC LIMIT 1', (run_id,),
        ).fetchone()
        assert audit == (starting_status.value, 'QUARANTINED', 'MATTER_INELIGIBLE')
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.workflow_step WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1
        with worker_conn.transaction():
            worker_conn.execute(
                "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(matter_id)),
            )
            for table in TIER_B_TABLES:
                assert worker_conn.execute(
                    f'SELECT count(*) FROM weaverlaw.{table} WHERE run_id = %s', (run_id,),
                ).fetchone()[0] == 0
        with pytest.raises(Denied):
            transition(
                worker_conn, firm_id=FIRM_A, matter_id=matter_id, run_id=run_id,
                expected=State.VALIDATED, target=State.RUNNING,
                actor=Actor.COORDINATOR, reason='RETRY_REVOKED',
            )


def test_resolution_uses_immutable_intake_evidence_and_rejects_mismatched_signals(seeded_cluster):
    import psycopg.errors

    envelope = _make_envelope(source_ref='synthetic-bound-resolution-evidence')
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        run_id, created = receive(
            intake_conn, envelope, trusted_firm_id=FIRM_A,
            trusted_signals=['clio-live-a'], payload_sha256='a' * 64,
        )
        assert created is True
        before = admin.execute(
            'SELECT status, resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()
        for unbound in (['clio-live-b'], [], ['clio-live-a', 'unbound-signal']):
            with pytest.raises(Denied):
                resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id, trusted_signals=unbound)
            assert admin.execute(
                'SELECT status, resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
            ).fetchone() == before
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, MATTER_A, 'MATTER_VERIFIED',
        )
        inbox_id, evidence = admin.execute(
            'SELECT inbox_id, resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()
        assert evidence['run_id'] == str(run_id)
        assert evidence['inbox_id'] == str(inbox_id)
        assert evidence['source_reference'] == envelope.source_ref
        assert evidence['source_version'] == envelope.source_version
        assert evidence['extracted_signals'] == ['clio-live-a']
        assert evidence['matched_clio_ids'] == ['clio-live-a']
        assert evidence['mapping_version'] == {'clio-live-a': 'v1'}
        assert evidence['decision'] == 'VALIDATED'
        assert evidence['reason_code'] == 'MATTER_VERIFIED'
        inbox_row = admin.execute(
            'SELECT matter_signals, envelope_sha256, payload_sha256 '
            'FROM weaverlaw.event_inbox WHERE inbox_id = %s', (inbox_id,),
        ).fetchone()
        assert inbox_row[0] == ['clio-live-a']
        assert len(inbox_row[1]) == 64 and inbox_row[2] == 'a' * 64
        with pytest.raises(psycopg.errors.CheckViolation, match='(?i)immutable'), admin.transaction():
            admin.execute(
                "UPDATE weaverlaw.event_inbox SET matter_signals = %s::jsonb WHERE inbox_id = %s",
                (json.dumps(['clio-live-b']), inbox_id),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match='(?i)immutable'), admin.transaction():
            admin.execute(
                "UPDATE weaverlaw.workflow_run SET resolution_evidence = '{}'::jsonb WHERE run_id = %s", (run_id,),
            )
        assert admin.execute(
            'SELECT resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == evidence
        no_evidence_run, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-empty-bound-resolution'),
            trusted_firm_id=FIRM_A,
        )
        with pytest.raises(Denied):
            resolve_run(resolver_conn, firm_id=FIRM_A, run_id=no_evidence_run, trusted_signals=['clio-live-a'])
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=no_evidence_run) == (
            State.QUARANTINED, None, 'UNMAPPED_MATTER_SIGNAL',
        )


@pytest.mark.parametrize('conflict', ['signals', 'payload'])
def test_contradictory_replay_quarantines_one_bound_run_and_records_anomaly(seeded_cluster, conflict):
    envelope = _make_envelope(source_ref=f'synthetic-conflicting-replay-{conflict}')
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        run_id, created = receive(
            intake_conn, envelope, trusted_firm_id=FIRM_A,
            trusted_signals=['clio-live-a'], payload_sha256='a' * 64,
        )
        assert created is True
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, MATTER_A, 'MATTER_VERIFIED',
        )
        before = admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        duplicate_envelope = _make_envelope(source_ref=envelope.source_ref)
        assert receive(
            intake_conn, duplicate_envelope, trusted_firm_id=FIRM_A,
            trusted_signals=['clio-live-a', 'clio-live-a'], payload_sha256='a' * 64,
        ) == (run_id, False)
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == before
        original_evidence = admin.execute(
            'SELECT resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        assert receive(
            intake_conn, duplicate_envelope, trusted_firm_id=FIRM_A,
            trusted_signals=['clio-live-b'] if conflict == 'signals' else ['clio-live-a'],
            payload_sha256='b' * 64 if conflict == 'payload' else 'a' * 64,
        ) == (run_id, False)
        assert admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code, resolution_evidence '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone() == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'CONFLICTING_REPLAY_SIGNALS', original_evidence)
        assert admin.execute(
            """SELECT count(*) FROM weaverlaw.workflow_run r JOIN weaverlaw.event_inbox i
               ON i.inbox_id = r.inbox_id WHERE i.source = %s AND i.dedup_key = %s""",
            (envelope.source, envelope.dedup_key),
        ).fetchone()[0] == 1
        assert admin.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s AND reason_code = 'CONFLICTING_REPLAY_SIGNALS'",
            (run_id,),
        ).fetchone()[0] >= 1
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.outbox WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1


def test_contradictory_replay_preserves_terminal_run_and_records_anomaly(seeded_cluster):
    envelope = _make_envelope(source_ref='synthetic-terminal-conflicting-replay')
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker_conn,
    ):
        run_id, _ = receive(intake_conn, envelope, trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'])
        resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id)
        transition(
            worker_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
            expected=State.VALIDATED, target=State.CANCELLED, actor=Actor.COORDINATOR, reason='CANCEL',
        )
        before = admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code, resolution_evidence '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()
        audit_count = admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        assert revalidate_matter_eligibility(
            worker_conn, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
        ) is False
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == audit_count
        assert receive(
            intake_conn, envelope, trusted_firm_id=FIRM_A, trusted_signals=['clio-live-b'],
        ) == (run_id, False)
        assert admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code, resolution_evidence '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone() == before
        assert admin.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s AND reason_code = 'CONFLICTING_REPLAY_SIGNALS'",
            (run_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize('dispatch', ['adapter', 'direct_sql'])
def test_ongoing_transition_serializes_with_two_session_mapping_revocation(seeded_cluster, dispatch):
    import psycopg.errors

    matter_id = uuid4()
    signal = f'clio-ongoing-race-{matter_id.hex}'
    with (
        _connect(seeded_cluster) as admin_seed,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        admin_seed.execute(
            """INSERT INTO weaverlaw.matter_mapping
               (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
               VALUES (%s, %s, %s, 'OPEN', 'v1')""", (FIRM_A, matter_id, signal),
        )
        run_id, _ = receive(
            intake_conn, _make_envelope(source_ref=f'synthetic-ongoing-race-{matter_id.hex}'),
            trusted_firm_id=FIRM_A, trusted_signals=[signal],
        )
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, matter_id, 'MATTER_VERIFIED',
        )

    barrier = threading.Barrier(2)
    with (
        _connect(seeded_cluster) as revoker,
        _connect(seeded_cluster, 'wl_worker') as worker,
        _connect(seeded_cluster) as monitor,
    ):
        worker_pid = worker.info.backend_pid

        def attempt_forward_transition():
            barrier.wait(timeout=10)
            if dispatch == 'adapter':
                try:
                    transition(
                        worker, firm_id=FIRM_A, matter_id=matter_id, run_id=run_id,
                        expected=State.VALIDATED, target=State.RUNNING,
                        actor=Actor.COORDINATOR, reason='RACE_START',
                    )
                except Denied:
                    return 'DENIED'
                return 'MOVED'
            try:
                with worker.transaction():
                    worker.execute(
                        "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                        "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                        (str(FIRM_A), str(matter_id)),
                    )
                    changed = worker.execute(
                        "UPDATE weaverlaw.workflow_run SET status = 'RUNNING', reason_code = 'DIRECT_RACE_START' "
                        "WHERE run_id = %s AND status = 'VALIDATED' RETURNING run_id", (run_id,),
                    ).fetchone()
                return 'NO_ROWS' if changed is None else 'MOVED'
            except psycopg.errors.InsufficientPrivilege as err:
                assert err.sqlstate == '42501'
                return 'DENIED_SQL'

        with ThreadPoolExecutor(max_workers=1) as pool:
            with revoker.transaction():
                revoker.execute(
                    "UPDATE weaverlaw.matter_mapping SET eligibility = 'CLOSED', mapping_version = 'v2' "
                    'WHERE firm_id = %s AND verified_matter_id = %s', (FIRM_A, matter_id),
                )
                future = pool.submit(attempt_forward_transition)
                barrier.wait(timeout=10)
                deadline = time.monotonic() + 5.0
                blocked = False
                while time.monotonic() < deadline:
                    wait = monitor.execute(
                        'SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = %s', (worker_pid,),
                    ).fetchone()
                    if wait and wait[0] == 'Lock':
                        blocked = True
                        break
                    time.sleep(0.01)
                assert blocked, 'ongoing transition did not wait for the concurrent mapping revocation'
            outcome = future.result(timeout=10)

        assert monitor.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s AND new_status = 'RUNNING'", (run_id,),
        ).fetchone()[0] == 0
        row = monitor.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()
        if dispatch == 'adapter':
            assert outcome == 'DENIED'
            assert row == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE')
        else:
            assert outcome in ('DENIED_SQL', 'NO_ROWS')
            assert row == ('VALIDATED', 'MATTER_VERIFIED', matter_id, 'MATTER_VERIFIED')
            assert revalidate_matter_eligibility(
                worker, firm_id=FIRM_A, matter_id=matter_id, run_id=run_id,
            ) is False
            assert monitor.execute(
                'SELECT status, scope_mode, verified_matter_id, reason_code '
                'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
            ).fetchone() == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE')
        assert monitor.execute(
            'SELECT old_status, new_status, reason_code FROM weaverlaw.audit_event '
            'WHERE run_id = %s ORDER BY audit_id DESC LIMIT 1', (run_id,),
        ).fetchone() == ('VALIDATED', 'QUARANTINED', 'MATTER_INELIGIBLE')



def test_provider_identifier_reassignment_quarantines_original_run_and_hides_tier_b(seeded_cluster):
    matter_a, matter_b = uuid4(), uuid4()
    original_signal = f'clio-original-{matter_a.hex}'
    alias_signal = f'clio-alias-{matter_a.hex}'
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker,
    ):
        admin.execute(
            """INSERT INTO weaverlaw.matter_mapping
               (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
               VALUES (%s, %s, %s, 'OPEN', 'v1'), (%s, %s, %s, 'OPEN', 'v1')""",
            (FIRM_A, matter_a, original_signal, FIRM_A, matter_b, f'clio-placeholder-{matter_b.hex}'),
        )
        run_id, _ = receive(
            intake_conn, _make_envelope(source_ref=f'synthetic-provider-reassignment-{matter_a.hex}'),
            trusted_firm_id=FIRM_A, trusted_signals=[original_signal],
        )
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, matter_a, 'MATTER_VERIFIED',
        )
        original_evidence = admin.execute(
            'SELECT resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        _seed_tier_b_bundle(admin, firm_id=FIRM_A, run_id=run_id, matter_id=matter_a, tag=matter_a.hex)
        with admin.transaction():
            admin.execute(
                "UPDATE weaverlaw.matter_mapping SET clio_matter_id = %s, mapping_version = 'v2' "
                'WHERE firm_id = %s AND verified_matter_id = %s',
                (alias_signal, FIRM_A, matter_a),
            )
            admin.execute(
                "UPDATE weaverlaw.matter_mapping SET clio_matter_id = %s, mapping_version = 'v2' "
                'WHERE firm_id = %s AND verified_matter_id = %s',
                (original_signal, FIRM_A, matter_b),
            )
        assert admin.execute(
            'SELECT clio_matter_id, eligibility FROM weaverlaw.matter_mapping '
            'WHERE firm_id = %s AND verified_matter_id = %s', (FIRM_A, matter_a),
        ).fetchone() == (alias_signal, 'OPEN')
        # Rebinding the provider identity revokes retrieval immediately, before quarantine is persisted.
        for after_quarantine in (False, True):
            if after_quarantine:
                with pytest.raises(Denied):
                    transition(
                        worker, firm_id=FIRM_A, matter_id=matter_a, run_id=run_id,
                        expected=State.VALIDATED, target=State.RUNNING,
                        actor=Actor.COORDINATOR, reason='REBIND_START',
                    )
            with worker.transaction():
                worker.execute(
                    "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
                    "pg_catalog.set_config('app.verified_matter_id', %s, true)",
                    (str(FIRM_A), str(matter_a)),
                )
                for table in TIER_B_TABLES:
                    assert worker.execute(
                        f'SELECT count(*) FROM weaverlaw.{table} WHERE run_id = %s', (run_id,),
                    ).fetchone()[0] == 0
        assert admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code, resolution_evidence '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone() == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE', original_evidence)
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.workflow_step WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1


def test_concurrent_provider_reassignment_serializes_before_ongoing_transition(seeded_cluster):
    matter_a, matter_b = uuid4(), uuid4()
    original_signal = f'clio-reassignment-race-{matter_a.hex}'
    with (
        _connect(seeded_cluster) as admin_seed,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        admin_seed.execute(
            """INSERT INTO weaverlaw.matter_mapping
               (firm_id, verified_matter_id, clio_matter_id, eligibility, mapping_version)
               VALUES (%s, %s, %s, 'OPEN', 'v1'), (%s, %s, %s, 'OPEN', 'v1')""",
            (FIRM_A, matter_a, original_signal, FIRM_A, matter_b, f'clio-placeholder-{matter_b.hex}'),
        )
        run_id, _ = receive(
            intake_conn, _make_envelope(source_ref=f'synthetic-provider-race-{matter_a.hex}'),
            trusted_firm_id=FIRM_A, trusted_signals=[original_signal],
        )
        assert resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id) == (
            State.VALIDATED, matter_a, 'MATTER_VERIFIED',
        )
    barrier = threading.Barrier(2)
    with (
        _connect(seeded_cluster) as remapper,
        _connect(seeded_cluster, 'wl_worker') as worker,
        _connect(seeded_cluster) as monitor,
    ):
        worker_pid = worker.info.backend_pid

        def attempt_transition():
            barrier.wait(timeout=10)
            with pytest.raises(Denied):
                transition(
                    worker, firm_id=FIRM_A, matter_id=matter_a, run_id=run_id,
                    expected=State.VALIDATED, target=State.RUNNING,
                    actor=Actor.COORDINATOR, reason='CONCURRENT_REASSIGNMENT',
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            with remapper.transaction():
                remapper.execute(
                    "UPDATE weaverlaw.matter_mapping SET clio_matter_id = %s, mapping_version = 'v2' "
                    'WHERE firm_id = %s AND verified_matter_id = %s',
                    (f'clio-alias-{matter_a.hex}', FIRM_A, matter_a),
                )
                remapper.execute(
                    "UPDATE weaverlaw.matter_mapping SET clio_matter_id = %s, mapping_version = 'v2' "
                    'WHERE firm_id = %s AND verified_matter_id = %s',
                    (original_signal, FIRM_A, matter_b),
                )
                future = pool.submit(attempt_transition)
                barrier.wait(timeout=10)
                deadline = time.monotonic() + 5.0
                blocked = False
                while time.monotonic() < deadline:
                    wait = monitor.execute(
                        'SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = %s', (worker_pid,),
                    ).fetchone()
                    if wait and wait[0] == 'Lock':
                        blocked = True
                        break
                    time.sleep(0.01)
                assert blocked, 'ongoing eligibility recheck did not lock the bound provider mapping'
            future.result(timeout=10)
        assert monitor.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code '
            'FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone() == ('QUARANTINED', 'INTAKE_RESTRICTED', None, 'MATTER_INELIGIBLE')
        assert monitor.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s AND new_status = 'RUNNING'", (run_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize('signals', [
    None, {}, {'signal': 'clio-live-a'}, 'clio-live-a', [False], [1], [''],
    ['clio live a'], ['clio\tlive-a'], ['clio\nlive-a'], ['clio\x1flive-a'],
    ['clio\x7flive-a'], ['clio\x85live-a'], ['clio\u00a0live-a'], ['clio\u2000live-a'],
    ['s' * 129], [f'clio-{index}' for index in range(33)], ['clio-live-a'] * 1025,
])
def test_sql_signal_boundary_rejects_malformed_or_unbounded_evidence(seeded_cluster, signals):
    import psycopg.errors

    with (
        _connect(seeded_cluster) as admin,
        pytest.raises(psycopg.errors.CheckViolation),
        admin.transaction(),
    ):
        admin.execute('SELECT weaverlaw.canonical_signals(%s::jsonb)', (json.dumps(signals),)).fetchone()


def test_sql_signal_boundary_accepts_bounded_exact_canonical_values(seeded_cluster):
    with _connect(seeded_cluster) as admin:
        assert admin.execute(
            'SELECT weaverlaw.canonical_signals(%s::jsonb)',
            (json.dumps(['clio-z', 'clio-a'] * 512),),
        ).fetchone()[0] == ['clio-a', 'clio-z']
        signals = [f'clio-{index:02d}' for index in range(32)]
        assert admin.execute(
            'SELECT weaverlaw.canonical_signals(%s::jsonb)', (json.dumps(list(reversed(signals))),),
        ).fetchone()[0] == signals


def test_identical_received_replay_completes_bound_resolution_once(seeded_cluster):
    envelope = _make_envelope(source_ref='synthetic-received-replay-resolution')
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
    ):
        run_id, created = receive(
            intake_conn, envelope, trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
        )
        assert created is True
        assert admin.execute(
            'SELECT status FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 'RECEIVED'
        for _ in range(3):
            assert receive(
                intake_conn, _make_envelope(source_ref=envelope.source_ref),
                trusted_firm_id=FIRM_A, trusted_signals=['clio-live-a'],
            ) == (run_id, False)
        status, evidence = admin.execute(
            'SELECT status, resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()
        assert status == 'VALIDATED'
        assert evidence['extracted_signals'] == ['clio-live-a']
        assert evidence['matched_clio_ids'] == ['clio-live-a']
        assert evidence['verified_matter_id'] == str(MATTER_A)
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 2
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.outbox WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize('terminal', [False, True])
def test_replay_storm_bounds_anomaly_audits_and_counts_all_conflicts(seeded_cluster, terminal):
    envelope = _make_envelope(source_ref=f'synthetic-replay-storm-{terminal}')
    with (
        _connect(seeded_cluster) as admin,
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster, 'wl_worker') as worker,
    ):
        run_id, _ = receive(
            intake_conn, envelope, trusted_firm_id=FIRM_A,
            trusted_signals=['clio-live-a'], payload_sha256='a' * 64,
        )
        resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_id)
        if terminal:
            transition(
                worker, firm_id=FIRM_A, matter_id=MATTER_A, run_id=run_id,
                expected=State.VALIDATED, target=State.CANCELLED,
                actor=Actor.COORDINATOR, reason='CANCEL',
            )
        original_evidence = admin.execute(
            'SELECT resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s', (run_id,),
        ).fetchone()[0]
        for number in list(range(1, 41)) + [40] * 10:
            assert receive(
                intake_conn, envelope, trusted_firm_id=FIRM_A,
                trusted_signals=['clio-live-a'], payload_sha256=f'{number:064x}',
            ) == (run_id, False)
        status, scope, matter, reason, count, digests, evidence = admin.execute(
            'SELECT status, scope_mode, verified_matter_id, reason_code, replay_conflict_count, '
            'replay_conflict_digests::text[], resolution_evidence FROM weaverlaw.workflow_run WHERE run_id = %s',
            (run_id,),
        ).fetchone()
        assert count == 50
        assert len(digests) == 8 and len(set(digests)) == 8
        assert evidence == original_evidence
        if terminal:
            assert (status, scope, matter, reason) == ('CANCELLED', 'MATTER_VERIFIED', MATTER_A, 'CANCEL')
        else:
            assert (status, scope, matter, reason) == (
                'QUARANTINED', 'INTAKE_RESTRICTED', None, 'CONFLICTING_REPLAY_SIGNALS',
            )
        assert admin.execute(
            "SELECT count(*) FROM weaverlaw.audit_event WHERE run_id = %s AND reason_code = 'CONFLICTING_REPLAY_SIGNALS'",
            (run_id,),
        ).fetchone()[0] == 8
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.workflow_run r JOIN weaverlaw.event_inbox i '
            'ON i.inbox_id = r.inbox_id WHERE i.source = %s AND i.dedup_key = %s',
            (envelope.source, envelope.dedup_key),
        ).fetchone()[0] == 1
        assert admin.execute(
            'SELECT count(*) FROM weaverlaw.outbox WHERE run_id = %s', (run_id,),
        ).fetchone()[0] == 1
