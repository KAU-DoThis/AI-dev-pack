
"""MySQL provider using PyMySQL."""

from sqlalchemy import create_engine

from ._sqlalchemy import SQLStorage


class MySQLStorage(SQLStorage):
    def __init__(self, dsn: str):
        if dsn.startswith("mysql://"):
            dsn = dsn.replace(
                "mysql://",
                "mysql+pymysql://",
                1,
            )
        elif not dsn.startswith("mysql+pymysql://"):
            raise ValueError("invalid MySQL URL")

        super().__init__(
            create_engine(dsn, pool_pre_ping=True)
        )
