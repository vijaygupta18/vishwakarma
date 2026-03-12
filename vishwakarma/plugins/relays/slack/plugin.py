"""
Slack destination — post investigation results to a Slack channel.

Features:
  - Markdown-formatted investigation summary
  - Attaches PDF report if available
  - Thread replies for follow-up actions
  - Alert deduplication via thread_ts tracking
"""
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class SlackDestination:
    """
    Post investigation results to Slack via Web API.

    Config:
      token: xoxb-...
      channel: '#sre-alerts'
      mention_on_critical: '@sre-oncall'
    """

    def __init__(self, config: dict):
        self._token = config.get("token") or os.environ.get("SLACK_BOT_TOKEN", "")
        self._channel = config.get("channel", "#sre-alerts")
        self._mention = config.get("mention_on_critical", "")
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client
        from slack_sdk import WebClient
        self._client = WebClient(token=self._token)
        return self._client

    def post_investigation(
        self,
        title: str,
        analysis: str,
        severity: str = "info",
        source: str = "",
        channel: str | None = None,
        thread_ts: str | None = None,
        pdf_path: str | None = None,
    ) -> dict:
        """
        Post investigation result to Slack.
        Returns the Slack API response (includes ts for threading).
        """
        channel = channel or self._channel
        client = self._get_client()

        # Build blocks
        header_emoji = _severity_emoji(severity)
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{header_emoji} {title[:150]}"},
            },
        ]

        if source:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Source: *{source}*"}],
            })

        # Split analysis into sections (Slack has 3000 char limit per block)
        chunks = _split_text(analysis, 2900)
        for chunk in chunks:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            })

        # Mention on critical
        text = f"{header_emoji} {title}"
        if severity in ("critical", "high") and self._mention:
            text = f"{self._mention} {text}"

        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "blocks": blocks,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        try:
            response = client.chat_postMessage(**kwargs)

            # Upload PDF if provided
            if pdf_path and os.path.exists(pdf_path):
                try:
                    client.files_upload_v2(
                        channel=channel,
                        file=pdf_path,
                        filename=f"rca_{title[:40].replace(' ', '_')}.pdf",
                        title=f"RCA Report: {title[:80]}",
                        thread_ts=response["ts"],
                    )
                except Exception as e:
                    log.warning(f"PDF upload failed: {e}")

            return {"ts": response["ts"], "channel": response["channel"]}
        except Exception as e:
            log.error(f"Slack post failed: {e}")
            return {}

    def post_error(self, title: str, error: str, channel: str | None = None) -> dict:
        channel = channel or self._channel
        try:
            client = self._get_client()
            r = client.chat_postMessage(
                channel=channel,
                text=f":x: *{title}*\n```{error[:500]}```",
            )
            return {"ts": r["ts"]}
        except Exception as e:
            log.error(f"Slack error post failed: {e}")
            return {}


def _severity_emoji(severity: str) -> str:
    return {
        "critical": ":red_circle:",
        "high": ":large_orange_circle:",
        "medium": ":large_yellow_circle:",
        "low": ":large_green_circle:",
        "info": ":information_source:",
    }.get(severity.lower(), ":white_circle:")


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split on newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
