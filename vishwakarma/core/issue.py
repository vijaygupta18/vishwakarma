"""
Issue model — represents a ticket or alert from any source
(AlertManager, Jira, PagerDuty, OpsGenie, GitHub).
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel


class IssueStatus(str):
    OPEN = "open"
    CLOSED = "closed"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class Issue(BaseModel):
    """
    Unified issue/alert/ticket across all sources.

    Sources map their native format to this model before
    passing it to the investigation engine.
    """
    # Identity
    id: str
    title: str
    source: str                         # alertmanager | jira | pagerduty | opsgenie | github
    source_url: str | None = None

    # Content
    description: str | None = None
    severity: str = "warning"
    status: str = IssueStatus.OPEN
    started_at: datetime | None = None
    labels: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    raw: dict[str, Any] | None = None   # original payload from source

    def question(self) -> str:
        """Build the investigation question from this issue."""
        parts = [f"Alert '{self.title}' is firing."]

        if self.started_at:
            now = datetime.now(timezone.utc)
            window_start = self.started_at - timedelta(minutes=10)
            duration_min = int((now - self.started_at).total_seconds() / 60)
            parts.append(
                f"It started at {self.started_at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                f"({duration_min} minutes ago). "
                f"Investigation time window: {window_start.strftime('%Y-%m-%dT%H:%M:%SZ')} to now."
            )

        if self.description:
            parts.append(self.description)

        # Include key labels for context
        useful_labels = {
            k: v for k, v in self.labels.items()
            if k in ("namespace", "service", "job", "cluster", "env", "instance", "pod")
        }
        if useful_labels:
            label_str = ", ".join(f"{k}={v}" for k, v in useful_labels.items())
            parts.append(f"Affected: {label_str}.")

        parts.append("Investigate the root cause and provide recommendations.")
        return " ".join(parts)
