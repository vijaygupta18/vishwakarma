"""
Datadog toolset — metrics, logs, and events via Datadog API.

Config:
  api_key: dd_api_xxx
  app_key: dd_app_xxx
  site: datadoghq.com  # or datadoghq.eu
"""
import json
import logging
import time

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class DatadogToolset(Toolset):
    name = "datadog"
    description = "Query Datadog metrics, logs, monitors, and events"

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._app_key = config.get("app_key", "")
        site = config.get("site", "datadoghq.com")
        self._base = f"https://api.{site}/api/v1"
        self._v2 = f"https://api.{site}/api/v2"
        self._headers = {
            "DD-API-KEY": self._api_key,
            "DD-APPLICATION-KEY": self._app_key,
        }

    def check_prerequisites(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "datadog api_key not configured"
        try:
            r = requests.get(f"{self._base}/validate", headers=self._headers, timeout=5)
            if r.ok:
                return True, ""
            return False, f"Datadog API validation failed: {r.status_code}"
        except Exception as e:
            return False, f"Cannot reach Datadog: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="datadog_query_metrics",
                description="Query Datadog metrics using Datadog query syntax.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Datadog metric query e.g. 'avg:kubernetes.cpu.usage.total{namespace:payments}'",
                        },
                        "from_time": {
                            "type": "integer",
                            "description": "Unix timestamp for start of range",
                        },
                        "to_time": {
                            "type": "integer",
                            "description": "Unix timestamp for end of range. Defaults to now.",
                        },
                    },
                    "required": ["query", "from_time"],
                },
            ),
            ToolDef(
                name="datadog_search_logs",
                description="Search Datadog logs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Log search query e.g. 'service:rider-app status:error'",
                        },
                        "from_time": {"type": "string", "description": "ISO 8601 start time"},
                        "to_time": {"type": "string", "description": "ISO 8601 end time"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["query", "from_time", "to_time"],
                },
            ),
            ToolDef(
                name="datadog_get_monitors",
                description="Get Datadog monitor statuses.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Filter monitors by name/tag",
                        },
                        "status": {
                            "type": "string",
                            "description": "Filter by status: Alert, Warn, No Data, OK",
                        },
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="datadog_get_events",
                description="Get Datadog events (deployments, alerts, custom events).",
                parameters={
                    "type": "object",
                    "properties": {
                        "start": {"type": "integer", "description": "Unix timestamp"},
                        "end": {"type": "integer"},
                        "tags": {"type": "string", "description": "Filter by tags e.g. 'env:prod,team:sre'"},
                        "priority": {"type": "string", "enum": ["normal", "low"]},
                    },
                    "required": ["start"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "datadog_query_metrics": self._query_metrics,
            "datadog_search_logs": self._search_logs,
            "datadog_get_monitors": self._get_monitors,
            "datadog_get_events": self._get_events,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _query_metrics(self, params: dict) -> ToolOutput:
        query = params["query"]
        from_ts = params["from_time"]
        to_ts = params.get("to_time", int(time.time()))
        invocation = f"datadog_query_metrics({query[:60]})"
        try:
            r = requests.get(
                f"{self._base}/query",
                headers=self._headers,
                params={"query": query, "from": from_ts, "to": to_ts},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            series = data.get("series", [])
            if not series:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for s in series:
                metric = s.get("metric", "")
                scope = s.get("scope", "")
                points = s.get("pointlist", [])
                if points:
                    vals = [p[1] for p in points if p[1] is not None]
                    if vals:
                        lines.append(
                            f"{metric}{{{scope}}}: min={min(vals):.3g} max={max(vals):.3g} "
                            f"latest={vals[-1]:.3g} ({len(vals)} points)"
                        )
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _search_logs(self, params: dict) -> ToolOutput:
        invocation = f"datadog_search_logs({params.get('query', '')[:60]})"
        try:
            body = {
                "filter": {
                    "query": params["query"],
                    "from": params["from_time"],
                    "to": params["to_time"],
                },
                "page": {"limit": params.get("limit", 50)},
                "sort": "timestamp",
            }
            r = requests.post(
                f"{self._v2}/logs/events/search",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            logs = r.json().get("data", [])
            if not logs:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for log in logs:
                attrs = log.get("attributes", {})
                ts = attrs.get("timestamp", "")
                msg = attrs.get("message", "")
                svc = attrs.get("service", "")
                lines.append(f"[{ts}] {svc} {msg[:200]}")
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _get_monitors(self, params: dict) -> ToolOutput:
        invocation = "datadog_get_monitors()"
        try:
            p = {}
            if params.get("query"):
                p["name"] = params["query"]
            r = requests.get(
                f"{self._base}/monitor",
                headers=self._headers,
                params=p,
                timeout=15,
            )
            r.raise_for_status()
            monitors = r.json()
            if params.get("status"):
                monitors = [m for m in monitors if m.get("overall_state") == params["status"]]
            if not monitors:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for m in monitors:
                state = m.get("overall_state", "?")
                name = m.get("name", "?")
                mtype = m.get("type", "")
                lines.append(f"[{state.upper()}] {name} ({mtype})")
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _get_events(self, params: dict) -> ToolOutput:
        invocation = "datadog_get_events()"
        try:
            p: dict = {"start": params["start"], "end": params.get("end", int(time.time()))}
            if params.get("tags"):
                p["tags"] = params["tags"]
            r = requests.get(f"{self._base}/events", headers=self._headers, params=p, timeout=15)
            r.raise_for_status()
            events = r.json().get("events", [])
            if not events:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = [f"[{e.get('date_happened', '')}] {e.get('title', '')} — {e.get('text', '')[:100]}" for e in events]
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
