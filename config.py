"""
Centralized configuration. Currently just re-exports the DB path resolution
from persistence.database, kept as a separate module so future config
(e.g. a settings file for reporting currency, fixed FX fallback behavior
toggles) has an obvious home that isn't buried inside the persistence layer.
"""
from persistence.database import get_db_path

REPORTING_CURRENCY = "GBP"

DB_PATH = get_db_path()
