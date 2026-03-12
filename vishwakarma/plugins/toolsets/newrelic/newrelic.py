"""
New Relic toolset — NRQL queries via NerdGraph API.

Config:
  api_key: NRAK-xxx
  account_id: 12345678
"""
import json
import logging

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

NERDGRAPH_URL = "https://api.newrelic.com/graphql"


@register_toolset
class NewRelicToolset(Toolset):
    name = "newrelic"
    description = "Query New Relic metrics, APM data, and logs via NRQL"

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._account_id = config.get("account_id", "")
        self._headers = {
            "API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    def check_prerequisites(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "newrelic api_key not configured"
        if not self._account_id:
            return False, "newrelic account_id not configured"
        try:
            r = requests.post(
                NERDGRAPH_URL,
                headers=self._headers,
                json={"query": "{ actor { user { name } } }"},
                timeout=5,
            )
            if r.ok:
                return True, ""
            return False, f"New Relic API returned {r.status_code}"
        except Exception as e:
            return False, f"Cannot reach New Relic: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="newrelic_nrql",
                description=(
                    "Run a NRQL query against New Relic. "
                    "Use for APM metrics, error rates, throughput, custom events."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "NRQL query e.g. "
                                "'SELECT average(duration) FROM Transaction WHERE appName = \"rider-app\" SINCE 1 hour ago'"
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDef(
                name="newrelic_get_alerts",
                description="Get open New Relic alert violations.",
                parameters={
                    "type": "object",
                    "properties": {
                        "policy_name": {
                            "type": "string",
                            "description": "Filter by policy name",
                        },
                    },
                    "required": [],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "newrelic_nrql": self._nrql,
            "newrelic_get_alerts": self._get_alerts,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _nrql(self, params: dict) -> ToolOutput:
        query = params["query"]
        invocation = f"newrelic_nrql({query[:80]})"
        gql = {
            "query": f"""
            {{
              actor {{
                account(id: {self._account_id}) {{
                  nrql(query: "{query.replace('"', '\\"')}") {{
                    results
                  }}
                }}
              }}
            }}
            """
        }
        try:
            r = requests.post(NERDGRAPH_URL, headers=self._headers, json=gql, timeout=30)
            r.raise_for_status()
            data = r.json()
            results = (
                data.get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("nrql", {})
                .get("results", [])
            )
            if not results:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=json.dumps(results, indent=2),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _get_alerts(self, params: dict) -> ToolOutput:
        invocation = "newrelic_get_alerts()"
        nrql = "SELECT * FROM NrAiIncident WHERE event = 'open' SINCE 24 hours ago"
        if params.get("policy_name"):
            nrql += f" WHERE policyName = '{params['policy_name']}'"
        return self._nrql({"query": nrql})
