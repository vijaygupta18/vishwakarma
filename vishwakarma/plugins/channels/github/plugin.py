"""
GitHub Issues source — fetch open issues as investigation targets.

Config:
  token: ghp_xxx
  owner: myorg
  repo: myrepo
  labels: ["incident", "bug"]  # filter by labels
"""
import logging

import requests

from vishwakarma.core.issue import Issue, IssueStatus

log = logging.getLogger(__name__)
GH_BASE = "https://api.github.com"


class GitHubSource:

    def __init__(self, config: dict):
        self._token = config.get("token", "")
        self._owner = config.get("owner", "")
        self._repo = config.get("repo", "")
        self._labels = ",".join(config.get("labels", []))
        self._session = requests.Session()
        if self._token:
            self._session.headers["Authorization"] = f"Bearer {self._token}"
        self._session.headers["Accept"] = "application/vnd.github+json"

    def fetch_issues(self) -> list[Issue]:
        if not (self._owner and self._repo):
            log.warning("GitHub source: owner/repo not configured")
            return []
        params = {"state": "open", "per_page": 25, "sort": "created", "direction": "desc"}
        if self._labels:
            params["labels"] = self._labels
        try:
            r = self._session.get(
                f"{GH_BASE}/repos/{self._owner}/{self._repo}/issues",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            gh_issues = r.json()
        except Exception as e:
            log.error(f"GitHub fetch failed: {e}")
            return []

        issues = []
        for gi in gh_issues:
            if gi.get("pull_request"):
                continue  # skip PRs
            label_names = [l["name"] for l in gi.get("labels", [])]
            issues.append(Issue(
                id=f"github:{self._owner}/{self._repo}#{gi['number']}",
                title=f"[#{gi['number']}] {gi['title']}",
                description=gi.get("body", "")[:2000],
                source="github",
                source_url=gi["html_url"],
                labels={
                    "repo": f"{self._owner}/{self._repo}",
                    "number": str(gi["number"]),
                    **{l: "true" for l in label_names},
                },
                severity="high" if any(l in label_names for l in ["incident", "critical", "P1"]) else "medium",
                status=IssueStatus.OPEN,
            ))
        return issues

    def write_back(self, issue_number: int, analysis: str) -> bool:
        """Post analysis as a GitHub issue comment."""
        try:
            r = self._session.post(
                f"{GH_BASE}/repos/{self._owner}/{self._repo}/issues/{issue_number}/comments",
                json={"body": f"## 🤖 Vishwakarma Analysis\n\n{analysis}"},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"GitHub write-back failed: {e}")
            return False
