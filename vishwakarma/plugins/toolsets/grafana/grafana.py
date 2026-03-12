"""
Grafana toolset — dashboards, Loki logs, and Tempo traces.

Config:
  url: http://grafana:3000
  api_key: glsa_...       (Service Account token, recommended)
  username/password       (basic auth, alternative)
"""
import json
import logging
import time
from typing import Any

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class GrafanaToolset(Toolset):
    name = "grafana"
    description = "Query Grafana dashboards, Loki logs, and Tempo traces"

    def __init__(self, config: dict):
        self.url = config.get("url", "http://grafana:3000").rstrip("/")
        self._session = requests.Session()
        if config.get("api_key"):
            self._session.headers["Authorization"] = f"Bearer {config['api_key']}"
        elif config.get("username"):
            self._session.auth = (config["username"], config.get("password", ""))

        # Loki datasource UID (auto-discovered if not set)
        self._loki_uid: str | None = config.get("loki_datasource_uid")
        self._tempo_uid: str | None = config.get("tempo_datasource_uid")

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            r = self._session.get(f"{self.url}/api/health", timeout=5)
            if r.ok:
                return True, ""
            return False, f"Grafana health check returned {r.status_code}"
        except Exception as e:
            return False, f"Cannot reach Grafana at {self.url}: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="grafana_list_dashboards",
                description="Search Grafana dashboards by name or tag.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Dashboard name search string"},
                        "tag": {"type": "string", "description": "Filter by tag"},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="grafana_get_dashboard",
                description="Get a Grafana dashboard panels and queries by UID or URL slug.",
                parameters={
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string", "description": "Dashboard UID"},
                    },
                    "required": ["uid"],
                },
            ),
            ToolDef(
                name="loki_query",
                description=(
                    "Query Loki logs with LogQL. "
                    "Use for log-based investigation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "LogQL expression e.g. '{namespace=\"payments\",app=\"rider\"} |= \"ERROR\"'",
                        },
                        "start": {
                            "type": "string",
                            "description": "Start time — nanoseconds unix or relative like 'now-1h'",
                        },
                        "end": {
                            "type": "string",
                            "description": "End time. Defaults to now.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max log lines to return",
                            "default": 100,
                        },
                        "direction": {
                            "type": "string",
                            "description": "backward (newest first) or forward",
                            "enum": ["backward", "forward"],
                            "default": "backward",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDef(
                name="loki_label_values",
                description="List values for a Loki label (e.g. all namespaces, apps).",
                parameters={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Label name e.g. 'namespace'"},
                        "query": {"type": "string", "description": "Optional stream selector to filter"},
                    },
                    "required": ["label"],
                },
            ),
            ToolDef(
                name="tempo_search_traces",
                description="Search Tempo distributed traces by service, duration, or tags.",
                parameters={
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "min_duration": {
                            "type": "string",
                            "description": "Minimum trace duration e.g. '500ms', '2s'",
                        },
                        "tags": {
                            "type": "object",
                            "description": "Key-value tags to filter by",
                        },
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": [],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "grafana_list_dashboards": self._list_dashboards,
            "grafana_get_dashboard": self._get_dashboard,
            "loki_query": self._loki_query,
            "loki_label_values": self._loki_label_values,
            "tempo_search_traces": self._tempo_search_traces,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _list_dashboards(self, params: dict) -> ToolOutput:
        p: dict[str, Any] = {"type": "dash-db"}
        if params.get("query"):
            p["query"] = params["query"]
        if params.get("tag"):
            p["tag"] = params["tag"]
        try:
            r = self._session.get(f"{self.url}/api/search", params=p, timeout=10)
            r.raise_for_status()
            results = r.json()
            if not results:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation="grafana_list_dashboards()")
            lines = [f"{d.get('uid', '?')} — {d.get('title', '?')} ({d.get('url', '')})" for d in results]
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation="grafana_list_dashboards()",
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation="grafana_list_dashboards()")

    def _get_dashboard(self, params: dict) -> ToolOutput:
        uid = params["uid"]
        try:
            r = self._session.get(f"{self.url}/api/dashboards/uid/{uid}", timeout=10)
            r.raise_for_status()
            data = r.json()
            dashboard = data.get("dashboard", {})
            title = dashboard.get("title", "")
            panels = dashboard.get("panels", [])
            lines = [f"Dashboard: {title}"]
            for panel in panels:
                ptype = panel.get("type", "")
                ptitle = panel.get("title", "")
                # Include the query targets
                targets = panel.get("targets", [])
                for t in targets:
                    expr = t.get("expr") or t.get("query") or t.get("rawSql", "")
                    if expr:
                        lines.append(f"  [{ptype}] {ptitle}: {expr[:200]}")
                        break
                else:
                    lines.append(f"  [{ptype}] {ptitle}")
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation=f"grafana_get_dashboard({uid})",
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=f"grafana_get_dashboard({uid})")

    def _loki_query(self, params: dict) -> ToolOutput:
        query = params["query"]
        limit = params.get("limit", 100)
        direction = params.get("direction", "backward")

        now_ns = int(time.time() * 1e9)
        start_raw = params.get("start", "now-1h")
        start_ns = _resolve_ns(start_raw, now_ns)
        end_ns = _resolve_ns(params.get("end", "now"), now_ns)

        invocation = f"loki_query({query[:80]})"
        try:
            r = self._session.get(
                f"{self.url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_ns,
                    "end": end_ns,
                    "limit": limit,
                    "direction": direction,
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            streams = data.get("data", {}).get("result", [])
            if not streams:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

            lines = []
            for stream in streams:
                labels = stream.get("stream", {})
                for ts_ns, line in stream.get("values", []):
                    ts_s = int(ts_ns) // int(1e9)
                    from datetime import datetime, timezone
                    ts_str = datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    lines.append(f"[{ts_str}] {line}")

            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _loki_label_values(self, params: dict) -> ToolOutput:
        label = params["label"]
        query = params.get("query", "")
        invocation = f"loki_label_values({label})"
        try:
            p: dict[str, Any] = {}
            if query:
                p["query"] = query
            r = self._session.get(
                f"{self.url}/loki/api/v1/label/{label}/values",
                params=p,
                timeout=10,
            )
            r.raise_for_status()
            values = r.json().get("data", [])
            if not values:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(sorted(values)),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _tempo_search_traces(self, params: dict) -> ToolOutput:
        p: dict[str, Any] = {"limit": params.get("limit", 20)}
        if params.get("service_name"):
            p["service.name"] = params["service_name"]
        if params.get("min_duration"):
            p["minDuration"] = params["min_duration"]
        if params.get("tags"):
            for k, v in params["tags"].items():
                p[k] = v

        invocation = "tempo_search_traces()"
        try:
            r = self._session.get(f"{self.url}/api/search", params=p, timeout=15)
            r.raise_for_status()
            traces = r.json().get("traces", [])
            if not traces:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for t in traces:
                tid = t.get("traceID", "?")
                root_svc = t.get("rootServiceName", "?")
                root_name = t.get("rootTraceName", "?")
                duration_ms = int(t.get("durationMs", 0))
                start_time = t.get("startTimeUnixNano", "")
                lines.append(f"{tid} | {root_svc}/{root_name} | {duration_ms}ms")
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)


def _resolve_ns(t: str, now_ns: int) -> int:
    if t == "now":
        return now_ns
    if t.startswith("now-"):
        parts = t[4:]
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if parts[-1] in units:
            secs = float(parts[:-1]) * units[parts[-1]]
        else:
            secs = float(parts)
        return int(now_ns - secs * 1e9)
    # Assume already a numeric timestamp
    return int(t)
