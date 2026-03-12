"""
Internet/network toolset — DNS, ping, traceroute, whois, port checks.
"""
import logging
import socket
import subprocess
import time

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class InternetToolset(Toolset):
    name = "internet"
    description = "Network diagnostics: DNS lookup, ping, traceroute, port checks"

    def __init__(self, config: dict):
        self.timeout = config.get("timeout", 10)

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="dns_lookup",
                description="Resolve a hostname to IP addresses (A, AAAA records).",
                parameters={
                    "type": "object",
                    "properties": {
                        "hostname": {"type": "string"},
                    },
                    "required": ["hostname"],
                },
            ),
            ToolDef(
                name="ping",
                description="Ping a host to check connectivity and latency.",
                parameters={
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "count": {"type": "integer", "default": 4},
                    },
                    "required": ["host"],
                },
            ),
            ToolDef(
                name="check_port",
                description="Check if a TCP port is open on a host.",
                parameters={
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                        "timeout": {"type": "integer", "default": 5},
                    },
                    "required": ["host", "port"],
                },
            ),
            ToolDef(
                name="dig",
                description="Run a DNS query using dig (supports MX, NS, TXT, CNAME lookups).",
                parameters={
                    "type": "object",
                    "properties": {
                        "hostname": {"type": "string"},
                        "record_type": {
                            "type": "string",
                            "description": "A, AAAA, MX, NS, TXT, CNAME, SOA",
                            "default": "A",
                        },
                    },
                    "required": ["hostname"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "dns_lookup": self._dns_lookup,
            "ping": self._ping,
            "check_port": self._check_port,
            "dig": self._dig,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _dns_lookup(self, params: dict) -> ToolOutput:
        hostname = params["hostname"]
        invocation = f"dns_lookup({hostname})"
        try:
            results = socket.getaddrinfo(hostname, None)
            ips = sorted(set(r[4][0] for r in results))
            if not ips:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=f"{hostname} resolves to:\n" + "\n".join(ips),
                invocation=invocation,
            )
        except socket.gaierror as e:
            return ToolOutput(status=ToolStatus.ERROR, error=f"DNS lookup failed: {e}", invocation=invocation)

    def _ping(self, params: dict) -> ToolOutput:
        host = params["host"]
        count = params.get("count", 4)
        invocation = f"ping({host})"
        try:
            result = subprocess.run(
                ["ping", "-c", str(count), "-W", "3", host],
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout or result.stderr
            return ToolOutput(
                status=ToolStatus.SUCCESS if result.returncode == 0 else ToolStatus.ERROR,
                output=output if result.returncode == 0 else None,
                error=output if result.returncode != 0 else None,
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _check_port(self, params: dict) -> ToolOutput:
        host = params["host"]
        port = params["port"]
        timeout = params.get("timeout", 5)
        invocation = f"check_port({host}:{port})"
        try:
            t0 = time.time()
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            latency_ms = int((time.time() - t0) * 1000)
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output=f"[OPEN] {host}:{port} — connected in {latency_ms}ms",
                invocation=invocation,
            )
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            return ToolOutput(
                status=ToolStatus.ERROR,
                error=f"[CLOSED/UNREACHABLE] {host}:{port} — {e}",
                invocation=invocation,
            )

    def _dig(self, params: dict) -> ToolOutput:
        hostname = params["hostname"]
        record_type = params.get("record_type", "A")
        invocation = f"dig({hostname}, {record_type})"
        try:
            result = subprocess.run(
                ["dig", "+short", record_type, hostname],
                capture_output=True, text=True, timeout=15,
            )
            output = result.stdout.strip()
            if not output:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output=output, invocation=invocation)
        except FileNotFoundError:
            # dig not available, fall back to nslookup
            try:
                result = subprocess.run(
                    ["nslookup", hostname],
                    capture_output=True, text=True, timeout=15,
                )
                return ToolOutput(status=ToolStatus.SUCCESS, output=result.stdout, invocation=invocation)
            except Exception as e:
                return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
