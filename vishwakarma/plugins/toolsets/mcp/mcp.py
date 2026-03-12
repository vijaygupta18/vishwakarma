"""
MCP (Model Context Protocol) toolset adapter.

Connects Vishwakarma to external MCP servers. Each MCP server is discovered
via JSON config, connected via stdio or SSE transport, and its tools are
dynamically exposed as Vishwakarma tools.

Config:
  mcp_servers:
    filesystem:
      command: npx
      args: [-y, "@modelcontextprotocol/server-filesystem", "/data"]
    postgres:
      command: npx
      args: [-y, "@modelcontextprotocol/server-postgres", "postgresql://..."]

This toolset is registered per MCP server, not as a single toolset.
The ToolsetManager handles multi-server registration.
"""
import json
import logging
import subprocess
import threading
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


class MCPToolset(Toolset):
    """
    A single MCP server exposed as a Vishwakarma toolset.
    Uses stdio transport: sends JSON-RPC to MCP server process stdin.
    """

    def __init__(self, server_name: str, config: dict):
        self.name = f"mcp_{server_name}"
        self.description = f"MCP server: {server_name}"
        self._server_name = server_name
        self._config = config
        self._proc: subprocess.Popen | None = None
        self._tools: list[ToolDef] = []
        self._request_id = 0
        self._lock = threading.Lock()

    def _start_process(self):
        if self._proc and self._proc.poll() is None:
            return
        cmd = [self._config["command"]] + self._config.get("args", [])
        env = {**(__import__("os").environ), **self._config.get("env", {})}
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # Initialize
        self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vishwakarma", "version": "1.0.0"},
        })

    def _send_request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._request_id += 1
            req = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            self._proc.stdin.write(json.dumps(req) + "\n")  # type: ignore
            self._proc.stdin.flush()  # type: ignore
            # Read response line
            line = self._proc.stdout.readline()  # type: ignore
            if not line:
                raise RuntimeError("MCP server closed connection")
            return json.loads(line)

    def check_prerequisites(self) -> tuple[bool, str]:
        if "command" not in self._config:
            return False, f"MCP server '{self._server_name}' has no 'command' in config"
        try:
            self._start_process()
            return True, ""
        except Exception as e:
            return False, f"Failed to start MCP server '{self._server_name}': {e}"

    def get_tools(self) -> list[ToolDef]:
        if self._tools:
            return self._tools
        try:
            self._start_process()
            resp = self._send_request("tools/list", {})
            mcp_tools = resp.get("result", {}).get("tools", [])
            self._tools = [
                ToolDef(
                    name=f"mcp_{self._server_name}_{t['name']}",
                    description=t.get("description", t["name"]),
                    parameters=t.get("inputSchema", {"type": "object", "properties": {}}),
                )
                for t in mcp_tools
            ]
        except Exception as e:
            log.warning(f"Failed to list tools from MCP server '{self._server_name}': {e}")
            self._tools = []
        return self._tools

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        # Strip the "mcp_{server_name}_" prefix to get the actual MCP tool name
        prefix = f"mcp_{self._server_name}_"
        actual_tool = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name
        invocation = f"mcp:{self._server_name}/{actual_tool}"
        try:
            self._start_process()
            resp = self._send_request("tools/call", {
                "name": actual_tool,
                "arguments": params,
            })
            if "error" in resp:
                return ToolOutput(
                    status=ToolStatus.ERROR,
                    error=resp["error"].get("message", str(resp["error"])),
                    invocation=invocation,
                )
            result = resp.get("result", {})
            content = result.get("content", [])
            parts = []
            for c in content:
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
            output = "\n".join(parts) if parts else str(result)
            if not output.strip():
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def __del__(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


def load_mcp_toolsets(mcp_servers_config: dict) -> list[MCPToolset]:
    """Create MCPToolset instances from the mcp_servers config block."""
    toolsets = []
    for name, cfg in mcp_servers_config.items():
        ts = MCPToolset(server_name=name, config=cfg)
        toolsets.append(ts)
    return toolsets
