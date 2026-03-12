"""
OpsGenie source — fetch open alerts.

Config:
  api_key: your-opsgenie-api-key
  query: "status:open AND priority:P1"
"""
import logging

import requests

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)
OG_BASE = "https://api.opsgenie.com/v2"


class OpsGenieSource:

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._query = config.get("query", "status:open")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"GenieKey {self._api_key}"

    def fetch_issues(self) -> list[Issue]:
        try:
            r = self._session.get(
                f"{OG_BASE}/alerts",
                params={"query": self._query, "limit": 25, "sort": "createdAt", "order": "desc"},
                timeout=10,
            )
            r.raise_for_status()
            alerts = r.json().get("data", [])
        except Exception as e:
            log.error(f"OpsGenie fetch failed: {e}")
            return []

        issues = []
        for alert in alerts:
            issues.append(Issue(
                id=f"opsgenie:{alert['id']}",
                title=f"[{alert.get('tinyId', '')}] {alert.get('message', '')}",
                description=alert.get("description", ""),
                source="opsgenie",
                source_url=f"https://app.opsgenie.com/alert/detail/{alert['id']}",
                labels={
                    "alias": alert.get("alias", ""),
                    "source": alert.get("source", ""),
                    "priority": alert.get("priority", "P3"),
                    **{t: "true" for t in alert.get("tags", [])},
                },
                severity=_map_priority(alert.get("priority", "P3")),
                status=IssueStatus.OPEN,
            ))
        return issues

    def write_back(self, alert_id: str, analysis: str) -> bool:
        """Add a note to an OpsGenie alert."""
        try:
            r = self._session.post(
                f"{OG_BASE}/alerts/{alert_id}/notes",
                json={"note": f"Vishwakarma analysis:\n\n{analysis}", "source": "vishwakarma"},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"OpsGenie write-back failed: {e}")
            return False


def _map_priority(priority: str) -> str:
    return {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low", "P5": "info"}.get(priority, "medium")
