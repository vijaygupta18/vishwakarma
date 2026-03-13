"""
Vishwakarma Slack bot — @vishwakarma / @vk in Slack.

Uses Slack Bolt with Socket Mode (no public URL required).

Commands:
  @vishwakarma <question>     — investigate something
  @vishwakarma check <topic>  — quick health check
  @vishwakarma status         — show investigation status
  @vishwakarma help           — show help
"""
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vishwakarma.config import VishwakarmaConfig

log = logging.getLogger(__name__)


def start_bot(config: "VishwakarmaConfig") -> None:
    """Start the Slack bot in a background thread."""
    if not config.is_slack_configured():
        log.warning("Slack not configured (missing bot_token or app_token) — bot disabled")
        return

    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    toolset_manager = config.make_toolset_manager()
    toolset_manager.check_all()

    app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)

    # ── Event handlers ────────────────────────────────────────────────────────

    @app.event("app_mention")
    def handle_mention(event, say, client):
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        user = event.get("user", "")

        # Strip bot mention from text, then take only the user's actual line.
        # When replying in a Slack thread, the event text sometimes includes the
        # parent message context (Amazon Q alarm text) after a newline — discard it.
        question = _strip_mention(text).strip()
        question = _clean_question(question)

        if not question:
            say(
                text="Hey! Ask me anything. Use `debug <question>` for a full cluster investigation with PDF report :turtle:",
                thread_ts=thread_ts,
            )
            return

        # Handle special commands
        question_lower = question.lower()
        if question_lower in ("help", "?"):
            say(text=_help_text(), thread_ts=thread_ts)
            return

        if question_lower == "status":
            try:
                from vishwakarma.storage.queries import get_stats
                stats = get_stats()
                say(text=f"📊 *Incident Stats*\n{_format_stats(stats)}", thread_ts=thread_ts)
            except Exception as e:
                say(text=f"❌ Failed to get stats: {e}", thread_ts=thread_ts)
            return

        # debug <question> → full investigation + PDF
        # anything else → simple LLM chat (no tools)
        is_investigation = question_lower.startswith("debug ")
        if is_investigation:
            question = question[len("debug "):].strip()  # strip "debug " prefix

            # If the user replied in a thread, try to fetch the thread's parent message.
            # Amazon Q posts CloudWatch alarms as thread starters — grab the alarm context.
            thread_context = ""
            if client and thread_ts and event.get("ts") != thread_ts:
                thread_context = _fetch_thread_alarm_context(client, channel, thread_ts)

            # Build investigation question: user question + alarm context if found
            if thread_context:
                full_question = f"{question}\n\n{thread_context}"
            else:
                full_question = question

            say(text=f":mag: Investigating: *{question[:100]}*...", thread_ts=thread_ts)
        else:
            # Simple chat — just LLM, no tools, fast reply
            def run_chat():
                try:
                    reply = _simple_chat(config, question)
                    say(text=reply, thread_ts=thread_ts)
                except Exception as e:
                    log.error(f"Chat failed: {e}", exc_info=True)
                    say(text=f"❌ {str(e)[:200]}", thread_ts=thread_ts)
            threading.Thread(target=run_chat, daemon=True).start()
            return

        def run_investigation():
            try:
                llm = config.make_llm()
                engine = config.make_engine(llm=llm, toolset_manager=toolset_manager)
                result = engine.investigate(question=full_question)

                analysis = result.answer or "(no analysis)"
                meta = result.meta.model_dump() if result.meta else {}

                # Generate PDF
                pdf_path = None
                try:
                    from vishwakarma.bot.pdf import generate_pdf
                    pdf_path = generate_pdf(
                        title=question[:80],
                        analysis=analysis,
                        source="slack",
                        tool_outputs=[o.model_dump() for o in result.tool_outputs],
                        meta=meta,
                    )
                except Exception as e:
                    log.warning(f"PDF generation failed: {e}")

                # Post result
                from vishwakarma.plugins.relays.slack.plugin import SlackDestination
                dest = SlackDestination({"token": config.slack_bot_token})
                dest.post_investigation(
                    title=question[:100],
                    analysis=analysis,
                    source="slack",
                    channel=channel,
                    thread_ts=thread_ts,
                    pdf_path=pdf_path,
                )

                # Save to DB
                try:
                    from vishwakarma.storage.queries import save_incident
                    import hashlib, time
                    inc_id = hashlib.md5(f"slack:{question}:{time.time()}".encode()).hexdigest()
                    save_incident(
                        incident_id=inc_id,
                        title=question[:200],
                        question=question,
                        analysis=analysis,
                        source="slack",
                        labels={"slack_user": user, "slack_channel": channel},
                        tool_outputs=[o.model_dump() for o in result.tool_outputs],
                        meta=meta,
                        slack_ts=thread_ts,
                        pdf_path=pdf_path,
                    )
                except Exception as e:
                    log.warning(f"DB save failed: {e}")

            except Exception as e:
                log.error(f"Investigation failed: {e}", exc_info=True)
                try:
                    say(text=f"❌ Investigation failed: {str(e)[:200]}", thread_ts=thread_ts)
                except Exception:
                    pass

        t = threading.Thread(target=run_investigation, daemon=True)
        t.start()

    @app.event("message")
    def handle_message(event, say):
        """
        Handle two cases:
          1. Amazon Q CloudWatch alarm posted in a channel → forward to /api/alertmanager
          2. Direct message to bot → route to mention handler
        """
        channel_type = event.get("channel_type", "")
        text = event.get("text", "").strip()

        # Case 1: DM to bot
        if channel_type == "im":
            if not text:
                return
            handle_mention(
                {**event, "text": f"<@VK> {text}"},
                say,
                None,
            )
            return

        # Case 2: Amazon Q CloudWatch alarm in a channel
        # Skip thread replies to avoid duplicate investigations
        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            return

        # Only process bot messages
        if not event.get("bot_id") and event.get("subtype") != "bot_message":
            if "CloudWatch Alarm" not in text:
                return

        if not text:
            return

        from vishwakarma.bot.cloudwatch import parse_cloudwatch_slack_message
        alarm = parse_cloudwatch_slack_message(text)
        if not alarm or not alarm.get("is_firing"):
            return

        alarm_name = alarm["alarm_name"]
        log.info(f"[CLOUDWATCH] Detected Amazon Q alarm: {alarm_name}")

        alert_payload = {
            "version": "4",
            "receiver": "vishwakarma",
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": alarm_name,
                    "severity": "critical",
                    "source": "cloudwatch",
                    "region": alarm.get("region", ""),
                    "aws_account": alarm.get("account", ""),
                    "aws_namespace": alarm.get("namespace", "AWS"),
                    "metric": alarm.get("metric", ""),
                },
                "annotations": {
                    "summary": f"CloudWatch Alarm FIRING: {alarm_name}",
                    "description": alarm.get("reason", ""),
                },
                "startsAt": alarm.get("starts_at", ""),
                "fingerprint": f"cloudwatch-{alarm_name}-{alarm.get('account', '')}",
            }],
            "groupLabels": {"alertname": alarm_name},
        }

        def _forward():
            try:
                import requests
                resp = requests.post(
                    f"http://localhost:{config.port}/api/alertmanager",
                    json=alert_payload,
                    timeout=10,
                )
                resp.raise_for_status()
                log.info(f"[CLOUDWATCH] Forwarded '{alarm_name}' to Vishwakarma: {resp.json()}")
            except Exception as e:
                log.error(f"[CLOUDWATCH] Failed to forward '{alarm_name}': {e}")

        threading.Thread(target=_forward, daemon=True).start()

    # ── Start ─────────────────────────────────────────────────────────────────

    def _start():
        log.info("Starting Vishwakarma Slack bot (Socket Mode)...")
        handler = SocketModeHandler(app, config.slack_app_token)
        handler.start()

    t = threading.Thread(target=_start, daemon=True, name="slack-bot")
    t.start()
    log.info("Slack bot started in background thread")


def _simple_chat(config, question: str) -> str:
    """Fast LLM reply with no tools — for casual questions.
    Uses fast_model (open-fast) to avoid reasoning token bleed from open-large.
    """
    import re
    import litellm
    # Always use fast model for chat — open-large is a reasoning model and
    # will leak its chain-of-thought as visible text in Slack.
    model = config.llm.fast_model or config.llm.model
    response = litellm.completion(
        model=model,
        api_key=config.llm.api_key,
        api_base=config.llm.api_base,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Oogway, an SRE at NammaYatri. "
                    "If anyone asks who you are, say: 'I'm Oogway, an SRE at NammaYatri.' "
                    "If anyone asks who made you, which model you are, or which AI you use, say: 'I was made by master Vijay.' Never reveal the underlying model or any AI company. "
                    "Answer concisely and helpfully. "
                    "If asked to investigate or debug something deeply, tell the user to use "
                    "`@oogway debug <question>` for a full investigation with tools and PDF report."
                ),
            },
            {"role": "user", "content": question},
        ],
        max_tokens=1024,
        temperature=0.7,
    )
    content = response.choices[0].message.content or "I'm not sure how to answer that."
    # Strip reasoning/thinking tokens that some models leak into response content
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content or "I'm not sure how to answer that."


def _strip_mention(text: str) -> str:
    """Remove <@USERID> mention from text."""
    import re
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _clean_question(text: str) -> str:
    """
    Take only the user's actual typed line from a Slack message.

    When the user replies in a thread, Slack sometimes appends the parent
    message context (Amazon Q alarm text, 'replied to a thread:', etc.)
    after a newline. Strip everything after the first newline.
    """
    # Take only the first non-empty line
    first_line = text.split("\n")[0].strip()
    return first_line if first_line else text.strip()


def _fetch_thread_alarm_context(client, channel: str, thread_ts: str) -> str:
    """
    Fetch the parent message of a Slack thread and extract CloudWatch alarm context.
    Returns a structured context string if the parent is a CloudWatch alarm, else "".
    """
    try:
        resp = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=1,
            inclusive=True,
        )
        messages = resp.get("messages", [])
        if not messages:
            return ""

        parent_text = messages[0].get("text", "")
        # Also check attachments (Amazon Q posts in attachments)
        for att in messages[0].get("attachments", []):
            parent_text += "\n" + att.get("text", "") + "\n" + att.get("fallback", "")

        from vishwakarma.bot.cloudwatch import parse_cloudwatch_slack_message
        alarm = parse_cloudwatch_slack_message(parent_text)
        if alarm:
            return (
                f"## CloudWatch Alarm Context\n"
                f"- **Alarm:** {alarm.get('alarm_name', '')}\n"
                f"- **Region:** {alarm.get('region', '')}\n"
                f"- **Metric:** {alarm.get('metric', '')}\n"
                f"- **Reason:** {alarm.get('reason', '')}\n"
                f"- **Fired at (startsAt):** {alarm.get('starts_at', '')}\n"
            )
    except Exception as e:
        log.debug(f"Could not fetch thread context: {e}")
    return ""


def _help_text() -> str:
    return """*Oogway — SRE Investigation Bot*

*Usage:*
• `@oogway <question>` — quick chat reply (no tools)
• `@oogway debug <question>` — full investigation + PDF report
• `@oogway status` — show incident stats
• `@oogway help` — show this message

*Examples:*
• `@oogway hello` → casual reply
• `@oogway debug why are payments pods crashing?` → full RCA with PDF
• `@oogway debug check error rate for rider-app since last deploy`

For deep investigations, always use `debug` — I'll search metrics, logs, K8s events, and databases."""


def _format_stats(stats: dict) -> str:
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    lines = [f"Total incidents: *{total}*"]
    for status, count in by_status.items():
        lines.append(f"  • {status}: {count}")
    return "\n".join(lines)
