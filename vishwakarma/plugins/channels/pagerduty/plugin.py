"""
PagerDuty source — fetch triggered/acknowledged incidents.

Config:
  api_key: your-pagerduty-api-key
  service_ids: [P1234, P5678]  # filter by service IDs (optional)
"""
import logging

import requests

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)
PD_BASE = "https://api.pagerduty.com"


class PagerDutySource:

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._service_ids = config.get("service_ids", [])
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Token token={self._api_key}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        })

    def fetch_issues(self) -> list[Issue]:
        params: dict = {"statuses[]": ["triggered", "acknowledged"], "limit": 25}
        if self._service_ids:
            params["service_ids[]"] = self._service_ids
        try:
            r = self._session.get(f"{PD_BASE}/incidents", params=params, timeout=10)
            r.raise_for_status()
            incidents = r.json().get("incidents", [])
        except Exception as e:
            log.error(f"PagerDuty fetch failed: {e}")
            return []

        issues = []
        for inc in incidents:
            severity = inc.get("urgency", "high")
            issues.append(Issue(
                id=f"pagerduty:{inc['id']}",
                title=f"[PD-{inc['incident_number']}] {inc.get('title', '')}",
                description=inc.get("description", ""),
                source="pagerduty",
                source_url=inc.get("html_url", ""),
                labels={
                    "incident_number": str(inc.get("incident_number", "")),
                    "service": inc.get("service", {}).get("summary", ""),
                    "urgency": inc.get("urgency", ""),
                },
                severity=severity,
                status=IssueStatus.OPEN,
            ))
        return issues

    def write_back(self, incident_id: str, analysis: str, from_email: str = "vishwakarma@sre") -> bool:
        """Add a note to a PagerDuty incident."""
        try:
            r = self._session.post(
                f"{PD_BASE}/incidents/{incident_id}/notes",
                json={"note": {"content": f"Vishwakarma analysis:\n\n{analysis}"}},
                headers={"From": from_email},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"PagerDuty write-back failed: {e}")
            return False
