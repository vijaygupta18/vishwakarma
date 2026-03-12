"""
Prometheus AlertManager source — fetch firing alerts as Issues.

Connects to AlertManager API to list active alerts and convert them to
Vishwakarma Issue objects for investigation.
"""
import logging
from datetime import datetime, timezone

import requests

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)


class AlertManagerSource:
    """
    Pull active alerts from Prometheus AlertManager.

    Config:
      url: http://alertmanager:9093
      filter_labels: {env: prod}  # only fetch alerts with these labels
    """

    def __init__(self, config: dict):
        self.url = config.get("url", "http://alertmanager:9093").rstrip("/")
        self.filter_labels: dict = config.get("filter_labels", {})
        self._session = requests.Session()
        if config.get("username"):
            self._session.auth = (config["username"], config.get("password", ""))

    def fetch_issues(self) -> list[Issue]:
        """Fetch all active alerts from AlertManager."""
        try:
            r = self._session.get(f"{self.url}/api/v2/alerts", timeout=10)
            r.raise_for_status()
            alerts = r.json()
        except Exception as e:
            log.error(f"Failed to fetch alerts from AlertManager: {e}")
            return []

        issues = []
        for alert in alerts:
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            status = alert.get("status", {})

            # Apply label filter
            if self.filter_labels:
                if not all(labels.get(k) == v for k, v in self.filter_labels.items()):
                    continue

            # Skip resolved alerts
            alert_state = status.get("state", "active")
            if alert_state == "suppressed":
                continue

            name = labels.get("alertname", "UnknownAlert")
            severity = labels.get("severity", "warning")
            namespace = labels.get("namespace", "")
            service = labels.get("service", labels.get("job", ""))
            summary = annotations.get("summary", "")
            description = annotations.get("description", "")

            # Parse start time
            starts_at = alert.get("startsAt", "")
            started_at = None
            if starts_at:
                try:
                    started_at = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                except Exception:
                    pass

            title = f"[{severity.upper()}] {name}"
            if namespace:
                title += f" in {namespace}"
            if service:
                title += f" ({service})"

            issue = Issue(
                id=_alert_id(labels),
                title=title,
                description=description or summary,
                source="alertmanager",
                source_url=f"{self.url}/#/alerts",
                labels=labels,
                annotations=annotations,
                severity=severity,
                status=IssueStatus.OPEN,
                started_at=started_at,
            )
            issues.append(issue)

        log.info(f"Fetched {len(issues)} active alerts from AlertManager")
        return issues


def parse_alertmanager_webhook(payload: dict) -> list[Issue]:
    """
    Parse an AlertManager webhook POST body into Issue objects.
    Used by the /api/alertmanager endpoint.
    """
    issues = []
    alerts = payload.get("alerts", [])
    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alert_status = alert.get("status", "firing")

        if alert_status != "firing":
            continue

        name = labels.get("alertname", "UnknownAlert")
        severity = labels.get("severity", "warning")
        namespace = labels.get("namespace", "")
        service = labels.get("service", labels.get("job", ""))
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")

        title = f"[{severity.upper()}] {name}"
        if namespace:
            title += f" in {namespace}"
        if service:
            title += f" ({service})"

        starts_at = alert.get("startsAt", "")
        started_at = None
        if starts_at:
            try:
                started_at = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            except Exception:
                pass

        issue = Issue(
            id=_alert_id(labels),
            title=title,
            description=description or summary,
            source="alertmanager",
            labels=labels,
            annotations=annotations,
            severity=severity,
            status=IssueStatus.OPEN,
            started_at=started_at,
        )
        issues.append(issue)

    return issues


def _alert_id(labels: dict) -> str:
    """Build a stable ID from alert labels."""
    alertname = labels.get("alertname", "")
    namespace = labels.get("namespace", "")
    service = labels.get("service", labels.get("job", ""))
    return f"alertmanager:{alertname}:{namespace}:{service}"
