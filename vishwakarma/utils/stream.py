"""
SSE (Server-Sent Events) stream formatter for FastAPI streaming responses.
Formats agentic loop events into SSE format consumable by clients.
"""
import json
from collections.abc import Generator
from typing import Any


def sse_event(event_type: str, data: Any) -> str:
    """Format a single SSE event."""
    payload = json.dumps(data) if not isinstance(data, str) else data
    return f"event: {event_type}\ndata: {payload}\n\n"


def sse_done() -> str:
    """Send SSE stream termination signal."""
    return "event: done\ndata: {}\n\n"


def stream_chat_formatter(
    events: Generator,
    follow_up_actions: list[dict] | None = None,
) -> Generator[str, None, None]:
    """
    Wrap agentic loop events into SSE format.

    Event types emitted:
      - tool_call_start  : LLM decided to call a tool
      - tool_call_result : Tool executed, result ready
      - analysis_chunk   : Text chunk from LLM
      - analysis_done    : Full analysis complete
      - follow_up        : Follow-up action suggestions
      - error            : Something went wrong
      - done             : Stream complete
    """
    try:
        for event in events:
            event_type = event.get("type", "unknown")
            yield sse_event(event_type, event)

        if follow_up_actions:
            yield sse_event("follow_up", {"actions": follow_up_actions})

        yield sse_done()

    except Exception as e:
        yield sse_event("error", {"message": str(e)})
        yield sse_done()
