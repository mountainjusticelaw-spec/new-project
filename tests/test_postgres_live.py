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
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from services.correspondence.core import Actor, Denied, Envelope, State
from services.correspondence.postgres import receive, resolve_run, transition

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SQL = REPO_ROOT / 'database' / 'migrations' / '001_correspondence_control_layer.sql'
TEST_SQL = REPO_ROOT / 'database' / 'tests' / '001_correspondence_control_layer_test.sql'

ROLES = ('wl_intake', 'wl_resolver', 'wl_worker', 'wl_reviewer', 'wl_attorney', 'wl_executor')
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
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'weaverlaw' AND c.relkind = 'r'"""
        ).fetchall()
        assert {row[0] for row in rls_rows} == set(ALL_TABLES)
        assert all(row[1] and row[2] for row in rls_rows)

        role_rows = admin.execute(
            """SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
               FROM pg_roles WHERE rolname = ANY(%s)""",
            (list(ROLES),),
        ).fetchall()
        assert {row[0] for row in role_rows} == set(ROLES)
        assert all((not can_login and not is_super and not bypass_rls)
                   for _, can_login, is_super, bypass_rls in role_rows)

    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster) as admin,
    ):
        run_a, ok_a = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-a'), trusted_firm_id=FIRM_A)
        run_b, ok_b = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-b'), trusted_firm_id=FIRM_A)
        run_q, ok_q = receive(intake_conn, _make_envelope(source_ref='synthetic-rls-q'), trusted_firm_id=FIRM_A)
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
                "SELECT set_config('app.verified_firm_id', %s, true), "
                "set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_A)),
            )
            visible_runs = conn.execute(
                'SELECT run_id, verified_matter_id, status FROM weaverlaw.workflow_run'
            ).fetchall()
            assert visible_runs == [(run_a, MATTER_A, 'VALIDATED')]
            for table in TIER_B_TABLES:
                matters = conn.execute(
                    f'SELECT verified_matter_id FROM weaverlaw.{table}'
                ).fetchall()
                assert matters == [(MATTER_A,)], f'{role} leaked rows on {table}: {matters}'

    # wl_intake and wl_resolver cannot read Tier B tables; wl_executor has zero table grants in PR1
    for denied_role in ('wl_intake', 'wl_resolver', 'wl_executor'):
        with _connect(seeded_cluster, denied_role) as conn:
            for table in TIER_B_TABLES:
                with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                    conn.execute(
                        "SELECT set_config('app.verified_firm_id', %s, true), "
                        "set_config('app.verified_matter_id', %s, true)",
                        (str(FIRM_A), str(MATTER_A)),
                    )
                    conn.execute(f'SELECT count(*) FROM weaverlaw.{table}').fetchone()

    # wl_intake cannot see MATTER_VERIFIED promoted runs, only INTAKE_RESTRICTED quarantined/received runs
    with _connect(seeded_cluster, 'wl_intake') as intake_conn, intake_conn.transaction():
        intake_conn.execute(
            "SELECT set_config('app.verified_firm_id', %s, true), "
            "set_config('app.verified_matter_id', %s, true)",
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
            intake_conn, _make_envelope(source_ref='synthetic-audit-chain'), trusted_firm_id=FIRM_A,
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

        # Prove UPDATE, DELETE, and TRUNCATE on audit_event fail for all 6 roles
        for role in ROLES:
            with _connect(seeded_cluster, role) as role_conn:
                for stmt in (
                    "UPDATE weaverlaw.audit_event SET reason_code = 'TAMPER'",
                    'DELETE FROM weaverlaw.audit_event',
                    'TRUNCATE weaverlaw.audit_event',
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege), role_conn.transaction():
                        role_conn.execute(stmt)

        # Prove direct forged INSERT into audit_event outside trigger fails RLS (pg_trigger_depth() == 0)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), worker_conn.transaction():
            worker_conn.execute(
                "SELECT set_config('app.verified_firm_id', %s, true), "
                "set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_A)),
            )
            worker_conn.execute(
                """INSERT INTO weaverlaw.audit_event
                   (firm_id, run_id, scope_mode, verified_matter_id, old_status, new_status, actor, reason_code)
                   VALUES (%s, %s, 'MATTER_VERIFIED', %s, 'REJECTED', 'REJECTED', 'wl_worker', 'FORGED')""",
                (FIRM_A, run_id, MATTER_A),
            )

        # Prove simulated audit-insert failure completely rolls back workflow_run transition
        run_rb, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-audit-rollback'), trusted_firm_id=FIRM_A,
        )
        resolve_run(resolver_conn, firm_id=FIRM_A, run_id=run_rb, trusted_signals=['clio-live-a'])
        admin.execute('REVOKE INSERT ON weaverlaw.audit_event FROM wl_worker')
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
            admin.execute('GRANT INSERT ON weaverlaw.audit_event TO wl_worker')


def test_concurrency_dedup_cas_and_mapping_revocation_races(seeded_cluster):
    import psycopg.errors

    # (a) Simultaneous identical (source, dedup_key) submissions across parallel connections
    workers = 6
    barrier = threading.Barrier(workers)

    def submit_duplicate(idx: int):
        env = _make_envelope(source_ref='synthetic-concurrent-dedup-001', event_id=uuid4())
        with _connect(seeded_cluster, 'wl_intake') as conn:
            barrier.wait(timeout=10)
            return receive(conn, env, trusted_firm_id=FIRM_A)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(submit_duplicate, range(workers)))

    winners = [run_id for run_id, created in results if created]
    losers = [run_id for run_id, created in results if not created]
    assert len(winners) == 1 and winners[0] is not None
    assert losers == [None] * (workers - 1)
    dedup_run_id = winners[0]

    with _connect(seeded_cluster) as admin:
        outbox_count = admin.execute(
            'SELECT count(*) FROM weaverlaw.outbox WHERE run_id = %s', (dedup_run_id,),
        ).fetchone()[0]
        assert outbox_count == 1

    # (b) Simultaneous competing transitions on the same run_id
    with _connect(seeded_cluster, 'wl_resolver') as resolver_conn:
        resolve_run(resolver_conn, firm_id=FIRM_A, run_id=dedup_run_id, trusted_signals=['clio-live-a'])

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

    # (c) Mapping eligibility revocation enforced inside enforce_run_edge trigger
    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
    ):
        revoked_run, _ = receive(
            intake_conn, _make_envelope(source_ref='synthetic-revoked-map-001'), trusted_firm_id=FIRM_A,
        )
        with pytest.raises(psycopg.errors.RaiseException), resolver_conn.transaction():
            resolver_conn.execute(
                "SELECT set_config('app.verified_firm_id', %s, true), "
                "set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_CLOSED)),
            )
            resolver_conn.execute(
                """UPDATE weaverlaw.workflow_run
                   SET status = 'VALIDATED', scope_mode = 'MATTER_VERIFIED',
                       verified_matter_id = %s, reason_code = 'MATTER_VERIFIED'
                   WHERE run_id = %s AND status = 'RECEIVED'""",
                (MATTER_CLOSED, revoked_run),
            )


def test_connection_pool_set_local_and_role_never_leak(seeded_cluster):
    with (
        _connect(seeded_cluster, 'wl_intake') as intake_conn,
        _connect(seeded_cluster, 'wl_resolver') as resolver_conn,
        _connect(seeded_cluster) as admin,
    ):
        pool_run_id, created = receive(
            intake_conn, _make_envelope(source_ref='synthetic-pool-isolation-001'), trusted_firm_id=FIRM_A,
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
                "SELECT set_config('app.verified_firm_id', %s, true), "
                "set_config('app.verified_matter_id', %s, true)",
                (str(FIRM_A), str(MATTER_A)),
            )
            in_tx_user, in_tx_firm, in_tx_matter = pooled_conn.execute(
                "SELECT current_user, current_setting('app.verified_firm_id', true), "
                "current_setting('app.verified_matter_id', true)"
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
                "SELECT current_user, coalesce(current_setting('app.verified_firm_id', true), ''), "
                "coalesce(current_setting('app.verified_matter_id', true), '')"
            ).fetchone()
            assert after_user == base_user
            assert after_firm == ''
            assert after_matter == ''

            pooled_conn.execute('SET LOCAL ROLE wl_worker')
            leaked_runs = pooled_conn.execute('SELECT count(*) FROM weaverlaw.workflow_run').fetchone()[0]
            assert leaked_runs == 0
            for table in TIER_B_TABLES:
                assert pooled_conn.execute(f'SELECT count(*) FROM weaverlaw.{table}').fetchone()[0] == 0
