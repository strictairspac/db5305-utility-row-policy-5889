"""Idempotent schema bootstrap for the demo database.

``MetaData.create_all`` creates missing tables but never alters existing ones.
The storage cutover adds columns to long-lived tables, so this module adds the
few missing columns in place. It is safe to run on every start and does not
touch existing data.
"""

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from src.models.tables import schema, settings

COLUMNS = {
    'business_cases': {
        'storage_version': "VARCHAR(4) NOT NULL DEFAULT 'v1'",
        'updated_at': 'VARCHAR(32)',
    },
    'business_runs': {
        'payload_snapshot': 'JSON',
        'storage_version': 'VARCHAR(4)',
    },
    'business_evidence': {
        'storage_version': 'VARCHAR(4)',
    },
}
NEW_TABLES = ('business_case_orders', 'business_intake_receipts', 'business_settings')
MODE_KEY = 'case_storage_mode'


def _ensure_default_mode(engine):
    """Create the storage-mode row so intake can always take a shared lock."""
    with engine.begin() as conn:
        if conn.execute(select(settings.c.key).where(settings.c.key == MODE_KEY)).first():
            return
        try:
            with conn.begin_nested():
                conn.execute(settings.insert().values(key=MODE_KEY, value='legacy',
                                                      updated_at=''))
        except IntegrityError:
            pass


def ensure_schema(engine):
    schema.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in COLUMNS.items():
            if not inspector.has_table(table):
                continue
            existing = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in existing:
                    continue
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
    _ensure_default_mode(engine)
    return {'tables': len(schema.tables), 'added_tables': list(NEW_TABLES)}
