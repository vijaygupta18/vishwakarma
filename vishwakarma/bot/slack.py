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

        # Strip bot mention from text
        question = _strip_mention(text).strip()

        if not question:
            say(
                text="Hi! I'm Vishwakarma, your SRE agent. Ask me to investigate something!\n"
                     "Usage: `@vishwakarma <your question>`",
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
            question = question[6:].strip()  # strip "debug " prefix
            say(text=f"🔍 Investigating: *{question[:100]}*...", thread_ts=thread_ts)
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
                result = engine.investigate(question=question)

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
    def handle_dm(event, say):
        """Handle direct messages to the bot — same logic as mentions."""
        channel_type = event.get("channel_type", "")
        if channel_type != "im":
            return
        text = event.get("text", "").strip()
        if not text:
            return
        # Re-use mention handler by injecting text as if it were a mention
        handle_mention(
            {**event, "text": f"<@VK> {text}"},
            say,
            None,
        )

    # ── Start ─────────────────────────────────────────────────────────────────

    def _start():
        log.info("Starting Vishwakarma Slack bot (Socket Mode)...")
        handler = SocketModeHandler(app, config.slack_app_token)
        handler.start()

    t = threading.Thread(target=_start, daemon=True, name="slack-bot")
    t.start()
    log.info("Slack bot started in background thread")


def _simple_chat(config, question: str) -> str:
    """Fast LLM reply with no tools — for casual questions."""
    import litellm
    response = litellm.completion(
        model=config.llm.model,
        api_key=config.llm.api_key,
        api_base=config.llm.api_base,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Vishwakarma, an SRE assistant built by the SRE Platform platform team. "
                    "Answer concisely and helpfully. "
                    "If asked to investigate or debug something deeply, tell the user to use "
                    "`@vishwakarma debug <question>` for a full investigation with tools and PDF report."
                ),
            },
            {"role": "user", "content": question},
        ],
        max_tokens=1024,
        temperature=0.7,
    )
    return response.choices[0].message.content or "I'm not sure how to answer that."


def _strip_mention(text: str) -> str:
    """Remove <@USERID> mention from text."""
    import re
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _help_text() -> str:
    return """*Vishwakarma — SRE Investigation Bot*

*Usage:*
• `@vishwakarma <question>` — quick chat reply (no tools)
• `@vishwakarma debug <question>` — full investigation + PDF report
• `@vishwakarma status` — show incident stats
• `@vishwakarma help` — show this message

*Examples:*
• `@vishwakarma hello` → casual reply
• `@vishwakarma debug why are payments pods crashing?` → full RCA with PDF
• `@vishwakarma debug check error rate for rider-app since last deploy`

For deep investigations, always use `debug` — I'll search metrics, logs, K8s events, and databases."""


def _format_stats(stats: dict) -> str:
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    lines = [f"Total incidents: *{total}*"]
    for status, count in by_status.items():
        lines.append(f"  • {status}: {count}")
    return "\n".join(lines)
