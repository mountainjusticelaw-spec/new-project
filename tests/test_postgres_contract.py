"""DB adapter unit tests, not a substitute for actual PostgreSQL RLS tests."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID
import pytest
from services.correspondence.core import Actor, Denied, Envelope, State
from services.correspondence.postgres import receive, resolve_run, transition


class FakeConnection:
    def __init__(self, returned=None, rows=()):
        self.returned = returned
        self.rows = list(rows)
        self.info = SimpleNamespace(transaction_status=0)
        self.commands = []
        self.commits = 0
        self.rollbacks = 0
    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
    @contextmanager
    def cursor(self):
        yield self
    def execute(self, sql, params):
        self.commands.append((sql,params))
    def fetchone(self):
        if isinstance(self.returned, list):
            return self.returned.pop(0)
        return self.returned
    def fetchall(self):
        return list(self.rows)


def sample_envelope(firm_id=UUID(int=1)):
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, 14, tzinfo=timezone.utc)
    return Envelope(
        schema_version=1,
        event_id=UUID(int=4),
        source='synthetic.gmail',
        event_type='correspondence.received',
        firm_id=firm_id,
        occurred_at=now,
        received_at=now,
        correlation_id=UUID(int=5),
        source_ref='synthetic-message-001',
        source_version='v1',
        sensitivity='synthetic',
    )


def move(conn, expected=State.VALIDATED, target=State.RUNNING, actor=Actor.COORDINATOR):
    return transition(conn,firm_id=UUID(int=1),matter_id=UUID(int=2),run_id=UUID(int=3),
                      expected=expected,target=target,actor=actor,reason='START')


def test_conditional_update_uses_status_and_scope():
    conn = FakeConnection((UUID(int=3),))
    move(conn)
    sql, args = conn.commands[-1]
    assert 'WHERE run_id=%s AND firm_id=%s AND status=%s' in sql
    assert 'verified_matter_id IS NOT DISTINCT FROM %s' in sql
    assert args[-2:] == ('VALIDATED',UUID(int=2))
    assert conn.commits == 1 and not conn.rollbacks


def test_stale_update_rolls_back():
    conn = FakeConnection(None)
    with pytest.raises(Denied):
        move(conn)
    assert conn.rollbacks == 1 and conn.commits == 0


def test_illegal_update_never_emits_mutation():
    conn = FakeConnection(None)
    with pytest.raises(Denied):
        move(conn,target=State.EXECUTING,actor=Actor.AGENT)
    assert all('UPDATE' not in sql for sql,_ in conn.commands) and not conn.commits


def test_nested_transaction_denied():
    conn = FakeConnection(None)
    conn.info.transaction_status = 2
    with pytest.raises(Denied):
        move(conn)
    assert not conn.commands and not conn.commits


def test_receive_new_replay_and_firm_mismatch():
    env = sample_envelope(UUID(int=1))
    with pytest.raises(Denied):
        receive(FakeConnection(), env, trusted_firm_id=UUID(int=99))

    conn_new = FakeConnection(returned=[(UUID(int=11),)])
    run_id, created = receive(conn_new, env, trusted_firm_id=UUID(int=1))
    assert created is True and run_id is not None and conn_new.commits == 1
    assert len(conn_new.commands) == 4  # scope + inbox + run + outbox

    conn_dup = FakeConnection(returned=[None])
    dup_run_id, dup_created = receive(conn_dup, env, trusted_firm_id=UUID(int=1))
    assert dup_run_id is None and dup_created is False and conn_dup.commits == 1
    assert len(conn_dup.commands) == 2  # scope + inbox conflict only


def test_resolve_run_promotes_or_quarantines():
    firm, matter, run_id = UUID(int=1), UUID(int=2), UUID(int=3)
    conn_ok = FakeConnection(
        returned=(run_id,),
        rows=[(firm, matter, 'clio-001', 'OPEN')],
    )
    target, resolved_matter, reason = resolve_run(
        conn_ok, firm_id=firm, run_id=run_id, trusted_signals=['clio-001'],
    )
    assert (target, resolved_matter, reason) == (State.VALIDATED, matter, 'MATTER_VERIFIED')
    assert 'clio_matter_id::text=ANY(%s::text[])' in conn_ok.commands[1][0]
    assert conn_ok.commits == 1

    conn_quarantine = FakeConnection(returned=(run_id,), rows=[])
    q_target, q_matter, q_reason = resolve_run(
        conn_quarantine, firm_id=firm, run_id=run_id, trusted_signals=['bad signal!'],
    )
    assert (q_target, q_matter, q_reason) == (State.QUARANTINED, None, 'UNMAPPED_MATTER_SIGNAL')
    assert conn_quarantine.commits == 1


def test_transition_guards_independent_review_and_resolution_path():
    conn = FakeConnection((UUID(int=3),))
    with pytest.raises(Denied):
        transition(
            conn, firm_id=UUID(int=1), matter_id=UUID(int=2), run_id=UUID(int=3),
            expected=State.RUNNING, target=State.REVIEW_REQUIRED,
            actor=Actor.INDEPENDENT_REVIEW, reason='REVIEW', independently_checked=False,
        )
    with pytest.raises(Denied):
        transition(
            conn, firm_id=UUID(int=1), matter_id=None, run_id=UUID(int=3),
            expected=State.RECEIVED, target=State.VALIDATED,
            actor=Actor.RESOLVER, reason='MATTER_VERIFIED',
        )
    assert not conn.commands and not conn.commits
