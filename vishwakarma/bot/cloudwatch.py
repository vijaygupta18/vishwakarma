"""
CloudWatch alarm Slack parser + Lambda → AlertManager forwarder.

Two components:
  1. CloudWatch SNS → AlertManager converter (for Lambda)
  2. CloudWatch alarm Slack message parser (for bot notifications)

CloudWatch alarms arrive via SNS → Lambda → AlertManager webhook format.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# ── Lambda forwarder ──────────────────────────────────────────────────────────

def sns_to_alertmanager(sns_event: dict) -> dict | None:
    """
    Convert an SNS CloudWatch alarm event to AlertManager webhook format.
    Called by the Lambda handler.

    Returns a dict in AlertManager webhook format or None if not a valid alarm.
    """
    try:
        records = sns_event.get("Records", [])
        if not records:
            return None

        sns_record = records[0].get("Sns", {})
        subject = sns_record.get("Subject", "")
        message_str = sns_record.get("Message", "{}")

        try:
            message = json.loads(message_str)
        except Exception:
            log.warning("CloudWatch SNS message is not JSON")
            return None

        alarm_name = message.get("AlarmName", "UnknownAlarm")
        alarm_description = message.get("AlarmDescription", "")
        new_state = message.get("NewStateValue", "ALARM")
        old_state = message.get("OldStateValue", "OK")
        reason = message.get("NewStateReason", "")
        region = message.get("Region", "<region>")
        account = message.get("AWSAccountId", "")

        # Map CloudWatch state to alert status
        if new_state != "ALARM":
            # Resolved alert
            status = "resolved"
        else:
            status = "firing"

        # Extract namespace and metric from trigger if available
        trigger = message.get("Trigger", {})
        namespace = trigger.get("Namespace", "AWS")
        metric_name = trigger.get("MetricName", "")
        dimensions = {d["name"]: d["value"] for d in trigger.get("Dimensions", [])}

        labels = {
            "alertname": alarm_name,
            "namespace": dimensions.get("kubernetes_namespace", dimensions.get("Namespace", "")),
            "severity": _infer_severity(alarm_name),
            "aws_namespace": namespace,
            "aws_region": region,
            "aws_account": account,
            "source": "cloudwatch",
        }
        if metric_name:
            labels["metric"] = metric_name
        labels.update(dimensions)

        annotations = {
            "summary": subject or alarm_name,
            "description": alarm_description or reason,
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason[:500],
        }

        now = datetime.now(tz=timezone.utc).isoformat()
        alert = {
            "status": status,
            "labels": labels,
            "annotations": annotations,
            "startsAt": now,
        }

        return {
            "version": "4",
            "receiver": "vishwakarma",
            "status": status,
            "alerts": [alert],
            "groupLabels": {"alertname": alarm_name},
            "commonLabels": labels,
            "commonAnnotations": annotations,
        }

    except Exception as e:
        log.error(f"Failed to parse CloudWatch SNS event: {e}")
        return None


def _infer_severity(alarm_name: str) -> str:
    name_lower = alarm_name.lower()
    if any(w in name_lower for w in ["critical", "p1", "down", "unavailable"]):
        return "critical"
    if any(w in name_lower for w in ["high", "error", "p2"]):
        return "high"
    if any(w in name_lower for w in ["warn", "p3", "throttle", "latency"]):
        return "medium"
    return "low"


# ── Slack message parser ───────────────────────────────────────────────────────

def parse_cloudwatch_slack_message(text: str) -> dict | None:
    """
    Parse a CloudWatch alarm notification posted to Slack.
    Returns structured alarm data or None if not recognized.

    Example Slack messages from CloudWatch:
    - "ALARM: 'rider-app-cpu-high' in Asia Pacific (Mumbai)"
    - "OK: 'rider-app-cpu-high' state is now OK"
    """
    if not text:
        return None

    # Match state: ALARM, OK, INSUFFICIENT_DATA
    state_match = re.match(r"^(ALARM|OK|INSUFFICIENT_DATA):\s*['\"]?([^'\"]+)['\"]?", text)
    if not state_match:
        return None

    state = state_match.group(1)
    alarm_name = state_match.group(2).strip()

    # Extract region if present
    region_match = re.search(r"in\s+(.+?)(?:\s+region|\s*$)", text, re.IGNORECASE)
    region = region_match.group(1).strip() if region_match else ""

    return {
        "alarm_name": alarm_name,
        "state": state,
        "region": region,
        "is_firing": state == "ALARM",
        "raw_text": text[:500],
    }
