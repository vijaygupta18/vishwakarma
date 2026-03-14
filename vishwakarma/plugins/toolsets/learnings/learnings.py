"""
Learnings toolset — gives the agent access to accumulated incident knowledge.

Two tools:
  learnings_list   — list available categories with fact counts
  learnings_read   — read the full content of a category
"""
import logging

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class LearningsToolset(Toolset):
    name = "learnings"
    description = "Access accumulated incident knowledge and learned facts organized by category"

    def __init__(self, config: dict):
        path = config.get("path", "/data/learnings")
        from vishwakarma.core.learnings import LearningsManager
        self._lm = LearningsManager(path=path)

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            cats = self._lm.list_categories()
            return True, f"{len(cats)} categories available"
        except Exception as e:
            return False, str(e)

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="learnings_list",
                description=(
                    "List all available learning categories with fact counts. "
                    "Call this at the start of every investigation to see what knowledge is available, "
                    "then use learnings_read to load relevant categories."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            ToolDef(
                name="learnings_read",
                description=(
                    "Read the full content of a learning category. "
                    "Use after learnings_list to load categories relevant to the current alert."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Category name e.g. 'rds', 'redis', 'drainer', 'kubernetes'",
                        }
                    },
                    "required": ["category"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name == "learnings_list":
            return self._list(params)
        if tool_name == "learnings_read":
            return self._read(params)
        return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")

    def _list(self, params: dict) -> ToolOutput:
        invocation = "learnings_list()"
        try:
            cats = self._lm.list_categories()
            if not cats:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = []
            for c in cats:
                facts = c["fact_count"]
                if facts > 0:
                    lines.append(f"- {c['category']}: {facts} facts")
            if not lines:
                return ToolOutput(
                    status=ToolStatus.NO_DATA,
                    output="No facts stored yet in any category.",
                    invocation=invocation,
                )
            return ToolOutput(
                status=ToolStatus.SUCCESS,
                output="Available learning categories (use learnings_read to load):\n" + "\n".join(lines),
                invocation=invocation,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _read(self, params: dict) -> ToolOutput:
        category = params.get("category", "").strip().lower()
        invocation = f"learnings_read({category})"
        if not category:
            return ToolOutput(status=ToolStatus.ERROR, error="Missing required parameter 'category'", invocation=invocation)
        try:
            content = self._lm.get(category)
            if not content or content.strip() == f"# {category.capitalize()} Learnings":
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output=content, invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
