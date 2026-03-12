"""
Bash toolset — run shell commands with allow/deny enforcement.

Rules are loaded from config and also from the global BashRules in VishwakarmaConfig.
The engine additionally enforces session-level approval for individual commands.

Config:
  safe_mode: false        # if true, only safe commands allowed
  allow: [kubectl, aws]   # extra allowed prefixes (in addition to safe list)
  block: [rm, wget]       # prefixes to always block

The engine layer handles require_approval / bash_always_allow / bash_always_deny.
This toolset handles the configured allow/block lists.
"""
import logging
import shlex
import subprocess
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120  # seconds


@register_toolset
class BashToolset(Toolset):
    name = "bash"
    description = (
        "Run shell/bash commands. This is the PRIMARY tool for all infrastructure queries. "
        "Use kubectl for Kubernetes (pods, events, logs, deployments). "
        "Use aws CLI for AWS resources (RDS, CloudWatch, ElastiCache, ALB). "
        "Use stern for multi-pod log streaming. "
        "Supports: kubectl, aws, stern, jq, grep, awk, sort, timeout, head, tail."
    )

    def __init__(self, config: dict):
        self.safe_mode: bool = config.get("safe_mode", False)
        self.allow: list[str] = config.get("allow", [])
        self.block: list[str] = config.get("block", [])
        self.timeout: int = config.get("timeout", DEFAULT_TIMEOUT)

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""  # bash is always available

    def get_tools(self) -> list[ToolDef]:
        desc = "Run a bash command."
        if self.safe_mode:
            desc += " [SAFE MODE: only pre-approved commands are allowed]"
        if self.block:
            desc += f" Blocked: {', '.join(self.block[:5])}."

        return [
            ToolDef(
                name="bash",
                description=desc,
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": (
                                "The bash command to run. "
                                "Prefer single, readable commands. "
                                "Avoid pipelines longer than 3 stages."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name != "bash":
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")

        command = params.get("command", "").strip()
        if not command:
            return ToolOutput(status=ToolStatus.ERROR, error="Empty command")

        # Check rules
        allowed, reason = self._is_allowed(command)
        if not allowed:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=reason,
                invocation=f"bash({command[:100]})",
            )

        log.debug(f"bash: {command}")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = result.stdout
            if result.returncode != 0:
                stderr = result.stderr.strip()
                error_msg = f"Exit code {result.returncode}"
                if stderr:
                    error_msg += f"\n{stderr}"
                if result.stdout:
                    error_msg += f"\nstdout:\n{result.stdout}"
                return ToolOutput(
                    status=ToolStatus.ERROR,
                    error=error_msg,
                    invocation=f"bash({command})",
                )
            if not output.strip():
                return ToolOutput(
                    status=ToolStatus.NO_DATA,
                    invocation=f"bash({command})",
                )
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=output,
                invocation=f"bash({command})",
            )
        except subprocess.TimeoutExpired:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=f"Command timed out after {self.timeout}s",
                invocation=f"bash({command})",
            )
        except Exception as e:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=str(e),
                invocation=f"bash({command})",
            )

    def _is_allowed(self, command: str) -> tuple[bool, str]:
        """
        Apply local bash rules (safe_mode, allow, block).
        Note: engine-level bash_always_allow/deny takes priority — this is a secondary check.
        """
        from vishwakarma.config import HARDCODED_BLOCK, SAFE_BASH_COMMANDS
        cmd = command.strip()

        # Hardcoded dangerous patterns
        for pattern in HARDCODED_BLOCK:
            if pattern in cmd:
                return False, f"Blocked by hardcoded safety rule: {pattern}"

        # Config block list
        for blocked in self.block:
            # Match start of command or after pipe
            parts = [p.strip() for p in cmd.replace("|", "\n").replace(";", "\n").split("\n")]
            for part in parts:
                if part.startswith(blocked):
                    return False, f"Command blocked by config rule: {blocked}"

        # Config allow list (takes priority over safe_mode)
        for allowed in self.allow:
            if cmd.startswith(allowed):
                return True, ""

        # Safe mode check
        if self.safe_mode:
            first_cmd = cmd.split()[0] if cmd.split() else ""
            if any(first_cmd == safe or first_cmd.endswith(f"/{safe}") for safe in SAFE_BASH_COMMANDS):
                return True, ""
            return False, (
                f"safe_mode is on — '{first_cmd}' not in allowed command list. "
                f"Add '{first_cmd}' to bash.config.allow in config.yaml to permit it."
            )

        return True, ""
