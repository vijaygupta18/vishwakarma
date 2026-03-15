"""
Context compaction — keeps the investigation alive when approaching the LLM context limit.

Strategy (same proven approach as state-of-the-art agents):
  1. Count actual tokens (via litellm.token_counter)
  2. When total tokens hit 80% of context window → LLM-based compaction:
       Ask the LLM to summarize all findings so far into a structured brief.
       Rebuild: [system prompt] + [original user question] + [LLM summary] + [continue marker]
  3. If LLM compaction wasn't enough → proportional truncation:
       Allocate remaining space fairly across tool messages (smaller tools get their full content,
       larger tools get proportionally truncated). No tool message is dropped entirely.

Session continuity:
  - The original user question is always preserved (never compacted away)
  - System prompt is always preserved (tool definitions, runbooks, guidelines)
  - The LLM summary preserves all key findings: pod names, errors, metric values, timestamps
  - After compaction, the LLM continues investigating with full context of what was found
"""
import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

# Trigger LLM compaction when we hit this % of the context window
COMPACTION_THRESHOLD_PCT = 80

# Minimum tokens to reserve for LLM output
MIN_OUTPUT_RESERVATION = 4096

# Truncation notice appended to cut-off tool results
TRUNCATION_NOTICE = "\n\n[... truncated to fit context window ...]"

# SRE-focused compaction prompt — preserves investigation-critical details
COMPACTION_PROMPT = """\
The investigation conversation history is approaching the context window limit.
Summarize all investigation findings so far into a concise but complete brief.
This summary will replace the full history — do not lose critical details.

Your summary MUST include:

## Alert Under Investigation
What alert or question is being investigated (exact name, namespace, service).

## Hypotheses Tested
For each hypothesis: what was the suspected cause, what was checked, and was it confirmed/eliminated.

## Evidence Found
Key facts discovered. Be specific:
- Exact pod names, namespaces, deployment names
- Exact error messages and log excerpts (verbatim)
- Exact metric values and timestamps
- K8s events with timestamps

## Tool Calls Summary
- ✅ tool_name(args) → key finding
- ❌ tool_name(args) → no data / error

## Current Assessment
What you believe is the root cause, with confidence level (HIGH/MEDIUM/LOW) and supporting evidence.

## Remaining Leads
What hypotheses or data sources still need investigation.

Preserve all specific technical details — names, values, timestamps. Generic summaries lose context.
"""


def _count_tokens(messages: list[dict], model: str = "gpt-4o") -> int:
    """Count tokens in a message list using litellm's token counter."""
    try:
        import litellm
        return litellm.token_counter(model=model, messages=messages)
    except Exception:
        # Fallback: rough estimate (3 chars per token)
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get("text", "") or part.get("content", "")))
            else:
                total += len(str(content))
        return total // 3


def _get_context_window(model: str) -> int:
    """Get the model's context window size."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        return info.get("max_input_tokens") or info.get("max_tokens") or 128_000
    except Exception:
        return 128_000


def compact_messages(
    messages: list[dict],
    llm=None,  # VishwakarmaLLM instance — used for LLM-based compaction
) -> tuple[list[dict], bool]:
    """
    Compact messages if approaching the context window limit.

    Pass the llm instance to enable LLM-based compaction (recommended).
    Without llm, falls back to truncation-only mode.

    Returns (compacted_messages, did_compact).
    """
    if not messages:
        return messages, False

    model = llm.cfg.model if llm else "gpt-4o"
    token_count = _count_tokens(messages, model)
    context_window = _get_context_window(model)
    threshold = int(context_window * COMPACTION_THRESHOLD_PCT / 100)

    if token_count < threshold:
        return messages, False

    log.warning(
        f"Context window at {token_count}/{context_window} tokens ({token_count * 100 // context_window}%). "
        "Running compaction..."
    )

    # ── LLM-based compaction ──────────────────────────────────────────────────
    if llm is not None:
        compacted = _llm_compact(messages, llm)
        new_count = _count_tokens(compacted, model)
        if new_count < token_count:
            log.info(f"LLM compaction: {token_count} → {new_count} tokens")
            messages = compacted
            token_count = new_count

    # ── Proportional truncation fallback ─────────────────────────────────────
    output_reservation = max(
        MIN_OUTPUT_RESERVATION,
        int(context_window * 0.15),  # reserve 15% for LLM output
    )
    available = context_window - output_reservation

    if token_count > available:
        messages = _proportional_truncate(messages, available, model)
        log.info(
            f"Proportional truncation: {token_count} → {_count_tokens(messages, model)} tokens"
        )

    return messages, True


def _llm_compact(messages: list[dict], llm) -> list[dict]:
    """
    Ask the LLM to summarize the investigation so far.
    Returns: [system_prompt, original_user_question, llm_summary, continue_marker]
    """
    # Extract key parts
    system_msgs = [m for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    original_question = user_msgs[0] if user_msgs else None

    # Ask LLM to summarize (no tools — pure summarization)
    compaction_messages = [m for m in messages if m.get("role") != "system"]
    compaction_messages.append({"role": "user", "content": COMPACTION_PROMPT})

    def _excerpt(content: str, max_chars: int = 2000) -> str:
        """Keep head + tail of long content to preserve both context and errors."""
        if len(content) <= max_chars:
            return content
        head = max_chars * 2 // 3
        tail = max_chars - head
        return content[:head] + f"\n... [{len(content) - max_chars} chars omitted] ...\n" + content[-tail:]

    try:
        summary = llm.summarize(
            # Pass as a single string — summarize() does a simple completion
            "\n\n".join(
                f"[{m.get('role', 'unknown').upper()}]: "
                + _excerpt(m.get("content") or "")
                for m in compaction_messages[:-1]  # exclude the compaction prompt itself
            )
            + f"\n\n{COMPACTION_PROMPT}"
        )
    except Exception as e:
        log.warning(f"LLM compaction failed: {e} — falling back to truncation")
        return messages

    # Rebuild: system + original question + summary + continue
    rebuilt: list[dict] = []
    rebuilt.extend(system_msgs)
    if original_question:
        rebuilt.append(original_question)
    rebuilt.append({
        "role": "assistant",
        "content": f"**Investigation Summary (context compacted)**\n\n{summary}",
    })
    rebuilt.append({
        "role": "system",
        "content": (
            "The conversation history has been compacted to preserve context window space. "
            "The summary above contains all key findings. Continue the investigation."
        ),
    })
    return rebuilt


def _proportional_truncate(
    messages: list[dict], available_tokens: int, model: str
) -> list[dict]:
    """
    Proportionally truncate tool messages to fit within available_tokens.
    Smaller tools get their full content; larger tools are trimmed.
    System prompt and non-tool messages are never truncated.
    """
    # Separate tool and non-tool messages
    non_tool = [m for m in messages if m.get("role") != "tool"]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    non_tool_tokens = _count_tokens(non_tool, model)
    if non_tool_tokens >= available_tokens:
        log.error("Non-tool messages alone exceed context window — cannot truncate further")
        return messages

    space_for_tools = available_tokens - non_tool_tokens
    if not tool_msgs:
        return messages

    # Sort by token count ascending (small first — they get their full allocation)
    def tool_tokens(msg):
        return _count_tokens([msg], model)

    tool_msgs_with_size = sorted(
        [(m, tool_tokens(m)) for m in tool_msgs],
        key=lambda x: x[1],
    )

    remaining = space_for_tools
    result_tools: list[dict] = []

    for i, (msg, size) in enumerate(tool_msgs_with_size):
        remaining_tools = len(tool_msgs_with_size) - i
        max_allocation = remaining // remaining_tools
        if size <= max_allocation:
            result_tools.append(msg)
            remaining -= size
        else:
            # Truncate this message to max_allocation tokens (~3 chars/token)
            max_chars = max(100, max_allocation * 3)
            content = str(msg.get("content", ""))
            truncated = {
                **msg,
                "content": content[:max_chars] + TRUNCATION_NOTICE,
            }
            result_tools.append(truncated)
            remaining -= max_allocation

    # Rebuild messages in original order
    result = []
    tool_iter = iter(result_tools)
    for msg in messages:
        if msg.get("role") == "tool":
            result.append(next(tool_iter, msg))
        else:
            result.append(msg)

    return result
