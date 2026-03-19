"""
Fast RCA — quick classification for alerts with known root cause patterns.

For alerts like NoDriverDrainerRunning (15+/week, only 4 known root causes),
this module runs targeted checks via a specialized toolset and classifies
the result with a single fast_model LLM call (~5-10s) instead of a full
40-step agentic investigation (~15 min).

The fast RCA is posted to Slack immediately; the deep investigation follows
as a thread reply.
"""
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# ── Registry: alert_name → (toolset_name, tool_name, params) ─────────────────

# ── Companion checks: additional tools to run in parallel for certain alerts ──
# Maps tool_name → list of (toolset_name, tool_name, params) to run alongside
_COMPANION_CHECKS: dict[str, list[tuple[str, str, dict]]] = {
    # Example: "investigate_rds_cpu": [("my_toolset", "investigate_alb_5xx", {})],
}


_REGISTRY: dict[str, tuple[str, str, dict]] = {
    # Populate with your alert_name → (toolset_name, tool_name, params) mappings.
    # Example:
    # "RDSHighCPU": ("my_alerts_toolset", "investigate_rds_cpu", {"db_cluster": "main"}),
    # "ALB5xxErrors": ("my_alerts_toolset", "investigate_alb_5xx", {}),
}



def match_fast_rca(alert_name: str) -> tuple[str, str, dict] | None:
    """Check if an alert has a fast-RCA handler. Returns (toolset, tool, params) or None."""
    # Exact match first
    if alert_name in _REGISTRY:
        return _REGISTRY[alert_name]
    # Add substring matching for your alert naming conventions here
    return None


def get_companion_checks(tool_name: str) -> list[tuple[str, str, dict]]:
    """Return additional tools to run in parallel for this alert type."""
    return _COMPANION_CHECKS.get(tool_name, [])


# ── Decision tree prompts per alert category ──────────────────────────────────

# ── Decision tree prompts per alert category ──────────────────────────────────
# Add your decision trees here. Each is a multi-line string with scenarios.
# Example:
# _MY_ALERT_DECISION_TREE = """\
# - **Scenario A**: condition → Root cause: description
# - **Scenario B**: condition → Root cause: description
# - **Scenario H (Normal)**: all metrics within baseline → Normal load, false alarm"""

_DECISION_TREES: dict[str, str] = {}
# Map alert names to decision trees:
# _DECISION_TREES["RDSHighCPU"] = _MY_RDS_DECISION_TREE



def synthesize_fast_rca(llm, checks: dict, alert_name: str) -> dict:
    """
    Single fast_model LLM call to classify the root cause from check results.

    Returns dict with: root_cause, confidence, scenario, impact, suggested_fix, evidence_summary
    """
    # Pick the right decision tree; default to ALB if alert contains "5xx"
    decision_tree = _DECISION_TREES.get(alert_name)
    if not decision_tree and "5xx" in alert_name.lower():
        decision_tree = _ALB_5XX_DECISION_TREE
    if not decision_tree and ("replication" in alert_name.lower() or "replica" in alert_name.lower() or "slot" in alert_name.lower()):
        decision_tree = _RDS_REPLICATION_LAG_DECISION_TREE
    if not decision_tree and ("rds" in alert_name.lower() or ("cpu" in alert_name.lower() and "redis" not in alert_name.lower())):
        decision_tree = _RDS_CPU_DECISION_TREE
    if not decision_tree and ("ratio" in alert_name.lower() or "search" in alert_name.lower()):
        decision_tree = _RATIO_DROP_DECISION_TREE
    if not decision_tree and "redis" in alert_name.lower():
        decision_tree = _REDIS_DECISION_TREE
    if not decision_tree:
        decision_tree = _DRAINER_DECISION_TREE

    checks_text = _summarize_checks(checks)
    prompt = f"""{alert_name}. {checks_text}. Compare current vs yesterday_*. If current similar to yesterday=normal. Return JSON: {{"root_cause":"x","confidence":"high","scenario":"H","impact":"No user impact","suggested_fix":"No action needed","evidence_summary":"x"}}"""

    try:
        # Use streaming to avoid timeout while model is producing tokens
        from litellm import completion as _completion
        model = llm._get_main_chain()[0]
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
            "temperature": 0.0,
            "timeout": 120,
            "stream": True,
            "num_retries": 1,
            # Disable reasoning/thinking for faster, cleaner JSON output
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False, "thinking": False}},
        }
        if llm.cfg.api_key:
            kwargs["api_key"] = llm.cfg.api_key
        if llm.cfg.api_base:
            kwargs["api_base"] = llm.cfg.api_base

        raw = ""
        for chunk in _completion(**kwargs):
            delta = chunk.choices[0].delta.content or ""
            raw += delta
        raw = raw.strip()
        log.info(f"Fast RCA synthesis response: len={len(raw)}, preview={raw[:100]}")
        # Strip reasoning preamble and extract JSON
        import re
        # Try multiple extraction strategies
        # Strategy 1: Find JSON block containing "root_cause" (handles nested reasoning text)
        json_match = re.search(r'\{"root_cause".*?"evidence_summary"\s*:\s*"[^"]*"\s*\}', raw, re.DOTALL)
        if not json_match:
            # Strategy 2: Find last JSON object in the response (reasoning models put JSON at the end)
            all_jsons = list(re.finditer(r'\{[^{}]{20,}\}', raw))
            if all_jsons:
                json_match = all_jsons[-1]
        if not json_match:
            # Strategy 3: Find anything that looks like our expected JSON
            json_match = re.search(r'\{[^{}]*"root_cause"[^}]*\}', raw, re.DOTALL)
        if json_match:
            raw = json_match.group()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Fast RCA synthesis failed ({type(e).__name__}): {e}")
        # Graceful degradation: summarize raw checks without LLM
        check_summary = []
        for k, v in (checks or {}).items():
            if isinstance(v, str) and not v.startswith("(error"):
                lines = v.strip().split("\n")
                last = lines[-1] if lines else ""
                if last:
                    check_summary.append(f"{k}: {last[:80]}")
        evidence = "; ".join(check_summary[:5]) if check_summary else str(e)
        return {
            "root_cause": "LLM classification unavailable — raw check results below (deep investigation will follow)",
            "confidence": "low",
            "scenario": "unknown",
            "impact": "Unknown — LLM unavailable, raw checks collected",
            "suggested_fix": "Deep investigation in progress",
            "evidence_summary": evidence,
        }


def format_slack_message(result: dict, title: str) -> str:
    """Format fast RCA result as Slack mrkdwn."""
    confidence = result.get("confidence", "low")
    scenario = result.get("scenario", "?")

    if confidence == "high":
        icon = ":large_green_circle:"
    elif confidence == "medium":
        icon = ":large_yellow_circle:"
    else:
        icon = ":red_circle:"

    lines = [
        f":zap: *Fast RCA: {title}*",
        "",
        f"{icon} *Confidence:* {confidence.upper()} (Scenario {scenario})",
        f":mag: *Root Cause:* {result.get('root_cause', 'Unknown')}",
        f":warning: *Impact:* {result.get('impact', 'Unknown')}",
        f":wrench: *Suggested Fix:* {result.get('suggested_fix', 'N/A')}",
        "",
        f"_Evidence: {result.get('evidence_summary', 'N/A')}_",
        "",
        ":hourglass_flowing_sand: _Deep investigation in progress — full RCA with PDF will follow in this thread..._",
    ]
    return "\n".join(lines)
