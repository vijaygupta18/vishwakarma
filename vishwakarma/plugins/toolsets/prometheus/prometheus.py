"""
Prometheus / VictoriaMetrics toolset.

Queries metrics via the HTTP API (/api/v1/query, /api/v1/query_range, etc.).
Compatible with both Prometheus and VictoriaMetrics.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus, ToolsetHealth
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class PrometheusToolset(Toolset):
    name = "prometheus"
    description = "Query Prometheus/VictoriaMetrics metrics — instant queries, range queries, alerts, targets"

    def __init__(self, config: dict):
        self.url = config.get("url", "http://prometheus:9090")
        self.headers = {}
        if config.get("bearer_token"):
            self.headers["Authorization"] = f"Bearer {config['bearer_token']}"
        if config.get("username") and config.get("password"):
            self._auth = (config["username"], config["password"])
        else:
            self._auth = None
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    def check_prerequisites(self) -> tuple[bool, str]:
        import urllib.parse
        try:
            # Try standard Prometheus health endpoint
            r = self._session.get(
                f"{self.url}/-/healthy",
                timeout=5,
                auth=self._auth,
            )
            if r.ok:
                return True, ""
            # VictoriaMetrics: health endpoint is at server root, not under the select path
            # e.g. http://vmselect:8481/select/0/prometheus → health at http://vmselect:8481/health
            parsed = urllib.parse.urlparse(self.url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            r2 = self._session.get(f"{base_url}/health", timeout=5, auth=self._auth)
            if r2.ok:
                return True, ""
            return False, f"Prometheus health check failed: {r.status_code}"
        except Exception as e:
            return False, f"Cannot reach Prometheus at {self.url}: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="prometheus_query",
                description=(
                    "Run a PromQL instant query at a specific time. "
                    "Use for current metric values."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "PromQL expression e.g. 'up{job=\"kubernetes-pods\"}'",
                        },
                        "time": {
                            "type": "string",
                            "description": "RFC3339 or Unix timestamp. Defaults to now.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDef(
                name="prometheus_query_range",
                description=(
                    "Run a PromQL range query to get metric values over time. "
                    "Use for trend analysis and anomaly detection."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "PromQL expression"},
                        "start": {
                            "type": "string",
                            "description": "Start time — RFC3339 or Unix timestamp or relative like 'now-1h'",
                        },
                        "end": {
                            "type": "string",
                            "description": "End time — RFC3339 or Unix timestamp. Defaults to now.",
                        },
                        "step": {
                            "type": "string",
                            "description": "Resolution step e.g. '1m', '5m', '1h'",
                            "default": "1m",
                        },
                    },
                    "required": ["query", "start"],
                },
            ),
            ToolDef(
                name="prometheus_get_alerts",
                description="Get all currently firing Prometheus/VictoriaMetrics alerts.",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDef(
                name="prometheus_get_targets",
                description="List all Prometheus scrape targets and their health status.",
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "Filter by state: active, dropped, any",
                            "enum": ["active", "dropped", "any"],
                            "default": "active",
                        }
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="prometheus_label_values",
                description="Get all values for a label (e.g. all job names, all namespaces).",
                parameters={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Label name e.g. 'job', 'namespace'"},
                        "match": {
                            "type": "string",
                            "description": "Optional series selector to filter e.g. '{namespace=\"payments\"}'",
                        },
                    },
                    "required": ["label"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "prometheus_query": self._query,
            "prometheus_query_range": self._query_range,
            "prometheus_get_alerts": self._get_alerts,
            "prometheus_get_targets": self._get_targets,
            "prometheus_label_values": self._label_values,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _get(self, path: str, params: dict) -> dict:
        r = self._session.get(
            f"{self.url}{path}",
            params=params,
            timeout=30,
            auth=self._auth,
        )
        r.raise_for_status()
        return r.json()

    def _query(self, params: dict) -> ToolOutput:
        q = params.get("query", "")
        p: dict[str, Any] = {"query": q}
        if params.get("time"):
            p["time"] = params["time"]

        try:
            data = self._get("/api/v1/query", p)
            result = data.get("data", {}).get("result", [])
            invocation = f"prometheus_query({q})"
            if not result:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=_format_instant(result),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=f"prometheus_query({q})")

    def _query_range(self, params: dict) -> ToolOutput:
        q = params.get("query", "")
        start = _resolve_time(params.get("start", "now-1h"))
        end = _resolve_time(params.get("end", "now"))
        step = params.get("step", "1m")

        try:
            data = self._get("/api/v1/query_range", {
                "query": q, "start": start, "end": end, "step": step,
            })
            result = data.get("data", {}).get("result", [])
            invocation = f"prometheus_query_range({q}, {start}→{end}, step={step})"
            if not result:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=_format_range(result),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=f"prometheus_query_range({q})")

    def _get_alerts(self, params: dict) -> ToolOutput:
        try:
            data = self._get("/api/v1/alerts", {})
            alerts = data.get("data", {}).get("alerts", [])
            if not alerts:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation="prometheus_get_alerts()")
            lines = []
            for a in alerts:
                state = a.get("state", "")
                name = a.get("labels", {}).get("alertname", "?")
                severity = a.get("labels", {}).get("severity", "")
                summary = a.get("annotations", {}).get("summary", "")
                lines.append(f"[{state.upper()}] {name} ({severity}) — {summary}")
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation="prometheus_get_alerts()",
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation="prometheus_get_alerts()")

    def _get_targets(self, params: dict) -> ToolOutput:
        state = params.get("state", "active")
        try:
            data = self._get("/api/v1/targets", {"state": state})
            active = data.get("data", {}).get("activeTargets", [])
            if not active:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation="prometheus_get_targets()")
            lines = []
            for t in active:
                health = t.get("health", "?")
                labels = t.get("labels", {})
                job = labels.get("job", "?")
                instance = labels.get("instance", "?")
                err = t.get("lastError", "")
                line = f"[{health.upper()}] {job} @ {instance}"
                if err:
                    line += f" — {err}"
                lines.append(line)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(lines),
                invocation="prometheus_get_targets()",
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation="prometheus_get_targets()")

    def _label_values(self, params: dict) -> ToolOutput:
        label = params.get("label", "")
        match = params.get("match", "")
        try:
            p: dict[str, Any] = {}
            if match:
                p["match[]"] = match
            data = self._get(f"/api/v1/label/{label}/values", p)
            values = data.get("data", [])
            invocation = f"prometheus_label_values({label})"
            if not values:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="\n".join(sorted(values)),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=f"prometheus_label_values({label})")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _resolve_time(t: str) -> str:
    """Convert relative time (now-1h) to Unix timestamp string."""
    if t.startswith("now"):
        now = time.time()
        if "-" in t:
            offset_str = t.split("-", 1)[1]
            offset = _parse_duration(offset_str)
            return str(int(now - offset))
        return str(int(now))
    return t


def _parse_duration(s: str) -> float:
    """Parse '1h', '30m', '7d' → seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


def _format_instant(result: list) -> str:
    lines = []
    for r in result:
        metric = r.get("metric", {})
        value = r.get("value", [None, "?"])
        label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items())
        lines.append(f"{{{label_str}}} = {value[1]}")
    return "\n".join(lines)


def _format_range(result: list) -> str:
    """Summarize range results — show min/max/latest per metric."""
    lines = []
    for r in result:
        metric = r.get("metric", {})
        values = r.get("values", [])
        if not values:
            continue
        label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items())
        nums = [float(v[1]) for v in values if v[1] not in ("NaN", "+Inf", "-Inf")]
        if nums:
            latest = float(values[-1][1])
            summary = f"min={min(nums):.3g} max={max(nums):.3g} latest={latest:.3g} ({len(values)} points)"
        else:
            summary = "(no numeric values)"
        lines.append(f"{{{label_str}}} {summary}")
    return "\n".join(lines)
