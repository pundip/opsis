"""PostgreSQL connector."""

from __future__ import annotations

from typing import Any, ClassVar

import psycopg
from psycopg.adapt import Buffer, Loader
from psycopg.pq import Format

from opsis.connectors.base import ColumnSpec, ColumnType, StreamSpec
from opsis.connectors.sql_base import SqlConnector, modes_for

# Postgres holds dates Python cannot ('infinity', years before 1 or after 9999).
# Their loaders return the server's text instead of failing the whole read, and
# the conversion layer then stores them as NULL (or as text) and reports them.
GUARDED_TYPES = ("date", "timestamp", "timestamptz")


def _guarded(base: type[Loader]) -> type[Loader]:
    class Guarded(Loader):
        def __init__(self, oid: int, context: Any = None) -> None:
            super().__init__(oid, context)
            self._inner = base(oid, context)

        def load(self, data: Buffer) -> Any:
            try:
                return self._inner.load(data)
            except psycopg.DataError:
                return bytes(data).decode("utf-8", errors="replace")

    return Guarded


def guard_loaders(conn: Any) -> None:
    """Make a connection return out-of-range dates as text, and intervals in the
    server's own words ('1 year 2 mons') rather than as a number of days."""
    adapters = conn.adapters
    for name in GUARDED_TYPES:
        oid = adapters.types[name].oid
        base = adapters.get_loader(oid, Format.TEXT)
        if base is not None:
            adapters.register_loader(oid, _guarded(base))
    text = adapters.get_loader(adapters.types["text"].oid, Format.TEXT)
    if text is not None:
        adapters.register_loader(adapters.types["interval"].oid, text)


TYPE_MAP = {
    "smallint": ColumnType.INTEGER,
    "integer": ColumnType.INTEGER,
    "bigint": ColumnType.INTEGER,
    "numeric": ColumnType.DECIMAL,
    "real": ColumnType.FLOAT,
    "double precision": ColumnType.FLOAT,
    "boolean": ColumnType.BOOLEAN,
    "date": ColumnType.DATE,
    "timestamp without time zone": ColumnType.TIMESTAMP,
    "timestamp with time zone": ColumnType.TIMESTAMP,
    "json": ColumnType.JSON,
    "jsonb": ColumnType.JSON,
}

COLUMNS_SQL = """
SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable, c.numeric_scale,
       c.numeric_precision
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
  AND t.table_type IN ('BASE TABLE', 'VIEW')
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

KEYS_SQL = """
SELECT kcu.table_schema, kcu.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
ORDER BY kcu.table_schema, kcu.table_name, kcu.ordinal_position
"""

ESTIMATES_SQL = """
SELECT n.nspname, c.relname, c.reltuples::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND c.reltuples > 0
"""


class PostgresConnector(SqlConnector):
    id: ClassVar[str] = "postgres"
    name: ClassVar[str] = "PostgreSQL"
    version: ClassVar[str] = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["host", "database", "user"],
            "properties": {
                "host": {"type": "string", "title": "Host", "minLength": 1},
                "port": {
                    "type": "integer",
                    "title": "Port",
                    "default": 5432,
                    "minimum": 1,
                    "maximum": 65535,
                },
                "database": {"type": "string", "title": "Database", "minLength": 1},
                "user": {"type": "string", "title": "User", "minLength": 1},
                "password": {"type": "string", "title": "Password", "x-secret": True},
                "sslmode": {
                    "type": "string",
                    "title": "SSL mode",
                    "enum": ["prefer", "require", "verify-full", "disable"],
                    "default": "prefer",
                },
            },
        }

    def _connect(self) -> Any:
        conn = psycopg.connect(
            host=self.config["host"],
            port=int(self.config.get("port", 5432)),
            dbname=self.config["database"],
            user=self.config["user"],
            password=self.config.get("password") or None,
            sslmode=self.config.get("sslmode", "prefer"),
            connect_timeout=10,
            application_name="opsis",
        )
        conn.read_only = True
        guard_loaders(conn)
        return conn

    def _quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _table_sql(self, stream: str) -> str:
        schema, _, table = stream.partition(".")
        return f"{self._quote(schema)}.{self._quote(table)}"

    def _stream_cursor(self, conn: Any) -> Any:
        return conn.cursor(name="opsis_read")  # server-side: rows are streamed

    def _discover(self, conn: Any) -> list[StreamSpec]:
        with conn.cursor() as cur:
            columns: dict[tuple[str, str], list[ColumnSpec]] = {}
            for row in cur.execute(COLUMNS_SQL):
                schema, table, name, data_type, nullable, scale, precision = row
                ctype = TYPE_MAP.get(data_type, ColumnType.STRING)
                columns.setdefault((schema, table), []).append(
                    ColumnSpec(
                        name=name,
                        type=ctype,
                        nullable=nullable == "YES",
                        source_type=data_type,
                        # More than 38 digits cannot be held exactly: stored as a double.
                        scale=scale
                        if ctype == ColumnType.DECIMAL and (precision or 0) <= 38
                        else None,
                        tz_aware=data_type == "timestamp with time zone",
                    )
                )
            keys: dict[tuple[str, str], list[str]] = {}
            for schema, table, name in cur.execute(KEYS_SQL):
                keys.setdefault((schema, table), []).append(name)
            estimates = {(s, t): n for s, t, n in cur.execute(ESTIMATES_SQL)}
        conn.rollback()

        streams = []
        for (schema, table), cols in columns.items():
            if "." in schema or "." in table:
                continue  # cannot be addressed as schema.table
            primary_key = tuple(keys.get((schema, table), ()))
            candidates, modes = modes_for(tuple(cols), primary_key)
            streams.append(
                StreamSpec(
                    name=f"{schema}.{table}",
                    columns=tuple(cols),
                    primary_key=primary_key,
                    cursor_candidates=candidates,
                    supported_modes=modes,
                    row_count_estimate=estimates.get((schema, table)),
                )
            )
        return streams


CONNECTOR = PostgresConnector
