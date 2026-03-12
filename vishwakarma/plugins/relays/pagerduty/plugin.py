"""
PagerDuty destination — update incident with investigation results.
"""
import logging

import requests

log = logging.getLogger(__name__)
PD_BASE = "https://api.pagerduty.com"


class PagerDutyDestination:
    """
    Write investigation results back to PagerDuty incidents.

    Config:
      api_key: your-api-key
      from_email: vishwakarma@sre-team.com
    """

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._from_email = config.get("from_email", "vishwakarma@sre")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Token token={self._api_key}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "From": self._from_email,
        })

    def add_note(self, incident_id: str, analysis: str) -> bool:
        try:
            r = self._session.post(
                f"{PD_BASE}/incidents/{incident_id}/notes",
                json={"note": {"content": f"Vishwakarma RCA:\n\n{analysis}"}},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"PagerDuty add_note failed: {e}")
            return False

    def resolve_incident(self, incident_id: str, resolution_note: str) -> bool:
        try:
            r = self._session.put(
                f"{PD_BASE}/incidents/{incident_id}",
                json={"incident": {"type": "incident_reference", "status": "resolved"}},
                timeout=10,
            )
            r.raise_for_status()
            if resolution_note:
                self.add_note(incident_id, resolution_note)
            return True
        except Exception as e:
            log.error(f"PagerDuty resolve failed: {e}")
            return False
