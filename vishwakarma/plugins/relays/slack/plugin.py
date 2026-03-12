"""
Slack destination — post investigation results to a Slack channel.

Flow (matches Holmes):
  1. Post main message to channel → capture thread ts
  2. Upload PDF with files_upload_v2 (no channel needed)
  3. Post PDF permalink as thread reply using thread_ts
"""
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class SlackDestination:

    def __init__(self, config: dict):
        self._token = config.get("token") or os.environ.get("SLACK_BOT_TOKEN", "")
        self._channel = config.get("channel") or os.environ.get("SLACK_CHANNEL", "#sre-alerts")
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
        channel = channel or self._channel
        client = self._get_client()
        header_emoji = _severity_emoji(severity)

        # Build main message blocks
        blocks: list[dict] = [
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
        for chunk in _split_text(analysis, 2900):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

        text = f"{header_emoji} {title}"
        if severity in ("critical", "high") and self._mention:
            text = f"{self._mention} {text}"

        kwargs: dict[str, Any] = {"channel": channel, "text": text, "blocks": blocks}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        try:
            response = client.chat_postMessage(**kwargs)
            msg_ts = response["ts"]
        except Exception as e:
            log.error(f"Slack post failed: {e}")
            return {}

        # Upload PDF and reply in thread (Holmes pattern: upload first, then post permalink)
        if pdf_path and os.path.exists(pdf_path):
            try:
                filename = f"rca_{title[:40].replace(' ', '_')}.pdf"
                file_resp = client.files_upload_v2(
                    content=open(pdf_path, "rb").read(),
                    filename=filename,
                    title=f"RCA Report: {title[:80]}",
                )
                if file_resp and "file" in file_resp:
                    permalink = file_resp["file"]["permalink"]
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=msg_ts,
                        text=f":page_facing_up: *Full RCA Report*\n<{permalink}|:arrow_down: Download PDF: {title[:60]}>",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":page_facing_up: *Full RCA Report*\n<{permalink}|:arrow_down: Download PDF: {title[:60]}>",
                                },
                            }
                        ],
                    )
            except Exception as e:
                log.warning(f"PDF upload failed: {e}")

        return {"ts": msg_ts, "channel": response["channel"]}

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
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
