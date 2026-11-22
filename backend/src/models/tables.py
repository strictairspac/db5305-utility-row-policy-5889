from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('storage_version', String(4), nullable=False, server_default='v1'),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32)))
# One row per work order once an archive is stored per order (v2). The unique
# (case_id, order_id) key makes retried migrations and dual-side intake writes
# idempotent: a second attempt updates the same row instead of inserting again.
case_orders = Table('business_case_orders', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('order_id', String(32), nullable=False),
    Column('ordinal', Integer, nullable=False),
    Column('payload', JSON, nullable=False),
    Column('source', String(10), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32), nullable=False),
    UniqueConstraint('case_id', 'order_id', name='uq_case_orders_case_order'))
# Durable acceptance record from each intake side. A receipt is unique per
# side and client key so a retry returns the original acceptance instead of
# writing the archive twice.
receipts = Table('business_intake_receipts', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('source', String(10), nullable=False),
    Column('receipt_key', String(80), nullable=False),
    Column('payload_digest', String(64), nullable=False),
    Column('storage_version', String(4), nullable=False),
    Column('payload_snapshot', JSON, nullable=False),
    Column('accepted_at', String(32), nullable=False),
    UniqueConstraint('source', 'receipt_key', name='uq_receipts_source_key'))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    # Frozen at acceptance so a queued verification always reads the archive
    # exactly as it looked when the run was accepted.
    Column('payload_snapshot', JSON),
    Column('storage_version', String(4)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('storage_version', String(4)),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
settings = Table('business_settings', schema,
    Column('key', String(40), primary_key=True),
    Column('value', String(200), nullable=False),
    Column('updated_at', String(32), nullable=False))
