"""
RCA Pattern Database — stores confirmed investigation patterns for replay.

When an RCA is marked ✅ Correct, the LLM extracts:
  - root_cause_type: category (missing_index, autovacuum, connection_pool, etc.)
  - investigation_steps: ordered list of ACTUAL tool calls that found the root cause
  - verification_keywords: keywords that MUST appear in tool output for pattern to match
  - verification_anti_keywords: keywords that must NOT appear (different root cause)
  - fix: recommended action

On the next alert of the same type:
  1. Cross-check with fast RCA — if fast RCA classified differently, skip pattern
  2. Replay the investigation_steps (targeted tool calls)
  3. Check verification_keywords in tool output (deterministic, no LLM)
  4. If match → instant RCA. If not → fall back to full investigation.
"""
import json
import logging
import re
import time
from typing import Any

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

# ── Schema (added to main DB) ────────────────────────────────────────────────

PATTERNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rca_patterns (
    id                  TEXT PRIMARY KEY,
    alert_name          TEXT NOT NULL,
    root_cause_type     TEXT NOT NULL,
    root_cause_detail   TEXT NOT NULL,
    investigation_steps TEXT NOT NULL,       -- JSON array of {tool, params, what_to_check}
    verification_keywords TEXT NOT NULL,     -- JSON array of strings that MUST appear in tool output
    verification_anti_keywords TEXT DEFAULT '[]', -- JSON array of strings that must NOT appear
    fix                 TEXT NOT NULL,
    confidence          TEXT DEFAULT 'high',
    hit_count           INTEGER DEFAULT 1,
    miss_count          INTEGER DEFAULT 0,
    first_seen          REAL NOT NULL,
    last_seen           REAL NOT NULL,
    last_incident_id    TEXT,
    status              TEXT DEFAULT 'active'  -- active / expired / wrong
);

CREATE INDEX IF NOT EXISTS idx_patterns_alert ON rca_patterns(alert_name, status);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON rca_patterns(root_cause_type);
"""


def init_patterns() -> None:
    """Create patterns table if it doesn't exist."""
    conn = _get_conn()
    with _lock:
        conn.executescript(PATTERNS_SCHEMA)
        conn.commit()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def save_pattern(
    pattern_id: str,
    alert_name: str,
    root_cause_type: str,
    root_cause_detail: str,
    investigation_steps: list[dict],
    verification_keywords: list[str],
    verification_anti_keywords: list[str] | None = None,
    fix: str = "",
    confidence: str = "high",
    incident_id: str | None = None,
) -> str:
    """Save a new pattern or increment hit_count if similar pattern exists."""
    conn = _get_conn()
    now = time.time()

    with _lock:
        existing = conn.execute(
            "SELECT id, hit_count FROM rca_patterns "
            "WHERE alert_name = ? AND root_cause_type = ? AND status = 'active'",
            (alert_name, root_cause_type),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE rca_patterns SET hit_count = hit_count + 1, last_seen = ?, "
                "last_incident_id = ?, root_cause_detail = ?, investigation_steps = ?, "
                "verification_keywords = ?, verification_anti_keywords = ?, fix = ? WHERE id = ?",
                (now, incident_id, root_cause_detail,
                 json.dumps(investigation_steps),
                 json.dumps(verification_keywords),
                 json.dumps(verification_anti_keywords or []),
                 fix, existing["id"]),
            )
            conn.commit()
            log.info(f"Pattern updated: {existing['id']} (hit_count={existing['hit_count'] + 1})")
            return existing["id"]
        else:
            conn.execute(
                "INSERT INTO rca_patterns "
                "(id, alert_name, root_cause_type, root_cause_detail, investigation_steps, "
                "verification_keywords, verification_anti_keywords, fix, confidence, "
                "hit_count, first_seen, last_seen, last_incident_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'active')",
                (pattern_id, alert_name, root_cause_type, root_cause_detail,
                 json.dumps(investigation_steps),
                 json.dumps(verification_keywords),
                 json.dumps(verification_anti_keywords or []),
                 fix, confidence, now, now, incident_id),
            )
            conn.commit()
            log.info(f"New pattern saved: {pattern_id} ({alert_name}/{root_cause_type})")
            return pattern_id


def get_patterns_for_alert(alert_name: str, max_age_days: int = 30) -> list[dict]:
    """Fetch active patterns for an alert, ordered by hit_count."""
    conn = _get_conn()
    cutoff = time.time() - (max_age_days * 86400)
    rows = conn.execute(
        "SELECT * FROM rca_patterns "
        "WHERE alert_name = ? AND status = 'active' AND last_seen > ? "
        "ORDER BY hit_count DESC LIMIT 5",
        (alert_name, cutoff),
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["investigation_steps"] = json.loads(d["investigation_steps"])
        d["verification_keywords"] = json.loads(d.get("verification_keywords", "[]"))
        d["verification_anti_keywords"] = json.loads(d.get("verification_anti_keywords", "[]"))
        results.append(d)
    return results


def mark_pattern_hit(pattern_id: str, incident_id: str | None = None) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET hit_count = hit_count + 1, last_seen = ?, "
            "last_incident_id = ? WHERE id = ?",
            (time.time(), incident_id, pattern_id),
        )
        conn.commit()


def mark_pattern_miss(pattern_id: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET miss_count = miss_count + 1 WHERE id = ?",
            (pattern_id,),
        )
        conn.execute(
            "UPDATE rca_patterns SET status = 'expired' "
            "WHERE id = ? AND miss_count > hit_count * 2",
            (pattern_id,),
        )
        conn.commit()


def mark_pattern_wrong(alert_name: str, root_cause_type: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE rca_patterns SET status = 'wrong' "
            "WHERE alert_name = ? AND root_cause_type = ? AND status = 'active'",
            (alert_name, root_cause_type),
        )
        conn.commit()


# ── Available tools (for extraction prompt) ───────────────────────────────────

AVAILABLE_TOOLS = """
Available tools (use ONLY these exact names):
- bash: Run shell commands. params: {"command": "the command"}. Use for: kubectl, aws CLI, stern, jq
- prometheus_query: PromQL instant query. params: {"query": "promql"}
- prometheus_query_range: PromQL range query. params: {"query": "promql", "start": "iso", "end": "iso", "step": "60"}
- elasticsearch_search: ES query. params: {"index": "idx", "query": {...}, "size": 10}
- db_query: SQL query. params: {"connection": "clickhouse|bap_pg|bpp_pg", "query": "SQL"}
- http_get: HTTP GET. params: {"url": "..."}
"""


# ── Pattern Extraction ────────────────────────────────────────────────────────

def extract_pattern_from_rca(llm, alert_name: str, analysis: str, tool_outputs: list) -> dict | None:
    """Extract a replayable pattern from a confirmed RCA.

    Returns dict with: root_cause_type, root_cause_detail, investigation_steps,
    verification_keywords, verification_anti_keywords, fix.
    """
    # Build tool call summary using ACTUAL tool names from the investigation
    tool_summary = []
    for t in (tool_outputs or [])[:20]:
        if isinstance(t, dict):
            name = t.get("tool_name", "?")
            params = t.get("params", {})
            status = t.get("status", "?")
            output_preview = str(t.get("output", ""))[:200]
        else:
            name = getattr(t, "tool_name", "?")
            params = getattr(t, "params", {})
            status = getattr(t, "status", "?")
            output_preview = str(getattr(t, "output", ""))[:200]
        if name in ("todo_write", "todo_read", "learnings_list", "learnings_read"):
            continue
        tool_summary.append(f"- tool={name}, params={json.dumps(params)[:300]}, status={status}, output_preview={output_preview}")

    prompt = f"""You are analyzing a CONFIRMED correct RCA for a "{alert_name}" alert.
Extract a replayable investigation pattern.

{AVAILABLE_TOOLS}

## Full RCA Analysis
{analysis[:4000]}

## Actual Tool Calls Made (with real tool names and params)
{chr(10).join(tool_summary[:15])}

RULES:
1. investigation_steps MUST use exact tool names from the "Available tools" list above (bash, prometheus_query, db_query, etc.)
2. For kubectl/aws commands, use tool="bash" with params={{"command": "the actual command"}}
3. Include 3-5 key steps that were most important for finding this specific root cause
4. verification_keywords: list of 3-5 specific strings/patterns that MUST appear in the tool outputs for this root cause type (e.g. "Running", "seq_scan", "autovacuum:", "stop_status")
5. verification_anti_keywords: list of 2-3 strings that indicate a DIFFERENT root cause (e.g. if pattern is "false_alarm", anti-keywords would be "CrashLoopBackOff", "OOMKilled", "Error")

Respond ONLY with valid JSON (no markdown fences):
{{"root_cause_type": "short_category", "root_cause_detail": "one sentence", "investigation_steps": [{{"tool": "bash", "params": {{"command": "actual command"}}, "what_to_check": "what to verify"}}, ...], "verification_keywords": ["keyword1", "keyword2"], "verification_anti_keywords": ["anti1", "anti2"], "fix": "recommended action"}}"""

    try:
        raw = llm.summarize(prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        pattern = json.loads(raw)

        required = ["root_cause_type", "root_cause_detail", "investigation_steps",
                     "verification_keywords", "fix"]
        if not all(k in pattern for k in required):
            log.warning(f"Pattern extraction missing fields: {[k for k in required if k not in pattern]}")
            return None

        # Validate tool names — reject if LLM invented fake tools
        valid_tools = {"bash", "prometheus_query", "prometheus_query_range",
                       "elasticsearch_search", "db_query", "http_get"}
        for step in pattern["investigation_steps"]:
            if step.get("tool") not in valid_tools:
                log.warning(f"Pattern has invalid tool name: {step.get('tool')} — fixing to 'bash'")
                step["tool"] = "bash"  # best guess fallback

        return pattern
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Pattern extraction failed: {e}")
        return None


# ── Pattern Replay (deterministic validation) ─────────────────────────────────

def replay_pattern(
    pattern: dict,
    executor,
    llm=None,  # only used as fallback if keyword check is ambiguous
    alert_context: str = "",
    fast_rca_result: dict | None = None,
) -> dict | None:
    """Replay a pattern's investigation steps and validate deterministically.

    Validation is keyword-based, NOT LLM-based:
    1. Run the stored tool calls
    2. Check verification_keywords appear in outputs (must match)
    3. Check verification_anti_keywords do NOT appear (must not match)
    4. If keywords match and anti-keywords don't → MATCHED
    5. If anti-keywords found → NOT MATCHED (different root cause)
    """
    steps = pattern.get("investigation_steps", [])
    if not steps:
        return None

    # ── Pre-check: cross-reference with fast RCA ──
    # If fast RCA already classified this as a different root cause type, skip replay
    if fast_rca_result:
        fast_scenario = fast_rca_result.get("scenario", "").lower()
        pattern_type = pattern.get("root_cause_type", "").lower()
        # Map fast RCA scenarios to pattern types for cross-check
        scenario_type_map = {
            "a": "missing_index", "b": "bad_deploy", "c": "autovacuum",
            "d": "connection_pool", "e": "replication_lag", "f": "db_5xx",
            "g": "background_job", "h": "normal_load",
        }
        fast_type = scenario_type_map.get(fast_scenario, "")
        if fast_type and fast_type != pattern_type and fast_rca_result.get("confidence") == "high":
            log.info(f"Pattern skip: fast RCA says '{fast_type}' (HIGH), pattern is '{pattern_type}'")
            return {"matched": False, "confidence": "low",
                    "root_cause": f"Fast RCA classified as {fast_type}, not {pattern_type}",
                    "evidence": "Cross-check with fast RCA", "differences": "Different classification"}

    # ── Execute pattern steps ──
    all_output_text = ""
    step_results = []
    for step in steps[:5]:
        tool_name = step.get("tool", "")
        params = step.get("params", step.get("params_template", {}))
        what_to_check = step.get("what_to_check", "")

        try:
            output = executor.execute(tool_name, params)
            content = str(output.output) if output.output else str(output.error or "")
            step_results.append({
                "tool": tool_name,
                "what_to_check": what_to_check,
                "output": content[:2000],
                "status": str(output.status),
            })
            all_output_text += f"\n{content}"
        except Exception as e:
            step_results.append({
                "tool": tool_name,
                "what_to_check": what_to_check,
                "output": f"(error: {e})",
                "status": "error",
            })
            all_output_text += f"\n(error: {e})"

    # ── Deterministic keyword validation ──
    output_lower = all_output_text.lower()

    # Check verification keywords (must appear)
    keywords = pattern.get("verification_keywords", [])
    keywords_found = []
    keywords_missing = []
    for kw in keywords:
        if kw.lower() in output_lower:
            keywords_found.append(kw)
        else:
            keywords_missing.append(kw)

    # Check anti-keywords (must NOT appear)
    anti_keywords = pattern.get("verification_anti_keywords", [])
    anti_found = []
    for akw in anti_keywords:
        if akw.lower() in output_lower:
            anti_found.append(akw)

    # ── Decision logic ──
    keyword_match_ratio = len(keywords_found) / max(len(keywords), 1)
    has_anti = len(anti_found) > 0

    if has_anti:
        # Anti-keywords found → different root cause, definitely not a match
        return {
            "matched": False,
            "confidence": "high",
            "root_cause": f"Different root cause detected (found: {', '.join(anti_found)})",
            "evidence": f"Anti-keywords present: {anti_found}. Keywords matched: {keywords_found}/{keywords}",
            "differences": f"Found {anti_found} which indicates a different root cause type",
        }

    if keyword_match_ratio >= 0.6:
        # Enough keywords match → pattern confirmed
        confidence = "high" if keyword_match_ratio >= 0.8 else "medium"
        return {
            "matched": True,
            "confidence": confidence,
            "root_cause": pattern.get("root_cause_detail", ""),
            "evidence": f"Keywords matched: {keywords_found} ({keyword_match_ratio:.0%}). Missing: {keywords_missing}",
            "differences": f"Missing keywords: {keywords_missing}" if keywords_missing else "None",
            "pattern_id": pattern.get("id", ""),
            "fix": pattern.get("fix", ""),
            "root_cause_type": pattern.get("root_cause_type", ""),
            "hit_count": pattern.get("hit_count", 0),
            "step_results": step_results,
        }

    # Low keyword match — not enough evidence
    return {
        "matched": False,
        "confidence": "low",
        "root_cause": f"Insufficient evidence — only {len(keywords_found)}/{len(keywords)} keywords matched",
        "evidence": f"Found: {keywords_found}. Missing: {keywords_missing}",
        "differences": "Not enough matching evidence",
    }
