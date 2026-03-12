"""
Prompt builder — constructs system and user prompts for investigations.

Prompts are composable: each PromptSection can be toggled on/off
via behavior_controls in the API request.
"""
from enum import Enum
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

# ── Prompt Sections ───────────────────────────────────────────────────────────

class Section(str, Enum):
    INTRO = "intro"
    PLANNING = "planning"
    GUIDELINES = "guidelines"
    RUNBOOKS = "runbooks"
    ASK_USER = "ask_user"


# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_INTRO = """\
You are Vishwakarma, an expert Site Reliability Engineer (SRE) and autonomous \
troubleshooting agent for SRE Platform. You investigate infrastructure incidents, \
diagnose root causes, and recommend fixes.

You have access to tools to query Kubernetes, metrics (Prometheus/VictoriaMetrics), \
logs (Elasticsearch, Loki), databases, cloud services, and more.

Always:
- Gather evidence before concluding
- Be specific: include service names, namespaces, pod names, timestamps
- State what you checked and what you found (or didn't find)
- Give a clear root cause and actionable recommendations
- If a tool returns no data, say so and try a different angle
"""

INVESTIGATION_PHASES = """\
## Investigation Protocol

You investigate in 3 structured phases. Do not skip phases or merge them.

### PHASE 1 — RECON (run tools in parallel, broad signals)
Gather wide signals simultaneously. Fire multiple tool calls in one response:
- K8s: pod status, recent events, resource usage for the affected namespace/service
- Metrics: error rate, latency p99, saturation for the affected service (use the alert time window)
- Recent changes: `kubectl rollout history`, recent K8s events sorted by time
- A quick log search for ERROR/FATAL patterns in the 10 min window around alert start

### PHASE 2 — HYPOTHESES
After phase 1 results arrive, state your top 3 hypotheses BEFORE running more tools:
```
Hypothesis 1: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence so far: <what points to this>
Hypothesis 2: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence so far: <what points to this>
Hypothesis 3: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence so far: <what points to this>
```
Then test each hypothesis with targeted tool calls. Eliminate, confirm, or update confidence.

### PHASE 3 — ROOT CAUSE & RECOMMENDATIONS
Once you have confirmed the root cause, end your investigation with this EXACT structure:

## Root Cause
<1-2 sentences stating the confirmed root cause>

## Confidence
HIGH / MEDIUM / LOW — <reason for confidence level>

## Evidence Chain
1. <trigger event with timestamp>
2. <cascade effect>
3. <impact observed>

## Immediate Fix
<exact command or action to resolve right now>

## Prevention
<what change prevents recurrence>

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking>
"""

WHAT_CHANGED = """\
## Mandatory First Step: What Changed?

For EVERY alert, before anything else, run these simultaneously:
1. `kubectl rollout history <service> -n <namespace>` — detect recent deploys
2. `kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -30` — detect K8s-level changes
3. Prometheus: compare error rate at `alert_start_time - 1h` vs `alert_start_time` — sudden spike = deploy/change, gradual = leak/exhaustion

Pattern:
- **Sudden spike** at a specific time → look for deploy, config change, or traffic surge at that exact time
- **Gradual increase** over hours → memory leak, connection pool exhaustion, disk fill, slow query accumulation
- **Step function** (stable → new stable level) → quota hit, rate limit, downstream degradation
"""

GENERAL_GUIDELINES = """\
## Investigation Guidelines

**Timing:**
- Always use the alert's start time as your anchor. Query the window from `start - 10min` to `start + 30min`.
- If `startsAt` is given, use it. Never query arbitrary recent time ranges.

**Tool strategy:**
- Fire multiple independent tool calls in a SINGLE response (they execute in parallel)
- Start broad (metrics, events) before going narrow (specific pod logs, DB queries)
- If a tool returns no data: try adjacent time windows, looser queries, or a different source

**Evidence quality:**
- Always include timestamps with findings ("at 17:03:42 UTC, error rate jumped from 0.2% to 18%")
- Correlate across sources: a pod OOM at 17:03 + metric spike at 17:03 = strong signal
- Distinguish between cause and symptom (high CPU is often a symptom, not a cause)

**When to stop:**
- Stop only when you can state the root cause with evidence OR when you've exhausted all reasonable angles
- Never give up after one failed tool call — try an alternative approach
"""

RCA_OUTPUT_FORMAT = """\
## Output Format Reminder
End every investigation with the structured Root Cause section from Phase 3.
Be specific: include service names, namespaces, pod names, exact timestamps, metric values.
Vague answers like "there may be a resource issue" are not acceptable.
"""

ASK_USER_PROMPT = """\
If you need clarification or additional context from the user, \
you may ask ONE specific question before proceeding with the investigation.
"""


def build_system_prompt(
    toolsets: list,                      # list of Toolset objects
    cluster_name: str = "",
    runbooks: list[str] | None = None,
    extra_prompt: str | None = None,
    sections_off: set[Section] | None = None,
) -> str:
    """Build the complete system prompt for an investigation."""
    sections_off = sections_off or set()
    parts = []

    if Section.INTRO not in sections_off:
        parts.append(SYSTEM_INTRO)

    if cluster_name:
        parts.append(f"You are operating on cluster: **{cluster_name}**\n")

    if Section.PLANNING not in sections_off:
        parts.append(INVESTIGATION_PHASES)
        parts.append(WHAT_CHANGED)

    if Section.GUIDELINES not in sections_off:
        parts.append(GENERAL_GUIDELINES)
        parts.append(RCA_OUTPUT_FORMAT)

    # Available toolsets
    if toolsets:
        tool_lines = []
        for ts in toolsets:
            if hasattr(ts, "description") and ts.description:
                tool_lines.append(f"- **{ts.name}**: {ts.description}")
            else:
                tool_lines.append(f"- **{ts.name}**")
        if tool_lines:
            parts.append("## Available Toolsets\n" + "\n".join(tool_lines))

    # Runbooks
    if runbooks and Section.RUNBOOKS not in sections_off:
        rb_text = "\n".join(f"- {r}" for r in runbooks)
        parts.append(f"## Relevant Runbooks\n{rb_text}")

    if extra_prompt:
        parts.append(extra_prompt)

    if Section.ASK_USER not in sections_off:
        parts.append(ASK_USER_PROMPT)

    return "\n\n".join(p.strip() for p in parts if p.strip())


# ── User Prompt ───────────────────────────────────────────────────────────────

def build_user_prompt(
    question: str,
    context: dict[str, Any] | None = None,
    files: list[str] | None = None,
) -> str:
    """Build the user-side prompt, optionally with attached file content."""
    parts = [question]

    if context:
        for key, value in context.items():
            parts.append(f"\n**{key}:**\n{value}")

    if files:
        for file_content in files:
            parts.append(f"\n```\n{file_content}\n```")

    return "\n".join(parts)


# ── Message Builder ───────────────────────────────────────────────────────────

def build_messages(
    question: str,
    history: list[dict],
    system_prompt: str,
    images: list[dict] | None = None,
    files: list[str] | None = None,
) -> list[dict]:
    """
    Assemble the full message list for the LLM:
      [system, ...history, user]
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # Add conversation history
    messages.extend(history)

    # Build user message
    user_content: Any = build_user_prompt(question, files=files)

    # Add images for vision-capable models
    if images:
        content_parts = [{"type": "text", "text": user_content}]
        for img in images:
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": img.get("url", ""),
                    "detail": img.get("detail", "auto"),
                },
            })
        user_content = content_parts

    messages.append({"role": "user", "content": user_content})
    return messages
