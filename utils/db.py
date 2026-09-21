from functools import lru_cache

from agno.db.sqlite import SqliteDb

from utils.settings import DB_FILE


@lru_cache(maxsize=1)
def get_db() -> SqliteDb:
    return SqliteDb(db_file=DB_FILE)
