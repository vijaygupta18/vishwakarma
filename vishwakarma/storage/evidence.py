"""
Evidence Memory — learns from confirmed investigations to auto-resolve known patterns.

Architecture:
  1. After fast RCA, extract numeric metrics from check results
  2. Compare against learned baselines (mean ± stddev from confirmed investigations)
  3. If all metrics within normal range → auto-resolve (no deep investigation)
  4. If anomalies detected → inject specific anomalies into deep investigation
  5. On ✅ Correct → store evidence snapshot → refine baselines

No LLM involved in validation — pure statistical comparison.
"""
import json
import logging
import math
import re
import time
from typing import Any

from vishwakarma.storage.db import _get_conn, _lock

log = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id              TEXT PRIMARY KEY,
    alert_name      TEXT NOT NULL,
    scenario        TEXT,               -- fast RCA scenario (A, B, C, H, etc.)
    root_cause_type TEXT,               -- normal_load, missing_index, autovacuum, etc.
    metrics         TEXT NOT NULL,       -- JSON: {"metric_name": numeric_value}
    outcome         TEXT DEFAULT 'pending', -- correct / wrong / pending
    incident_id     TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_baselines (
    alert_name      TEXT NOT NULL,
    metric_name     TEXT NOT NULL,
    mean            REAL NOT NULL,
    stddev          REAL NOT NULL,
    min_val         REAL,
    max_val         REAL,
    sample_count    INTEGER DEFAULT 0,
    last_updated    REAL NOT NULL,
    PRIMARY KEY (alert_name, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_evidence_alert ON evidence_snapshots(alert_name, outcome);
CREATE INDEX IF NOT EXISTS idx_baselines_alert ON learned_baselines(alert_name);
"""


def init_evidence() -> None:
    conn = _get_conn()
    with _lock:
        conn.executescript(EVIDENCE_SCHEMA)
        conn.commit()


# ── Metric Extraction ─────────────────────────────────────────────────────────
# Parse structured numeric values from fast RCA check outputs.
# The fast RCA returns text like "avg=17% max=17%" or "32 5xx" or "load=0.14: IO:DataFileRead".
# We extract the key numbers.

def extract_metrics_from_checks(checks: dict) -> dict[str, float]:
    """Extract numeric metric values from fast RCA check results.

    Returns dict of {metric_name: numeric_value} for the LATEST/most relevant value.
    """
    metrics: dict[str, float] = {}

    for check_name, raw_output in checks.items():
        if not isinstance(raw_output, str) or raw_output.startswith("(error"):
            continue

        output = raw_output.strip()
        if not output:
            continue

        # Get the last line (most recent datapoint)
        lines = [l.strip() for l in output.split("\n") if l.strip()]
        if not lines:
            continue

        # Strategy: extract the most meaningful number from each check type
        try:
            if "cpu" in check_name.lower():
                # "avg=17% max=17%" → extract avg
                m = re.search(r'avg=(\d+)', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "connection" in check_name.lower():
                # "avg=135 max=135"
                m = re.search(r'avg=(\d+)', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "iops" in check_name.lower():
                # "avg=126 max=126"
                m = re.search(r'avg=(\d+)', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "memory" in check_name.lower() or "freeable" in check_name.lower():
                # FreeableMemory in bytes → GB
                m = re.search(r'avg=(\d+)', lines[-1])
                if m:
                    val = float(m.group(1))
                    if val > 1_000_000_000:  # bytes → GB
                        val = val / 1_073_741_824
                    metrics[check_name] = round(val, 2)

            elif "5xx" in check_name.lower() or "target_5xx" in check_name:
                # "32 5xx" or "Sum: 32"
                vals = re.findall(r'(\d+(?:\.\d+)?)\s*(?:5xx|elb)', output)
                if vals:
                    # Average of all 5xx values
                    nums = [float(v) for v in vals]
                    metrics[check_name] = round(sum(nums) / len(nums), 1)

            elif "response_time" in check_name.lower():
                # "avg=0.012s" → ms
                m = re.search(r'avg=(\d+(?:\.\d+)?)(?:ms|s)', lines[-1])
                if m:
                    val = float(m.group(1))
                    if "s" in lines[-1] and "ms" not in lines[-1] and val < 1:
                        val = val * 1000  # seconds to ms
                    metrics[check_name] = round(val, 1)

            elif "pi_wait" in check_name.lower():
                # "load=0.14: IO:DataFileRead" → top wait event load
                m = re.search(r'load=(\d+(?:\.\d+)?)', lines[0])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "pi_top_sql" in check_name.lower():
                # "load=0.02: UPDATE..." → top SQL load
                m = re.search(r'load=(\d+(?:\.\d+)?)', lines[0])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "slot_lag" in check_name.lower():
                # "min=225d avg=225d max=225d (19514600s)"
                m = re.search(r'max=(\d+)d', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "replica_lag" in check_name.lower():
                # "avg=7ms max=20ms"
                m = re.search(r'avg=(\d+)ms', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

            elif "success_rate" in check_name.lower():
                # Single number like "99.6" or "{} = 99.6"
                m = re.search(r'(\d+(?:\.\d+)?)', output)
                if m:
                    val = float(m.group(1))
                    if val <= 100:  # percentage
                        metrics[check_name] = val

            elif "ratio" in check_name.lower():
                # Ratio values
                m = re.search(r'(\d+(?:\.\d+)?)', output)
                if m:
                    metrics[check_name] = float(m.group(1))

            else:
                # Generic: try to extract any number from the last line
                m = re.search(r'(?:avg=|max=|=\s*)(\d+(?:\.\d+)?)', lines[-1])
                if m:
                    metrics[check_name] = float(m.group(1))

        except (ValueError, IndexError):
            continue

    return metrics


# ── Evidence Storage ──────────────────────────────────────────────────────────

def store_evidence(
    evidence_id: str,
    alert_name: str,
    metrics: dict[str, float],
    scenario: str = "",
    root_cause_type: str = "",
    incident_id: str = "",
    outcome: str = "pending",
) -> None:
    """Store an evidence snapshot from a fast RCA run."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO evidence_snapshots "
            "(id, alert_name, scenario, root_cause_type, metrics, outcome, incident_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, alert_name, scenario, root_cause_type,
             json.dumps(metrics), outcome, incident_id, time.time()),
        )
        conn.commit()


def mark_evidence_correct(incident_id: str) -> None:
    """Mark evidence as correct and update baselines."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE evidence_snapshots SET outcome = 'correct' WHERE incident_id = ?",
            (incident_id,),
        )
        conn.commit()

    # Recompute baselines for this alert
    row = conn.execute(
        "SELECT alert_name, metrics FROM evidence_snapshots WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()
    if row:
        _update_baselines(row["alert_name"])


def mark_evidence_wrong(incident_id: str) -> None:
    """Mark evidence as wrong — don't update baselines."""
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE evidence_snapshots SET outcome = 'wrong' WHERE incident_id = ?",
            (incident_id,),
        )
        conn.commit()


# ── Baseline Computation ──────────────────────────────────────────────────────

def _update_baselines(alert_name: str) -> None:
    """Recompute baselines from all correct evidence snapshots for this alert."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT metrics FROM evidence_snapshots "
        "WHERE alert_name = ? AND outcome = 'correct' "
        "ORDER BY created_at DESC LIMIT 50",  # last 50 confirmed
        (alert_name,),
    ).fetchall()

    if len(rows) < 2:
        return  # need at least 2 samples for stddev

    # Collect all metric values
    all_metrics: dict[str, list[float]] = {}
    for row in rows:
        snapshot = json.loads(row["metrics"])
        for k, v in snapshot.items():
            if isinstance(v, (int, float)):
                all_metrics.setdefault(k, []).append(v)

    # Compute mean and stddev for each metric
    now = time.time()
    with _lock:
        for metric_name, values in all_metrics.items():
            if len(values) < 2:
                continue
            n = len(values)
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / (n - 1)
            stddev = math.sqrt(variance) if variance > 0 else 0.01  # avoid zero stddev
            min_val = min(values)
            max_val = max(values)

            conn.execute(
                "INSERT OR REPLACE INTO learned_baselines "
                "(alert_name, metric_name, mean, stddev, min_val, max_val, sample_count, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (alert_name, metric_name, mean, stddev, min_val, max_val, n, now),
            )
        conn.commit()
    log.info(f"Baselines updated for {alert_name}: {len(all_metrics)} metrics from {len(rows)} samples")


def get_baselines(alert_name: str) -> dict[str, dict]:
    """Get learned baselines for an alert.

    Returns: {"metric_name": {"mean": X, "stddev": Y, "min": Z, "max": W, "samples": N}}
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM learned_baselines WHERE alert_name = ?",
        (alert_name,),
    ).fetchall()
    return {
        r["metric_name"]: {
            "mean": r["mean"],
            "stddev": r["stddev"],
            "min": r["min_val"],
            "max": r["max_val"],
            "samples": r["sample_count"],
        }
        for r in rows
    }


# ── Anomaly Detection ─────────────────────────────────────────────────────────

def compare_against_baselines(
    alert_name: str,
    current_metrics: dict[str, float],
) -> dict:
    """Compare current metrics against learned baselines.

    Returns:
        {
            "has_baselines": True/False,
            "all_normal": True/False (all metrics within 2σ),
            "anomalies": [{"metric": X, "value": Y, "baseline_mean": M, "z_score": Z}],
            "normal": [{"metric": X, "value": Y, "baseline_mean": M, "z_score": Z}],
            "sample_count": N (min samples across metrics),
            "summary": "human-readable summary"
        }
    """
    baselines = get_baselines(alert_name)
    if not baselines:
        return {"has_baselines": False, "all_normal": False, "anomalies": [], "normal": [],
                "sample_count": 0, "summary": "No baselines yet — need confirmed investigations first"}

    anomalies = []
    normal = []
    min_samples = 999

    for metric_name, value in current_metrics.items():
        if metric_name not in baselines:
            continue

        b = baselines[metric_name]
        mean = b["mean"]
        stddev = b["stddev"]
        samples = b["samples"]
        min_samples = min(min_samples, samples)

        if stddev > 0:
            z_score = (value - mean) / stddev
        else:
            z_score = 0.0 if value == mean else 10.0

        entry = {
            "metric": metric_name,
            "value": value,
            "baseline_mean": round(mean, 2),
            "baseline_stddev": round(stddev, 2),
            "z_score": round(z_score, 2),
            "samples": samples,
        }

        if abs(z_score) > 2.5:
            anomalies.append(entry)
        else:
            normal.append(entry)

    all_normal = len(anomalies) == 0 and len(normal) > 0
    min_samples = min_samples if min_samples < 999 else 0

    # Build summary
    if not normal and not anomalies:
        summary = "No overlapping metrics with baselines"
    elif all_normal:
        summary = f"All {len(normal)} metrics within normal range (baselines from {min_samples} confirmed investigations)"
    else:
        anom_strs = [f"{a['metric']}={a['value']} (baseline: {a['baseline_mean']}±{a['baseline_stddev']}, z={a['z_score']})" for a in anomalies[:3]]
        summary = f"{len(anomalies)} anomalous metrics: {'; '.join(anom_strs)}"

    return {
        "has_baselines": True,
        "all_normal": all_normal,
        "anomalies": anomalies,
        "normal": normal,
        "sample_count": min_samples,
        "summary": summary,
    }


def should_auto_resolve(
    alert_name: str,
    current_metrics: dict[str, float],
    fast_rca_confidence: str = "",
    min_samples: int = 3,
) -> tuple[bool, str]:
    """Determine if this alert can be auto-resolved based on learned evidence.

    Returns: (should_resolve: bool, reason: str)
    """
    comparison = compare_against_baselines(alert_name, current_metrics)

    if not comparison["has_baselines"]:
        return False, "No baselines — need more confirmed investigations"

    if comparison["sample_count"] < min_samples:
        return False, f"Only {comparison['sample_count']} samples (need {min_samples}+)"

    if not comparison["all_normal"]:
        anomalies = comparison["anomalies"]
        return False, f"Anomalies detected: {', '.join(a['metric'] + '=' + str(a['value']) for a in anomalies[:3])}"

    # All metrics normal + enough samples → auto-resolve
    reason = (
        f"All {len(comparison['normal'])} metrics within learned baselines "
        f"(from {comparison['sample_count']} confirmed investigations). "
        f"{comparison['summary']}"
    )
    return True, reason
