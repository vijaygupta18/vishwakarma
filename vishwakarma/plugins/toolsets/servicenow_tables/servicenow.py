"""
ServiceNow toolset — query incidents, change requests, and CMDB tables.

Config:
  instance: your-instance.service-now.com
  username: api_user
  password: api_pass
  # OR
  client_id: oauth_client_id
  client_secret: oauth_secret
"""
import json
import logging

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class ServiceNowToolset(Toolset):
    name = "servicenow"
    description = "Query ServiceNow incidents, change requests, and CMDB tables"

    def __init__(self, config: dict):
        instance = config.get("instance", "")
        self._base = f"https://{instance}/api/now"
        self._session = requests.Session()
        if config.get("username"):
            self._session.auth = (config["username"], config.get("password", ""))
        self._session.headers["Accept"] = "application/json"

    def check_prerequisites(self) -> tuple[bool, str]:
        if "///" in self._base:  # instance not configured
            return False, "servicenow instance not configured"
        try:
            r = self._session.get(f"{self._base}/table/incident?sysparm_limit=1", timeout=5)
            if r.ok:
                return True, ""
            return False, f"ServiceNow returned {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"Cannot reach ServiceNow: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="servicenow_get_incidents",
                description="Get ServiceNow incidents matching a query.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "ServiceNow encoded query e.g. 'active=true^severity=1'",
                        },
                        "limit": {"type": "integer", "default": 20},
                        "fields": {
                            "type": "string",
                            "description": "Comma-separated fields to return",
                            "default": "number,short_description,state,severity,assigned_to,opened_at",
                        },
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="servicenow_get_incident",
                description="Get details of a specific ServiceNow incident by number.",
                parameters={
                    "type": "object",
                    "properties": {
                        "number": {"type": "string", "description": "Incident number e.g. INC0012345"},
                    },
                    "required": ["number"],
                },
            ),
            ToolDef(
                name="servicenow_get_changes",
                description="Get recent ServiceNow change requests.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Encoded query"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="servicenow_query_table",
                description="Query any ServiceNow table by name.",
                parameters={
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Table name e.g. cmdb_ci_server"},
                        "query": {"type": "string", "description": "Encoded query filter"},
                        "fields": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["table"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "servicenow_get_incidents": lambda p: self._query_table("incident", p),
            "servicenow_get_incident": self._get_incident,
            "servicenow_get_changes": lambda p: self._query_table("change_request", p),
            "servicenow_query_table": lambda p: self._query_table(p.get("table", ""), p),
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _query_table(self, table: str, params: dict) -> ToolOutput:
        invocation = f"servicenow_{table}({params.get('query', '')[:60]})"
        p = {
            "sysparm_limit": params.get("limit", 20),
            "sysparm_display_value": "true",
        }
        if params.get("query"):
            p["sysparm_query"] = params["query"]
        if params.get("fields"):
            p["sysparm_fields"] = params["fields"]
        try:
            r = self._session.get(f"{self._base}/table/{table}", params=p, timeout=15)
            r.raise_for_status()
            records = r.json().get("result", [])
            if not records:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for rec in records:
                lines.append(json.dumps({
                    k: v.get("display_value", v) if isinstance(v, dict) else v
                    for k, v in rec.items()
                }))
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _get_incident(self, params: dict) -> ToolOutput:
        number = params["number"]
        return self._query_table("incident", {"query": f"number={number}", "limit": 1})
