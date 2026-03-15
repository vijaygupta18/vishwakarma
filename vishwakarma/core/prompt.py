"""
Prompt builder — constructs system and user prompts for investigations.

Prompts are composable: each PromptSection can be toggled on/off
via behavior_controls in the API request.
"""
from enum import Enum
from typing import Any

# ── Prompt Sections ───────────────────────────────────────────────────────────

class Section(str, Enum):
    INTRO = "intro"
    PLANNING = "planning"
    GUIDELINES = "guidelines"
    RUNBOOKS = "runbooks"
    KNOWLEDGE = "knowledge"
    ASK_USER = "ask_user"


# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_INTRO = """\
You are Vishwakarma, an expert Site Reliability Engineer (SRE) and autonomous \
troubleshooting agent. You investigate infrastructure incidents, \
diagnose root causes, and recommend fixes.

You have access to tools to query Kubernetes, metrics (Prometheus/VictoriaMetrics), \
logs (Elasticsearch, Loki), cloud services (AWS CLI), and more.

**READ-ONLY MODE:** You are an observability agent only. NEVER run commands that modify \
cluster state: no `kubectl delete`, `kubectl apply`, `kubectl edit`, `kubectl scale`, \
`kubectl cordon`, `kubectl drain`, or any AWS write operations. Investigate only.

Always:
- Check the **Site Knowledge Base** first for cluster-specific values (namespaces, service names, metric names, proven commands) before making any tool calls
- Gather evidence before concluding
- Be specific: include service names, namespaces, pod names, timestamps, metric values
- State what you checked and what you found (or didn't find)
- Give a clear root cause with actionable recommendations
- Use hedging language (possible, likely, may) when root cause cannot be directly confirmed
"""

INVESTIGATION_PHASES = """\
## Investigation Protocol

**MANDATORY FIRST ACTION:** Call `todo_write` with your full investigation plan before anything else.
- If a runbook is provided: your todo_write steps MUST mirror the runbook's steps exactly — do not substitute generic RECON steps
- List every step with status `pending`
- On the FIRST `todo_write` call: mark all INDEPENDENT tasks as `in_progress` simultaneously and start executing them NOW in the same response (parallel tool calls)
- Update status `in_progress` → `completed` as each step finishes
- If you discover new investigation areas, add them to the todo list

**RUNBOOK TAKES PRECEDENCE:** If a runbook is provided, follow the runbook's steps as your investigation plan. Do NOT default to generic Kubernetes RECON if the runbook tells you to start with AWS CLI, metrics, or other sources.

**STEP BUDGET AWARENESS:** You have a finite number of steps. Prioritize high-signal sources (metrics, error logs) over exhaustive enumeration. If you've tried 3 different approaches on the same angle with no data, move on — record it as "checked, no data" and proceed.

If no runbook is provided, investigate in structured phases:

### PHASE 1 — RECON (parallel, broad signals)
Gather wide signals simultaneously — fire multiple bash/tool calls in one response.
Use raw bash commands via the `bash` tool: kubectl, aws CLI, stern, grep.
Example parallel calls: `kubectl get pods -n <namespace>`, `aws rds describe-db-instances`, `stern -n <namespace> <service> --since=30m`

### PHASE 2 — HYPOTHESES
After phase 1 results arrive, state your top 3 hypotheses BEFORE running more tools:
```
Hypothesis 1: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence: <what points to this>
Hypothesis 2: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence: <what points to this>
Hypothesis 3: <specific cause> — Confidence: HIGH/MEDIUM/LOW — Evidence: <what points to this>
```
Confidence calibration: HIGH = direct evidence (metric spike, error log); MEDIUM = strong correlation but no single smoking gun; LOW = pattern fits but no direct evidence yet.
Then test each hypothesis with targeted tool calls. Eliminate, confirm, or update confidence.

### PHASE EVALUATION (after each phase)
After completing all tasks in a phase, stop and ask yourself:
- Do I have enough information to completely answer the question?
- Have I applied five whys to reach the ACTUAL root cause (not just a symptom)?
- Are there gaps, unexplored angles, or additional root causes to check?
- Did investigation reveal new questions that need exploration?

If ANY answer is "yes → investigation incomplete" → create a new phase with `todo_write` and continue.

### FINAL REVIEW (mandatory before final answer)
Before writing your final answer:
1. Re-read the original user question word-by-word
2. Verify every claim is backed by direct tool output — not assumed
3. Verify the five whys chain leads to actual root cause, not a symptom
4. Check for overconfident claims: if no direct evidence, use "likely" or "possible"

### PHASE 3 — ROOT CAUSE & RECOMMENDATIONS
End your investigation with this EXACT structure:

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
## Detecting What Changed (for K8s/app alerts without a runbook)

If investigating a Kubernetes or application alert without a runbook, run these bash commands simultaneously:
1. `kubectl rollout history deployment/<service> -n <namespace>` — detect recent deploys
2. `kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -30` — detect K8s-level changes
3. Use `prometheus_query_range` for error rate at `alert_start_time - 1h` vs `alert_start_time`

Pattern:
- **Sudden spike** at a specific time → deploy, config change, or traffic surge at that time
- **Gradual increase** over hours → memory leak, connection pool exhaustion, disk fill
- **Step function** (stable → new stable level) → quota hit, rate limit, downstream degradation

**For AWS alerts (RDS, ElastiCache, ALB, etc.):** Skip this section. Follow the runbook.
"""

GENERAL_GUIDELINES = """\
## Investigation Guidelines

**CRITICAL TOOL RULES (violations cause investigation loops — follow strictly):**
- **NEVER call a tool with identical parameters more than once.** If a tool already ran with those exact params, reuse its result — do NOT call it again.
- **If a tool returns no data or an error → modify the parameters and try a different approach.** Do NOT retry the same call. Try: different time window, looser query, different namespace, different service, different tool.
- **If `aws cloudwatch describe-alarms --state-value ALARM` returns empty → the alarm has already resolved (normal for RDS/CPU spikes). Do NOT retry this command.** Use `startsAt` from the alert as your time anchor and proceed with investigation.
- **If a bash command returns non-zero exit code → do not retry the same command.** Read the error, fix the command, or try an alternative approach.

**Pre-fetched context:**
- Before the investigation loop starts, `kubectl get pods`, `kubectl get events`, and `kubectl get replicasets` are run and provided to you as context. DO NOT re-run these exact commands — use the results already provided. You may run narrower follow-up commands (e.g. `kubectl describe pod <specific-pod>`, `kubectl logs <pod>`).

**Timing:**
- Always use the alert's `startsAt` as your anchor. Query window: `startsAt - 10min` to `startsAt + 1h`.
- For Prometheus range queries: `start = startsAt - 10min`, `end = startsAt + 1h`, `step = 60` (seconds).
- If `startsAt` not available, use `now - 30min`.
- Never use arbitrary recent time ranges.

**Tool routing (use the dedicated tool, not http_get):**
- Metrics/PromQL → `prometheus_query` or `prometheus_query_range` (NEVER `http_get`)
- Log search → `elasticsearch_search` or `loki_query` (NEVER `http_get`)
- K8s/AWS/system commands → `bash` tool
- External URLs only → `http_get`

**Parallel execution:**
- Fire ALL independent tool calls in a SINGLE response — they run in parallel
- Start broad (metrics, events) before narrow (specific pod logs, DB queries)

**Evidence quality:**
- Include timestamps with every finding ("at 17:03:42 UTC, error rate jumped from 0.2% to 18%")
- Correlate across sources: pod OOM at 17:03 + metric spike at 17:03 = strong signal
- Distinguish cause from symptom (high CPU is usually a symptom, not a cause)
- Treat error messages as exact diagnostic evidence: `authentication failed` means the user EXISTS and password is wrong — never add "or the user may not exist"

**Five Whys — drill to root cause:**
Do NOT stop at the first why. Apply iteratively:
1. Why did the alert fire? → service X returned 5xx
2. Why did service X return 5xx? → it couldn't connect to Redis
3. Why couldn't it connect to Redis? → Redis CPU was at 100%
4. Why was Redis CPU at 100%? → key eviction storm caused by memory exhaustion
5. Why did memory exhaust? → a new deployment 20 min earlier added a missing cache TTL

**When to stop:**
- Stop only when you can state the root cause with evidence OR exhausted all reasonable angles
- Never give up after one failed tool call — try an alternative approach
- If investigation is inconclusive, state that clearly with what was checked and what's still unknown
"""

RCA_OUTPUT_FORMAT = """\
## Output Format Reminder
End every investigation with the structured Root Cause section from Phase 3.
Be specific: include service names, namespaces, pod names, exact timestamps, metric values.
Vague answers like "there may be a resource issue" are not acceptable.
"""

ASK_USER_PROMPT = """\
**ALWAYS investigate immediately. Never ask for clarification.**
Extract whatever context is available (ride IDs, booking data, error messages, service names, time hints) and start investigating with tools NOW.
If the user pastes a Slack thread, DB query results, or raw JSON — treat it as investigation context and use the data directly.
Only ask a question if you have literally zero information to work with (e.g. empty message).
"""


def build_system_prompt(
    toolsets: list,                      # list of Toolset objects (enabled)
    cluster_name: str = "",
    runbooks: list[str] | None = None,
    knowledge: str | None = None,        # site-specific knowledge base (from /data/knowledge.md)
    extra_prompt: str | None = None,
    sections_off: set[Section] | None = None,
    all_toolsets: list | None = None,    # all toolsets including disabled (optional)
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
            desc = getattr(ts, "description", "") or ""
            if desc:
                tool_lines.append(f"- **{ts.name}**: {desc}")
            else:
                tool_lines.append(f"- **{ts.name}**")

        tool_lines.append(
            "\n**Tool routing rules:**\n"
            "- Metrics/PromQL → use `prometheus_query` or `prometheus_query_range` (NEVER http_get)\n"
            "- Log search → use `elasticsearch_search` or `loki_query` (NEVER http_get)\n"
            "- K8s/AWS/system commands → use `bash` tool\n"
            "- External URLs only → use `http_get`"
        )
        parts.append("## Available Toolsets\n" + "\n".join(tool_lines))

    # Show disabled/failed toolsets so LLM understands the landscape (matches Holmes)
    disabled = []
    if all_toolsets:
        from vishwakarma.core.tools import ToolsetHealth
        enabled_names = {ts.name for ts in toolsets}
        for ts in all_toolsets:
            if ts.name not in enabled_names:
                status = "disabled"
                health = getattr(ts, "_health", None)
                if health and health == ToolsetHealth.FAILED:
                    status = f"failed — {getattr(ts, '_error', '')}"
                disabled.append(f"- **{ts.name}**: {status}")

    if disabled:
        parts.append(
            "## Disabled / Failed Toolsets\n"
            "The following toolsets are not available. If investigation requires one of these, "
            "tell the operator which toolset needs to be enabled.\n"
            + "\n".join(disabled)
        )

    # Site knowledge base (always injected — contains infra-specific mappings and known commands)
    if knowledge and Section.KNOWLEDGE not in sections_off:
        parts.append(f"## Site Knowledge Base\n{knowledge.strip()}")

    # Runbooks
    if runbooks and Section.RUNBOOKS not in sections_off:
        rb_text = "\n\n---\n\n".join(runbooks)
        parts.append(f"## Relevant Runbook\n\n{rb_text}")

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
