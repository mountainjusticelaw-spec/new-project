"""DB adapter unit tests, not a substitute for actual PostgreSQL RLS tests."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID
import pytest
from services.correspondence.core import Actor, State, Denied
from services.correspondence.postgres import transition


class FakeConnection:
    def __init__(self, returned):
        self.returned = returned
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
        return self.returned


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
