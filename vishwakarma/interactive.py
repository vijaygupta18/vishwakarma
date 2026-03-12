"""
Interactive investigation REPL.

Slash commands:
  /quit /exit /q     — exit
  /clear             — clear conversation history
  /tools             — list available tools
  /toolsets          — list toolsets with health status
  /history           — show conversation history
  /context           — show token count and context window usage
  /last              — show all tool outputs from the last response
  /save [path]       — save last result to markdown file
  /pdf [path]        — generate PDF from last result
  /reset             — start fresh investigation
  /model             — show current model
  /help              — show help
"""
import logging
import readline  # enables history/editing in input()
import sys
from pathlib import Path

log = logging.getLogger(__name__)


SLASH_COMMANDS = {
    "/quit", "/exit", "/q",
    "/clear",
    "/tools",
    "/toolsets",
    "/history",
    "/context",
    "/last",
    "/save",
    "/pdf",
    "/reset",
    "/model",
    "/help",
    "/?",
}

HELP_TEXT = """
╔══════════════════════════════════════════════════════════════╗
║         VISHWAKARMA — Interactive Investigation Mode        ║
╠══════════════════════════════════════════════════════════════╣
║  Type your investigation question and press Enter.          ║
║  Conversation history is preserved across turns.            ║
╠══════════════════════════════════════════════════════════════╣
║  Slash Commands:                                             ║
║    /tools       — list available tools                      ║
║    /toolsets    — toolset health status                     ║
║    /history     — show conversation so far                  ║
║    /context     — token count and context window usage      ║
║    /last        — show all tool outputs from last response  ║
║    /save [path] — save last result to markdown              ║
║    /pdf  [path] — generate PDF from last result             ║
║    /clear       — clear history (start fresh context)       ║
║    /reset       — same as /clear                            ║
║    /model       — show current LLM config                   ║
║    /help /?     — show this help                            ║
║    /quit /q     — exit                                      ║
╚══════════════════════════════════════════════════════════════╝
"""


class InteractiveSession:
    def __init__(self, config, toolset_manager=None, session_id: str | None = None):
        import uuid
        self.config = config
        self._tm = toolset_manager or config.make_toolset_manager()
        self._last_result = None
        self._last_question = ""
        self._last_tool_outputs: list[dict] = []
        self._session_id = session_id or str(uuid.uuid4())

        # Load existing session or start fresh
        if session_id:
            self._history = self._load_session(session_id)
        else:
            self._history: list[dict] = []

    def _load_session(self, session_id: str) -> list[dict]:
        try:
            from vishwakarma.storage.queries import load_oracle_session
            messages = load_oracle_session(session_id)
            if messages:
                log.info(f"Resumed oracle session {session_id} ({len(messages)} messages)")
                return messages
        except Exception as e:
            log.warning(f"Could not load session {session_id}: {e}")
        return []

    def _save_session(self) -> None:
        try:
            from vishwakarma.storage.queries import save_oracle_session
            save_oracle_session(self._session_id, self._history)
        except Exception as e:
            log.warning(f"Could not persist session: {e}")

    def run(self):
        from vishwakarma.utils.colors import (
            AI_COLOR, USER_COLOR, TOOL_COLOR, ERROR_COLOR, INFO_COLOR, DIM_COLOR
        )

        print(HELP_TEXT)
        if self._history:
            print(f"{INFO_COLOR}Resumed session {self._session_id} ({len(self._history)} messages in history)\033[0m\n")
        else:
            print(f"{INFO_COLOR}Session ID: {self._session_id}  (resume with: vk oracle --resume {self._session_id})\033[0m\n")
        self._tm.check_all()

        active = self._tm.active_toolsets()
        print(f"{INFO_COLOR}Active toolsets: {', '.join(ts.name for ts in active)}\033[0m")
        print()

        while True:
            try:
                question = input(f"{USER_COLOR}>>> \033[0m").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n{DIM_COLOR}Goodbye.\033[0m")
                break

            if not question:
                continue

            # Handle slash commands
            if question.startswith("/"):
                cmd_parts = question.split(None, 1)
                cmd = cmd_parts[0].lower()
                arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

                if cmd in ("/quit", "/exit", "/q"):
                    print(f"{DIM_COLOR}Goodbye.\033[0m")
                    break

                elif cmd in ("/clear", "/reset"):
                    import uuid
                    self._history = []
                    self._session_id = str(uuid.uuid4())
                    print(f"{INFO_COLOR}History cleared. New session: {self._session_id}\033[0m")

                elif cmd == "/tools":
                    self._show_tools()

                elif cmd == "/toolsets":
                    self._show_toolsets()

                elif cmd == "/history":
                    self._show_history()

                elif cmd == "/context":
                    self._show_context()

                elif cmd == "/last":
                    self._show_last_tool_outputs()

                elif cmd == "/save":
                    self._save_result(arg or "vishwakarma_result.md")

                elif cmd == "/pdf":
                    self._gen_pdf(arg or "vishwakarma_result.pdf")

                elif cmd == "/model":
                    cfg = self.config.llm
                    print(f"{INFO_COLOR}Model: {cfg.model}")
                    if cfg.api_base:
                        print(f"  API base: {cfg.api_base}")
                    print(f"  Max tokens: {cfg.max_tokens}\033[0m")

                elif cmd in ("/help", "/?"):
                    print(HELP_TEXT)

                else:
                    print(f"{ERROR_COLOR}Unknown command: {cmd}. Type /help for commands.\033[0m")
                continue

            # Run investigation
            self._last_question = question
            self._investigate(question)

    def _investigate(self, question: str):
        from vishwakarma.utils.colors import AI_COLOR, TOOL_COLOR, ERROR_COLOR, DIM_COLOR

        llm = self.config.make_llm()
        engine = self.config.make_engine(llm=llm, toolset_manager=self._tm)

        print()
        tool_count = 0
        current_tool_outputs: list[dict] = []

        # Snapshot for rollback on interrupt
        history_snapshot = list(self._history)

        try:
            for event in engine.stream_investigate(
                question=question,
                history=self._history,
            ):
                etype = event.get("type", "")

                if etype == "text_delta":
                    print(f"{AI_COLOR}{event.get('content', '')}\033[0m", end="", flush=True)

                elif etype == "tool_call_start":
                    tool = event.get("tool", "")
                    params = event.get("params", {})
                    tool_count += 1
                    param_str = _short_params(params)
                    print(f"\n{TOOL_COLOR}  ⚙ {tool}({param_str})\033[0m", flush=True)

                elif etype == "tool_call_result":
                    status = event.get("status", "")
                    output = event.get("output", "")
                    tool = event.get("tool", "")
                    marker = "✓" if status == "success" else "✗"
                    print(f"{TOOL_COLOR}    {marker} {status}\033[0m", flush=True)
                    current_tool_outputs.append({"tool": tool, "status": status, "output": output})

                elif etype == "done":
                    content = event.get("content", "")
                    if content:
                        print(f"\n{AI_COLOR}{content}\033[0m")
                    print(f"\n{DIM_COLOR}─── {tool_count} tool calls ───\033[0m\n")
                    # Save full message history (all tool calls/results included)
                    full_messages = event.get("messages")
                    if full_messages:
                        self._history = [
                            m for m in full_messages
                            if m.get("role") != "system"
                        ]
                    else:
                        self._history.append({"role": "user", "content": question})
                        self._history.append({"role": "assistant", "content": content})
                    self._last_result = {"analysis": content, "question": question}
                    self._last_tool_outputs = current_tool_outputs
                    # Persist to SQLite after every completed turn (crash-safe)
                    self._save_session()

                elif etype == "max_steps_reached":
                    print(f"\n{ERROR_COLOR}Max steps reached.\033[0m\n")
                    full_messages = event.get("messages")
                    if full_messages:
                        self._history = [
                            m for m in full_messages
                            if m.get("role") != "system"
                        ]
                    self._save_session()

                elif etype == "error":
                    msg = event.get("message", "Unknown error")
                    print(f"\n{ERROR_COLOR}Error: {msg}\033[0m\n")

        except KeyboardInterrupt:
            # Roll back history to pre-investigation state (interrupted = incomplete context)
            self._history = history_snapshot
            print(f"\n{DIM_COLOR}Investigation interrupted — history rolled back.\033[0m\n")
        except Exception as e:
            self._history = history_snapshot
            print(f"\n{ERROR_COLOR}Error: {e}\033[0m\n")
            log.debug("Investigation error", exc_info=True)

    def _show_tools(self):
        from vishwakarma.utils.colors import INFO_COLOR
        active = self._tm.active_toolsets()
        for ts in active:
            tools = ts.get_tools()
            print(f"\n{INFO_COLOR}[{ts.name}]\033[0m")
            for t in tools:
                print(f"  {t.name} — {t.description[:60]}")

    def _show_toolsets(self):
        from vishwakarma.utils.colors import INFO_COLOR, ERROR_COLOR
        from vishwakarma.core.tools import ToolsetHealth
        results = self._tm.check_all()
        for ts_name, health in sorted(results.items()):
            color = INFO_COLOR if health != ToolsetHealth.FAILED else ERROR_COLOR
            print(f"{color}  {ts_name}: {health.value}\033[0m")

    def _show_history(self):
        from vishwakarma.utils.colors import DIM_COLOR
        if not self._history:
            print(f"{DIM_COLOR}(no history)\033[0m")
            return
        for msg in self._history:
            role = msg.get("role", "")
            content = str(msg.get("content", ""))[:200]
            print(f"  [{role}] {content}")

    def _show_context(self):
        from vishwakarma.utils.colors import INFO_COLOR, DIM_COLOR
        try:
            import litellm
            token_count = litellm.token_counter(
                model=self.config.llm.model,
                messages=self._history,
            )
        except Exception:
            token_count = sum(len(str(m.get("content", ""))) // 4 for m in self._history)

        try:
            info = litellm.get_model_info(self.config.llm.model)
            context_window = info.get("max_input_tokens") or info.get("max_tokens") or 128000
        except Exception:
            context_window = 128000

        pct = token_count * 100 // context_window if context_window else 0
        bar_len = 30
        filled = bar_len * pct // 100
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"{INFO_COLOR}Context: [{bar}] {pct}% — {token_count:,} / {context_window:,} tokens")
        print(f"  History: {len(self._history)} messages\033[0m")

    def _show_last_tool_outputs(self):
        from vishwakarma.utils.colors import TOOL_COLOR, DIM_COLOR, ERROR_COLOR
        if not self._last_tool_outputs:
            print(f"{DIM_COLOR}(no tool outputs from last response)\033[0m")
            return
        for i, out in enumerate(self._last_tool_outputs, 1):
            tool = out.get("tool", "unknown")
            status = out.get("status", "")
            output = str(out.get("output", ""))
            color = TOOL_COLOR if status == "success" else ERROR_COLOR
            print(f"\n{color}[{i}] {tool} — {status}\033[0m")
            # Show up to 1000 chars
            if len(output) > 1000:
                print(output[:1000] + f"\n{DIM_COLOR}  … ({len(output) - 1000} more chars, use /save to see full output)\033[0m")
            else:
                print(output)

    def _save_result(self, path: str):
        from vishwakarma.utils.colors import INFO_COLOR, ERROR_COLOR
        if not self._last_result:
            print(f"{ERROR_COLOR}No result to save yet.\033[0m")
            return
        try:
            content = f"# {self._last_result['question']}\n\n{self._last_result['analysis']}"
            Path(path).write_text(content, encoding="utf-8")
            print(f"{INFO_COLOR}Saved to {path}\033[0m")
        except Exception as e:
            print(f"{ERROR_COLOR}Save failed: {e}\033[0m")

    def _gen_pdf(self, path: str):
        from vishwakarma.utils.colors import INFO_COLOR, ERROR_COLOR
        if not self._last_result:
            print(f"{ERROR_COLOR}No result to generate PDF from yet.\033[0m")
            return
        try:
            from vishwakarma.bot.pdf import generate_pdf
            result = generate_pdf(
                title=self._last_result["question"][:80],
                analysis=self._last_result["analysis"],
                output_path=path,
            )
            if result:
                print(f"{INFO_COLOR}PDF saved to {result}\033[0m")
            else:
                print(f"{ERROR_COLOR}PDF generation failed (check weasyprint install)\033[0m")
        except Exception as e:
            print(f"{ERROR_COLOR}PDF error: {e}\033[0m")


def _short_params(params: dict, max_len: int = 60) -> str:
    parts = []
    for k, v in params.items():
        val = str(v)
        if len(val) > 30:
            val = val[:27] + "..."
        parts.append(f"{k}={val!r}")
    result = ", ".join(parts)
    if len(result) > max_len:
        result = result[:max_len - 3] + "..."
    return result
