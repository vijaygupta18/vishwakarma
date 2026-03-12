"""
HTTP toolset — check endpoints, fetch URLs, test APIs.

Useful for:
  - Checking if a service is responding
  - Testing API endpoints during investigations
  - Comparing response times / status codes
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
class HttpToolset(Toolset):
    name = "http"
    description = "Check HTTP endpoints, fetch URLs, and test REST APIs"

    def __init__(self, config: dict):
        self.default_timeout: int = config.get("timeout", 30)
        self.default_headers: dict = config.get("headers", {})

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="http_get",
                description="Make an HTTP GET request and return status code, headers, and body.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "headers": {
                            "type": "object",
                            "description": "Additional request headers",
                        },
                        "timeout": {"type": "integer", "default": 30},
                        "follow_redirects": {"type": "boolean", "default": True},
                    },
                    "required": ["url"],
                },
            ),
            ToolDef(
                name="http_post",
                description="Make an HTTP POST request with a JSON body.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "body": {"type": "object", "description": "JSON request body"},
                        "headers": {"type": "object"},
                        "timeout": {"type": "integer", "default": 30},
                    },
                    "required": ["url"],
                },
            ),
            ToolDef(
                name="http_check",
                description=(
                    "Quick health check: make a GET request and report "
                    "status code, latency, and whether it's healthy."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "expected_status": {
                            "type": "integer",
                            "description": "Expected HTTP status code",
                            "default": 200,
                        },
                        "timeout": {"type": "integer", "default": 10},
                    },
                    "required": ["url"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "http_get": self._get,
            "http_post": self._post,
            "http_check": self._check,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _get(self, params: dict) -> ToolOutput:
        url = params["url"]
        headers = {**self.default_headers, **params.get("headers", {})}
        timeout = params.get("timeout", self.default_timeout)
        allow_redirects = params.get("follow_redirects", True)
        invocation = f"http_get({url})"
        try:
            t0 = time.time()
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
            latency_ms = int((time.time() - t0) * 1000)
            body = _truncate(r.text, 3000)
            output = (
                f"Status: {r.status_code}\n"
                f"Latency: {latency_ms}ms\n"
                f"Content-Type: {r.headers.get('Content-Type', '')}\n"
                f"Body:\n{body}"
            )
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _post(self, params: dict) -> ToolOutput:
        url = params["url"]
        body = params.get("body", {})
        headers = {**self.default_headers, "Content-Type": "application/json", **params.get("headers", {})}
        timeout = params.get("timeout", self.default_timeout)
        invocation = f"http_post({url})"
        try:
            t0 = time.time()
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
            latency_ms = int((time.time() - t0) * 1000)
            resp_body = _truncate(r.text, 3000)
            output = f"Status: {r.status_code}\nLatency: {latency_ms}ms\nBody:\n{resp_body}"
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _check(self, params: dict) -> ToolOutput:
        url = params["url"]
        expected = params.get("expected_status", 200)
        timeout = params.get("timeout", 10)
        invocation = f"http_check({url})"
        try:
            t0 = time.time()
            r = requests.get(url, timeout=timeout, allow_redirects=True)
            latency_ms = int((time.time() - t0) * 1000)
            healthy = r.status_code == expected
            status_str = "HEALTHY" if healthy else "UNHEALTHY"
            output = (
                f"[{status_str}] {url}\n"
                f"Status: {r.status_code} (expected {expected})\n"
                f"Latency: {latency_ms}ms"
            )
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except requests.exceptions.Timeout:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=f"[UNHEALTHY] {url} — timed out after {timeout}s",
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=f"[UNHEALTHY] {url} — {e}",
                invocation=invocation,
            )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"
