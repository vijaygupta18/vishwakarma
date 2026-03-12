"""
Database toolset — run read-only SQL queries against PostgreSQL or MySQL.

Config:
  connections:
    - name: rider_db
      type: postgresql
      host: db.example.com
      port: 5432
      database: atlas_app_v2
      username: readonly_user
      password: ...
    - name: driver_db
      type: mysql
      host: mysql.example.com
      database: atlas_driver

IMPORTANT: Only SELECT queries are allowed. Vishwakarma enforces read-only mode.
"""
import logging
import re
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

# Only allow read-only SQL
ALLOWED_SQL_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH")
BLOCKED_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


@register_toolset
class DatabaseToolset(Toolset):
    name = "database"
    description = "Run read-only SQL queries against PostgreSQL or MySQL databases"

    def __init__(self, config: dict):
        self._connections_config = config.get("connections", [])
        self._connections: dict[str, Any] = {}
        self._conn_types: dict[str, str] = {}

    def check_prerequisites(self) -> tuple[bool, str]:
        if not self._connections_config:
            return False, "No database connections configured. Add 'connections' to database toolset config."

        # Try to import drivers
        errors = []
        has_psycopg2 = True
        has_pymysql = True
        try:
            import psycopg2  # noqa
        except ImportError:
            has_psycopg2 = False

        try:
            import pymysql  # noqa
        except ImportError:
            has_pymysql = False

        for conn in self._connections_config:
            db_type = conn.get("type", "postgresql")
            if db_type == "postgresql" and not has_psycopg2:
                errors.append("psycopg2 not installed (pip install psycopg2-binary)")
            if db_type == "mysql" and not has_pymysql:
                errors.append("pymysql not installed (pip install pymysql)")

        if errors:
            return False, "; ".join(errors)
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        conn_names = [c.get("name", f"conn_{i}") for i, c in enumerate(self._connections_config)]
        return [
            ToolDef(
                name="db_query",
                description=(
                    "Run a read-only SQL query against a configured database. "
                    f"Available connections: {', '.join(conn_names) or '(none configured)'}. "
                    "Only SELECT/SHOW/EXPLAIN queries are allowed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {
                            "type": "string",
                            "description": f"Connection name — one of: {', '.join(conn_names)}",
                        },
                        "query": {
                            "type": "string",
                            "description": "SQL SELECT query to run",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows to return (applied as LIMIT if not already in query)",
                            "default": 50,
                        },
                    },
                    "required": ["connection", "query"],
                },
            ),
            ToolDef(
                name="db_list_tables",
                description="List all tables in a database connection.",
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {
                            "type": "string",
                            "description": f"Connection name — one of: {', '.join(conn_names)}",
                        },
                    },
                    "required": ["connection"],
                },
            ),
            ToolDef(
                name="db_describe_table",
                description="Get column definitions for a table.",
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {"type": "string"},
                        "table": {"type": "string"},
                    },
                    "required": ["connection", "table"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "db_query": self._query,
            "db_list_tables": self._list_tables,
            "db_describe_table": self._describe_table,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _get_conn(self, name: str):
        """Get or create a database connection."""
        if name in self._connections:
            return self._connections[name], self._conn_types[name]

        cfg = next((c for c in self._connections_config if c.get("name") == name), None)
        if not cfg:
            raise ValueError(f"No connection named '{name}'. Available: {[c.get('name') for c in self._connections_config]}")

        db_type = cfg.get("type", "postgresql")
        if db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(
                host=cfg.get("host", "localhost"),
                port=cfg.get("port", 5432),
                database=cfg["database"],
                user=cfg.get("username"),
                password=cfg.get("password"),
                connect_timeout=10,
                options="-c default_transaction_read_only=on",
            )
        elif db_type == "mysql":
            import pymysql
            conn = pymysql.connect(
                host=cfg.get("host", "localhost"),
                port=cfg.get("port", 3306),
                database=cfg["database"],
                user=cfg.get("username"),
                password=cfg.get("password", ""),
                connect_timeout=10,
            )
        else:
            raise ValueError(f"Unsupported DB type: {db_type}")

        self._connections[name] = conn
        self._conn_types[name] = db_type
        return conn, db_type

    def _validate_query(self, query: str) -> tuple[bool, str]:
        stripped = query.strip().upper()
        if not any(stripped.startswith(p) for p in ALLOWED_SQL_PREFIXES):
            return False, f"Only read-only queries allowed (SELECT, SHOW, DESCRIBE, EXPLAIN). Got: {stripped[:50]}"
        if BLOCKED_SQL_KEYWORDS.search(query):
            return False, "Query contains disallowed write operation"
        return True, ""

    def _query(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        query = params.get("query", "").strip()
        limit = params.get("limit", 50)
        invocation = f"db_query({conn_name}, {query[:80]})"

        ok, reason = self._validate_query(query)
        if not ok:
            return ToolOutput(status=ToolStatus.ERROR, error=reason, invocation=invocation)

        # Auto-add LIMIT if not present
        if "limit" not in query.lower() and not query.upper().startswith("EXPLAIN"):
            query = f"{query.rstrip(';')} LIMIT {limit}"

        try:
            conn, db_type = self._get_conn(conn_name)
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                cols = [desc[0] for desc in cur.description] if cur.description else []
                lines = ["\t".join(str(c) for c in cols)]
                lines += ["\t".join(str(v) for v in row) for row in rows]
                return ToolOutput(
                    status=ToolStatus.SUCCESS,
                    output="\n".join(lines),
                    invocation=invocation,
                )
        except Exception as e:
            # Reconnect on next call
            if conn_name in self._connections:
                try:
                    self._connections[conn_name].close()
                except Exception:
                    pass
                del self._connections[conn_name]
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _list_tables(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        invocation = f"db_list_tables({conn_name})"
        try:
            conn, db_type = self._get_conn(conn_name)
            if db_type == "postgresql":
                query = "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            else:
                query = "SHOW TABLES"
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                tables = [row[0] for row in rows]
                return ToolOutput(
                    status=ToolStatus.SUCCESS,
                    output="\n".join(tables),
                    invocation=invocation,
                )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _describe_table(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        table = params.get("table", "")
        invocation = f"db_describe_table({conn_name}, {table})"
        try:
            conn, db_type = self._get_conn(conn_name)
            if db_type == "postgresql":
                query = f"""
                    SELECT column_name, data_type, character_maximum_length, is_nullable
                    FROM information_schema.columns
                    WHERE table_name = '{table}' ORDER BY ordinal_position
                """
            else:
                query = f"DESCRIBE {table}"
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                cols = [desc[0] for desc in cur.description] if cur.description else []
                lines = ["\t".join(str(c) for c in cols)]
                lines += ["\t".join(str(v) for v in row) for row in rows]
                return ToolOutput(
                    status=ToolStatus.SUCCESS,
                    output="\n".join(lines),
                    invocation=invocation,
                )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
