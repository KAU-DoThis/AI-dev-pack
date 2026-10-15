
"""PostgreSQL provider using psycopg 3."""

from sqlalchemy import create_engine

from ._sqlalchemy import SQLStorage


class PostgreSQLStorage(SQLStorage):
    def __init__(self, dsn: str):
        if dsn.startswith("postgresql://"):
            dsn = dsn.replace(
                "postgresql://",
                "postgresql+psycopg://",
                1,
            )
        elif not dsn.startswith("postgresql+psycopg://"):
            raise ValueError("invalid PostgreSQL URL")

        super().__init__(
            create_engine(dsn, pool_pre_ping=True)
        )
