"""
Vishwakarma FastAPI server.

Endpoints:
  POST /api/investigate      — main investigation endpoint
  POST /api/alertmanager     — AlertManager webhook (dedup + PDF + Slack)
  GET  /api/model            — list available LLM config
  GET  /api/incidents        — list incidents from storage
  GET  /api/incidents/{id}   — get incident details
  GET  /api/stats            — investigation statistics
  POST /api/checks/execute   — run a health check
  GET  /healthz              — liveness probe
  GET  /readyz               — readiness probe (toolset health)
"""
import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

# In-memory set of fingerprints currently being investigated.
# Skip only if investigation is RUNNING — clear immediately on completion.
# Matches Holmes behavior: no time-based window.
_active_fingerprints: set[str] = set()
_active_fingerprints_lock = threading.Lock()

# Global concurrency limit — max simultaneous investigations.
# Alerts beyond this limit queue and wait rather than running in parallel.
# Prevents LLM rate limits, memory pressure, and tool contention under alert storms.
# Override via VK_MAX_CONCURRENT_INVESTIGATIONS env var.
MAX_CONCURRENT_INVESTIGATIONS = int(os.environ.get("VK_MAX_CONCURRENT_INVESTIGATIONS", "2"))
_investigation_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore():
    import asyncio
    global _investigation_semaphore
    if _investigation_semaphore is None:
        _investigation_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INVESTIGATIONS)
    return _investigation_semaphore

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from vishwakarma.core.models import (
    ApprovalDecision,
    InvestigateRequest,
    InvestigationResult,
    LLMResult,
    ToolOutput,
    ToolStatus,
)
from vishwakarma.utils.log import suppress_probe_logs
from vishwakarma.utils.stream import sse_event, sse_done

log = logging.getLogger(__name__)


def create_app(config=None) -> FastAPI:
    """Create and configure the FastAPI application."""
    from vishwakarma.config import VishwakarmaConfig

    if config is None:
        config = VishwakarmaConfig.load()

    app = FastAPI(
        title="Vishwakarma",
        description="Autonomous SRE Investigation Agent",
        version="1.0.0",
        docs_url="/api/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    suppress_probe_logs()

    # Initialize toolset manager and storage once at startup
    _state: dict[str, Any] = {}

    from vishwakarma.ui.routes import create_ui_router
    app.include_router(create_ui_router(_state))

    @app.on_event("startup")
    async def startup():
        from vishwakarma.storage.db import init_db
        init_db(config.db_path)
        from vishwakarma.core.learnings import LearningsManager
        _state["learnings"] = LearningsManager()
        _state["toolset_manager"] = config.make_toolset_manager()
        _state["toolset_manager"].check_all()

        log.info("Vishwakarma server ready")

    # ── /healthz ──────────────────────────────────────────────────────────────

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    # ── /readyz ───────────────────────────────────────────────────────────────

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        tm = _state.get("toolset_manager")
        if not tm:
            return Response(status_code=503, content="not ready")
        return {"status": "ready", "toolsets": len(tm.active_toolsets())}

    # ── /api/investigate ──────────────────────────────────────────────────────

    @app.post("/api/investigate")
    async def investigate(request: InvestigateRequest):
        """Main investigation endpoint — runs in thread to avoid blocking event loop."""
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")

        llm = config.make_llm()
        engine = config.make_engine(llm=llm, toolset_manager=tm)

        def _run():
            return engine.investigate(
                question=request.question,
                history=request.history,
                extra_system_prompt=request.extra_system_prompt,
                images=request.images,
                files=request.files,
                runbooks=request.runbooks,
                require_approval=request.require_approval,
                approval_decisions=request.approval_decisions,
                bash_always_allow=request.bash_always_allow,
                bash_always_deny=request.bash_always_deny,
                sections_off=request.prompt_overrides,
                response_schema=request.response_schema,
            )

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            log.error(f"Investigation failed: {e}", exc_info=True)
            raise HTTPException(500, str(e))

        return InvestigationResult(
            analysis=result.answer,
            tool_outputs=result.tool_outputs,
            history=result.messages,
            meta=result.meta,
            pending_approvals=result.pending_approvals,
        )

    # ── /api/investigate/stream ────────────────────────────────────────────────

    @app.post("/api/investigate/stream")
    async def investigate_stream(request: InvestigateRequest):
        """Streaming investigation endpoint — returns SSE.

        Runs the blocking generator in a thread and bridges events to the
        async world via a queue so the event loop stays unblocked.
        """
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")

        llm = config.make_llm()
        engine = config.make_engine(llm=llm, toolset_manager=tm)

        _SENTINEL = object()
        q: queue.Queue = queue.Queue()

        def _produce():
            try:
                for event in engine.stream_investigate(
                    question=request.question,
                    history=request.history,
                    extra_system_prompt=request.extra_system_prompt,
                    images=request.images,
                    runbooks=request.runbooks,
                    require_approval=request.require_approval,
                    approval_decisions=request.approval_decisions,
                    bash_always_allow=request.bash_always_allow,
                    bash_always_deny=request.bash_always_deny,
                ):
                    q.put(event)
            except Exception as e:
                q.put(e)
            finally:
                q.put(_SENTINEL)

        threading.Thread(target=_produce, daemon=True).start()

        async def event_stream() -> AsyncGenerator[str, None]:
            loop = asyncio.get_event_loop()
            while True:
                item = await loop.run_in_executor(None, q.get)
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    log.error(f"Stream investigation error: {item}", exc_info=True)
                    yield sse_event("error", {"message": str(item)})
                    break
                yield sse_event(item.get("type", "event"), item)
            yield sse_done()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── /api/alertmanager ─────────────────────────────────────────────────────

    @app.post("/api/alertmanager")
    async def alertmanager_webhook(request: Request):
        """
        AlertManager webhook receiver.
        Deduplicates, triggers background investigation, posts to Slack.
        """
        import asyncio

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        from vishwakarma.plugins.channels.alertmanager.plugin import parse_alertmanager_webhook
        from vishwakarma.storage.queries import save_incident, alert_fingerprint

        issues = parse_alertmanager_webhook(payload)
        if not issues:
            return {"status": "no_issues"}

        triggered = []
        for issue in issues:
            fingerprint = alert_fingerprint(issue.labels)

            # Skip only if an investigation for this alert is currently running (Holmes pattern)
            with _active_fingerprints_lock:
                if fingerprint in _active_fingerprints:
                    log.info(f"Alert deduplicated (investigation in progress): {issue.title}")
                    triggered.append({"title": issue.title, "status": "deduplicated"})
                    continue
                _active_fingerprints.add(fingerprint)

            incident_id = str(uuid.uuid4())

            # Background investigation
            asyncio.create_task(
                _run_alert_investigation(config, _state, issue, incident_id, fingerprint)
            )
            triggered.append({"title": issue.title, "status": "investigating", "incident_id": incident_id})

        return {"status": "ok", "alerts": triggered}

    # ── /api/model ────────────────────────────────────────────────────────────

    @app.get("/api/model")
    async def get_model():
        return {
            "model": config.llm.model,
            "api_base": config.llm.api_base,
            "max_tokens": config.llm.max_tokens,
            "cluster": config.cluster_name,
        }

    # ── /api/incidents ────────────────────────────────────────────────────────

    @app.get("/api/incidents")
    async def list_incidents(
        source: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        if search:
            from vishwakarma.storage.queries import search_incidents
            return {"incidents": search_incidents(search, limit=limit)}
        from vishwakarma.storage.queries import list_incidents as _list
        return {"incidents": _list(source=source, status=status, limit=limit, offset=offset)}

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str):
        from vishwakarma.storage.queries import get_incident as _get
        inc = _get(incident_id)
        if not inc:
            raise HTTPException(404, f"Incident {incident_id} not found")
        return inc

    @app.get("/api/stats")
    async def stats():
        from vishwakarma.storage.queries import get_stats
        return get_stats()

    # ── /api/toolsets ─────────────────────────────────────────────────────────

    @app.get("/api/toolsets")
    async def list_toolsets():
        tm = _state.get("toolset_manager")
        if not tm:
            raise HTTPException(503, "Server not ready")
        return {
            "toolsets": [
                {
                    "name": ts.name,
                    "description": getattr(ts, "description", ""),
                    "enabled": ts.enabled,
                    "health": ts.health.value if ts.health else "unknown",
                }
                for ts in tm.all_toolsets()
            ]
        }

    return app


# ── Background investigation ───────────────────────────────────────────────────

async def _run_alert_investigation(config, state, issue, incident_id: str, fingerprint: str = ""):
    import asyncio

    semaphore = _get_semaphore()
    queue_pos = MAX_CONCURRENT_INVESTIGATIONS - semaphore._value
    if queue_pos >= MAX_CONCURRENT_INVESTIGATIONS:
        log.info(f"Alert queued (concurrency limit {MAX_CONCURRENT_INVESTIGATIONS} reached): {issue.title}")

    async with semaphore:
        await _do_investigation(config, state, issue, incident_id, fingerprint)


async def _do_investigation(config, state, issue, incident_id: str, fingerprint: str = ""):
    import asyncio

    tm = state.get("toolset_manager")
    llm = config.make_llm()
    engine = config.make_engine(llm=llm, toolset_manager=tm)

    # Scale investigation depth by alert severity
    _severity_steps = {"critical": 60, "high": 50, "warning": 40, "medium": 40, "low": 25, "info": 20}
    engine.max_steps = _severity_steps.get((issue.severity or "").lower(), config.max_steps)

    try:
        question = issue.question()
        alert_name = issue.labels.get("alertname") or issue.title

        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        from vishwakarma.config import load_matching_runbooks
        from vishwakarma.core.fast_rca import match_fast_rca, get_companion_checks, synthesize_fast_rca, format_slack_message

        # ── Post immediate acknowledgment to Slack ──
        ack_ts = None
        slack_channel_id = None
        slack_client = None
        if config.is_slack_configured():
            try:
                from vishwakarma.plugins.relays.slack.plugin import SlackDestination
                dest = SlackDestination({"token": config.slack_bot_token})
                slack_client = dest._get_client()
                slack_channel_id = dest._resolve_channel_id(
                    os.environ.get("SLACK_CHANNEL", "#sre-alerts")
                )
                severity_color = "#FF0000" if (issue.severity or "").lower() in ("critical", "high") else "#FFA500"
                ack_text = f":rotating_light: Investigating: {issue.title}"
                ack_blocks = [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f":rotating_light: {issue.title[:150]}", "emoji": True},
                    },
                    {"type": "divider"},
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": ":hourglass_flowing_sand: _Investigation in progress — fast RCA + full RCA with PDF will follow in this thread..._"}],
                    },
                ]
                resp = slack_client.chat_postMessage(
                    channel=slack_channel_id,
                    text=ack_text,
                    attachments=[{"color": severity_color, "blocks": ack_blocks}],
                )
                ack_ts = resp["ts"]
            except Exception as e:
                log.warning(f"Slack ack failed (non-fatal): {e}")

        # ── Launch fast RCA + pre-enrichment in parallel ──
        fast_match = match_fast_rca(alert_name)
        fast_rca_result = None
        fast_rca_ts = None
        fast_checks_raw: dict = {}  # raw check results for evidence extraction

        async def _run_fast_rca():
            """Fast RCA: parallel checks → LLM classify → post to Slack."""
            nonlocal fast_rca_result, fast_rca_ts
            if not fast_match:
                return
            toolset_name, tool_name, tool_params = fast_match
            fast_ts = tm.get(toolset_name)
            if not fast_ts or not fast_ts.enabled:
                return
            try:
                # Run primary + companion checks in parallel
                companions = get_companion_checks(tool_name)
                primary_future = loop.run_in_executor(
                    None, lambda: fast_ts.execute(tool_name, tool_params)
                )
                companion_futures = []
                for c_toolset, c_tool, c_params in companions:
                    c_ts = tm.get(c_toolset)
                    if c_ts and c_ts.enabled:
                        companion_futures.append(
                            loop.run_in_executor(
                                None, lambda ts=c_ts, t=c_tool, p=c_params: ts.execute(t, p)
                            )
                        )

                all_outputs = await _asyncio.gather(primary_future, *companion_futures, return_exceptions=True)

                # Merge all check results
                check_output = all_outputs[0]
                if isinstance(check_output, Exception) or check_output.status != ToolStatus.SUCCESS:
                    return
                checks = json.loads(check_output.output) if isinstance(check_output.output, str) else check_output.output
                merged_checks = dict(checks.get("checks", checks))

                # Merge companion results under prefixed keys
                for i, (c_toolset, c_tool, c_params) in enumerate(companions):
                    c_output = all_outputs[i + 1]
                    if isinstance(c_output, Exception) or c_output.status != ToolStatus.SUCCESS:
                        continue
                    c_data = json.loads(c_output.output) if isinstance(c_output.output, str) else c_output.output
                    for k, v in c_data.get("checks", c_data).items():
                        merged_checks[f"{c_tool}_{k}"] = v

                checks["checks"] = merged_checks
                nonlocal fast_checks_raw
                fast_checks_raw = merged_checks  # capture for evidence extraction
                fast_rca_result = await loop.run_in_executor(
                    None, lambda: synthesize_fast_rca(llm, merged_checks, alert_name)
                )
                # Post to Slack immediately — as thread reply under ack message
                if config.is_slack_configured() and fast_rca_result:
                    slack_text = format_slack_message(fast_rca_result, issue.title)
                    try:
                        if slack_client and slack_channel_id:
                            resp = slack_client.chat_postMessage(
                                channel=slack_channel_id,
                                text=slack_text,
                                thread_ts=ack_ts,
                            )
                            fast_rca_ts = resp["ts"]
                        else:
                            from vishwakarma.plugins.relays.slack.plugin import SlackDestination
                            dest = SlackDestination({"token": config.slack_bot_token})
                            channel = os.environ.get("SLACK_CHANNEL", "#sre-alerts")
                            resp = dest._get_client().chat_postMessage(
                                channel=dest._resolve_channel_id(channel),
                                text=slack_text,
                            )
                            fast_rca_ts = resp["ts"]
                    except Exception as e:
                        log.warning(f"Fast RCA Slack post failed: {e}")
                    log.info(f"Fast RCA posted for {alert_name} in {checks.get('elapsed_seconds', '?')}s")
            except Exception as e:
                log.warning(f"Fast RCA failed for {alert_name}: {e}")

        # Run fast RCA and all 4 pre-enrichment tasks in parallel
        fast_rca_future = _run_fast_rca()
        prefetch_future = loop.run_in_executor(None, _prefetch_alert_context, issue)
        prior_future = loop.run_in_executor(None, _build_prior_context, issue)
        entities_future = loop.run_in_executor(None, _extract_alert_entities, issue, llm)
        runbooks_future = loop.run_in_executor(None, load_matching_runbooks, alert_name, llm)

        # Wait for all to complete (fast RCA + 4 pre-enrichment tasks)
        _, prefetch_ctx, prior_ctx, entities_ctx, matched_runbooks = await _asyncio.gather(
            fast_rca_future, prefetch_future, prior_future, entities_future, runbooks_future
        )

        # Pre-inject learnings relevant to this alert
        learnings_mgr = state.get("learnings")
        learnings_ctx = learnings_mgr.for_alert(alert_name) if learnings_mgr else ""

        # Merge all pre-investigation context into extra_system_prompt
        extra_parts = [p for p in [entities_ctx, prefetch_ctx, prior_ctx, learnings_ctx] if p]

        # Inject fast RCA as starting context for deep investigation
        if fast_rca_result:
            extra_parts.insert(0,
                "## Fast RCA (preliminary, posted to Slack)\n"
                f"{json.dumps(fast_rca_result, indent=2)}\n"
                "Verify or refute this preliminary finding with deeper investigation. "
                "If it's correct, focus on root cause chain and remediation details. "
                "If your evidence contradicts it, explain why in your final RCA."
            )

        extra_parts.append(
            "## Learned Knowledge\n"
            "Relevant facts from past incidents are pre-injected above (if any). "
            "Use `learnings_list` + `learnings_read` only if you need categories not shown above."
        )
        extra_system_prompt = "\n\n".join(extra_parts) or None

        # ── Evidence-based auto-resolve: compare metrics against learned baselines ──
        auto_resolved = False
        evidence_metrics = {}
        try:
            from vishwakarma.storage.evidence import (
                extract_metrics_from_checks, store_evidence,
                should_auto_resolve, compare_against_baselines,
            )
            # Extract numeric metrics from fast RCA raw check results
            if fast_checks_raw:
                evidence_metrics = extract_metrics_from_checks(fast_checks_raw)

            if evidence_metrics:
                # Store evidence snapshot (outcome=pending until ✅/❌)
                store_evidence(
                    evidence_id=incident_id,
                    alert_name=alert_name,
                    metrics=evidence_metrics,
                    scenario=fast_rca_result.get("scenario", "") if fast_rca_result else "",
                    root_cause_type=fast_rca_result.get("root_cause", "")[:100] if fast_rca_result else "",
                    incident_id=incident_id,
                )
                log.info(f"Evidence stored: {len(evidence_metrics)} metrics for {alert_name}")

                # Check if we can auto-resolve
                can_resolve, reason = should_auto_resolve(
                    alert_name, evidence_metrics,
                    fast_rca_confidence=fast_rca_result.get("confidence", "") if fast_rca_result else "",
                )
                if can_resolve:
                    auto_resolved = True
                    from vishwakarma.core.models import LLMResult, InvestigationMeta
                    comparison = compare_against_baselines(alert_name, evidence_metrics)
                    analysis = (
                        f"## Auto-Resolved: Known Normal Pattern\n\n"
                        f"**{reason}**\n\n"
                        f"## Evidence Comparison\n"
                        f"{comparison['summary']}\n\n"
                        f"## Metric Details\n"
                    )
                    for n in comparison.get("normal", []):
                        analysis += f"- {n['metric']}: {n['value']} (baseline: {n['baseline_mean']}±{n['baseline_stddev']}, z={n['z_score']})\n"
                    if fast_rca_result:
                        analysis += f"\n## Fast RCA Classification\n{json.dumps(fast_rca_result, indent=2)}\n"
                    analysis += (
                        f"\n## Resolution\nNo action needed. This alert pattern has been confirmed normal "
                        f"{comparison.get('sample_count', '?')} times. "
                        f"Consider adjusting the alert threshold to reduce false alarms."
                    )
                    result = LLMResult(
                        answer=analysis,
                        tool_outputs=[],
                        messages=[],
                        meta=InvestigationMeta(steps=0),
                    )
                    log.info(f"Auto-resolved {alert_name}: {reason}")

                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=f":brain: Auto-resolved — all metrics within learned baselines",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": f":brain: _Auto-resolved: {reason[:200]}_"}
                                ]}],
                            )
                        except Exception:
                            pass
                else:
                    # Not auto-resolvable — inject anomaly info into investigation
                    comparison = compare_against_baselines(alert_name, evidence_metrics)
                    if comparison.get("anomalies"):
                        anomaly_text = "## Evidence Memory — Anomalies Detected\n"
                        anomaly_text += "These metrics are OUTSIDE learned baselines (from confirmed investigations):\n"
                        for a in comparison["anomalies"]:
                            anomaly_text += f"- **{a['metric']}** = {a['value']} (baseline: {a['baseline_mean']}±{a['baseline_stddev']}, z-score={a['z_score']})\n"
                        anomaly_text += "\nFocus investigation on these anomalous metrics first."
                        extra_parts.append(anomaly_text)
                        extra_system_prompt = "\n\n".join(extra_parts) or None
                        log.info(f"Anomalies injected for {alert_name}: {[a['metric'] for a in comparison['anomalies']]}")
        except Exception as e:
            log.debug(f"Evidence check failed (non-fatal): {e}")

        # ── Pattern replay: check if a confirmed pattern matches ──
        pattern_matched = False
        try:
            from vishwakarma.storage.patterns import get_patterns_for_alert, replay_pattern, mark_pattern_hit, mark_pattern_miss
            patterns = await loop.run_in_executor(
                None, lambda: get_patterns_for_alert(alert_name)
            )
            if patterns:
                # Try the most confirmed pattern first
                best = patterns[0]
                log.info(f"Found pattern for {alert_name}: {best['root_cause_type']} (hit_count={best['hit_count']})")

                # Post pattern replay status
                if slack_client and slack_channel_id and ack_ts:
                    try:
                        slack_client.chat_postMessage(
                            channel=slack_channel_id, thread_ts=ack_ts,
                            text=f":brain: Known pattern found: *{best['root_cause_type']}* (confirmed {best['hit_count']}x). Replaying investigation steps...",
                            blocks=[{"type": "context", "elements": [
                                {"type": "mrkdwn", "text": f":brain: _Known pattern: *{best['root_cause_type']}* (confirmed {best['hit_count']}x) — replaying {len(best['investigation_steps'])} steps..._"}
                            ]}],
                        )
                    except Exception:
                        pass

                validation = await loop.run_in_executor(
                    None, lambda: replay_pattern(best, engine.executor, llm, question, fast_rca_result=fast_rca_result)
                )
                if validation and validation.get("matched") and validation.get("confidence") in ("high", "medium"):
                    pattern_matched = True
                    mark_pattern_hit(best["id"], incident_id)
                    # Build instant RCA from pattern
                    analysis = (
                        f"## Root Cause\n{validation.get('root_cause', best['root_cause_detail'])}\n\n"
                        f"## Confidence: {validation.get('confidence', 'medium').upper()}\n"
                        f"Known pattern (confirmed {best['hit_count'] + 1}x). "
                        f"Root cause type: {best['root_cause_type']}\n\n"
                        f"## Evidence\n{validation.get('evidence', 'Pattern matched')}\n\n"
                        f"## Differences from Previous\n{validation.get('differences', 'None')}\n\n"
                        f"## Immediate Fix\n{best.get('fix', 'See previous incidents')}\n\n"
                        f"## Investigation Method\nPattern replay — {len(best['investigation_steps'])} targeted tool calls instead of full investigation.\n"
                        f"Previously confirmed on: {time.strftime('%Y-%m-%d', time.localtime(best['last_seen']))}"
                    )
                    log.info(f"Pattern matched for {alert_name}: {best['root_cause_type']} — skipping full investigation")

                    # Post match result
                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=f":white_check_mark: Pattern matched! {validation.get('root_cause', '')}",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": f":white_check_mark: _Pattern matched ({validation.get('confidence', '?')} confidence) — instant RCA generated_"}
                                ]}],
                            )
                        except Exception:
                            pass

                    # Create result object for PDF + Slack posting
                    from vishwakarma.core.models import LLMResult, InvestigationMeta
                    result = LLMResult(
                        answer=analysis,
                        tool_outputs=[],
                        messages=[],
                        meta=InvestigationMeta(steps=len(best["investigation_steps"])),
                    )
                else:
                    # Pattern didn't match current data
                    if validation:
                        mark_pattern_miss(best["id"])
                    log.info(f"Pattern did not match for {alert_name} — falling back to full investigation")
                    if slack_client and slack_channel_id and ack_ts:
                        try:
                            slack_client.chat_postMessage(
                                channel=slack_channel_id, thread_ts=ack_ts,
                                text=":x: Pattern didn't match current data — running full investigation",
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": ":x: _Pattern didn't match current data — different root cause. Running full investigation..._"}
                                ]}],
                            )
                        except Exception:
                            pass
        except Exception as e:
            log.debug(f"Pattern check failed (non-fatal): {e}")

        # ── Streaming investigation with real-time Slack updates ──
        # Same style as the Slack "debug" path: small context blocks,
        # real-time tool call start/result, yellow status message.

        def _run_streaming_investigation():
            """Run stream_investigate() with live Slack tool-by-tool updates."""
            status_ts = None
            tool_lines: list[str] = []
            analysis = ""

            def _short_params(params: dict) -> str:
                """Shorten params for display."""
                if not params:
                    return ""
                val = str(next(iter(params.values()), ""))
                return val[:50].replace("\n", " ")

            # Post initial status message in thread
            log.info(f"Streaming investigation: slack_client={bool(slack_client)} channel={slack_channel_id} ack_ts={ack_ts}")
            if slack_client and slack_channel_id and ack_ts:
                try:
                    resp = slack_client.chat_postMessage(
                        channel=slack_channel_id,
                        thread_ts=ack_ts,
                        text=":hourglass: Starting deep investigation...",
                        blocks=[{"type": "context", "elements": [
                            {"type": "mrkdwn", "text": ":hourglass: _Starting deep investigation..._"}
                        ]}],
                    )
                    status_ts = resp["ts"]
                    log.info(f"Status message posted: ts={status_ts}")
                except Exception as e:
                    log.warning(f"Status message failed: {e}")

            for event in engine.stream_investigate(
                question=question,
                runbooks=matched_runbooks or None,
                extra_system_prompt=extra_system_prompt,
            ):
                etype = event.get("type", "")

                if etype == "tool_call_start":
                    tool = event.get("tool", "")
                    params = event.get("params", {})
                    param_str = _short_params(params)
                    tool_lines.append(f":gear: `{tool}({param_str})`")
                    visible = tool_lines[-10:]
                    status_text = "\n".join(visible)
                    if slack_client and status_ts:
                        try:
                            slack_client.chat_update(
                                channel=slack_channel_id, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": status_text}
                                ]}],
                            )
                        except Exception:
                            pass

                elif etype == "tool_call_result":
                    status = event.get("status", "")
                    marker = ":white_check_mark:" if status == "success" else ":x:"
                    tool_name = event.get("tool", "")
                    for i in range(len(tool_lines) - 1, -1, -1):
                        if tool_name and f"`{tool_name}(" in tool_lines[i] and ":white_check_mark:" not in tool_lines[i] and ":x:" not in tool_lines[i]:
                            tool_lines[i] = tool_lines[i] + f" {marker}"
                            break
                    visible = tool_lines[-10:]
                    status_text = "\n".join(visible)
                    if slack_client and status_ts:
                        try:
                            slack_client.chat_update(
                                channel=slack_channel_id, ts=status_ts, text=status_text,
                                blocks=[{"type": "context", "elements": [
                                    {"type": "mrkdwn", "text": status_text}
                                ]}],
                            )
                        except Exception:
                            pass

                elif etype == "compaction":
                    tool_lines.append(":compression: _context compacted_")

                elif etype == "max_steps_reached":
                    analysis = event.get("content", "") or "Investigation reached max steps."

                elif etype == "done":
                    analysis = event.get("content", "") or analysis

            # Finalize status message
            tool_count = len([t for t in tool_lines if ":gear:" in t])
            if slack_client and status_ts:
                try:
                    final_text = "\n".join(tool_lines[-10:]) + f"\n:white_check_mark: _Done — {tool_count} tools_"
                    slack_client.chat_update(
                        channel=slack_channel_id, ts=status_ts, text=final_text,
                        blocks=[{"type": "context", "elements": [
                            {"type": "mrkdwn", "text": final_text}
                        ]}],
                    )
                except Exception:
                    pass

            # Build a result-like object for the rest of the flow
            from vishwakarma.core.models import LLMResult, InvestigationMeta
            return LLMResult(
                answer=analysis,
                tool_outputs=[],
                messages=[],
                meta=InvestigationMeta(steps=tool_count),
            )

        if not auto_resolved and not pattern_matched:
            result = await loop.run_in_executor(None, _run_streaming_investigation)
    except Exception as e:
        log.error(f"Alert investigation failed for {issue.title}: {e}", exc_info=True)
        if fingerprint:
            with _active_fingerprints_lock:
                _active_fingerprints.discard(fingerprint)
        return

    analysis = result.answer or "(no analysis)"
    meta = result.meta.model_dump() if result.meta else {}

    # Generate PDF
    pdf_path = None
    try:
        from vishwakarma.bot.pdf import generate_pdf
        pdf_path = generate_pdf(
            title=issue.title,
            analysis=analysis,
            source=issue.source,
            severity=issue.severity,
            tool_outputs=[o.model_dump() for o in result.tool_outputs],
            meta=meta,
        )
    except Exception as e:
        log.warning(f"PDF generation failed: {e}")

    # Update ack message to show completion
    if slack_client and slack_channel_id and ack_ts:
        try:
            severity_color = "#36a64f"  # green for completed
            slack_client.chat_update(
                channel=slack_channel_id, ts=ack_ts,
                text=f":white_check_mark: RCA complete for {issue.title}",
                attachments=[{"color": severity_color, "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": f":white_check_mark: {issue.title[:150]}", "emoji": True}},
                    {"type": "divider"},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": ":thread: _Investigation complete. See thread for full RCA report + PDF._"}]},
                ]}],
            )
        except Exception as e:
            log.debug(f"Ack update failed (non-fatal): {e}")

    # Post to Slack — thread reply if fast RCA was posted, otherwise new message
    slack_ts = None
    if config.is_slack_configured():
        try:
            from vishwakarma.plugins.relays.slack.plugin import SlackDestination
            dest = SlackDestination({"token": config.slack_bot_token})
            resp = dest.post_investigation(
                title=issue.title,
                analysis=analysis,
                source=issue.source,
                severity=issue.severity,
                pdf_path=pdf_path,
                incident_id=incident_id,
                thread_ts=ack_ts or fast_rca_ts,
            )
            slack_ts = resp.get("ts")
        except Exception as e:
            log.warning(f"Slack notification failed: {e}")

    # Save to DB
    try:
        from vishwakarma.storage.queries import save_incident
        save_incident(
            incident_id=incident_id,
            title=issue.title,
            question=question,
            analysis=analysis,
            source=issue.source,
            severity=issue.severity,
            labels=issue.labels,
            tool_outputs=[o.model_dump() for o in result.tool_outputs],
            meta=meta,
            slack_ts=slack_ts,
            pdf_path=pdf_path,
        )
    except Exception as e:
        log.warning(f"DB save failed: {e}")

    # Release the dedup lock — next firing of this alert will trigger a fresh investigation
    if fingerprint:
        with _active_fingerprints_lock:
            _active_fingerprints.discard(fingerprint)
        log.info(f"Investigation complete for {issue.title} — dedup lock released")


def _prefetch_alert_context(issue) -> str:
    """
    Pre-fetch K8s context before the agentic loop starts.
    Runs kubectl commands in parallel so the LLM begins with real signal,
    not cold — saves the first 3-5 investigation steps.
    """
    import shlex
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed

    labels = issue.labels or {}
    raw_namespace = (
        labels.get("namespace")
        or labels.get("kubernetes_namespace")
        or labels.get("exported_namespace")
        or "atlas"
    )
    # Sanitize namespace — alert labels are untrusted input
    namespace = shlex.quote(raw_namespace)

    def _run(cmd: str) -> str:
        try:
            out = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=15,
            )
            return (out.stdout or "").strip() or "(no output)"
        except Exception as e:
            return f"(error: {e})"

    commands = {
        "pod_status": f"kubectl get pods -n {namespace} --no-headers 2>/dev/null | head -40",
        "recent_events": (
            f"kubectl get events -n {namespace} --sort-by=.lastTimestamp "
            f"--field-selector type!=Normal 2>/dev/null | tail -20"
        ),
        "recent_deploys": (
            f"kubectl get replicasets -n {namespace} --sort-by=.metadata.creationTimestamp "
            f"-o jsonpath='{{range .items[-5:]}}{{.metadata.name}} {{.metadata.creationTimestamp}}\\n{{end}}' 2>/dev/null"
        ),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_run, cmd): key for key, cmd in commands.items()}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    if all("(no output)" in v or "(error" in v for v in results.values()):
        return ""

    parts = ["## Pre-fetched Kubernetes Context\n*(gathered before investigation started — use this data directly. Do NOT re-run these kubectl commands.)*"]
    if results.get("pod_status") and "(error" not in results["pod_status"]:
        parts.append(f"\n### Pod Status (namespace: {namespace})\n```\n{results['pod_status']}\n```")
    if results.get("recent_events") and "(error" not in results["recent_events"]:
        parts.append(f"\n### Warning/Critical Events (namespace: {namespace})\n```\n{results['recent_events']}\n```")
    if results.get("recent_deploys") and "(error" not in results["recent_deploys"]:
        parts.append(f"\n### Recent ReplicaSets (last 5, namespace: {namespace})\n```\n{results['recent_deploys']}\n```")

    return "\n".join(parts)


def _extract_alert_entities(issue, llm) -> str:
    """
    Use the fast model to extract key investigation entities from the alert.
    Gives the main model a head start — costs ~200 tokens, saves 3+ steps.
    """
    if not llm or not llm.cfg.fast_model:
        return ""

    alert_name = issue.labels.get("alertname") or issue.title
    labels_str = "\n".join(f"  {k}: {v}" for k, v in (issue.labels or {}).items())
    description = getattr(issue, "description", "") or ""

    prompt = (
        f"Extract investigation entities from this alert. Be terse and specific.\n\n"
        f"Alert: {alert_name}\n"
        f"Labels:\n{labels_str}\n"
        f"Description: {description}\n\n"
        f"Return ONLY this structure (fill in what you can infer, leave blank if unknown):\n"
        f"Service: <kubernetes service name>\n"
        f"Namespace: <kubernetes namespace>\n"
        f"Impact: <what is broken for end users>\n"
        f"Likely area: <RDS/Redis/app/network/deploy>\n"
        f"Time anchor: <use alert startsAt if available>\n"
        f"Key metric: <the metric that triggered this alert>"
    )

    try:
        extracted = llm.summarize(prompt).strip()
        if not extracted:
            return ""
        return f"## Alert Entity Extraction (fast pre-analysis)\n{extracted}"
    except Exception:
        return ""


def _build_prior_context(issue) -> str:
    """
    Look up past investigations for the same alert and return a context block
    so the LLM knows if this is a recurrence and what was found before.
    """
    try:
        from vishwakarma.storage.queries import search_incidents
        # Search by alert name (from labels or title)
        alert_name = issue.labels.get("alertname") or issue.title
        past = search_incidents(query=alert_name, limit=3)
        if not past:
            return ""

        lines = [
            "## Prior Investigations for This Alert",
            f"This alert ('{alert_name}') has fired before. Previous findings:",
        ]
        for inc in past:
            created = inc.get("created_at", "")[:19]
            analysis_snippet = (inc.get("analysis") or "")[:400].replace("\n", " ")
            lines.append(f"\n**{created}** — {analysis_snippet}...")

        lines.append(
            "\nCheck if this is a recurrence of the same root cause. "
            "If the prior fix was applied, investigate why it recurred."
        )
        return "\n".join(lines)
    except Exception:
        return ""
