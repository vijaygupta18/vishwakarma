"""
Coralogix toolset — log search via Coralogix Logs API.

Config:
  api_key: your-coralogix-api-key
  region: EU1  # EU1, EU2, US1, US2, AP1, AP2, IN1
"""
import json
import logging
import time

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

REGION_ENDPOINTS = {
    "EU1": "https://ng-api-http.coralogix.com",
    "EU2": "https://ng-api-http.eu2.coralogix.com",
    "US1": "https://ng-api-http.coralogix.us",
    "US2": "https://ng-api-http.cx498.coralogix.com",
    "AP1": "https://ng-api-http.app.coralogix.in",
    "AP2": "https://ng-api-http.coralogixsg.com",
    "IN1": "https://ng-api-http.coralogix.in",
}


@register_toolset
class CoralogixToolset(Toolset):
    name = "coralogix"
    description = "Search Coralogix logs using DataPrime or Lucene syntax"

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        region = config.get("region", "EU1")
        self._base = REGION_ENDPOINTS.get(region, REGION_ENDPOINTS["EU1"])

    def check_prerequisites(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "coralogix api_key not configured"
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="coralogix_search_logs",
                description="Search Coralogix logs with a text query.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (Lucene syntax) e.g. 'level:error AND application:rider-app'",
                        },
                        "from_time": {
                            "type": "string",
                            "description": "Start time ISO 8601 e.g. '2024-01-01T10:00:00Z'",
                        },
                        "to_time": {
                            "type": "string",
                            "description": "End time ISO 8601",
                        },
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["query", "from_time", "to_time"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name != "coralogix_search_logs":
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return self._search_logs(params)

    def _search_logs(self, params: dict) -> ToolOutput:
        query = params["query"]
        invocation = f"coralogix_search_logs({query[:60]})"
        try:
            body = {
                "query": {
                    "lucene": query,
                },
                "metadata": {
                    "tier": ["FREQUENT_SEARCH"],
                    "syntax": "QUERY_SYNTAX_LUCENE",
                    "startDate": params["from_time"],
                    "endDate": params["to_time"],
                    "defaultSource": "logs",
                },
            }
            r = requests.post(
                f"{self._base}/api/v1/dataprime/query",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            # Response is newline-delimited JSON
            results = []
            for line in r.text.strip().split("\n"):
                if line:
                    try:
                        obj = json.loads(line)
                        if "result" in obj:
                            results.extend(obj["result"].get("results", []))
                    except Exception:
                        pass

            if not results:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

            limit = params.get("limit", 50)
            lines = []
            for entry in results[:limit]:
                user_data = entry.get("userData", {})
                ts = user_data.get("timestamp", "")
                msg = user_data.get("text", str(user_data)[:200])
                lines.append(f"[{ts}] {msg}")

            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
