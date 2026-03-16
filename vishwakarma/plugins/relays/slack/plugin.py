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

    def _resolve_channel_id(self, channel: str) -> str:
        """Resolve #channel-name to channel ID. Returns as-is if already an ID."""
        if not channel or channel.startswith("C") or channel.startswith("D") or channel.startswith("G"):
            return channel
        name = channel.lstrip("#")
        try:
            client = self._get_client()
            for page in client.conversations_list(types="public_channel,private_channel", limit=1000):
                for c in page.get("channels", []):
                    if c["name"] == name:
                        return c["id"]
        except Exception as e:
            log.warning(f"Could not resolve channel '{channel}': {e}")
        return channel  # fallback — might fail but at least chat_postMessage handles names

    def post_investigation(
        self,
        title: str,
        analysis: str,
        severity: str = "info",
        source: str = "",
        channel: str | None = None,
        thread_ts: str | None = None,
        pdf_path: str | None = None,
        incident_id: str | None = None,
    ) -> dict:
        channel = channel or self._channel
        client = self._get_client()
        # Resolve #channel-name to ID — files_upload_v2 requires channel ID
        channel = self._resolve_channel_id(channel)

        # If posting in an existing thread (e.g., @oogway debug response),
        # post the analysis directly as thread replies — no big header announcement.
        # If no thread_ts (e.g., alert-triggered investigation), post the header as a new message.
        if thread_ts:
            msg_ts = thread_ts  # reply in existing thread

            # Upload PDF directly in thread — this is the primary deliverable
            pdf_uploaded = False
            if pdf_path and os.path.exists(pdf_path):
                try:
                    with open(pdf_path, "rb") as f:
                        pdf_bytes = f.read()
                    client.files_upload_v2(
                        channel=channel,
                        thread_ts=msg_ts,
                        content=pdf_bytes,
                        filename=f"rca-{title[:40].replace(' ', '-')}.pdf",
                        title=f"RCA - {title}",
                        initial_comment=f":page_facing_up: *{title}*",
                    )
                    pdf_uploaded = True
                except Exception as e:
                    log.warning(f"PDF upload failed: {e}")

            # Fallback: if no PDF, post analysis as text
            if not pdf_uploaded:
                from vishwakarma.utils.slack_format import md_to_slack, chunk_for_slack, strip_code_wrapper
                slack_text = md_to_slack(strip_code_wrapper(analysis))
                for chunk in chunk_for_slack(slack_text):
                    client.chat_postMessage(
                        channel=channel, thread_ts=msg_ts, text=chunk,
                        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                    )

            # Feedback buttons in thread
            if incident_id:
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=msg_ts,
                        text="Was this RCA accurate?",
                        blocks=[
                            {"type": "section", "text": {"type": "mrkdwn", "text": "*Was this RCA accurate?*"}},
                            {
                                "type": "actions",
                                "block_id": f"rca_feedback_{incident_id[:16]}",
                                "elements": [
                                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Correct", "emoji": True}, "style": "primary", "action_id": "vk_rca_correct", "value": incident_id},
                                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Wrong", "emoji": True}, "style": "danger", "action_id": "vk_rca_wrong", "value": incident_id},
                                ],
                            },
                        ],
                    )
                except Exception as e:
                    log.warning(f"Feedback buttons post failed: {e}")

            return {"ts": msg_ts, "channel": channel}

        # No thread_ts — new top-level message (alert-triggered investigations)
        color = "#FF0000" if severity in ("critical", "high") else "#00FF00"
        main_text = f":rotating_light: RCA complete for {title}"

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f":rotating_light: RCA: {title[:150]}", "emoji": True},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":thread: Investigation complete. See thread for full RCA report."},
            },
        ]

        try:
            response = client.chat_postMessage(
                channel=channel,
                text=main_text,
                attachments=[{"color": color, "blocks": blocks}],
            )
            msg_ts = response["ts"]
        except Exception as e:
            log.error(f"Slack post failed: {e}")
            return {}

        # Upload PDF directly in thread
        pdf_uploaded = False
        if pdf_path and os.path.exists(pdf_path):
            try:
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                client.files_upload_v2(
                    channel=channel,
                    thread_ts=msg_ts,
                    content=pdf_bytes,
                    filename=f"rca-{title[:40].replace(' ', '-')}.pdf",
                    title=f"RCA - {title}",
                    initial_comment=f":page_facing_up: *Full RCA Report*",
                )
                pdf_uploaded = True
            except Exception as e:
                log.warning(f"PDF upload failed, falling back to text: {e}")

        if not pdf_uploaded:
            from vishwakarma.utils.slack_format import md_to_slack, chunk_for_slack, strip_code_wrapper
            slack_text = md_to_slack(strip_code_wrapper(analysis))
            for chunk in chunk_for_slack(slack_text):
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=msg_ts,
                    text=chunk,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                )

        if incident_id:
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=msg_ts,
                    text="Was this RCA accurate?",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": "*Was this RCA accurate?*"}},
                        {
                            "type": "actions",
                            "block_id": f"rca_feedback_{incident_id[:16]}",
                            "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "✅ Correct", "emoji": True}, "style": "primary", "action_id": "vk_rca_correct", "value": incident_id},
                                {"type": "button", "text": {"type": "plain_text", "text": "❌ Wrong", "emoji": True}, "style": "danger", "action_id": "vk_rca_wrong", "value": incident_id},
                            ],
                        },
                    ],
                )
            except Exception as e:
                log.warning(f"Feedback buttons post failed: {e}")

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
