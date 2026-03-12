"""
Built-in TodoWrite toolset — lets the LLM track investigation steps.

Always active (no config required). The LLM calls todo_write to maintain
a task list displayed in logs so operators watching stern/kubectl logs
can see what the agent is doing, how much is done, and what's remaining.
"""
import logging

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
    if not tasks:
        return "(empty task list)"

    col_id = max(2, max(len(str(t.get("id", ""))) for t in tasks))
    col_content = min(80, max(7, max(len(str(t.get("content", ""))) for t in tasks)))
    col_status = max(15, max(len(str(t.get("status", ""))) + 4 for t in tasks))

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
    description = "Track investigation progress — call todo_write at the start and after each step"

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="todo_write",
                description=(
                    "Create or update your investigation task list. "
                    "MUST be called at the very start of every investigation to show the plan. "
                    "Update after each completed step to show progress. "
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
                                    "id": {"type": "integer"},
                                    "content": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "failed", "skipped"],
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
