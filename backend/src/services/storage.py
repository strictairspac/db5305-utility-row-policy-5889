"""Storage cutover for meter-order archives.

Decision summary (mirrored in ``BUSINESS.md``):

* During handover two intake sides write at the same time: ``legacy`` keeps the
  whole archive as one JSON document (v1), ``v2`` stores one row per work order
  (v2). Intake never has to stop; every acceptance is a single transaction.
* The intake **mode** decides which side may accept and whose receipt is
  authoritative:

  ========  ==================  ==============================
  mode      accepted sources    authoritative receipt
  ========  ==================  ==============================
  legacy    legacy only         legacy (falls back to the only receipt)
  dual      legacy + v2         legacy, until the explicit cutover
  v2        v2 only             v2 (falls back to the only receipt)
  ========  ==================  ==============================

* Every acceptance is persisted in ``business_intake_receipts`` together with
  its idempotency key and a full payload snapshot, so a retried request returns
  the original acceptance and an archive acknowledged by both sides stays
  queryable after a failed cutover and a rollback.
* ``business_case_orders`` has a unique ``(case_id, order_id)`` key; migration
  upserts instead of inserting, so retries never duplicate rows.
* A verification run freezes the complete archive at acceptance time
  (``business_runs.payload_snapshot``) and records the storage version it was
  composed from. The queue never re-reads mutable case rows.
"""

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError

from src.constants.workflow import StorageMode, StorageVersion
from src.models.tables import (audit, case_orders, cases, receipts, runs,
                               settings)
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time

SETTINGS_KEY = 'case_storage_mode'
RECEIPT_KEY_MAX = 80
SOURCE_VERSION = {'legacy': StorageVersion.V1, 'v2': StorageVersion.V2}
ACCEPTING = {
    StorageMode.LEGACY: {'legacy'},
    StorageMode.DUAL: {'legacy', 'v2'},
    StorageMode.V2: {'v2'},
}
TRANSITIONS = {
    StorageMode.LEGACY: {StorageMode.DUAL},
    StorageMode.DUAL: {StorageMode.V2, StorageMode.LEGACY},
    StorageMode.V2: {StorageMode.DUAL, StorageMode.LEGACY},
}
# Which side's receipt counts while the mode is active, and therefore which
# side's storage version a new verification run reads.
AUTHORITY = {
    StorageMode.LEGACY: 'legacy',
    StorageMode.DUAL: 'legacy',
    StorageMode.V2: 'v2',
}
MODE_LABELS = {
    StorageMode.LEGACY: '整批存放（仅旧端受理）',
    StorageMode.DUAL: '双端受理（旧端回执为准）',
    StorageMode.V2: '逐单入库（新端回执为准）',
}
SOURCE_LABELS = {'legacy': '旧接单端', 'v2': '新接单端'}


def _audit(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action,
                 reference=str(reference)[:50], created_at=utc_time()))


def _validate_case(title, payload):
    title = (title or '').strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    return title


def _source(value):
    value = (value or 'legacy').strip()
    if value not in SOURCE_VERSION:
        raise HTTPException(422, '接单端须为 legacy 或 v2')
    return value


def _digest(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def _case(conn, case_id, lock=False):
    statement = select(cases).where(cases.c.id == case_id)
    if lock:
        statement = statement.with_for_update()
    row = conn.execute(statement).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该档案')
    return dict(row)


def current_mode(conn, lock=False, share=False):
    statement = select(settings.c.value).where(settings.c.key == SETTINGS_KEY)
    if lock:
        statement = statement.with_for_update()
    elif share:
        # Intake holds a shared lock so a concurrent mode switch cannot commit
        # between reading the mode and writing the archive.
        statement = statement.with_for_update(read=True)
    value = conn.execute(statement).scalar_one_or_none()
    try:
        return StorageMode(value or StorageMode.LEGACY)
    except ValueError:
        # An unknown value must not silently downgrade the authority side.
        raise HTTPException(500, f'存储模式配置无效（{value}）。') from None


def _set_mode_value(conn, mode):
    now = utc_time()
    updated = conn.execute(settings.update().where(settings.c.key == SETTINGS_KEY)
                           .values(value=mode.value, updated_at=now)).rowcount
    if not updated:
        try:
            with conn.begin_nested():
                conn.execute(settings.insert().values(key=SETTINGS_KEY, value=mode.value,
                                                      updated_at=now))
        except IntegrityError:
            # Another transaction created the row first; update is now safe.
            conn.execute(settings.update().where(settings.c.key == SETTINGS_KEY)
                         .values(value=mode.value, updated_at=now))


def _lock_receipt_key(conn, receipt_key):
    """Serialize acceptances that share one idempotency key across both sides."""
    if conn.dialect.name == 'postgresql':
        conn.exec_driver_sql('SELECT pg_advisory_xact_lock(hashtext(%s))', (receipt_key,))


def _orders(conn, case_id):
    return [dict(row) for row in conn.execute(
        select(case_orders).where(case_orders.c.case_id == case_id)
        .order_by(case_orders.c.ordinal)).mappings()]


def _receipts(conn, case_id):
    return [dict(row) for row in conn.execute(
        select(receipts).where(receipts.c.case_id == case_id)
        .order_by(receipts.c.id)).mappings()]


def _authoritative(receipt_rows, mode):
    preferred = AUTHORITY[mode]
    for row in receipt_rows:
        if row['source'] == preferred:
            return row
    return receipt_rows[-1] if receipt_rows else None


def receipt_view(row, authoritative_id):
    """Serialise one receipt, flagging the one this archive resolves to."""
    if row is None:
        return None
    return {
        'id': row['id'], 'case_id': row['case_id'], 'source': row['source'],
        'source_label': SOURCE_LABELS[row['source']], 'receipt_key': row['receipt_key'],
        'payload_digest': row['payload_digest'], 'storage_version': row['storage_version'],
        'accepted_at': row['accepted_at'],
        'authoritative': row['id'] == authoritative_id,
    }


def _sync_orders(conn, case_id, payload, source):
    """Upsert the per-order representation. Safe to retry any number of times."""
    now = utc_time()
    entries = payload.get('work_orders') if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise HTTPException(422, '逐单存储需要 work_orders 数组。')
    existing = {row['order_id']: row for row in _orders(conn, case_id)}
    seen = set()
    inserted = updated = 0
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HTTPException(422, '逐单存储的每张工单须为对象。')
        order_id = entry.get('order_id')
        if not isinstance(order_id, str) or not order_id:
            raise HTTPException(422, '逐单存储需要每个工单的 order_id。')
        if order_id in seen:
            raise HTTPException(422, f'工单编号重复（{order_id}）。')
        seen.add(order_id)
        if order_id in existing:
            conn.execute(case_orders.update().where(
                and_(case_orders.c.case_id == case_id,
                     case_orders.c.order_id == order_id)).values(
                ordinal=ordinal, payload=entry, source=source, updated_at=now))
            updated += 1
        else:
            conn.execute(case_orders.insert().values(
                case_id=case_id, order_id=order_id, ordinal=ordinal,
                payload=entry, source=source, created_at=now, updated_at=now))
            inserted += 1
    stale = set(existing) - seen
    if stale:
        conn.execute(delete(case_orders).where(
            and_(case_orders.c.case_id == case_id, case_orders.c.order_id.in_(stale))))
    return inserted, updated, len(stale)


def _write_representation(conn, case_id, payload, source, mode):
    """Keep the representations required by the mode in sync.

    The batch payload remains a complete projection in every mode so the old
    side can still read archives after a rollback. Per-order rows are written
    in dual and v2 mode; in legacy mode only the batch row is touched, and the
    per-order rows are refreshed on the next migration or dual-side write.
    """
    if mode in (StorageMode.DUAL, StorageMode.V2):
        inserted, updated, removed = _sync_orders(conn, case_id, payload, source)
        conn.execute(cases.update().where(cases.c.id == case_id).values(
            storage_version=StorageVersion.V2.value, updated_at=utc_time()))
        return StorageVersion.V2, {'inserted': inserted, 'updated': updated, 'removed': removed}
    return StorageVersion.V1, {'inserted': 0, 'updated': 0, 'removed': 0}


def _read_version(conn, case_row, mode):
    if mode == StorageMode.LEGACY:
        return StorageVersion.V1
    if mode == StorageMode.V2:
        if case_row['storage_version'] != StorageVersion.V2.value:
            raise HTTPException(409, '该档案尚未完成逐单迁移，暂不能按新端读取。')
        return StorageVersion.V2
    # In dual mode the legacy receipt is authoritative and the batch payload is
    # always complete, so read per order only when the projection is in sync.
    # A legacy-mode write during a rollback leaves the per-order rows stale;
    # falling back keeps both archives readable instead of failing the query.
    if case_row['storage_version'] == StorageVersion.V2.value and _consistent(conn, case_row):
        return StorageVersion.V2
    return StorageVersion.V1


def _compose(case_row, order_rows):
    payload = dict(case_row['payload'])
    payload['work_orders'] = [row['payload'] for row in order_rows]
    return payload


def read_archive(conn, case_id):
    """Read one archive in the representation chosen by the active mode."""
    case_row = _case(conn, case_id)
    mode = current_mode(conn, share=True)
    version = _read_version(conn, case_row, mode)
    order_rows = _orders(conn, case_id)
    if version == StorageVersion.V2 and order_rows:
        payload = _compose(case_row, order_rows)
        if canonical(payload) != canonical(case_row['payload']):
            raise HTTPException(409, '逐单存储与整批投影不一致，请重试迁移后再读。')
    elif version == StorageVersion.V2:
        # An archive with no work orders is legitimately empty per order.
        payload = {**case_row['payload'], 'work_orders': []}
    else:
        payload = case_row['payload']
    receipt_rows = _receipts(conn, case_id)
    receipt = _authoritative(receipt_rows, mode)
    authoritative_id = receipt['id'] if receipt else None
    return {
        'case': case_row,
        'payload': payload,
        'storage_version': version.value,
        'storage_mode': mode.value,
        'order_count': len(order_rows),
        'authoritative_source': receipt['source'] if receipt else None,
        'receipts': [receipt_view(row, authoritative_id) for row in receipt_rows],
    }


def _consistent(conn, case_row):
    """Compare the batch projection with the per-order rows for one case."""
    entries = case_row['payload'].get('work_orders') if isinstance(case_row['payload'], dict) else None
    if not isinstance(entries, list):
        return False
    actual = {row['order_id']: row['payload'] for row in _orders(conn, case_row['id'])}
    expected = {entry.get('order_id'): entry for entry in entries if isinstance(entry, dict)}
    if set(actual) != set(expected):
        return False
    return all(canonical(actual[key]) == canonical(expected[key]) for key in expected)


def migration_status(conn):
    pending_cases = pending_orders = 0
    stored = {'v1': 0, 'v2': 0}
    for row in conn.execute(select(cases.c.id, cases.c.storage_version,
                                   cases.c.payload)).mappings():
        stored[row['storage_version']] = stored.get(row['storage_version'], 0) + 1
        expected = row['payload'].get('work_orders') if isinstance(row['payload'], dict) else None
        expected_ids = {entry.get('order_id') for entry in expected
                        if isinstance(entry, dict)} if isinstance(expected, list) else set()
        actual_ids = set(conn.execute(select(case_orders.c.order_id)
                                      .where(case_orders.c.case_id == row['id'])).scalars())
        if row['storage_version'] != StorageVersion.V2.value or not _consistent(conn, row):
            pending_cases += 1
            pending_orders += len(expected_ids ^ actual_ids) or 1
    return {
        'pending_cases': pending_cases,
        'pending_orders': pending_orders,
        'cases_by_version': stored,
        'total_orders': conn.execute(select(func.count()).select_from(case_orders)).scalar_one(),
    }


def status(conn):
    mode = current_mode(conn)
    report = migration_status(conn)
    return {
        'mode': mode.value,
        'mode_label': MODE_LABELS[mode],
        'authoritative_source': AUTHORITY[mode],
        'accepting_sources': sorted(ACCEPTING[mode]),
        'can_switch_to': sorted(item.value for item in TRANSITIONS[mode]),
        'cutover_ready': report['pending_cases'] == 0,
        **report,
    }


def switch_mode(conn, target, actor):
    try:
        mode = StorageMode((target or '').strip())
    except ValueError:
        raise HTTPException(422, '存储模式须为 legacy、dual 或 v2') from None
    current = current_mode(conn, lock=True)
    if mode == current:
        return status(conn)
    if mode not in TRANSITIONS[current]:
        raise HTTPException(409, f'不能从 {current.value} 直接切换到 {mode.value}；'
                                 '请先进入双端受理并完成迁移。')
    if mode == StorageMode.V2:
        report = migration_status(conn)
        if report['pending_cases']:
            raise HTTPException(409, f'还有 {report["pending_cases"]} 份档案未完成逐单迁移，'
                                     '请先执行迁移并复验。')
    _set_mode_value(conn, mode)
    _audit(conn, actor, 'storage_mode', f'{current.value}->{mode.value}')
    return status(conn)


def migrate(conn, actor):
    """Materialise every batch archive into per-order rows. Idempotent.

    Only the per-order shape is checked here; archives were fully validated
    when they were accepted, and a cutover must not fail because a stored
    archive predates a later validation rule.
    """
    inserted = updated = migrated = 0
    for case_id in conn.execute(select(cases.c.id).order_by(cases.c.id)).scalars().all():
        case_row = _case(conn, case_id, lock=True)
        changes = _sync_orders(conn, case_id, case_row['payload'], 'migration')
        inserted += changes[0]
        updated += changes[1]
        if case_row['storage_version'] != StorageVersion.V2.value:
            conn.execute(cases.update().where(cases.c.id == case_id).values(
                storage_version=StorageVersion.V2.value, updated_at=utc_time()))
            migrated += 1
    if migrated or inserted or updated:
        _audit(conn, actor, 'storage_migrate', f'cases={migrated}')
    report = migration_status(conn)
    return {'migrated_cases': migrated, 'orders_inserted': inserted,
            'orders_updated': updated, **report}


def intake(conn, source, receipt_key, title, payload, actor):
    """Accept one archive from one intake side. Idempotent per receipt key."""
    source = _source(source)
    title = _validate_case(title, payload)
    mode = current_mode(conn, share=True)
    if source not in ACCEPTING[mode]:
        raise HTTPException(409, f'{SOURCE_LABELS[source]}在{mode.value}模式下不接单；'
                                 f'当前可受理：{"、".join(sorted(ACCEPTING[mode]))}。')
    receipt_key = (receipt_key or uuid.uuid4().hex).strip()
    if not receipt_key or len(receipt_key) > RECEIPT_KEY_MAX:
        raise HTTPException(422, f'回执键须为 1 至 {RECEIPT_KEY_MAX} 个字符。')
    digest = _digest(payload)
    _lock_receipt_key(conn, receipt_key)

    own = conn.execute(select(receipts).where(and_(
        receipts.c.source == source, receipts.c.receipt_key == receipt_key))).mappings().first()
    if own:
        if own['payload_digest'] != digest:
            raise HTTPException(409, '回执键已用于另一份档案，请使用新的回执键。')
        case_row = _case(conn, own['case_id'])
        resolved = _authoritative(_receipts(conn, own['case_id']), mode)
        return {'case': case_row, 'receipt': receipt_view(dict(own), resolved['id'] if resolved else None),
                'replayed': True, 'linked': False, 'storage_version': own['storage_version']}

    peers = [dict(row) for row in conn.execute(
        select(receipts).where(receipts.c.receipt_key == receipt_key)).mappings()]
    if any(row['payload_digest'] != digest for row in peers):
        raise HTTPException(409, '同一回执键在两端对应的档案内容不一致，已拒绝受理。')
    linked = bool(peers)
    now = utc_time()
    if linked:
        case_id = peers[0]['case_id']
        case_row = _case(conn, case_id, lock=True)
        if canonical(case_row['payload']) != canonical(payload):
            conn.execute(cases.update().where(cases.c.id == case_id).values(
                payload=payload, updated_at=now))
        if case_row['title'] != title:
            conn.execute(cases.update().where(cases.c.id == case_id).values(title=title))
    else:
        case_id = conn.execute(cases.insert().values(
            slug=uuid.uuid4().hex, title=title, payload=payload,
            storage_version=SOURCE_VERSION[source].value, author=actor,
            created_at=now, updated_at=now)).inserted_primary_key[0]
        case_row = _case(conn, case_id)

    version, _ = _write_representation(conn, case_id, payload, source, mode)
    receipt_id = conn.execute(receipts.insert().values(
        case_id=case_id, source=source, receipt_key=receipt_key,
        payload_digest=digest, storage_version=version.value,
        payload_snapshot=payload, accepted_at=now)).inserted_primary_key[0]
    _audit(conn, actor, 'intake', f'{case_id}:{source}')
    case_row = _case(conn, case_id)
    receipt_rows = _receipts(conn, case_id)
    resolved = _authoritative(receipt_rows, mode)
    receipt_row = next(row for row in receipt_rows if row['id'] == receipt_id)
    return {'case': case_row, 'receipt': receipt_view(receipt_row, resolved['id'] if resolved else None),
            'replayed': False, 'linked': linked, 'storage_version': version.value}


def batch_payload(conn, case_id):
    """The complete batch projection, regardless of the active mode."""
    return _case(conn, case_id)['payload']


def snapshot_for_run(conn, case_id):
    archive = read_archive(conn, case_id)
    return archive['payload'], archive['storage_version'], archive['storage_mode'], archive['authoritative_source']
