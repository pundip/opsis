"""MySQL (and MariaDB) connector."""

from __future__ import annotations

import re
import ssl
from typing import Any, ClassVar

import pymysql
import pymysql.cursors

from opsis.connectors.base import ColumnSpec, ColumnType, StreamSpec
from opsis.connectors.sql_base import SqlConnector, modes_for

TYPE_MAP = {
    "tinyint": ColumnType.INTEGER,
    "smallint": ColumnType.INTEGER,
    "mediumint": ColumnType.INTEGER,
    "int": ColumnType.INTEGER,
    "bigint": ColumnType.INTEGER,
    "year": ColumnType.INTEGER,
    "decimal": ColumnType.DECIMAL,
    "float": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "date": ColumnType.DATE,
    "datetime": ColumnType.TIMESTAMP,
    "timestamp": ColumnType.TIMESTAMP,
    "json": ColumnType.JSON,
}

COLUMNS_SQL = """
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.COLUMN_TYPE, c.IS_NULLABLE,
       c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.COLUMN_KEY, t.TABLE_ROWS
FROM information_schema.COLUMNS c
JOIN information_schema.TABLES t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA = %s AND t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def column_spec(
    name: str, data_type: str, column_type: str, nullable: bool, precision: Any, scale: Any
) -> ColumnSpec:
    """How a MySQL column is stored. Values that do not fit (a zero date, say) are
    stored as NULL and reported by the conversion layer."""
    ctype = TYPE_MAP.get(data_type, ColumnType.STRING)
    exact: int | None = int(scale) if ctype == ColumnType.DECIMAL and scale is not None else None
    if column_type.startswith("tinyint(1)") or column_type == "bit(1)":
        ctype = ColumnType.BOOLEAN
    elif data_type == "bit":
        # BIT(n): a number. BIT(64) can exceed a 64-bit signed integer.
        bits = re.search(r"\((\d+)\)", column_type)
        if bits and int(bits.group(1)) > 63:
            ctype, exact = ColumnType.DECIMAL, 0
        else:
            ctype = ColumnType.INTEGER
    elif data_type == "bigint" and "unsigned" in column_type:
        ctype, exact = ColumnType.DECIMAL, 0  # up to 18446744073709551615
    elif ctype == ColumnType.DECIMAL and precision is not None and int(precision) > 38:
        exact = None  # DECIMAL(65, 30) cannot be held exactly: stored as a double
    return ColumnSpec(
        name=name,
        type=ctype,
        nullable=nullable,
        source_type=column_type,
        scale=exact,
    )


class MySqlConnector(SqlConnector):
    id: ClassVar[str] = "mysql"
    name: ClassVar[str] = "MySQL"
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
                    "default": 3306,
                    "minimum": 1,
                    "maximum": 65535,
                },
                "database": {"type": "string", "title": "Database", "minLength": 1},
                "user": {"type": "string", "title": "User", "minLength": 1},
                "password": {"type": "string", "title": "Password", "x-secret": True},
                "ssl": {
                    "type": "string",
                    "title": "Encryption",
                    "description": "required encrypts without checking the certificate; "
                    "verify also checks it against the system certificate store.",
                    "enum": ["disabled", "required", "verify"],
                    "default": "disabled",
                },
            },
        }

    def _connect(self) -> Any:
        mode = self.config.get("ssl", "disabled")
        context: ssl.SSLContext | None = None
        if mode != "disabled":
            context = ssl.create_default_context()
            if mode == "required":
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        conn = pymysql.connect(
            host=self.config["host"],
            port=int(self.config.get("port", 3306)),
            database=self.config["database"],
            user=self.config["user"],
            password=self.config.get("password") or "",
            ssl=context,
            connect_timeout=10,
            read_timeout=600,
            charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        return conn

    def _friendly(self, exc: Exception) -> str:
        # PyMySQL errors are (code, message) tuples.
        if isinstance(exc, pymysql.MySQLError) and len(exc.args) >= 2:
            return str(exc.args[1])
        return super()._friendly(exc)

    def _quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def _table_sql(self, stream: str) -> str:
        return self._quote(stream)

    def _stream_cursor(self, conn: Any) -> Any:
        return conn.cursor(pymysql.cursors.SSCursor)  # unbuffered: rows are streamed

    def _discover(self, conn: Any) -> list[StreamSpec]:
        tables: dict[str, dict[str, Any]] = {}
        with conn.cursor() as cur:
            cur.execute(COLUMNS_SQL, (self.config["database"],))
            for row in cur.fetchall():
                table, name, data_type, column_type, nullable, precision, scale, key, rows = row
                table, name = _text(table), _text(name)
                data_type, column_type = _text(data_type).lower(), _text(column_type).lower()
                entry = tables.setdefault(table, {"columns": [], "pk": [], "rows": rows})
                entry["columns"].append(
                    column_spec(
                        name, data_type, column_type, _text(nullable) == "YES", precision, scale
                    )
                )
                if _text(key) == "PRI":
                    entry["pk"].append(name)

        streams = []
        for table, entry in tables.items():
            columns, primary_key = tuple(entry["columns"]), tuple(entry["pk"])
            candidates, modes = modes_for(columns, primary_key)
            streams.append(
                StreamSpec(
                    name=table,
                    columns=columns,
                    primary_key=primary_key,
                    cursor_candidates=candidates,
                    supported_modes=modes,
                    # information_schema.TABLE_ROWS is too rough to show (often far off
                    # for InnoDB), so no estimate is reported.
                    row_count_estimate=None,
                )
            )
        return streams


CONNECTOR = MySqlConnector
