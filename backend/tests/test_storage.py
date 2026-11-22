"""Storage cutover behaviour on a disposable PostgreSQL schema.

Run with::

    GB5305_TEST_DATABASE_URL=postgresql+psycopg://app@127.0.0.1/workflow \\
    PYTHONPATH=backend pytest backend/tests/test_storage.py

The tests skip when no test database is configured, so the default suite is
unaffected. Each test works on its own schema and drops it afterwards.
"""

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url

from src.models.migrations import ensure_schema
from src.models.tables import (audit, case_orders, cases, evidence, receipts,
                               runs, settings)
from src.services import storage, workflow
from src.services.domain import SPEC

URL = os.getenv('GB5305_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='GB5305_TEST_DATABASE_URL 未配置')


@pytest.fixture()
def engine():
    base = make_url(URL)
    name = f'gb5305_test_{uuid.uuid4().hex[:12]}'
    admin = create_engine(base.set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE {name}')
    engine = create_engine(base.set(database=name))
    ensure_schema(engine)
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.exec_driver_sql(f'DROP DATABASE {name} WITH (FORCE)')
    admin.dispose()


def payload():
    return json.loads(json.dumps(SPEC['sample']))


def clear(engine):
    with engine.begin() as conn:
        for table in (evidence, runs, receipts, case_orders, cases, audit):
            conn.execute(delete(table))


def test_intake_rejects_v2_until_dual(engine):
    clear(engine)
    with engine.begin() as conn:
        storage.intake(conn, 'legacy', 'K1', '批次', payload(), 'op')
        with pytest.raises(Exception) as error:
            storage.intake(conn, 'v2', 'K1', '批次', payload(), 'op')
        assert error.value.status_code == 409


def test_dual_side_receipts_link_and_survive_rollback(engine):
    clear(engine)
    with engine.begin() as conn:
        first = storage.intake(conn, 'legacy', 'K1', '批次', payload(), 'op')
        case_id = first['case']['id']
        storage.switch_mode(conn, 'dual', 'op')
    with engine.begin() as conn:
        second = storage.intake(conn, 'v2', 'K1', '批次', payload(), 'op')
        assert second['linked'] and second['case']['id'] == case_id
        archive = storage.read_archive(conn, case_id)
        assert archive['storage_version'] == 'v2'
        assert archive['authoritative_source'] == 'legacy'
        assert len(archive['receipts']) == 2
    with engine.begin() as conn:
        storage.switch_mode(conn, 'legacy', 'op')
        storage.migrate(conn, 'op')
        storage.migrate(conn, 'op')
    with engine.connect() as conn:
        archive = storage.read_archive(conn, case_id)
        assert archive['storage_version'] == 'v1'
        assert archive['payload']['work_orders']
        assert len(archive['receipts']) == 2
        assert conn.execute(select(case_orders.c.id)).all().__len__() == len(
            archive['payload']['work_orders'])


def test_migration_is_idempotent(engine):
    clear(engine)
    with engine.begin() as conn:
        storage.intake(conn, 'legacy', 'K1', '批次', payload(), 'op')
        storage.intake(conn, 'legacy', 'K2', '批次二', payload(), 'op')
    with engine.begin() as conn:
        first = storage.migrate(conn, 'op')
        count = conn.execute(select(case_orders.c.id)).all()
    with engine.begin() as conn:
        second = storage.migrate(conn, 'op')
        again = conn.execute(select(case_orders.c.id)).all()
    assert first['pending_cases'] == 0
    assert second['orders_inserted'] == 0
    assert len(count) == len(again) == 10


def test_cutover_requires_migration(engine):
    clear(engine)
    with engine.begin() as conn:
        storage.intake(conn, 'legacy', 'K1', '批次', payload(), 'op')
        storage.switch_mode(conn, 'dual', 'op')
        with pytest.raises(Exception) as error:
            storage.switch_mode(conn, 'v2', 'op')
        assert error.value.status_code == 409
        storage.migrate(conn, 'op')
    with engine.begin() as conn:
        assert storage.switch_mode(conn, 'v2', 'op')['mode'] == 'v2'


def test_queued_run_keeps_acceptance_snapshot(engine, monkeypatch):
    # The service module binds the application engine at import time; point it
    # at this test's disposable database.
    monkeypatch.setattr(workflow, 'engine', engine)
    clear(engine)
    with engine.begin() as conn:
        accepted = storage.intake(conn, 'legacy', 'K1', '批次', payload(), 'op')
        case_id = accepted['case']['id']
    # enqueue uses its own transaction and must see the committed archive.
    run = workflow.enqueue(case_id, 'op')
    assert run['storage_version'] == 'v1'
    assert 'payload_snapshot' not in run
    with engine.begin() as conn:
        changed = payload()
        changed['audit_at'] = '2026-11-21T11:00:00+08:00'
        conn.execute(cases.update().where(cases.c.id == case_id).values(payload=changed))
    with engine.begin() as conn:
        claimed = workflow.claim()
        assert claimed[2]['audit_at'] == '2026-11-21T09:00:00+08:00'
        workflow.finish(claimed[0], claimed[1], result={'summary': 'x', 'metrics': {},
                        'sections': []}, storage_version=claimed[3])
    with engine.connect() as conn:
        assert conn.execute(select(evidence.c.storage_version)).scalar_one() == 'v1'
