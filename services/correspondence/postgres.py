"""Trusted service-side PostgreSQL adapter, using a caller-owned psycopg 3 connection.

No connections, credentials or cloud calls are created here. A connection must be
idle and dedicated to the named database role. Scope values come from the trusted
server boundary, never an LLM or raw event. The database enforces role and RLS.
"""
from uuid import uuid4
from .core import Actor, Denied, Mapping, Resolver, State, check_edge


def _idle(conn):
    # psycopg TransactionStatus.IDLE == 0; reject nested/savepoint semantics.
    if conn.info.transaction_status != 0:
        raise Denied("dedicated idle connection required")


def _scope(cur, firm_id, matter_id=None):
    cur.execute("SELECT set_config('app.verified_firm_id', %s, true), "
                "set_config('app.verified_matter_id', %s, true)",
                (str(firm_id), str(matter_id) if matter_id else ''))


def _cas(cur, firm_id, run_id, expected, target, actor, matter_id, reason):
    check_edge(expected, target, actor, matter_id, reason)
    # A database AFTER trigger appends audit_event in this same transaction.
    cur.execute("""UPDATE weaverlaw.workflow_run
        SET status=%s, reason_code=%s, scope_mode=%s, verified_matter_id=%s
        WHERE run_id=%s AND firm_id=%s AND status=%s
          AND verified_matter_id IS NOT DISTINCT FROM %s
        RETURNING run_id""", (target.value, reason,
        'MATTER_VERIFIED' if matter_id else 'INTAKE_RESTRICTED', matter_id,
        run_id, firm_id, expected.value, None if expected == State.RECEIVED else matter_id))
    if cur.fetchone() is None:
        raise Denied('stale, missing or unauthorized run')


def receive(conn, envelope, *, trusted_firm_id):
    """wl_intake: inbox + received run + audit trigger + outbox commit together.

    Return (run_id, True) for new input; (None, False) for replay. Never expose
    a verified run through the restricted intake path. The caller acks on return.
    """
    if envelope.firm_id != trusted_firm_id:
        raise Denied('firm mismatch')
    inbox_id, run_id = uuid4(), uuid4()
    _idle(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            _scope(cur, trusted_firm_id)
            cur.execute("""INSERT INTO weaverlaw.event_inbox
                (inbox_id,firm_id,event_id,source,event_type,schema_version,occurred_at,received_at,
                 correlation_id,source_ref,source_version,sensitivity,dedup_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source,dedup_key) DO NOTHING RETURNING inbox_id""",
                (inbox_id, trusted_firm_id, envelope.event_id, envelope.source,
                 envelope.event_type,envelope.schema_version,envelope.occurred_at,
                 envelope.received_at,envelope.correlation_id,envelope.source_ref,
                 envelope.source_version,envelope.sensitivity,envelope.dedup_key))
            if cur.fetchone() is None:
                return None, False
            cur.execute("""INSERT INTO weaverlaw.workflow_run
                (run_id,firm_id,inbox_id) VALUES (%s,%s,%s)""", (run_id,trusted_firm_id,inbox_id))
            cur.execute("""INSERT INTO weaverlaw.outbox
                (outbox_id,firm_id,run_id,topic,idempotency_key)
                VALUES (%s,%s,%s,'RESOLUTION_PENDING',%s)""",
                (uuid4(),trusted_firm_id,run_id,str(run_id)+':resolve:v1'))
    return run_id, True


def resolve_run(conn, *, firm_id, run_id, trusted_signals):
    """wl_resolver: trusted synthetic signal lookup only; no Tier B access."""
    signals = list(dict.fromkeys(trusted_signals))
    _idle(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            _scope(cur, firm_id)
            cur.execute("""SELECT firm_id,verified_matter_id,clio_matter_id,eligibility
                FROM weaverlaw.matter_mapping WHERE firm_id=%s AND clio_matter_id::text=ANY(%s::text[])""",
                (firm_id,signals))
            matter, reason = Resolver(Mapping(*row) for row in cur.fetchall()).resolve(firm_id,signals)
            target = State.VALIDATED if matter else State.QUARANTINED
            _scope(cur,firm_id,matter)
            _cas(cur,firm_id,run_id,State.RECEIVED,target,Actor.RESOLVER,matter,reason)
    return target, matter, reason


def transition(conn, *, firm_id, matter_id, run_id, expected, target, actor, reason, independently_checked=False):
    """Internal service dispatch only; never expose actor or scope as client input.

    Independent-review calls require prior verified review evidence by the trusted
    review service. Its actual verifier is deferred. Approval/effects stay disabled.
    """
    if target == State.REVIEW_REQUIRED and independently_checked is not True:
        raise Denied('independent review required')
    if expected == State.RECEIVED or actor == Actor.RESOLVER or matter_id is None:
        raise Denied('use dedicated resolution path')
    _idle(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            _scope(cur,firm_id,matter_id)
            _cas(cur,firm_id,run_id,expected,target,actor,matter_id,reason)
