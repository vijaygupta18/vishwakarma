"""
MongoDB toolset — queries, aggregations, and diagnostics.

Config:
  uri: mongodb://user:pass@host:27017/dbname
  database: mydb
"""
import json
import logging
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class MongoDBToolset(Toolset):
    name = "mongodb"
    description = "Query MongoDB collections, run aggregations, and get server diagnostics"

    def __init__(self, config: dict):
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        import pymongo
        uri = self._config.get("uri", "mongodb://localhost:27017")
        self._client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
        return self._client

    def _get_db(self):
        client = self._get_client()
        db_name = self._config.get("database")
        if not db_name:
            raise ValueError("'database' not set in mongodb toolset config")
        return client[db_name]

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            import pymongo  # noqa
        except ImportError:
            return False, "pymongo not installed (pip install pymongo)"
        try:
            client = self._get_client()
            client.server_info()
            return True, ""
        except Exception as e:
            return False, f"Cannot connect to MongoDB: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="mongodb_find",
                description="Query a MongoDB collection with a filter.",
                parameters={
                    "type": "object",
                    "properties": {
                        "collection": {"type": "string"},
                        "filter": {
                            "type": "object",
                            "description": "MongoDB filter document e.g. {\"status\": \"active\"}",
                            "default": {},
                        },
                        "projection": {
                            "type": "object",
                            "description": "Fields to include/exclude",
                        },
                        "sort": {
                            "type": "object",
                            "description": "Sort spec e.g. {\"createdAt\": -1}",
                        },
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["collection"],
                },
            ),
            ToolDef(
                name="mongodb_count",
                description="Count documents matching a filter.",
                parameters={
                    "type": "object",
                    "properties": {
                        "collection": {"type": "string"},
                        "filter": {"type": "object", "default": {}},
                    },
                    "required": ["collection"],
                },
            ),
            ToolDef(
                name="mongodb_aggregate",
                description="Run a MongoDB aggregation pipeline.",
                parameters={
                    "type": "object",
                    "properties": {
                        "collection": {"type": "string"},
                        "pipeline": {
                            "type": "array",
                            "description": "Aggregation pipeline stages",
                        },
                    },
                    "required": ["collection", "pipeline"],
                },
            ),
            ToolDef(
                name="mongodb_list_collections",
                description="List all collections in the database.",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDef(
                name="mongodb_server_status",
                description="Get MongoDB server status (connections, memory, opcounters).",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "mongodb_find": self._find,
            "mongodb_count": self._count,
            "mongodb_aggregate": self._aggregate,
            "mongodb_list_collections": self._list_collections,
            "mongodb_server_status": self._server_status,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _find(self, params: dict) -> ToolOutput:
        col = params["collection"]
        invocation = f"mongodb_find({col})"
        try:
            db = self._get_db()
            cursor = db[col].find(
                filter=params.get("filter", {}),
                projection=params.get("projection"),
                sort=list(params["sort"].items()) if params.get("sort") else None,
                limit=params.get("limit", 20),
            )
            docs = list(cursor)
            if not docs:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            output = json.dumps(
                [{k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                  for k, v in doc.items()} for doc in docs],
                indent=2,
            )
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _count(self, params: dict) -> ToolOutput:
        col = params["collection"]
        invocation = f"mongodb_count({col})"
        try:
            db = self._get_db()
            count = db[col].count_documents(params.get("filter", {}))
            return ToolOutput(status=ToolStatus.SUCCESS, output=str(count), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _aggregate(self, params: dict) -> ToolOutput:
        col = params["collection"]
        invocation = f"mongodb_aggregate({col})"
        try:
            db = self._get_db()
            results = list(db[col].aggregate(params["pipeline"]))
            if not results:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            output = json.dumps(
                [{k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                  for k, v in doc.items()} for doc in results],
                indent=2,
            )
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _list_collections(self, params: dict) -> ToolOutput:
        invocation = "mongodb_list_collections()"
        try:
            db = self._get_db()
            names = sorted(db.list_collection_names())
            if not names:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(names), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _server_status(self, params: dict) -> ToolOutput:
        invocation = "mongodb_server_status()"
        try:
            db = self._get_client().admin
            status = db.command("serverStatus")
            summary = {
                "version": status.get("version"),
                "uptime_hours": round(status.get("uptime", 0) / 3600, 1),
                "connections": status.get("connections", {}),
                "mem_resident_mb": status.get("mem", {}).get("resident", 0),
                "opcounters": status.get("opcounters", {}),
            }
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=json.dumps(summary, indent=2),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
