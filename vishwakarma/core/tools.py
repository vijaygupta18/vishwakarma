"""
Tool system — definitions, execution, and toolset base class.

Two types of toolsets:
  1. YAML-based  — simple shell commands templated with params
  2. Python-based — Python class with methods, for complex integrations
"""
import json
import logging
import os
import shlex
import subprocess
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable

import yaml
from pydantic import BaseModel

from vishwakarma.core.models import ToolOutput, ToolStatus

log = logging.getLogger(__name__)


# ── Tool Definition ───────────────────────────────────────────────────────────

class ToolDef(BaseModel):
    """
    A single tool that the LLM can call.
    Maps to OpenAI function calling format.
    """
    name: str
    description: str
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    # For YAML tools: shell command template with {param} placeholders
    command: str | None = None
    # For Python tools: callable that receives (params) -> ToolOutput
    handler: Callable | None = None

    model_config = {"arbitrary_types_allowed": True}

    def to_openai_spec(self) -> dict:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ── Toolset Status ────────────────────────────────────────────────────────────

class ToolsetHealth(str, Enum):
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"
    UNCHECKED = "unchecked"


class ToolsetInfo(BaseModel):
    name: str
    health: ToolsetHealth = ToolsetHealth.UNCHECKED
    error: str = ""
    tool_count: int = 0


# ── Base Toolset ──────────────────────────────────────────────────────────────

class Toolset(ABC):
    """
    Base class for all toolsets.

    Each toolset groups related tools (e.g. all Prometheus tools,
    all Kubernetes tools) and validates its own prerequisites.
    """
    name: str = ""                  # e.g. "prometheus/metrics"
    description: str = ""           # shown to LLM in system prompt
    enabled: bool = True

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._health = ToolsetHealth.UNCHECKED
        self._error = ""

    @abstractmethod
    def get_tools(self) -> list[ToolDef]:
        """Return list of tools this toolset provides."""
        ...

    def check_prerequisites(self) -> tuple[bool, str]:
        """
        Validate that this toolset can actually work
        (e.g. Prometheus URL is reachable).
        Returns (ok, error_message).
        """
        return True, ""

    def run_prerequisites(self) -> ToolsetHealth:
        ok, err = self.check_prerequisites()
        if ok:
            self._health = ToolsetHealth.READY
            self._error = ""
        else:
            self._health = ToolsetHealth.FAILED
            self._error = err
        return self._health

    @property
    def health(self) -> ToolsetHealth:
        return self._health

    @property
    def error(self) -> str:
        return self._error

    def info(self) -> ToolsetInfo:
        tools = self.get_tools()
        return ToolsetInfo(
            name=self.name,
            health=self._health,
            error=self._error,
            tool_count=len(tools),
        )


# ── YAML Toolset ──────────────────────────────────────────────────────────────

class YAMLToolset(Toolset):
    """
    Toolset loaded from a YAML file.

    YAML format:
      name: kubernetes/core
      description: Kubernetes cluster investigation tools
      tools:
        - name: kubectl_get_pods
          description: List pods in a namespace
          parameters:
            type: object
            properties:
              namespace: {type: string, description: K8s namespace}
            required: [namespace]
          command: kubectl get pods -n {namespace} -o wide
    """

    def __init__(self, yaml_path: str, config: dict | None = None):
        super().__init__(config)
        self._yaml_path = yaml_path
        self._spec: dict = {}
        self._load()

    def _load(self):
        with open(self._yaml_path) as f:
            self._spec = yaml.safe_load(f)
        self.name = self._spec.get("name", os.path.basename(self._yaml_path))
        self.description = self._spec.get("description", "")
        self.enabled = self._spec.get("enabled", True)

    def get_tools(self) -> list[ToolDef]:
        tools = []
        for t in self._spec.get("tools", []):
            tools.append(ToolDef(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("parameters", {"type": "object", "properties": {}}),
                command=t.get("command"),
            ))
        return tools


# ── Tool Executor ─────────────────────────────────────────────────────────────

class ToolExecutor:
    """
    Executes tool calls from the LLM.
    Maintains the registry of all active toolsets.
    """

    def __init__(self, toolsets: list[Toolset]):
        self.toolsets = toolsets
        self._index: dict[str, ToolDef] = {}
        self._rebuild_index()

    def _rebuild_index(self):
        self._index = {}
        for ts in self.toolsets:
            if not ts.enabled:
                continue
            for tool in ts.get_tools():
                # Wire Python toolset's execute() as the handler if no handler/command set
                if tool.handler is None and tool.command is None:
                    _ts = ts  # capture for closure
                    _tool_name = tool.name
                    tool = tool.model_copy(update={"handler": lambda params, _t=_ts, _n=_tool_name: _t.execute(_n, params)})
                self._index[tool.name] = tool

    def all_tool_defs(self) -> list[ToolDef]:
        return list(self._index.values())

    def openai_tools(self) -> list[dict]:
        """Return tools in OpenAI function calling format."""
        return [t.to_openai_spec() for t in self.all_tool_defs()]

    def execute(self, tool_name: str, params: dict[str, Any]) -> ToolOutput:
        tool = self._index.get(tool_name)
        if not tool:
            return ToolOutput(
                tool_call_id="",
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error=f"Tool '{tool_name}' not found. Available: {list(self._index.keys())}",
            )

        # Python handler
        if tool.handler:
            return self._run_python(tool, params)

        # YAML shell command
        if tool.command:
            return self._run_shell(tool, params)

        return ToolOutput(
            tool_call_id="",
            tool_name=tool_name,
            status=ToolStatus.ERROR,
            error=f"Tool '{tool_name}' has no handler or command defined.",
        )

    def _run_python(self, tool: ToolDef, params: dict) -> ToolOutput:
        try:
            result = tool.handler(params)  # type: ignore
            if isinstance(result, ToolOutput):
                return result
            return ToolOutput(
                tool_call_id="",
                tool_name=tool.name,
                status=ToolStatus.SUCCESS,
                output=result,
                invocation=f"{tool.name}({json.dumps(params)})",
            )
        except Exception as e:
            log.error(f"Tool {tool.name} failed: {e}", exc_info=True)
            return ToolOutput(
                tool_call_id="",
                tool_name=tool.name,
                status=ToolStatus.ERROR,
                error=str(e),
                invocation=f"{tool.name}({json.dumps(params)})",
            )

    def _run_shell(self, tool: ToolDef, params: dict) -> ToolOutput:
        try:
            # Fill in {param} placeholders
            try:
                cmd = tool.command.format(**params)  # type: ignore
            except KeyError as e:
                return ToolOutput(
                    tool_call_id="",
                    tool_name=tool.name,
                    status=ToolStatus.ERROR,
                    error=f"Missing required parameter {e} for tool '{tool.name}'. Provided: {list(params.keys())}",
                    invocation=str(tool.command),
                )
            invocation = cmd

            log.debug(f"Running shell tool: {cmd}")
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )

            output = result.stdout.strip()
            if result.returncode != 0:
                error_detail = result.stderr.strip() or f"Exit code {result.returncode}"
                return ToolOutput(
                    tool_call_id="",
                    tool_name=tool.name,
                    status=ToolStatus.ERROR,
                    error=f"Command failed: {error_detail}\nCommand: {cmd}",
                    invocation=invocation,
                )

            if not output:
                return ToolOutput(
                    tool_call_id="",
                    tool_name=tool.name,
                    status=ToolStatus.NO_DATA,
                    output="",
                    invocation=invocation,
                )

            return ToolOutput(
                tool_call_id="",
                tool_name=tool.name,
                status=ToolStatus.SUCCESS,
                output=output,
                invocation=invocation,
            )

        except subprocess.TimeoutExpired:
            return ToolOutput(
                tool_call_id="",
                tool_name=tool.name,
                status=ToolStatus.ERROR,
                error=f"Tool timed out after 60s",
            )
        except Exception as e:
            return ToolOutput(
                tool_call_id="",
                tool_name=tool.name,
                status=ToolStatus.ERROR,
                error=str(e),
            )
