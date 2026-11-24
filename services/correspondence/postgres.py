"""Trusted service-side PostgreSQL adapter, using a caller-owned psycopg 3 connection.

No connections, credentials or cloud calls are created here. A connection must be
idle and dedicated to the named database role. Scope values come from the trusted
server boundary, never an LLM or raw event. The database enforces role and RLS.
"""
import json
from uuid import uuid4

from .core import (
    Actor,
    Denied,
    Envelope,
    Mapping,
    Resolver,
    State,
    canonical_signals,
    check_edge,
    intake_evidence,
    validate_transition,
)


def _idle(conn):
    # psycopg TransactionStatus.IDLE == 0; reject nested/savepoint semantics.
    if conn.info.transaction_status != 0:
        raise Denied("dedicated idle connection required")


def _scope(cur, firm_id, matter_id=None):
    cur.execute("SET LOCAL search_path = pg_catalog")
    cur.execute("SET LOCAL lock_timeout = '5s'")
    cur.execute("SET LOCAL statement_timeout = '15s'")
    cur.execute(
        "SELECT pg_catalog.set_config('app.verified_firm_id', %s, true), "
        "pg_catalog.set_config('app.verified_matter_id', %s, true)",
        (str(firm_id), str(matter_id) if matter_id else ''),
    )


def _cas(cur, firm_id, run_id, expected, target, actor, matter_id, reason):
    expected, target, actor = validate_transition(expected, target, actor)
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


def receive(conn, envelope, *, trusted_firm_id, trusted_signals=(), payload_sha256=None):
    """Atomically bind immutable synthetic evidence and create one run/outbox.

    Replays return the originating run ID. Contradictions quarantine active work
    through a narrow database function without granting intake matter access.
    """
    if envelope.firm_id != trusted_firm_id:
        raise Denied('firm mismatch')
    evidence = intake_evidence(envelope, trusted_signals, payload_sha256)
    inbox_id, run_id = uuid4(), uuid4()
    _idle(conn)
    with conn.transaction(), conn.cursor() as cur:
        _scope(cur, trusted_firm_id)
        cur.execute("""INSERT INTO weaverlaw.event_inbox
            (inbox_id,firm_id,event_id,source,event_type,schema_version,occurred_at,received_at,
             correlation_id,source_ref,source_version,sensitivity,dedup_key,
             matter_signals,envelope_sha256,payload_sha256,evidence_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::pg_catalog.jsonb,%s,%s,%s)
            ON CONFLICT (source,dedup_key) DO NOTHING RETURNING inbox_id""",
            (inbox_id, trusted_firm_id, envelope.event_id, envelope.source,
             envelope.event_type,envelope.schema_version,envelope.occurred_at,
             envelope.received_at,envelope.correlation_id,envelope.source_ref,
             envelope.source_version,envelope.sensitivity,envelope.dedup_key,
             json.dumps(evidence['matter_signals']), evidence['envelope_sha256'],
             evidence['payload_sha256'], evidence['evidence_sha256']))
        if cur.fetchone() is None:
            cur.execute("""SELECT inbox_id FROM weaverlaw.event_inbox
                WHERE firm_id=%s AND source=%s AND dedup_key=%s""",
                (trusted_firm_id, envelope.source, envelope.dedup_key))
            original = cur.fetchone()
            if original is None:
                raise Denied('missing or unauthorized intake evidence')
            cur.execute("""SELECT weaverlaw.check_replay(
                %s,%s,%s::pg_catalog.jsonb,%s::pg_catalog.text,
                %s::pg_catalog.text,%s::pg_catalog.text)""",
                (trusted_firm_id, original[0], json.dumps(evidence['matter_signals']),
                 evidence['envelope_sha256'], evidence['payload_sha256'], evidence['evidence_sha256']))
            existing = cur.fetchone()
            if existing is None or existing[0] is None:
                raise Denied('missing or unauthorized replay run')
            return existing[0], False
        cur.execute("""INSERT INTO weaverlaw.workflow_run
            (run_id,firm_id,inbox_id) VALUES (%s,%s,%s)""", (run_id,trusted_firm_id,inbox_id))
        cur.execute("""INSERT INTO weaverlaw.outbox
            (outbox_id,firm_id,run_id,topic,idempotency_key)
            VALUES (%s,%s,%s,'RESOLUTION_PENDING',%s)""",
            (uuid4(),trusted_firm_id,run_id,str(run_id)+':resolve:v1'))
    return run_id, True


ingest_event = receive


def resolve_run(conn, *, firm_id, run_id, trusted_signals=None):
    """Resolve only immutable evidence from the run's originating inbox.

    Optional caller signals are a consistency assertion, never an alternate
    source of authority. PostgreSQL independently derives the resolution audit.
    """
    supplied = None if trusted_signals is None else canonical_signals(trusted_signals)
    _idle(conn)
    with conn.transaction(), conn.cursor() as cur:
        _scope(cur, firm_id)
        cur.execute("""SELECT pg_catalog.to_jsonb(e)
            FROM weaverlaw.event_inbox e JOIN weaverlaw.workflow_run r
              ON r.inbox_id=e.inbox_id AND r.firm_id=e.firm_id
            WHERE r.firm_id=%s AND r.run_id=%s AND r.status='RECEIVED'""",
            (firm_id, run_id))
        row = cur.fetchone()
        if row is None:
            raise Denied('missing or unauthorized intake evidence')
        bound = row[0]
        try:
            envelope = Envelope.model_validate({key: bound[key] for key in Envelope.model_fields})
            evidence = intake_evidence(envelope, bound['matter_signals'], bound['payload_sha256'])
        except (KeyError, TypeError, ValueError) as err:
            raise Denied('invalid intake evidence') from err
        if any(evidence[key] != bound[key] for key in ('envelope_sha256','payload_sha256','evidence_sha256')):
            raise Denied('intake evidence digest mismatch')
        signals = canonical_signals(bound['matter_signals'])
        if list(signals) != bound['matter_signals'] or (supplied is not None and supplied != signals):
            raise Denied('signals do not match bound intake evidence')
        cur.execute("""SELECT firm_id,verified_matter_id,clio_matter_id,eligibility,mapping_version
            FROM weaverlaw.matter_mapping
            WHERE firm_id=%s AND clio_matter_id::pg_catalog.text=ANY(%s::pg_catalog.text[])
            ORDER BY verified_matter_id FOR SHARE""", (firm_id,list(signals)))
        matter, reason = Resolver(Mapping(*mapping) for mapping in cur.fetchall()).resolve(firm_id,signals)
        target = State.VALIDATED if matter else State.QUARANTINED
        _scope(cur,firm_id,matter)
        _cas(cur,firm_id,run_id,State.RECEIVED,target,Actor.RESOLVER,matter,reason)
    return target, matter, reason


def _revalidate(cur, firm_id, run_id, matter_id):
    cur.execute('SELECT weaverlaw.revalidate_run(%s,%s,%s)', (firm_id,run_id,matter_id))
    result = cur.fetchone()
    if result is None:
        raise Denied('missing or unauthorized run')
    return result[0] is True


def revalidate_matter_eligibility(conn, *, firm_id, matter_id, run_id):
    """Commit a revoked-scope quarantine and return whether work may continue."""
    if matter_id is None:
        raise Denied('verified scope required')
    _idle(conn)
    with conn.transaction(), conn.cursor() as cur:
        _scope(cur,firm_id,matter_id)
        eligible = _revalidate(cur,firm_id,run_id,matter_id)
    return eligible


def transition(
    conn, *, firm_id, matter_id, run_id, expected, target, actor, reason,
    independently_checked=False,
):
    """Trusted role dispatch with continuous eligibility and atomic CAS/audit."""
    expected, target, actor = validate_transition(expected, target, actor)
    if expected == State.RECEIVED or actor == Actor.RESOLVER or matter_id is None:
        raise Denied('use dedicated resolution path')
    check_edge(expected, target, actor, matter_id, reason)
    _idle(conn)
    with conn.transaction(), conn.cursor() as cur:
        _scope(cur,firm_id,matter_id)
        eligible = _revalidate(cur,firm_id,run_id,matter_id)
        if eligible:
            # Revoked bindings must be quarantined even when review evidence is
            # absent or a caller supplied the review flag on the wrong edge.
            if target == State.REVIEW_REQUIRED and independently_checked is not True:
                raise Denied('independent review required')
            if independently_checked is not False and (
                independently_checked is not True
                or (expected, target, actor)
                != (State.RUNNING, State.REVIEW_REQUIRED, Actor.INDEPENDENT_REVIEW)
            ):
                raise Denied('independent review binding denied')
            _cas(cur,firm_id,run_id,expected,target,actor,matter_id,reason)
    # Denial must not roll back the durable quarantine recorded above.
    if not eligible:
        raise Denied('matter no longer eligible; run quarantined')


transition_run = transition
