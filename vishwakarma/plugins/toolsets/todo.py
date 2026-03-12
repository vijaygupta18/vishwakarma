"""
Built-in TodoWrite/TodoRead toolset — lets the LLM track investigation steps.

Always active (no config required). The LLM calls todo_write to maintain
a task list that is displayed in logs and visible to operators watching
stern/kubectl logs.
"""
import logging
from typing import Any

from vishwakarma.core.models import ToolOutput, ToolStatus
from vishwakarma.core.tools import Toolset, ToolDef
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

STATUS_ICONS = {
    "completed": "[✓]",
    "complete": "[✓]",
    "done": "[✓]",
    "in_progress": "[~]",
    "in-progress": "[~]",
    "pending": "[ ]",
    "todo": "[ ]",
    "failed": "[✗]",
    "skipped": "[-]",
}


def _render_task_table(tasks: list[dict]) -> str:
    """Render tasks as an ASCII table for log output."""
    if not tasks:
        return "(empty task list)"

    col_id = max(2, max(len(str(t.get("id", ""))) for t in tasks))
    col_content = max(7, max(len(str(t.get("content", ""))) for t in tasks))
    col_content = min(col_content, 80)
    col_status = max(6, max(len(str(t.get("status", ""))) for t in tasks) + 4)

    sep = f"+{'-' * (col_id + 2)}+{'-' * (col_content + 2)}+{'-' * (col_status + 2)}+"
    header = f"| {'ID':<{col_id}} | {'Content':<{col_content}} | {'Status':<{col_status}} |"

    rows = [sep, header, sep]
    for t in tasks:
        tid = str(t.get("id", ""))
        content = str(t.get("content", ""))
        if len(content) > col_content:
            content = content[:col_content - 3] + "..."
        raw_status = str(t.get("status", "pending")).lower()
        icon = STATUS_ICONS.get(raw_status, "[ ]")
        status_display = f"{icon} {raw_status}"
        rows.append(f"| {tid:<{col_id}} | {content:<{col_content}} | {status_display:<{col_status}} |")

    rows.append(sep)
    return "\n".join(rows)


@register_toolset
class TodoToolset(Toolset):
    name = "todo"
    description = "Built-in task list management for investigation tracking"

    def __init__(self, config: dict):
        self._config = config

    def check_prerequisites(self) -> None:
        pass  # always available

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="todo_write",
                description=(
                    "Create or update your investigation task list. "
                    "Call this at the start of an investigation and after completing each step. "
                    "Use statuses: pending, in_progress, completed, failed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "description": "Full list of investigation tasks",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer", "description": "Task number (1-based)"},
                                    "content": {"type": "string", "description": "Task description"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "failed", "skipped"],
                                        "description": "Current task status",
                                    },
                                },
                                "required": ["id", "content", "status"],
                            },
                        }
                    },
                    "required": ["tasks"],
                },
                handler=self._todo_write,
            ),
            ToolDef(
                name="todo_read",
                description="Read the current investigation task list.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                handler=self._todo_read,
            ),
        ]

    def _todo_write(self, params: dict) -> ToolOutput:
        tasks = params.get("tasks", [])
        table = _render_task_table(tasks)
        log.info("Task List:\n" + table)
        return ToolOutput(
            tool_call_id="",
            tool_name="todo_write",
            status=ToolStatus.SUCCESS,
            output="Tasks updated.",
            invocation=f"todo_write({len(tasks)} tasks)",
        )

    def _todo_read(self, params: dict) -> ToolOutput:
        return ToolOutput(
            tool_call_id="",
            tool_name="todo_read",
            status=ToolStatus.SUCCESS,
            output="Use todo_write to set and update tasks.",
            invocation="todo_read()",
        )
