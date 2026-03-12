"""
Jira source — fetch issues via JQL and write back analysis.

Config:
  url: https://your-org.atlassian.net
  username: sre-bot@company.com
  api_token: your-api-token
  project: OPS
  jql: project=OPS AND status="In Progress" AND priority in (Highest, High)
"""
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)


class JiraSource:

    def __init__(self, config: dict):
        self._base = config.get("url", "").rstrip("/") + "/rest/api/3"
        self._session = requests.Session()
        self._session.auth = (config.get("username", ""), config.get("api_token", ""))
        self._session.headers["Content-Type"] = "application/json"
        self._default_jql = config.get("jql", "")
        self._max_results = config.get("max_results", 50)

    def fetch_issues(self, jql: str | None = None) -> list[Issue]:
        query = jql or self._default_jql
        if not query:
            log.warning("Jira source: no JQL configured, skipping")
            return []
        try:
            r = self._session.post(
                f"{self._base}/search",
                json={"jql": query, "maxResults": self._max_results},
                timeout=15,
            )
            r.raise_for_status()
            jira_issues = r.json().get("issues", [])
        except Exception as e:
            log.error(f"Jira fetch failed: {e}")
            return []

        issues = []
        for ji in jira_issues:
            fields = ji.get("fields", {})
            issue = Issue(
                id=f"jira:{ji['key']}",
                title=f"[{ji['key']}] {fields.get('summary', '')}",
                description=_extract_description(fields.get("description")),
                source="jira",
                source_url=f"{self._base.replace('/rest/api/3', '')}/browse/{ji['key']}",
                labels={"issue_key": ji["key"], "project": ji["key"].split("-")[0]},
                severity=_map_priority(fields.get("priority", {}).get("name", "Medium")),
                status=IssueStatus.OPEN,
            )
            issues.append(issue)
        return issues

    def write_back(self, issue_key: str, analysis: str) -> bool:
        """Add investigation result as a Jira comment."""
        try:
            body = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{
                        "type": "paragraph",
                        "content": [{
                            "type": "text",
                            "text": f"🤖 Vishwakarma Investigation:\n\n{analysis}",
                        }],
                    }],
                }
            }
            r = self._session.post(
                f"{self._base}/issue/{issue_key}/comment",
                json=body,
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"Failed to write back to Jira {issue_key}: {e}")
            return False


def _extract_description(desc_doc: Any) -> str:
    if not desc_doc:
        return ""
    if isinstance(desc_doc, str):
        return desc_doc
    # Atlassian Document Format
    parts = []
    for block in desc_doc.get("content", []):
        for inline in block.get("content", []):
            if inline.get("type") == "text":
                parts.append(inline.get("text", ""))
    return " ".join(parts)


def _map_priority(priority: str) -> str:
    return {
        "Highest": "critical",
        "High": "high",
        "Medium": "medium",
        "Low": "low",
        "Lowest": "info",
    }.get(priority, "medium")
