"""Conftest for the storage cutover tests.

The application imports its engine at module load time and requires
``AUTH_KEY``. When ``GB5305_TEST_DATABASE_URL`` is present, derive the demo
settings from that URL so importing the application modules never connects to
a production database.
"""

import os

from sqlalchemy.engine import make_url

_url = os.getenv('GB5305_TEST_DATABASE_URL')
if _url:
    _parsed = make_url(_url)
    os.environ.setdefault('DB_DRIVER', _parsed.drivername)
    os.environ.setdefault('DB_USER', _parsed.username or 'app')
    os.environ.setdefault('DB_PASSWORD', _parsed.password or '')
    os.environ.setdefault('DB_HOST', _parsed.host or '127.0.0.1')
    if _parsed.port:
        os.environ.setdefault('DB_PORT', str(_parsed.port))
    os.environ.setdefault('DB_NAME', _parsed.database or 'postgres')
os.environ.setdefault('AUTH_KEY', 'test-key')
