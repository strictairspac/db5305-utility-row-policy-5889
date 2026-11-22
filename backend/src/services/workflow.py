import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import storage
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        if table is runs:
            # Snapshots can be large; the list view only needs the run itself.
            statement = select(*[column for column in table.c
                                 if column.name != 'payload_snapshot'])
        else:
            statement = select(table)
        return [dict(row) for row in conn.execute(
            statement.order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor, receipt_key=None):
    """Accept an archive from the intake side that is currently allowed.

    The demo form is side-agnostic; it is treated as the legacy side while the
    old side still accepts, and as the new side once only v2 intake is open.
    The resulting receipt is durable and replayable like the dual-side API.
    """
    with engine.begin() as conn:
        mode = storage.current_mode(conn, share=True)
        source = 'legacy' if 'legacy' in storage.ACCEPTING[mode] else 'v2'
        accepted = storage.intake(conn, source, receipt_key or uuid.uuid4().hex,
                                  title, payload, actor)
        return accepted['case']


def enqueue(case_id, actor):
    # The archive is read and snapshotted in the same transaction that creates
    # the run, so a queued verification always sees its acceptance-time payload
    # even if the archive is migrated or updated before the worker claims it.
    with engine.begin() as conn:
        found(conn, cases, case_id)
        payload, version, mode, source = storage.snapshot_for_run(conn, case_id)
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status=Status.QUEUED, owner=actor,
            payload_snapshot=payload, storage_version=version,
            created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        log(conn, actor, 'enqueue_storage', f'{run_id}:{version}:{mode}:{source or "-"}')
        result = found(conn, runs, run_id)
    # The snapshot is kept server-side for the worker; the client only needs
    # the run identity and the storage version it was accepted under.
    result.pop('payload_snapshot', None)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id, runs.c.payload_snapshot,
                                  runs.c.storage_version).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        if run.payload_snapshot is not None:
            payload = run.payload_snapshot
        else:
            # Runs queued before snapshots existed froze no archive; fall back
            # to the always-complete batch projection exactly once.
            payload = storage.batch_payload(conn, run.case_id)
            conn.execute(runs.update().where(runs.c.id == run.id).values(
                payload_snapshot=payload,
                storage_version=run.storage_version or 'v1'))
        return run.id, token, payload, run.storage_version or 'v1'


def finish(run_id, token, result=None, error=None, storage_version=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            if storage_version is None:
                storage_version = conn.execute(select(runs.c.storage_version)
                                               .where(runs.c.id == run_id)).scalar_one()
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest,
                                                  storage_version=storage_version))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
