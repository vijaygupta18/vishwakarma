"""
Vishwakarma Slack bot — @vishwakarma / @vk in Slack.

Uses Slack Bolt with Socket Mode (no public URL required).

Commands:
  @vishwakarma <question>     — investigate something
  @vishwakarma check <topic>  — quick health check
  @vishwakarma costs          — run AWS cost report now
  @vishwakarma status         — show investigation status
  @vishwakarma help           — show help
"""
import json
import logging
import re
import threading
import time
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
    from vishwakarma.core.learnings import LearningsManager
    learnings_manager = LearningsManager()

    app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)

    # ── Oracle session state: thread_ts → {session_id, history} ──────────────
    # Keyed by thread_ts so any @mention in the same thread continues the session.
    _oracle_sessions: dict[str, dict] = {}
    _oracle_lock = threading.Lock()

    # ── Concurrency guard ──────────────────────────────────────────────────────
    _active_investigations = 0
    _investigation_limit = 2
    _inv_lock = threading.Lock()

    # ── Event handlers ────────────────────────────────────────────────────────

    @app.event("app_mention")
    def handle_mention(event, say, client):
        log.info(f"[EVENT RAW] {json.dumps(event, default=str)}")

        # ── Oracle session TTL: evict sessions older than 2 hours ──────────
        with _oracle_lock:
            stale = [ts for ts, s in _oracle_sessions.items()
                     if s.get("_created", 0) and time.time() - s["_created"] > 7200]
            for ts in stale:
                _oracle_sessions.pop(ts, None)

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

        if question_lower in ("costs", "cost report", "cost"):
            say(text="💰 Running AWS cost report...", thread_ts=thread_ts)
            def run_cost_report():
                try:
                    from vishwakarma.scheduler.cost_report import _generate_and_post
                    _generate_and_post(config, channel=channel, thread_ts=thread_ts)
                except Exception as e:
                    log.error(f"On-demand cost report failed: {e}", exc_info=True)
                    say(text=f"❌ Cost report failed: {str(e)[:200]}", thread_ts=thread_ts)
            threading.Thread(target=run_cost_report, daemon=True).start()
            return

        # learn [category] <fact>
        if question_lower.startswith("learn "):
            rest = question[6:].strip()
            parts = rest.split(" ", 1)
            known = learnings_manager._all_categories()
            if len(parts) >= 2 and parts[0].lower() in known:
                cat, fact = parts[0].lower(), parts[1]
            else:
                cat, fact = "general", rest
            learnings_manager.append(cat, fact)
            say(text=f"✅ Learned ({cat}): {fact}", thread_ts=thread_ts)
            return

        # forget [category] <keyword>
        if question_lower.startswith("forget "):
            rest = question[7:].strip()
            known = learnings_manager._all_categories()
            parts = rest.split(" ", 1)
            if len(parts) >= 2 and parts[0].lower() in known:
                cat, keyword = parts[0].lower(), parts[1]
            else:
                cat, keyword = "general", rest
                # Try all categories
                total = 0
                for c in known:
                    total += learnings_manager.forget(c, keyword)
                say(text=f"🗑️ Removed {total} fact(s) matching '{keyword}' across all categories", thread_ts=thread_ts)
                return
            removed = learnings_manager.forget(cat, keyword)
            say(text=f"🗑️ Removed {removed} fact(s) matching '{keyword}' from {cat}", thread_ts=thread_ts)
            return

        # oracle stop — end the session for this thread
        if question_lower in ("oracle stop", "oracle end", "oracle quit"):
            with _oracle_lock:
                session = _oracle_sessions.pop(thread_ts, None)
            if session:
                session_id = session["session_id"]
                say(text=f"🔮 Oracle session ended. Resume later with `oracle resume {session_id}`", thread_ts=thread_ts)
            else:
                say(text="No active oracle session in this thread.", thread_ts=thread_ts)
            return

        # oracle resume <session_id>
        if question_lower.startswith("oracle resume "):
            session_id = question[len("oracle resume "):].strip()
            try:
                from vishwakarma.storage.queries import load_oracle_session
                history = load_oracle_session(session_id) or []
                with _oracle_lock:
                    _oracle_sessions[thread_ts] = {"session_id": session_id, "history": history, "_created": time.time()}
                say(text=f"🔮 Oracle session resumed (`{session_id[:8]}...`) — {len(history)} messages in history. Ask your next question.", thread_ts=thread_ts)
            except Exception as e:
                say(text=f"❌ Could not resume session: {e}", thread_ts=thread_ts)
            return

        # oracle <question> — start or continue oracle session
        with _oracle_lock:
            is_oracle = question_lower.startswith("oracle ") or thread_ts in _oracle_sessions
        if is_oracle:
            if question_lower.startswith("oracle "):
                oracle_question = question[len("oracle "):].strip()
            else:
                oracle_question = question  # follow-up in existing oracle thread

            # Init session if new
            with _oracle_lock:
                if thread_ts not in _oracle_sessions:
                    import uuid
                    session_id = str(uuid.uuid4())
                    _oracle_sessions[thread_ts] = {"session_id": session_id, "history": [], "_created": time.time()}
                    new_session = True
                else:
                    session_id = _oracle_sessions[thread_ts]["session_id"]
                    new_session = False
            if new_session:
                say(text=f"🔮 *Oracle session started* (`{session_id[:8]}...`)\nI'll remember context across your follow-up questions in this thread.", thread_ts=thread_ts)

            say(text=f":mag: Investigating: *{oracle_question[:100]}*...", thread_ts=thread_ts)

            def run_oracle(q=oracle_question, sid=session_id, t_ts=thread_ts):
                nonlocal _active_investigations
                with _inv_lock:
                    if _active_investigations >= _investigation_limit:
                        say(text="Investigation queue full, try again in a few minutes.", thread_ts=t_ts)
                        return
                    _active_investigations += 1

                from slack_sdk import WebClient
                client_sdk = WebClient(token=config.slack_bot_token)
                status_ts = None

                try:
                    with _oracle_lock:
                        session = _oracle_sessions.get(t_ts, {"session_id": sid, "history": []})
                        history = list(session["history"])  # copy to avoid mutation under lock

                    llm = config.make_llm()
                    engine = config.make_engine(llm=llm, toolset_manager=toolset_manager)
                    injected = learnings_manager.for_alert(q)
                    extra = (
                        (f"## Relevant Learnings (pre-loaded)\n{injected}\n\n" if injected else "")
                        + "## Learned Knowledge\n"
                        "Use `learnings_list` to see available learned knowledge categories, "
                        "then `learnings_read(category)` to load the ones relevant to this investigation. "
                        "Do this early in your investigation."
                    )

                    # Post a live status message we'll keep updating with tool calls
                    status_resp = client_sdk.chat_postMessage(
                        channel=channel,
                        thread_ts=t_ts,
                        text="⏳ Starting investigation...",
                        blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "⏳ _Starting investigation..._"}]}],
                    )
                    status_ts = status_resp["ts"]

                    tool_lines: list[str] = []
                    analysis = ""

                    for event in engine.stream_investigate(question=q, extra_system_prompt=extra, history=history):
                        etype = event.get("type", "")

                        if etype == "tool_call_start":
                            tool = event.get("tool", "")
                            params = event.get("params", {})
                            param_str = _short_oracle_params(params)
                            tool_lines.append(f"⚙ `{tool}({param_str})`")
                            # Update status message — show last 10 tool calls
                            visible = tool_lines[-10:]
                            status_text = "\n".join(visible)
                            try:
                                client_sdk.chat_update(
                                    channel=channel,
                                    ts=status_ts,
                                    text=status_text,
                                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]}],
                                )
                            except Exception as _e:
                                log.debug(f"Status update failed (non-fatal): {_e}")

                        elif etype == "tool_call_result":
                            status = event.get("status", "")
                            marker = "✓" if status == "success" else "✗"
                            tool_name = event.get("tool", "")
                            for i in range(len(tool_lines) - 1, -1, -1):
                                if tool_name and f"`{tool_name}(" in tool_lines[i] and "✓" not in tool_lines[i] and "✗" not in tool_lines[i]:
                                    tool_lines[i] = tool_lines[i] + f"  {marker}"
                                    break
                            else:
                                if tool_lines:
                                    tool_lines[-1] = tool_lines[-1] + f"  {marker}"
                            visible = tool_lines[-10:]
                            status_text = "\n".join(visible)
                            try:
                                client_sdk.chat_update(
                                    channel=channel,
                                    ts=status_ts,
                                    text=status_text,
                                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]}],
                                )
                            except Exception as _e:
                                log.debug(f"Status update failed (non-fatal): {_e}")

                        elif etype == "max_steps_reached":
                            analysis = event.get("content", "") or "Investigation reached maximum steps without a final conclusion."

                        elif etype == "done":
                            analysis = event.get("content", "") or analysis or ""
                            # Update history from full messages if available
                            full_messages = event.get("messages")
                            if full_messages:
                                new_history = [m for m in full_messages if m.get("role") != "system"]
                            else:
                                new_history = list(history)
                                new_history.append({"role": "user", "content": q})
                                new_history.append({"role": "assistant", "content": analysis})

                            with _oracle_lock:
                                _oracle_sessions[t_ts] = {"session_id": sid, "history": new_history, "_created": time.time()}

                            # Persist to SQLite
                            try:
                                from vishwakarma.storage.queries import save_oracle_session
                                save_oracle_session(sid, new_history)
                            except Exception as e:
                                log.warning(f"Oracle session save failed: {e}")

                            # Finalise status message with tool summary
                            tool_count = len(tool_lines)
                            try:
                                client_sdk.chat_update(
                                    channel=channel,
                                    ts=status_ts,
                                    text=f"🔍 {tool_count} tool calls completed",
                                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": f"🔍 _{tool_count} tool calls · done_"}]}],
                                )
                            except Exception as _e:
                                log.debug(f"Status update failed (non-fatal): {_e}")

                            # Post the analysis in chunks (converted to Slack mrkdwn)
                            from vishwakarma.utils.slack_format import md_to_slack, chunk_for_slack, strip_code_wrapper
                            slack_text = md_to_slack(strip_code_wrapper(analysis))
                            chunks = chunk_for_slack(slack_text)
                            for chunk in chunks:
                                client_sdk.chat_postMessage(
                                    channel=channel,
                                    thread_ts=t_ts,
                                    text=chunk,
                                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                                )

                            # Turn hint
                            turn = len(new_history) // 2
                            client_sdk.chat_postMessage(
                                channel=channel,
                                thread_ts=t_ts,
                                text=f"Turn {turn} — ask a follow-up or @oogway oracle stop to end",
                                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Turn {turn} · Session `{sid[:8]}...` · Ask a follow-up or `@oogway oracle stop` to end_"}]}],
                            )

                except Exception as e:
                    log.error(f"Oracle investigation failed: {e}", exc_info=True)
                    if status_ts:
                        try:
                            client_sdk.chat_update(
                                channel=channel, ts=status_ts, text="❌ Investigation failed",
                                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "❌ _Investigation failed_"}]}],
                            )
                        except Exception:
                            pass
                    try:
                        say(text=f"❌ Oracle failed: {str(e)[:200]}", thread_ts=t_ts)
                    except Exception:
                        pass
                finally:
                    with _inv_lock:
                        _active_investigations -= 1

            threading.Thread(target=run_oracle, daemon=True).start()
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
            nonlocal _active_investigations
            with _inv_lock:
                if _active_investigations >= _investigation_limit:
                    say(text="Investigation queue full, try again in a few minutes.", thread_ts=thread_ts)
                    return
                _active_investigations += 1

            from slack_sdk import WebClient
            client_sdk = WebClient(token=config.slack_bot_token)
            status_ts = None

            try:
                llm = config.make_llm()
                engine = config.make_engine(llm=llm, toolset_manager=toolset_manager)
                injected = learnings_manager.for_alert(question)
                extra = (
                    (f"## Relevant Learnings (pre-loaded)\n{injected}\n\n" if injected else "")
                    + "## Learned Knowledge\n"
                    "Use `learnings_list` to see available learned knowledge categories, "
                    "then `learnings_read(category)` to load the ones relevant to this investigation. "
                    "Do this early in your investigation."
                )

                # Post a live status message for streaming tool call updates
                status_resp = client_sdk.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="⏳ Starting investigation...",
                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "⏳ _Starting investigation..._"}]}],
                )
                status_ts = status_resp["ts"]

                tool_lines: list[str] = []
                analysis = ""

                for event in engine.stream_investigate(question=full_question, extra_system_prompt=extra):
                    etype = event.get("type", "")

                    if etype == "tool_call_start":
                        tool = event.get("tool", "")
                        params = event.get("params", {})
                        param_str = _short_oracle_params(params)
                        tool_lines.append(f"⚙ `{tool}({param_str})`")
                        visible = tool_lines[-10:]
                        status_text = "\n".join(visible)
                        try:
                            client_sdk.chat_update(
                                channel=channel, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]}],
                            )
                        except Exception:
                            pass

                    elif etype == "tool_call_result":
                        status = event.get("status", "")
                        marker = "✓" if status == "success" else "✗"
                        tool_name = event.get("tool", "")
                        for i in range(len(tool_lines) - 1, -1, -1):
                            if tool_name and f"`{tool_name}(" in tool_lines[i] and "✓" not in tool_lines[i] and "✗" not in tool_lines[i]:
                                tool_lines[i] = tool_lines[i] + f"  {marker}"
                                break
                        else:
                            if tool_lines:
                                tool_lines[-1] = tool_lines[-1] + f"  {marker}"
                        visible = tool_lines[-10:]
                        status_text = "\n".join(visible)
                        try:
                            client_sdk.chat_update(
                                channel=channel, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": status_text}]}],
                            )
                        except Exception:
                            pass

                    elif etype == "max_steps_reached":
                        analysis = event.get("content", "") or "Investigation reached maximum steps without a final conclusion."

                    elif etype == "done":
                        analysis = event.get("content", "") or analysis or ""

                        # Finalise status message
                        tool_count = len(tool_lines)
                        try:
                            client_sdk.chat_update(
                                channel=channel, ts=status_ts,
                                text=f"🔍 {tool_count} tool calls completed",
                                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": f"🔍 _{tool_count} tool calls · done_"}]}],
                            )
                        except Exception:
                            pass

                # Generate PDF
                pdf_path = None
                try:
                    from vishwakarma.bot.pdf import generate_pdf
                    pdf_path = generate_pdf(
                        title=question[:80],
                        analysis=analysis,
                        source="slack",
                    )
                except Exception as e:
                    log.warning(f"PDF generation failed: {e}")

                # Save to DB for feedback buttons
                inc_id = None
                try:
                    from vishwakarma.storage.queries import save_incident
                    import hashlib
                    inc_id = hashlib.md5(f"slack:{question}:{time.time()}".encode()).hexdigest()
                    save_incident(
                        incident_id=inc_id,
                        title=question[:200],
                        question=question,
                        analysis=analysis,
                        source="slack",
                        labels={"slack_user": user, "slack_channel": channel},
                        tool_outputs=[],
                        meta={},
                        slack_ts=thread_ts,
                        pdf_path=pdf_path,
                    )
                except Exception as e:
                    log.warning(f"DB save failed: {e}")

                # Post PDF in thread (no text dump — PDF is the deliverable)
                if pdf_path:
                    try:
                        import os
                        with open(pdf_path, "rb") as f:
                            pdf_content = f.read()
                        file_resp = client_sdk.files_upload_v2(
                            channel=channel,
                            thread_ts=thread_ts,
                            content=pdf_content,
                            filename=f"rca-{question[:40].replace(' ', '-')}.pdf",
                            title=f"RCA - {question[:80]}",
                            initial_comment=f":page_facing_up: *Investigation complete — {question[:80]}*",
                        )
                    except Exception as e:
                        log.warning(f"PDF upload failed, posting text instead: {e}")
                        pdf_path = None

                # Fallback: if PDF failed, post analysis as text
                if not pdf_path:
                    from vishwakarma.utils.slack_format import md_to_slack, chunk_for_slack, strip_code_wrapper
                    slack_text = md_to_slack(strip_code_wrapper(analysis))
                    for chunk in chunk_for_slack(slack_text):
                        client_sdk.chat_postMessage(
                            channel=channel, thread_ts=thread_ts, text=chunk,
                            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                        )

                # Hint for saving learnings (small context block like oracle)
                if analysis:
                    try:
                        category = _infer_category(question, config=config, fact=analysis[:200])
                        client_sdk.chat_postMessage(
                            channel=channel, thread_ts=thread_ts,
                            text=f"Save learning: @oogway learn {category} <your finding>",
                            blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Save learning · `@oogway learn {category} <your finding>`_"}]}],
                        )
                    except Exception:
                        pass

            except Exception as e:
                log.error(f"Investigation failed: {e}", exc_info=True)
                if status_ts:
                    try:
                        client_sdk.chat_update(
                            channel=channel, ts=status_ts, text="❌ Investigation failed",
                            blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": "❌ _Investigation failed_"}]}],
                        )
                    except Exception:
                        pass
                try:
                    say(text=f"❌ Investigation failed: {str(e)[:200]}", thread_ts=thread_ts)
                except Exception:
                    pass
            finally:
                with _inv_lock:
                    _active_investigations -= 1

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
        bot_id = event.get("bot_id", "")
        subtype = event.get("subtype", "")
        is_thread_reply = bool(event.get("thread_ts") and event.get("thread_ts") != event.get("ts"))
        n_attachments = len(event.get("attachments", []))

        log.info(f"[MSG] channel_type={channel_type} bot_id={bot_id!r} subtype={subtype!r} thread_reply={is_thread_reply} attachments={n_attachments} text={text!r}")

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

        # Also pull in attachment text — Amazon Q posts alarm details in attachments, not body
        for att in event.get("attachments", []):
            text += "\n" + att.get("text", "") + "\n" + att.get("fallback", "")
        text = text.strip()

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

    # ── RCA Feedback handlers ─────────────────────────────────────────────────

    @app.action("vk_rca_correct")
    def handle_rca_correct(ack, body, client):
        ack()
        incident_id = body["actions"][0]["value"]
        user = body.get("user", {}).get("id", "unknown")
        channel_id = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        try:
            from vishwakarma.storage.queries import get_incident
            incident = get_incident(incident_id)
            if not incident:
                return

            analysis = incident.get("analysis", "")
            alert_name = (incident.get("labels") or {}).get("alertname") or incident.get("title", "")

            # Pass full analysis — LLM will extract key insight from the whole RCA
            existing = learnings_manager.get(_infer_category(alert_name))
            fact = _distill_fact(config, analysis, alert_name, existing_facts=existing)
            if not fact:
                client.chat_update(
                    channel=channel_id, ts=msg_ts,
                    text="✅ Already in learnings",
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *<@{user}>* — this root cause is already captured in learnings."}}],
                )
                return
            category = _infer_category(alert_name, config=config, fact=fact)
            learnings_manager.append(category, fact)
            log.info(f"[FEEDBACK] ✅ Appended to learnings[{category}]: {fact[:80]}")
            # Compact if category has grown large
            llm = config.make_llm()
            learnings_manager.compact(category, llm.summarize)

            # Replace buttons with confirmation
            client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=f"✅ RCA marked correct by <@{user}>",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"✅ *Marked correct by <@{user}>* — root cause added to `{category}` learnings."},
                }],
            )
        except Exception as e:
            log.error(f"[FEEDBACK] vk_rca_correct failed: {e}", exc_info=True)

    @app.action("vk_rca_wrong")
    def handle_rca_wrong(ack, body, client):
        ack()
        import json as _json
        incident_id = body["actions"][0]["value"]
        private_metadata = _json.dumps({
            "incident_id": incident_id,
            "channel": body["channel"]["id"],
            "msg_ts": body["message"]["ts"],
        })
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "vk_rca_wrong_modal",
                    "private_metadata": private_metadata,
                    "title": {"type": "plain_text", "text": "Incorrect RCA"},
                    "submit": {"type": "plain_text", "text": "Save to Learnings"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "real_cause",
                            "label": {"type": "plain_text", "text": "What was the real root cause?"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "real_cause_input",
                                "multiline": True,
                                "placeholder": {"type": "plain_text", "text": "e.g. OOM kill due to memory leak in payment service v2.3.1 — not Redis eviction as concluded"},
                            },
                        }
                    ],
                },
            )
        except Exception as e:
            log.error(f"[FEEDBACK] vk_rca_wrong modal open failed: {e}", exc_info=True)

    @app.view("vk_rca_wrong_modal")
    def handle_rca_wrong_submit(ack, body, client):
        ack()
        import json as _json
        user = body.get("user", {}).get("id", "unknown")
        real_cause = body["view"]["state"]["values"]["real_cause"]["real_cause_input"]["value"] or ""

        try:
            meta = _json.loads(body["view"].get("private_metadata") or "{}")
            incident_id = meta.get("incident_id", "")
            channel_id = meta.get("channel", "")
            msg_ts = meta.get("msg_ts", "")

            from vishwakarma.storage.queries import get_incident
            incident = get_incident(incident_id) if incident_id else None
            alert_name = (incident.get("labels") or {}).get("alertname") if incident else ""

            # Distill correction into a clean fact, deduped against existing learnings
            existing = learnings_manager.get(_infer_category(alert_name or ""))
            fact = _distill_fact(config, real_cause, alert_name or "", correction=True, existing_facts=existing)
            if not fact:
                # Duplicate — remove buttons from original message
                if channel_id and msg_ts:
                    client.chat_update(
                        channel=channel_id, ts=msg_ts,
                        text="✅ Already in learnings",
                        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *<@{user}>* — this correction is already captured in learnings."}}],
                    )
                return
            category = _infer_category(alert_name or "", config=config, fact=fact)
            learnings_manager.append(category, fact)
            llm = config.make_llm()
            learnings_manager.compact(category, llm.summarize)
            log.info(f"[FEEDBACK] ❌ Correction appended to learnings[{category}]: {fact[:80]}")

            # Update the original feedback message
            if channel_id and msg_ts:
                client.chat_update(
                    channel=channel_id,
                    ts=msg_ts,
                    text=f"❌ RCA corrected by <@{user}>",
                    blocks=[{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"❌ *Corrected by <@{user}>* — real cause added to `{category}` learnings."},
                    }],
                )
        except Exception as e:
            log.error(f"[FEEDBACK] vk_rca_wrong_submit failed: {e}", exc_info=True)

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
                    "TONE MATCHING: Mirror the user's communication style precisely. "
                    "If the user writes casually (slang, abbreviations, typos, short sentences, emojis) → reply casually and conversationally. "
                    "If the user writes formally (proper grammar, full sentences, professional language) → reply formally and professionally. "
                    "If the user is somewhere in between → match that middle ground. Never be stiff when someone is casual, never be sloppy when someone is formal. "
                    "Answer concisely and helpfully. "
                    "If asked to investigate or debug something deeply, tell the user to use "
                    "`@oogway debug <question>` for a full investigation with tools and PDF report."
                ),
            },
            {"role": "user", "content": question},
        ],
        max_tokens=1024,
        temperature=0.7,
        timeout=30,
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
    Clean up a Slack message for investigation.

    Keep the full multi-line message — users often paste ride IDs, DB results,
    JSON, or error logs as part of their question. Only strip Slack thread noise
    like 'replied to a thread:' or Amazon Q boilerplate appended after a separator.
    """
    # Strip Slack "replied to a thread:" boilerplate that appears after a blank line
    # followed by quoted content. Keep everything the user intentionally typed.
    noise_markers = [
        "\nreplied to a thread:",
        "\nAlso sent to the channel",
    ]
    for marker in noise_markers:
        idx = text.lower().find(marker.lower())
        if idx != -1:
            text = text[:idx]
    return text.strip()


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
    return (
        ":turtle: *Oogway — Autonomous SRE Investigation Bot*\n\n"

        "*:mag: Investigation*\n"
        "• `debug <question>` — full investigation with tools + PDF report\n"
        "• `debug <ride/booking UUID>` — traces ride across BAP/BPP databases\n"
        "• `oracle <question>` — multi-turn session (follow-ups remember context)\n"
        "• `oracle stop` — end the oracle session\n"
        "• `oracle resume <id>` — resume a previous session\n\n"

        "*:bar_chart: Reports*\n"
        "• `costs` — AWS cost report with anomaly detection + forecast + PDF\n"
        "• `status` — incident statistics\n\n"

        "*:brain: Knowledge*\n"
        "• `learn [category] <fact>` — teach me something new\n"
        "• `forget [category] <keyword>` — remove a learning\n\n"

        "*:speech_balloon: Chat*\n"
        "• `<anything else>` — quick chat reply (no tools)\n"
        "• `help` — this message\n\n"

        "*Examples:*\n"
        "```@oogway debug why are payments pods crashing?\n"
        "@oogway debug why did ride f6d18e1e-... get cancelled?\n"
        "@oogway debug RDS CPU high on customer cluster\n"
        "@oogway oracle check RDS CPU spike from 10am\n"
        "@oogway costs\n"
        "@oogway learn rds atlas-customer-r1 often spikes during morning peak```"
    )


def _extract_root_cause(analysis: str) -> str:
    """Extract plain text from the ## Root Cause section of a markdown RCA."""
    import re
    match = re.search(r'##\s*Root Cause\s*\n(.*?)(?=\n##|\Z)', analysis, re.DOTALL | re.IGNORECASE)
    if match:
        # Strip markdown formatting from the extracted section
        text = match.group(1).strip()
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)   # bold
        text = re.sub(r'`(.+?)`', r'\1', text)           # inline code
        text = re.sub(r'#{1,6}\s*', '', text)             # headers
        return text[:1000]
    # Fallback: first 500 chars stripped of markdown
    text = re.sub(r'#{1,6}\s*\S+.*\n?', '', analysis)
    return text.strip()[:500]


def _extract_key_finding(analysis: str) -> str:
    """Extract a one-line key finding from the analysis for the learn command."""
    import re
    # Try Root Cause section first
    match = re.search(r'##\s*Root Cause\s*\n(.*?)(?=\n##|\Z)', analysis, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    else:
        # Try Summary section
        match = re.search(r'##\s*Summary\s*\n(.*?)(?=\n##|\Z)', analysis, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
        else:
            text = analysis.strip()
    # Clean markdown and take first sentence
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\n+', ' ', text).strip()
    # First sentence
    sentence = re.split(r'(?<=[.!?])\s', text)[0].strip()
    return sentence[:200] if sentence else text[:200]


def _infer_category(alert_name: str, config=None, fact: str = "") -> str:
    """
    Pick the best learning category for an alert using the fast LLM.
    Falls back to keyword matching if LLM is unavailable.
    """
    from vishwakarma.core.learnings import LearningsManager, _ALERT_CATEGORY_KEYWORDS
    lm = LearningsManager()
    existing = [c["category"] for c in lm.list_categories()]

    if config:
        try:
            import litellm, re
            model = config.llm.fast_model or config.llm.model
            prompt = (
                f"You are categorizing an incident learning fact.\n\n"
                f"Alert: {alert_name}\n"
                f"Fact: {fact[:300]}\n\n"
                f"Existing categories: {', '.join(existing)}\n\n"
                f"Reply with ONLY a single category name (lowercase, no spaces — use underscores). "
                f"Pick an existing one if it fits well. Create a new short name if none fit. "
                f"Examples: redis, rds, kubernetes, networking, payments, drainer"
            )
            resp = litellm.completion(
                model=model,
                api_key=config.llm.api_key,
                api_base=config.llm.api_base,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0.0,
                timeout=15,
            )
            cat = (resp.choices[0].message.content or "").strip().lower()
            cat = re.sub(r"[^a-z0-9_-]", "", cat)[:64]
            if cat:
                # Create category if new
                if cat not in existing:
                    lm.create(cat)
                    log.info(f"[FEEDBACK] Created new learnings category: {cat}")
                return cat
        except Exception as e:
            log.warning(f"[FEEDBACK] LLM category inference failed: {e} — falling back to keywords")

    # Keyword fallback
    alert_lower = alert_name.lower()
    for cat, keywords in _ALERT_CATEGORY_KEYWORDS.items():
        if any(kw in alert_lower for kw in keywords):
            return cat
    return "general"


def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity on words >3 chars — used for fast dedup."""
    wa = set(w.lower() for w in a.split() if len(w) > 3)
    wb = set(w.lower() for w in b.split() if len(w) > 3)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _is_programmatic_duplicate(text: str, existing_facts: str) -> bool:
    """Return True if text overlaps >55% with any existing fact line."""
    for line in existing_facts.splitlines():
        if line.strip().startswith("- "):
            if _word_overlap(text, line[2:]) > 0.55:
                return True
    return False


def _distill_fact(config, text: str, alert_name: str, correction: bool = False, existing_facts: str = "") -> str:
    """
    Distill a root cause (or correction) into a concise, reusable learning fact.
    Deduplicates against existing facts (programmatic fast-path + LLM semantic check).
    Returns empty string if the fact is already covered.
    """
    # Fast programmatic dedup — catches exact or near-exact repeats before touching LLM
    if existing_facts and _is_programmatic_duplicate(text, existing_facts):
        log.info("[FEEDBACK] Programmatic dedup: fact already covered, skipping")
        return ""

    try:
        import litellm, json as _json, re as _re
        prefix = "Correction" if correction else "Finding"

        if correction:
            # ❌ Wrong — user correction: fix typos, one clean sentence
            prompt = (
                f"Alert: {alert_name}\n"
                f"Text: {text[:400]}\n\n"
                f'Respond with JSON only: {{"summary": "one sentence, typos fixed"}}'
            )
        else:
            # ✅ Correct — extract actionable debugging insight from full RCA
            # Strip markdown so model focuses on content
            clean = _re.sub(r'\*\*(.+?)\*\*', r'\1', text)
            clean = _re.sub(r'`(.+?)`', r'\1', clean)
            clean = _re.sub(r'^[-*]\s+', '', clean, flags=_re.MULTILINE)
            clean = _re.sub(r'\n{2,}', ' ', clean).strip()
            prompt = (
                f"Alert: {alert_name}\n"
                f"Full RCA:\n{clean[:1200]}\n\n"
                f'Respond with JSON only: {{"summary": "one concise sentence capturing: what caused it, how it was confirmed, and what to check first next time this alert fires"}}'
            )

        resp = litellm.completion(
            model=config.llm.model,
            api_key=config.llm.api_key,
            api_base=config.llm.api_base,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.0,
            timeout=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = _re.search(r'\{[^{}]*"summary"[^{}]*\}', raw, _re.DOTALL)
        summary = _json.loads(m.group()).get("summary", "").strip() if m else ""
        if not summary:
            raise ValueError("no summary in JSON")
        fact = f"{prefix}: {summary}"

        # Dedup — check cleaned fact against existing
        if existing_facts and _is_programmatic_duplicate(fact, existing_facts):
            log.info("[FEEDBACK] Semantic dedup: distilled fact already covered, skipping")
            return ""

        return fact
    except Exception as e:
        log.warning(f"[FEEDBACK] Distillation failed: {e} — falling back to first sentence")
        import re as _re
        prefix = "Correction" if correction else "Finding"
        first = _re.split(r'(?<=[.!?])\s', text.strip())[0].strip(" -•'\"\t\n")[:200]
        if not first:
            return ""
        first = first[0].upper() + first[1:]
        if first[-1] not in ".!?":
            first += "."
        return f"{prefix}: {first}"


def _short_oracle_params(params: dict, max_len: int = 60) -> str:
    """Compact param string for live tool call display in Slack."""
    parts = []
    for k, v in params.items():
        val = str(v)
        if len(val) > 30:
            val = val[:27] + "..."
        parts.append(f"{k}={val!r}")
    result = ", ".join(parts)
    return result[:max_len - 3] + "..." if len(result) > max_len else result


def _format_stats(stats: dict) -> str:
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    lines = [f"Total incidents: *{total}*"]
    for status, count in by_status.items():
        lines.append(f"  • {status}: {count}")
    return "\n".join(lines)
