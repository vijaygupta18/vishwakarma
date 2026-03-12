"""
Safeguards — prevent the LLM from getting stuck in a loop.

Two mechanisms (matching Holmes):
1. Identical call check: refuse if exact same tool+params was already called this session
2. Count-based fallback: block after MAX_IDENTICAL_CALLS if history-based check misses edge cases
"""
import hashlib
import json
import logging
import os

log = logging.getLogger(__name__)

# Max times the same tool+params can be called before we block (fallback)
MAX_IDENTICAL_CALLS = 2

# Can be disabled via env var for debugging
SAFEGUARDS_ENABLED = os.environ.get("TOOL_CALL_SAFEGUARDS_ENABLED", "true").lower() != "false"


def _call_fingerprint(tool_name: str, params: dict) -> str:
    """Create a stable hash for a tool call."""
    key = json.dumps({"tool": tool_name, "params": params}, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


def _has_previous_exact_same_tool_call(
    tool_name: str, params: dict, tool_outputs: list
) -> bool:
    """
    Check if the exact same tool+params was already called this session.
    Matches Holmes: checks against the executed tool_outputs list.
    """
    for output in tool_outputs:
        if (
            getattr(output, "tool_name", None) == tool_name
            and getattr(output, "params", None) == params
        ):
            return True
    return False


class LoopGuard:
    """
    Tracks tool calls within a single investigation.
    Two-layer protection:
    1. History check: exact match in previous tool_outputs (Holmes approach)
    2. Count-based fallback: MD5 fingerprint counter
    """

    def __init__(self, max_identical: int = MAX_IDENTICAL_CALLS):
        self._counts: dict[str, int] = {}
        self._max = max_identical

    def is_allowed(
        self,
        tool_name: str,
        params: dict,
        tool_outputs: list | None = None,
    ) -> tuple[bool, str]:
        """
        Check if this tool call is allowed.
        Returns (allowed, reason_if_blocked).
        """
        if not SAFEGUARDS_ENABLED:
            return True, ""

        # Layer 1: History-based exact match (matches Holmes behavior)
        if tool_outputs and _has_previous_exact_same_tool_call(tool_name, params, tool_outputs):
            msg = (
                f"Refusing to run '{tool_name}' — it was already called with identical parameters this session. "
                f"Reuse the existing result or modify the parameters to try a different approach."
            )
            log.warning(msg)
            return False, msg

        # Layer 2: Count-based fallback
        fp = _call_fingerprint(tool_name, params)
        count = self._counts.get(fp, 0) + 1
        self._counts[fp] = count

        if count > self._max:
            msg = (
                f"Tool '{tool_name}' called with identical parameters {count} times. "
                f"Skipping to prevent loop. Modify parameters or try a different approach."
            )
            log.warning(msg)
            return False, msg

        return True, ""

    def reset(self):
        """Reset counts after context compaction."""
        self._counts.clear()
