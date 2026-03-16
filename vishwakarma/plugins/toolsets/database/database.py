"""
Database toolset — run read-only SQL queries against PostgreSQL, MySQL, or ClickHouse.

Config:
  connections:
    - name: bap
      type: clickhouse
      host: clickhouse.example.com
      port: 8123
      username: readonly
      password: ...
    - name: bap_pg
      type: postgresql
      host: db.example.com
      port: 5432
      database: atlas_app_v2
      username: readonly_user
      password: ...

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
    description = (
        "Run read-only SQL queries against NammaYatri databases (BAP/BPP). "
        "Use db_list_tables to discover tables, db_describe_table for columns, "
        "and db_query to run SELECT queries. Read the 'database' learnings category first "
        "for ID resolution patterns and query templates."
    )

    def __init__(self, config: dict):
        self._connections_config = config.get("connections", [])
        self._connections: dict[str, Any] = {}
        self._conn_types: dict[str, str] = {}

    def check_prerequisites(self) -> tuple[bool, str]:
        if not self._connections_config:
            return False, "No database connections configured."

        errors = []
        for conn in self._connections_config:
            db_type = conn.get("type", "postgresql")
            if db_type == "postgresql":
                try:
                    import psycopg2  # noqa
                except ImportError:
                    errors.append("psycopg2 not installed (pip install psycopg2-binary)")
            elif db_type == "mysql":
                try:
                    import pymysql  # noqa
                except ImportError:
                    errors.append("pymysql not installed (pip install pymysql)")
            elif db_type == "clickhouse":
                pass  # Uses urllib — always available
            else:
                errors.append(f"Unsupported DB type: {db_type}")

        if errors:
            return False, "; ".join(set(errors))
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        conn_names = [c.get("name", f"conn_{i}") for i, c in enumerate(self._connections_config)]
        conn_desc = ", ".join(conn_names) or "(none configured)"
        return [
            ToolDef(
                name="db_query",
                description=(
                    "Run a read-only SQL query against a configured database. "
                    f"Available connections: {conn_desc}. "
                    "Only SELECT queries are allowed. "
                    "For ClickHouse: use database.table format (e.g., atlas_app.ride). "
                    "Always add LIMIT to avoid huge results. "
                    "Read 'database' learnings first for table schemas and ID resolution patterns."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {
                            "type": "string",
                            "description": f"Connection name — one of: {conn_desc}",
                        },
                        "query": {
                            "type": "string",
                            "description": "SQL SELECT query to run",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows to return (default 50)",
                            "default": 50,
                        },
                    },
                    "required": ["connection", "query"],
                },
            ),
            ToolDef(
                name="db_list_tables",
                description=(
                    "List all tables in a database. For ClickHouse, specify the database name "
                    "(atlas_app for BAP, atlas_driver_offer_bpp for BPP)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {
                            "type": "string",
                            "description": f"Connection name — one of: {conn_desc}",
                        },
                        "database": {
                            "type": "string",
                            "description": "Database name (for ClickHouse: atlas_app or atlas_driver_offer_bpp)",
                        },
                    },
                    "required": ["connection"],
                },
            ),
            ToolDef(
                name="db_describe_table",
                description=(
                    "Get column names and types for a table. "
                    "For ClickHouse, use database.table format."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "connection": {"type": "string"},
                        "table": {
                            "type": "string",
                            "description": "Table name (e.g., 'ride' or 'atlas_app.ride')",
                        },
                        "database": {
                            "type": "string",
                            "description": "Database name (for ClickHouse, if not in table name)",
                        },
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

    # ── Connection management ──────────────────────────────────────────────────

    def _get_conn(self, name: str):
        """Get or create a database connection. Returns (conn, db_type)."""
        if name in self._connections:
            return self._connections[name], self._conn_types[name]

        cfg = next((c for c in self._connections_config if c.get("name") == name), None)
        if not cfg:
            avail = [c.get("name") for c in self._connections_config]
            raise ValueError(f"No connection named '{name}'. Available: {avail}")

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
                options="-c default_transaction_read_only=on -c statement_timeout=10000",
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
        elif db_type == "clickhouse":
            # ClickHouse uses HTTP API — store config dict as "connection"
            conn = {
                "host": cfg.get("host", "localhost"),
                "port": cfg.get("port", 8123),
                "username": cfg.get("username", "default"),
                "password": cfg.get("password", ""),
                "max_execution_time": cfg.get("timeout", 30),
            }
        else:
            raise ValueError(f"Unsupported DB type: {db_type}")

        self._connections[name] = conn
        self._conn_types[name] = db_type
        return conn, db_type

    def _close_conn(self, name: str):
        """Close and remove a cached connection."""
        if name in self._connections:
            conn = self._connections.pop(name)
            db_type = self._conn_types.pop(name, "")
            if db_type != "clickhouse":
                try:
                    conn.close()
                except Exception:
                    pass

    # ── ClickHouse HTTP backend ────────────────────────────────────────────────

    def _ch_execute(self, conn: dict, query: str) -> tuple[list[str], list[list]]:
        """
        Execute a query via ClickHouse HTTP API.
        Returns (column_names, rows) where each row is a list of values.
        """
        import json
        import urllib.request
        import urllib.parse
        import urllib.error

        sql = query.strip().rstrip(";")
        sql += " FORMAT JSONEachRow"

        url = (
            f"http://{conn['host']}:{conn['port']}/"
            f"?user={urllib.parse.quote(conn['username'])}"
            f"&password={urllib.parse.quote(conn['password'])}"
            f"&max_execution_time={conn['max_execution_time']}"
        )

        req = urllib.request.Request(url, data=sql.encode("utf-8"), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=conn["max_execution_time"] + 5) as resp:
                raw = resp.read().decode("utf-8").strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"ClickHouse error: {body}")

        if not raw:
            return [], []

        # Parse JSONEachRow — each line is a JSON object
        rows_dicts = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                rows_dicts.append(json.loads(line))

        if not rows_dicts:
            return [], []

        cols = list(rows_dicts[0].keys())
        rows = [[str(row.get(c, "")) for c in cols] for row in rows_dicts]
        return cols, rows

    # ── Query validation ───────────────────────────────────────────────────────

    def _validate_query(self, query: str) -> tuple[bool, str]:
        stripped = query.strip().upper()
        if not any(stripped.startswith(p) for p in ALLOWED_SQL_PREFIXES):
            return False, f"Only read-only queries allowed (SELECT, SHOW, DESCRIBE, EXPLAIN). Got: {stripped[:50]}"
        if BLOCKED_SQL_KEYWORDS.search(query):
            return False, "Query contains disallowed write operation"
        return True, ""

    # ── Tool implementations ───────────────────────────────────────────────────

    def _query(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        query = params.get("query", "").strip()
        limit = params.get("limit", 50)
        invocation = f"db_query({conn_name}, {query[:100]})"

        ok, reason = self._validate_query(query)
        if not ok:
            return ToolOutput(status=ToolStatus.ERROR, error=reason, invocation=invocation)

        # Auto-add LIMIT if not present
        if "limit" not in query.lower() and not query.upper().startswith("EXPLAIN"):
            query = f"{query.rstrip(';')} LIMIT {limit}"

        try:
            conn, db_type = self._get_conn(conn_name)

            if db_type == "clickhouse":
                cols, rows = self._ch_execute(conn, query)
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                lines = ["\t".join(cols)]
                lines += ["\t".join(row) for row in rows]
            else:
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
            self._close_conn(conn_name)
            return ToolOutput(status=ToolStatus.ERROR, error=str(e)[:500], invocation=invocation)

    def _list_tables(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        database = params.get("database", "")
        invocation = f"db_list_tables({conn_name}, {database})"
        try:
            conn, db_type = self._get_conn(conn_name)

            if db_type == "clickhouse":
                if database:
                    query = f"SELECT name FROM system.tables WHERE database = '{database}' ORDER BY name"
                else:
                    query = "SELECT database, name FROM system.tables WHERE database NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA') ORDER BY database, name"
                cols, rows = self._ch_execute(conn, query)
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                tables = ["\t".join(row) for row in rows]
                return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(tables), invocation=invocation)
            elif db_type == "postgresql":
                query = "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            else:
                query = "SHOW TABLES"

            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                tables = [row[0] for row in rows]
                return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(tables), invocation=invocation)
        except Exception as e:
            self._close_conn(conn_name)
            return ToolOutput(status=ToolStatus.ERROR, error=str(e)[:500], invocation=invocation)

    def _describe_table(self, params: dict) -> ToolOutput:
        conn_name = params.get("connection", "")
        table = params.get("table", "")
        database = params.get("database", "")
        invocation = f"db_describe_table({conn_name}, {table})"
        try:
            conn, db_type = self._get_conn(conn_name)

            if db_type == "clickhouse":
                # Support both "table" and "database.table" format
                if "." in table:
                    db_part, tbl_part = table.split(".", 1)
                else:
                    db_part = database or "default"
                    tbl_part = table
                query = (
                    f"SELECT name, type FROM system.columns "
                    f"WHERE database = '{db_part}' AND table = '{tbl_part}' "
                    f"ORDER BY position"
                )
                cols, rows = self._ch_execute(conn, query)
                if not rows:
                    return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
                lines = ["\t".join(cols)]
                lines += ["\t".join(row) for row in rows]
                return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
            elif db_type == "postgresql":
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
                return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            self._close_conn(conn_name)
            return ToolOutput(status=ToolStatus.ERROR, error=str(e)[:500], invocation=invocation)
