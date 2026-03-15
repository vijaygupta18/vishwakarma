# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable)
pip install -e .

# Run the server + Slack bot
vk serve --config config.yaml

# Ad-hoc investigation from CLI
vk probe "why are payments pods crashing?" --config config.yaml
vk probe "..." --stream --show-tools   # streaming with tool trace
vk probe "..." --pdf /tmp/rca.pdf      # generate PDF report

# Interactive multi-turn session
vk oracle --config config.yaml
vk oracle --resume <session-id>

# Scan alert sources
vk scan alertmanager --config config.yaml --name "RDS.*"
vk scan jira --jql "project=OPS AND priority=High" --update

# Toolset health
vk arsenal list --check

# Incidents
vk incidents list --limit 20
vk incidents show <id>
vk incidents search "redis eviction"

# Config check (no secrets)
vk config --config config.yaml
```

Config is loaded from: `VK_CONFIG` env var → `~/.vishwakarma/config.yaml` → `./config.yaml`. All YAML fields can be overridden with `VK_` prefixed env vars.

## Architecture

### Alert → RCA Flow

```
AlertManager webhook POST /api/alertmanager
    → dedup by fingerprint (alertname+namespace+service)
    → 4 parallel pre-enrichment tasks:
        1. kubectl get pods/events/replicasets (prefetch_ctx)
        2. SQLite prior incidents for this alert (prior_ctx)
        3. fast_model entity extraction (service/namespace/impact)
        4. Runbook keyword match → agents.json → LLM fallback (runbooks)
    → InvestigationEngine.investigate() — agentic loop up to 40 steps
    → generate PDF + post to Slack + save to SQLite
```

### Agentic Loop (`core/engine.py`)

Each step: `LLM.complete(messages + tools)` → parse `tool_calls` → execute up to 16 tools in parallel via `ThreadPoolExecutor` → append results → repeat. Loop ends when the LLM returns a response with no `tool_calls` (plain text = final RCA).

Key behaviours:
- **Step 20 checkpoint**: injects a "RCA-or-continue" user message forcing the LLM to evaluate evidence
- **LoopGuard** (`safeguards.py`): blocks identical tool+params from re-running; checks history first, then MD5 count
- **Context compaction** (`compaction.py`): at 80% context window, fast_model summarises the full history into a structured brief; proportional truncation as fallback
- **Tool output compression**: any tool output > 8000 chars is compressed by fast_model to 20 most relevant lines before being added to messages
- **Max steps**: at step 40, forces a final `llm.complete(tools=None)` call to synthesise a best-effort RCA instead of returning a useless static string

### Two LLM Models

| Model | Used for |
|-------|----------|
| `llm.model` (main) | Agentic loop — tool calls, reasoning, RCA |
| `llm.fast_model` | Summarisation, context compaction, entity extraction, Slack chat |

Set both in config. `fast_model` calls use `llm.summarize()` which caps at 4096 output tokens.

### Toolsets

Two types:
1. **Python toolsets** (`plugins/toolsets/<name>/<name>.py`) — subclass `Toolset`, register with `@register_toolset` decorator. `execute(tool_name, params)` handles all tools in that class.
2. **YAML toolsets** (`plugins/toolsets/*.yaml`) — shell command templates with `{param}` placeholders. Auto-loaded from the directory.

All toolsets must opt-in via `config.yaml` (`enabled: true`). Disabled toolsets are shown to the LLM as "unavailable" so it doesn't try to call them. Active toolsets pass `check_prerequisites()` — health status cached 5 min.

The `bash` toolset is the primary investigation tool. It runs `subprocess.run(shell=True)` with allow/block lists. The hardcoded block list (`rm`, `curl`, `wget`, etc.) cannot be overridden by config.

### Runbook Matching (`config.py:load_matching_runbooks`)

Two stages:
1. **Keyword match**: `any(kw in alert_name.lower() for kw in entry["keywords"])` against `plugins/agents/agents.json`
2. **LLM classification fallback**: if no keyword match, fast_model picks from the agents catalog

Runbook content is injected into the system prompt as `## Relevant Runbook\n\n{content}`. The LLM is instructed that **runbook takes precedence** over generic RECON phases.

### Prompt Assembly (`core/prompt.py:build_system_prompt`)

Order of sections:
1. `SYSTEM_INTRO` — identity, READ-ONLY mode, always check knowledge base
2. Cluster name
3. `INVESTIGATION_PHASES` — todo_write mandate, runbook precedence, RECON/HYPOTHESES/RCA phases
4. `WHAT_CHANGED` — deploy/config change detection for K8s alerts
5. `GENERAL_GUIDELINES` — tool routing, anti-loop rules, timing (use `startsAt`), Five Whys
6. `RCA_OUTPUT_FORMAT`
7. Available toolsets (with tool routing rules)
8. Disabled toolsets
9. Site Knowledge Base (`knowledge.md`)
10. Runbook content
11. `ASK_USER_PROMPT` — never ask for clarification

### Knowledge Layers (don't confuse these)

| Layer | Where | What goes here |
|-------|-------|---------------|
| **Site Knowledge Base** | `/data/knowledge.md` on PVC | Static infra facts: instance IDs, namespaces, metric names, proven commands, IAM gaps |
| **Runbooks** | `plugins/runbooks/` | Per-alert investigation workflows — which tools, which order, what to look for |
| **Learnings** | `/data/learnings/*.md` on PVC | Patterns and gotchas discovered from real incidents. Agent reads via `learnings_list` + `learnings_read` |

Runbooks use `<placeholder>` for anything cluster-specific and tell the agent to "use the value from the Site Knowledge Base". Never hardcode instance IDs, namespaces, or region names in runbooks.

### Storage (`storage/db.py`)

SQLite at `config.storage.db_path` (default `/data/vishwakarma.db`). Stores full investigation history. Prior incidents for recurring alerts are loaded and injected into pre-enrichment context. Oracle (interactive) sessions are also stored here.

### Slack Bot (`bot/slack.py`)

Runs in Socket Mode in a background thread alongside FastAPI. Two paths:
- **`@bot debug <question>`** → full `engine.investigate()` + PDF + Slack post
- **`@bot <anything>`** → `_simple_chat()` with fast_model, no tools, tone-matched reply
- **Channel message with "CloudWatch Alarm"** → `parse_cloudwatch_slack_message()` → POST `/api/alertmanager`

The bot persona is "Oogway" (NammaYatri-specific). To change: edit `_simple_chat()` system prompt in `bot/slack.py`.

## Adding a Runbook

1. Create `vishwakarma/plugins/runbooks/custom/<alert-name>.md`
2. Register in `plugins/agents/agents.json`:
```json
{
  "id": "my-alert",
  "description": "Investigate MyAlert...",
  "keywords": ["myalert", "keyword2"],
  "runbook": "../runbooks/custom/<alert-name>.md"
}
```
3. Use `<placeholder>` for cluster-specific values, reference Site Knowledge Base
4. Include an Elasticsearch query step for any alert that involves application errors

## Adding a Python Toolset

```python
from vishwakarma.core.toolset_manager import register_toolset
from vishwakarma.core.tools import Toolset, ToolDef
from vishwakarma.core.models import ToolOutput, ToolStatus

@register_toolset
class MyToolset(Toolset):
    name = "my_toolset"
    description = "What this toolset does — shown to LLM"

    def get_tools(self) -> list[ToolDef]:
        return [ToolDef(name="my_tool", description="...", parameters={...})]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        ...
        return ToolOutput(tool_name=tool_name, status=ToolStatus.SUCCESS, output=result)

    def check_prerequisites(self) -> tuple[bool, str]:
        # Validate connectivity
        return True, ""
```

Enable in `config.yaml` under `toolsets.my_toolset.enabled: true`.

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `VK_CONFIG` | Config file path |
| `VK_API_KEY` | LLM API key (overrides config) |
| `VK_API_BASE` | LLM API base URL |
| `VK_FAST_MODEL` | Fast model override |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack credentials |
| `OVERRIDE_MAX_OUTPUT_TOKEN` | Override LLM max output tokens (e.g. for custom endpoints) |
| `OVERRIDE_MAX_CONTENT_SIZE` | Override LiteLLM context window size |
| `VK_MAX_CONCURRENT_INVESTIGATIONS` | Max parallel investigations (default: 2) |
| `TOOL_CALL_SAFEGUARDS_ENABLED` | Set `false` to disable loop guard (debugging only) |
