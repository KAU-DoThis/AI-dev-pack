
"""Default local SQLite provider."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from ._sqlalchemy import SQLStorage


class SQLiteStorage(SQLStorage):
    def __init__(
        self,
        path: str | Path = "./data/shs-agent.sqlite3",
    ):
        db_path = Path(path).expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            URL.create("sqlite", database=str(db_path)),
            connect_args={"timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def configure(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        super().__init__(engine)
