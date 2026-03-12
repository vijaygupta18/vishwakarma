"""
Safeguards — prevent the LLM from getting stuck in a loop
by calling the same tool with the same params repeatedly.
"""
import hashlib
import json
import logging

log = logging.getLogger(__name__)

# Max times the same tool+params can be called before we block it
MAX_IDENTICAL_CALLS = 2


def _call_fingerprint(tool_name: str, params: dict) -> str:
    """Create a stable hash for a tool call."""
    key = json.dumps({"tool": tool_name, "params": params}, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


class LoopGuard:
    """
    Tracks tool calls within a single investigation.
    Blocks identical calls beyond the threshold.
    """

    def __init__(self, max_identical: int = MAX_IDENTICAL_CALLS):
        self._counts: dict[str, int] = {}
        self._max = max_identical

    def is_allowed(self, tool_name: str, params: dict) -> tuple[bool, str]:
        """
        Check if this tool call is allowed.
        Returns (allowed, reason_if_blocked).
        """
        fp = _call_fingerprint(tool_name, params)
        count = self._counts.get(fp, 0) + 1
        self._counts[fp] = count

        if count > self._max:
            msg = (
                f"Tool '{tool_name}' called with identical parameters {count} times. "
                f"Skipping to prevent loop. Try a different approach or different parameters."
            )
            log.warning(msg)
            return False, msg

        return True, ""

    def reset(self):
        """Reset after context compaction."""
        self._counts.clear()
