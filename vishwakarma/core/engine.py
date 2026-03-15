"""
Investigation engine — the main agentic loop.

Flow:
  1. Build messages (system prompt + user question + history)
  2. Call LLM with available tools
  3. LLM returns tool calls → execute them → add results → goto 2
  4. LLM returns text (no tool calls) → investigation complete
  5. Return LLMResult with analysis + all tool outputs

Features:
  - Tool approval workflow (pause before executing)
  - Bash allow/deny enforcement
  - Loop detection (safeguards)
  - Context compaction (handle long investigations)
  - Streaming support
  - Multi-turn conversation
"""
import json
import logging
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from vishwakarma.core.compaction import compact_messages
from vishwakarma.core.llm import VishwakarmaLLM
from vishwakarma.core.models import (
    ApprovalDecision,
    InvestigationMeta,
    LLMResult,
    PendingApproval,
    ToolOutput,
    ToolStatus,
)
from vishwakarma.core.prompt import Section, build_messages, build_system_prompt
from vishwakarma.core.safeguards import LoopGuard
from vishwakarma.core.tools import ToolExecutor

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 40
CHECKPOINT_STEP = 20  # inject a reflection prompt at this step to force RCA-or-continue decision


class InvestigationEngine:
    """
    Main agentic investigation engine.
    One instance per investigation request.
    """

    def __init__(
        self,
        llm: VishwakarmaLLM,
        executor: ToolExecutor,
        max_steps: int = DEFAULT_MAX_STEPS,
        cluster_name: str = "",
        all_toolsets: list | None = None,
        knowledge: str = "",
    ):
        self.llm = llm
        self.executor = executor
        self.max_steps = max_steps
        self.cluster_name = cluster_name
        self.all_toolsets = all_toolsets  # includes disabled ones — shown to LLM
        self.knowledge = knowledge        # site-specific knowledge base (from /data/knowledge.md)

    def investigate(
        self,
        question: str,
        history: list[dict] | None = None,
        extra_system_prompt: str | None = None,
        images: list[dict] | None = None,
        files: list[str] | None = None,
        runbooks: list[str] | None = None,
        require_approval: bool = False,
        approval_decisions: list[ApprovalDecision] | None = None,
        bash_always_allow: bool = False,
        bash_always_deny: bool = False,
        sections_off: set[Section] | None = None,
        response_schema: dict | None = None,
    ) -> LLMResult:
        """
        Run a full investigation and return the result.
        Synchronous — blocks until complete.
        """
        start_time = time.time()
        guard = LoopGuard()
        compactions = 0
        all_tool_outputs: list[ToolOutput] = []
        pending_approvals: list[PendingApproval] = []
        tool_call_counter = 0
        checkpoint_injected = False

        # Decisions index for approval workflow
        decisions = {d.tool_call_id: d for d in (approval_decisions or [])}

        # Build bash approval session state
        approved_prefixes: set[str] = set()
        if approval_decisions:
            for d in approval_decisions:
                for prefix in d.remember_prefix:
                    approved_prefixes.add(prefix)

        # Build initial messages
        system = build_system_prompt(
            toolsets=self.executor.toolsets,
            cluster_name=self.cluster_name,
            runbooks=runbooks,
            knowledge=self.knowledge or None,
            extra_prompt=extra_system_prompt,
            sections_off=sections_off,
            all_toolsets=self.all_toolsets,
        )
        messages = build_messages(
            question=question,
            history=history or [],
            system_prompt=system,
            images=images,
            files=files,
        )

        tools = self.executor.openai_tools()

        for step in range(self.max_steps):
            log.debug(f"Investigation step {step + 1}/{self.max_steps}")

            # Checkpoint: at step 20, force the LLM to decide RCA-or-continue
            if step == CHECKPOINT_STEP and not checkpoint_injected:
                checkpoint_injected = True
                messages.append({
                    "role": "user",
                    "content": (
                        f"**Investigation Checkpoint (step {step}):** "
                        "You have gathered significant data. Pause and evaluate:\n"
                        "1. What is your current best hypothesis for the root cause?\n"
                        "2. Do you have enough evidence to write the final RCA now?\n"
                        "   - If YES → write the complete RCA immediately (Root Cause / Confidence / Evidence Chain / Immediate Fix / Prevention).\n"
                        "   - If NO → state in one sentence exactly what is still missing, then continue investigating.\n\n"
                        "Be decisive. Do not re-run tools you have already run."
                    ),
                })

            # Compact if needed (pass llm for LLM-based compaction)
            messages, did_compact = compact_messages(messages, llm=self.llm)
            if did_compact:
                compactions += 1
                guard.reset()

            # Call LLM
            response = self.llm.complete(
                messages=messages,
                tools=tools if tools else None,
                response_format=response_schema,
            )

            # Log intermediate AI reasoning text (mirrors Holmes "AI: ..." output)
            if response.content and response.content.strip():
                log.info(f"[bold #00FFFF]AI:[/bold #00FFFF] {response.content}")

            # No tool calls → LLM is done
            if not response.tool_calls:
                meta = self.llm.build_meta(step + 1, compactions, start_time)
                return LLMResult(
                    answer=response.content,
                    tool_outputs=all_tool_outputs,
                    messages=messages,
                    meta=meta,
                    pending_approvals=pending_approvals,
                )

            # Add assistant message with tool calls
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["params"]),
                        },
                    }
                    for tc in response.tool_calls
                ],
            })

            # Log tool call summary (mirrors Holmes-style progress output)
            n_calls = len(response.tool_calls)
            log.info(f"The AI requested {n_calls} tool call(s).")

            # Pre-check all tool calls (guards + approvals) then execute in parallel
            to_execute: list[tuple[str, str, dict]] = []  # (call_id, tool_name, params)
            blocked: dict[str, str | None] = {}  # call_id -> reply content (None = pending)

            for tc in response.tool_calls:
                tool_name = tc["name"]
                params = tc["params"]
                call_id = tc["id"]

                # Loop guard (pass tool_outputs for history-based check)
                allowed, reason = guard.is_allowed(tool_name, params, all_tool_outputs)
                if not allowed:
                    blocked[call_id] = reason
                    continue

                # Bash allow/deny
                if tool_name in ("bash", "run_command", "execute_command"):
                    cmd = params.get("command", "")
                    if bash_always_deny:
                        blocked[call_id] = f"Bash command denied by policy: {cmd}"
                        continue
                    if not bash_always_allow:
                        auto_approved = any(cmd.startswith(p) for p in approved_prefixes)
                        if not auto_approved and require_approval:
                            decision = decisions.get(call_id)
                            if decision is None:
                                pending_approvals.append(PendingApproval(
                                    tool_call_id=call_id,
                                    tool_name=tool_name,
                                    description=f"Run bash: {cmd}",
                                    params=params,
                                ))
                                blocked[call_id] = None
                                continue
                            if not decision.approved:
                                blocked[call_id] = f"User denied bash command: {cmd}"
                                continue
                            for prefix in decision.remember_prefix:
                                approved_prefixes.add(prefix)

                # Tool approval for non-bash tools
                elif require_approval:
                    decision = decisions.get(call_id)
                    if decision is None:
                        pending_approvals.append(PendingApproval(
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            description=f"Call {tool_name}",
                            params=params,
                        ))
                        blocked[call_id] = None
                        continue
                    if not decision.approved:
                        blocked[call_id] = f"User denied tool call: {tool_name}"
                        continue

                to_execute.append((call_id, tool_name, params))

            # Execute approved tools in parallel
            def _run_tool(call_id: str, tool_name: str, params: dict, tool_idx: int = 0) -> tuple[str, ToolOutput, str]:
                # Describe the call briefly (first param value, truncated)
                desc = next(iter(params.values()), "") if params else ""
                desc = str(desc)[:60].replace("\n", " ")
                log.info(f"Running tool #{tool_idx} [bold]{tool_name}[/bold]: {desc}")
                output = self.executor.execute(tool_name, params)
                output.tool_call_id = call_id
                output.params = params  # store for LoopGuard history check
                if output.status == ToolStatus.ERROR:
                    content = f"Error: {output.error}\nCommand: {output.invocation}"
                elif output.status == ToolStatus.NO_DATA:
                    content = f"No data returned. Command: {output.invocation}"
                else:
                    content = str(output.output) if output.output is not None else ""
                # Compress large outputs to avoid wasting context window
                if len(content) > 8000:
                    content = self.llm.summarize(
                        f"You are helping investigate an infrastructure incident. "
                        f"Compress the following {tool_name} output to the 20 most relevant lines. "
                        f"Keep error messages, timestamps, and anomalies. Remove repetitive healthy entries.\n\n"
                        f"{content}"
                    )
                return call_id, output, content

            executed: dict[str, tuple[ToolOutput, str]] = {}
            if to_execute:
                workers = min(16, len(to_execute))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {}
                    for cid, tname, tparams in to_execute:
                        tool_call_counter += 1
                        futures[pool.submit(_run_tool, cid, tname, tparams, tool_call_counter)] = cid
                    for future in as_completed(futures):
                        cid, output, content = future.result()
                        executed[cid] = (output, content)

            # Append messages in original tool-call order
            for tc in response.tool_calls:
                call_id = tc["id"]
                if call_id in blocked:
                    if blocked[call_id] is not None:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": blocked[call_id],
                        })
                elif call_id in executed:
                    output, content = executed[call_id]
                    all_tool_outputs.append(output)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content,
                    })

            # If we have pending approvals, stop and return them
            if pending_approvals:
                meta = self.llm.build_meta(step + 1, compactions, start_time)
                return LLMResult(
                    answer="",
                    tool_outputs=all_tool_outputs,
                    messages=messages,
                    meta=meta,
                    pending_approvals=pending_approvals,
                )

        # Max steps reached — force a final synthesis call
        log.warning(f"Max steps ({self.max_steps}) reached — forcing final synthesis")
        messages.append({
            "role": "user",
            "content": (
                f"**Max investigation steps ({self.max_steps}) reached.** "
                "You must now write the final RCA based on everything gathered so far. "
                "Do NOT call any more tools. Synthesize your best assessment:\n\n"
                "## Root Cause\n## Confidence\n## Evidence Chain\n## Immediate Fix\n## Prevention\n## Needs More Investigation\n\n"
                "If root cause is still unclear, state what was checked, what was found, and what would be needed to confirm."
            ),
        })
        try:
            final_response = self.llm.complete(messages=messages, tools=None)
            answer = final_response.content or "Investigation incomplete: max steps reached. Review tool outputs above."
        except Exception as e:
            log.error(f"Final synthesis call failed: {e}")
            answer = "Investigation incomplete: max steps reached. Review the tool outputs above for findings."

        meta = self.llm.build_meta(self.max_steps, compactions, start_time)
        return LLMResult(
            answer=answer,
            tool_outputs=all_tool_outputs,
            messages=messages,
            meta=meta,
        )

    def stream_investigate(
        self,
        question: str,
        history: list[dict] | None = None,
        extra_system_prompt: str | None = None,
        images: list[dict] | None = None,
        runbooks: list[str] | None = None,
        require_approval: bool = False,
        approval_decisions: list[ApprovalDecision] | None = None,
        bash_always_allow: bool = False,
        bash_always_deny: bool = False,
    ) -> Generator[dict, None, None]:
        """
        Stream investigation events as they happen.
        Yields dicts suitable for SSE streaming.
        """
        guard = LoopGuard()
        compactions = 0
        tool_call_counter = 0
        all_stream_tool_outputs: list[ToolOutput] = []
        decisions = {d.tool_call_id: d for d in (approval_decisions or [])}

        system = build_system_prompt(
            toolsets=self.executor.toolsets,
            cluster_name=self.cluster_name,
            runbooks=runbooks,
            knowledge=self.knowledge or None,
            extra_prompt=extra_system_prompt,
            all_toolsets=self.all_toolsets,
        )
        messages = build_messages(
            question=question,
            history=history or [],
            system_prompt=system,
            images=images,
        )

        tools = self.executor.openai_tools()

        checkpoint_injected_stream = False

        for step in range(self.max_steps):
            # Checkpoint: at step 20, force the LLM to decide RCA-or-continue
            if step == CHECKPOINT_STEP and not checkpoint_injected_stream:
                checkpoint_injected_stream = True
                messages.append({
                    "role": "user",
                    "content": (
                        f"**Investigation Checkpoint (step {step}):** "
                        "You have gathered significant data. Pause and evaluate:\n"
                        "1. What is your current best hypothesis for the root cause?\n"
                        "2. Do you have enough evidence to write the final RCA now?\n"
                        "   - If YES → write the complete RCA immediately (Root Cause / Confidence / Evidence Chain / Immediate Fix / Prevention).\n"
                        "   - If NO → state in one sentence exactly what is still missing, then continue investigating.\n\n"
                        "Be decisive. Do not re-run tools you have already run."
                    ),
                })

            messages, did_compact = compact_messages(messages, llm=self.llm)
            if did_compact:
                compactions += 1
                guard.reset()
                yield {"type": "compaction", "step": step}

            # Stream from LLM
            collected_content = ""
            collected_tool_calls = []

            for chunk in self.llm.stream(messages, tools=tools or None):
                chunk_type = chunk.get("type")
                yield chunk

                if chunk_type == "text_delta":
                    collected_content += chunk.get("content", "")
                elif chunk_type in ("tool_calls", "analysis_done"):
                    collected_content = chunk.get("content", collected_content)
                    collected_tool_calls = chunk.get("tool_calls", [])

            if not collected_tool_calls:
                # Append only the assistant message — user message is already in messages
                # from build_messages() and must not be duplicated
                messages.append({"role": "assistant", "content": collected_content})
                yield {"type": "done", "content": collected_content, "messages": messages}
                return

            # Add assistant turn
            messages.append({
                "role": "assistant",
                "content": collected_content,
                "tool_calls": collected_tool_calls,
            })

            # Execute tools — parallel if multiple calls
            parsed: list[tuple[str, str, dict]] = []  # (call_id, tool_name, params)
            stream_blocked: dict[str, str] = {}

            for tc in collected_tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    params = json.loads(tc["function"]["arguments"])
                except Exception:
                    params = {}
                call_id = tc["id"]

                allowed, reason = guard.is_allowed(tool_name, params, all_stream_tool_outputs)
                if not allowed:
                    stream_blocked[call_id] = reason
                    continue
                parsed.append((call_id, tool_name, params))

            # Log tool count and emit start events
            log.info(f"The AI requested {len(parsed)} tool call(s).")
            for call_id, tool_name, params in parsed:
                tool_call_counter += 1
                desc = next(iter(params.values()), "") if params else ""
                desc = str(desc)[:60].replace("\n", " ")
                log.info(f"Running tool #{tool_call_counter} [bold]{tool_name}[/bold]: {desc}")
                yield {"type": "tool_call_start", "tool": tool_name, "params": params}

            def _run_stream_tool(call_id: str, tool_name: str, params: dict):
                output = self.executor.execute(tool_name, params)
                output.tool_call_id = call_id
                output.params = params
                if output.status == ToolStatus.ERROR:
                    content = f"Error: {output.error}\nCommand: {output.invocation}"
                elif output.status == ToolStatus.NO_DATA:
                    content = f"No data. Command: {output.invocation}"
                else:
                    content = str(output.output) if output.output is not None else ""
                if len(content) > 8000:
                    content = self.llm.summarize(
                        f"You are helping investigate an infrastructure incident. "
                        f"Compress the following {tool_name} output to the 20 most relevant lines. "
                        f"Keep error messages, timestamps, and anomalies. Remove repetitive healthy entries.\n\n"
                        f"{content}"
                    )
                return call_id, tool_name, output, content

            stream_results: dict[str, tuple[str, Any, str]] = {}
            if parsed:
                workers = min(16, len(parsed))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(_run_stream_tool, cid, tname, tparams): cid
                        for cid, tname, tparams in parsed
                    }
                    for future in as_completed(futures):
                        cid, tool_name, output, content = future.result()
                        stream_results[cid] = (tool_name, output, content)
                        all_stream_tool_outputs.append(output)
                        yield {
                            "type": "tool_call_result",
                            "tool": tool_name,
                            "status": output.status,
                            "invocation": output.invocation,
                        }

            # Append messages in original order
            for tc in collected_tool_calls:
                call_id = tc["id"]
                if call_id in stream_blocked:
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": stream_blocked[call_id]})
                elif call_id in stream_results:
                    _, output, content = stream_results[call_id]
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

        yield {"type": "max_steps_reached", "steps": self.max_steps, "messages": messages}
