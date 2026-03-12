"""
Elasticsearch toolset — search logs and documents.

Supports:
  - Elasticsearch 7.x / 8.x
  - OpenSearch (compatible API)
"""
import json
import logging
from typing import Any

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class ElasticsearchToolset(Toolset):
    name = "elasticsearch"
    description = "Search Elasticsearch/OpenSearch logs and documents using Query DSL"

    def __init__(self, config: dict):
        self.url = config.get("url", "http://elasticsearch:9200")
        self._session = requests.Session()
        if config.get("username") and config.get("password"):
            self._session.auth = (config["username"], config["password"])
        if config.get("api_key"):
            self._session.headers["Authorization"] = f"ApiKey {config['api_key']}"

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            r = self._session.get(f"{self.url}/_cluster/health", timeout=5)
            if r.ok:
                status = r.json().get("status", "")
                return True, f"cluster health: {status}"
            return False, f"Elasticsearch returned {r.status_code}"
        except Exception as e:
            return False, f"Cannot reach Elasticsearch at {self.url}: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="elasticsearch_search",
                description=(
                    "Search Elasticsearch logs using Query DSL. "
                    "Use for full-text search, filtered queries, and aggregations."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "string",
                            "description": "Index pattern e.g. 'logstash-*', 'app-logs-2024.*'",
                        },
                        "query": {
                            "type": "object",
                            "description": "Elasticsearch Query DSL object",
                        },
                        "size": {
                            "type": "integer",
                            "description": "Max results to return",
                            "default": 20,
                        },
                        "sort": {
                            "type": "array",
                            "description": "Sort order e.g. [{\"@timestamp\": {\"order\": \"desc\"}}]",
                        },
                        "_source": {
                            "type": "array",
                            "description": "Fields to return. Omit for all fields.",
                        },
                    },
                    "required": ["index", "query"],
                },
            ),
            ToolDef(
                name="elasticsearch_count",
                description="Count documents matching a query (cheaper than search for volume checks).",
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {"type": "string"},
                        "query": {"type": "object"},
                    },
                    "required": ["index", "query"],
                },
            ),
            ToolDef(
                name="elasticsearch_list_indices",
                description="List all indices matching a pattern with doc counts and sizes.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Index pattern e.g. 'app-*'. Defaults to all.",
                            "default": "*",
                        }
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="elasticsearch_get_mappings",
                description="Get index mappings (field names and types).",
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {"type": "string"},
                    },
                    "required": ["index"],
                },
            ),
            ToolDef(
                name="elasticsearch_aggregate",
                description=(
                    "Run aggregation queries — date histograms, term counts, stats. "
                    "Great for error rate over time, top error messages, etc."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {"type": "string"},
                        "query": {
                            "type": "object",
                            "description": "Filter query (can be empty {})",
                            "default": {"match_all": {}},
                        },
                        "aggs": {
                            "type": "object",
                            "description": "Aggregations definition",
                        },
                        "size": {"type": "integer", "default": 0},
                    },
                    "required": ["index", "aggs"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "elasticsearch_search": self._search,
            "elasticsearch_count": self._count,
            "elasticsearch_list_indices": self._list_indices,
            "elasticsearch_get_mappings": self._get_mappings,
            "elasticsearch_aggregate": self._aggregate,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _search(self, params: dict) -> ToolOutput:
        index = params["index"]
        body: dict[str, Any] = {
            "query": params["query"],
            "size": params.get("size", 20),
        }
        if params.get("sort"):
            body["sort"] = params["sort"]
        if params.get("_source"):
            body["_source"] = params["_source"]

        invocation = f"elasticsearch_search({index}, {json.dumps(params['query'])[:100]})"
        try:
            r = self._session.post(
                f"{self.url}/{index}/_search",
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            total = data.get("hits", {}).get("total", {})
            if isinstance(total, dict):
                total_count = total.get("value", 0)
            else:
                total_count = total

            if not hits:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

            lines = [f"Total matches: {total_count}"]
            for hit in hits:
                src = hit.get("_source", {})
                ts = src.get("@timestamp", src.get("timestamp", ""))
                msg = src.get("message", src.get("msg", ""))
                level = src.get("level", src.get("log.level", ""))
                if ts or msg:
                    line = f"[{ts}] {level} {msg}".strip()
                else:
                    line = json.dumps(src)[:200]
                lines.append(line)

            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _count(self, params: dict) -> ToolOutput:
        index = params["index"]
        body = {"query": params["query"]}
        invocation = f"elasticsearch_count({index})"
        try:
            r = self._session.post(f"{self.url}/{index}/_count", json=body, timeout=15)
            r.raise_for_status()
            count = r.json().get("count", 0)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=str(count),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _list_indices(self, params: dict) -> ToolOutput:
        pattern = params.get("pattern", "*")
        invocation = f"elasticsearch_list_indices({pattern})"
        try:
            r = self._session.get(
                f"{self.url}/_cat/indices/{pattern}",
                params={"h": "index,docs.count,store.size,status", "s": "index"},
                timeout=15,
            )
            r.raise_for_status()
            if not r.text.strip():
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output=r.text.strip(), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _get_mappings(self, params: dict) -> ToolOutput:
        index = params["index"]
        invocation = f"elasticsearch_get_mappings({index})"
        try:
            r = self._session.get(f"{self.url}/{index}/_mapping", timeout=10)
            r.raise_for_status()
            data = r.json()
            # Flatten to just field names
            fields = []
            for idx_name, idx_data in data.items():
                props = idx_data.get("mappings", {}).get("properties", {})
                for field, meta in props.items():
                    ftype = meta.get("type", "object")
                    fields.append(f"{field}: {ftype}")
            if not fields:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(sorted(fields)),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _aggregate(self, params: dict) -> ToolOutput:
        index = params["index"]
        body: dict[str, Any] = {
            "query": params.get("query", {"match_all": {}}),
            "aggs": params["aggs"],
            "size": params.get("size", 0),
        }
        invocation = f"elasticsearch_aggregate({index})"
        try:
            r = self._session.post(f"{self.url}/{index}/_search", json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            agg_results = data.get("aggregations", {})
            if not agg_results:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=json.dumps(agg_results, indent=2),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
