"""
Vishwakarma configuration — loads from YAML + environment variable overrides.

Config file location (in order of precedence):
  1. VK_CONFIG env var (explicit path)
  2. ~/.vishwakarma/config.yaml
  3. ./config.yaml

Environment variables override any YAML config (prefix: VK_).

Bash rules:
  safe_mode: true/false — only allow pre-approved safe commands when true
  allow: [aws, kubectl, stern, ...]  — extra commands to permit
  block: [rm, wget, curl, ...]       — commands always blocked regardless

Example config.yaml:
  llm:
    model: openai/open-large        # main reasoning model
    fast_model: openai/open-fast    # cheap/fast model for summarization + compaction
    api_base: https://<llm-api-base>
    api_key: sk-...

  toolsets:
    kubernetes:
      enabled: true
    bash:
      enabled: true
      config:
        safe_mode: false
        allow: [aws, stern, kubectl]
        block: [rm, wget, curl, dd]
"""
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from vishwakarma.core.llm import LLMConfig, VishwakarmaLLM
from vishwakarma.core.engine import InvestigationEngine
from vishwakarma.core.toolset_manager import ToolsetManager

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".vishwakarma" / "config.yaml",
    Path("config.yaml"),
]

# Built-in safe commands for bash safe_mode
SAFE_BASH_COMMANDS = [
    "kubectl",
    "helm",
    "stern",
    "cat",
    "ls",
    "echo",
    "grep",
    "awk",
    "sed",
    "head",
    "tail",
    "sort",
    "uniq",
    "wc",
    "cut",
    "jq",
    "yq",
    "date",
    "whoami",
    "hostname",
    "ps",
    "top",
    "df",
    "du",
    "free",
    "uname",
    "env",
    "printenv",
    "which",
    "curl",
    "wget",
    "dig",
    "nslookup",
    "ping",
    "traceroute",
    "netstat",
    "ss",
    "lsof",
    "aws",
    "gcloud",
]

# Commands that are always blocked (even if allow list says otherwise)
HARDCODED_BLOCK = [
    "rm -rf /",
    "rm -rf /*",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "mkfs",
    "fdisk",
    "> /dev/sda",
]


class BashRules:
    """
    Simplified bash allow/deny rules.

    safe_mode=True  → only SAFE_BASH_COMMANDS + allow list are permitted
    safe_mode=False → all commands allowed except block list

    allow: additional command prefixes to permit (always merged in)
    block: command prefixes that are always denied
    """

    def __init__(
        self,
        safe_mode: bool = False,
        allow: list[str] | None = None,
        block: list[str] | None = None,
    ):
        self.safe_mode = safe_mode
        self.allow = allow or []
        self.block = block or []

    def is_allowed(self, command: str) -> tuple[bool, str]:
        """
        Check if a bash command is allowed.
        Returns (allowed, reason).
        """
        cmd = command.strip()

        # Hardcoded blocks always apply
        for pattern in HARDCODED_BLOCK:
            if pattern in cmd:
                return False, f"Command blocked by hardcoded safety rule: {pattern}"

        # Block list
        for blocked in self.block:
            if cmd.startswith(blocked):
                return False, f"Command blocked by rule: {blocked}"

        # Allow list (always permitted)
        for allowed in self.allow:
            if cmd.startswith(allowed):
                return True, ""

        # Safe mode: only permit pre-approved safe commands
        if self.safe_mode:
            for safe in SAFE_BASH_COMMANDS:
                if cmd.startswith(safe):
                    return True, ""
            return False, (
                f"safe_mode is on — command not in safe list. "
                f"Add to allow list or disable safe_mode."
            )

        return True, ""

    @classmethod
    def from_config(cls, cfg: dict) -> "BashRules":
        return cls(
            safe_mode=cfg.get("safe_mode", False),
            allow=cfg.get("allow", []),
            block=cfg.get("block", []),
        )

    def to_dict(self) -> dict:
        return {
            "safe_mode": self.safe_mode,
            "allow": self.allow,
            "block": self.block,
        }


class VishwakarmaConfig:
    """
    Main configuration object for Vishwakarma.
    Loaded from YAML file + environment variable overrides.
    """

    def __init__(self, raw: dict):
        self._raw = raw

        # Parse config
        llm_cfg = raw.get("llm", {})
        self.llm = LLMConfig(
            model=_env("VK_MODEL", llm_cfg.get("model", "gpt-4o")),
            fast_model=_env("VK_FAST_MODEL", llm_cfg.get("fast_model")),
            api_key=_env("VK_API_KEY", llm_cfg.get("api_key")),
            api_base=_env("VK_API_BASE", llm_cfg.get("api_base")),
            api_version=_env("VK_API_VERSION", llm_cfg.get("api_version")),
            max_tokens=int(_env("VK_MAX_TOKENS", str(llm_cfg.get("max_tokens", 65536)))),
            temperature=float(_env("VK_TEMPERATURE", str(llm_cfg.get("temperature", 0.0)))),
            timeout=int(_env("VK_TIMEOUT", str(llm_cfg.get("timeout", 300)))),
        )

        # Cluster context
        self.cluster_name: str = _env("VK_CLUSTER", raw.get("cluster_name", ""))

        # Server
        srv = raw.get("server", {})
        self.host: str = _env("VK_HOST", srv.get("host", "0.0.0.0"))
        self.port: int = int(_env("VK_PORT", str(srv.get("port", 5050))))
        self.max_steps: int = int(_env("VK_MAX_STEPS", str(raw.get("max_steps", 40))))

        # Slack bot
        slack = raw.get("slack", {})
        self.slack_bot_token: str | None = _env("SLACK_BOT_TOKEN", slack.get("bot_token"))
        self.slack_app_token: str | None = _env("SLACK_APP_TOKEN", slack.get("app_token"))
        self.slack_signing_secret: str | None = _env(
            "SLACK_SIGNING_SECRET", slack.get("signing_secret")
        )

        # Storage
        storage = raw.get("storage", {})
        self.db_path: str = _env(
            "VK_DB_PATH", storage.get("db_path", "/data/vishwakarma.db")
        )

        # Custom certificate
        self.certificate: str | None = _env("CERTIFICATE", raw.get("certificate"))

        # Toolsets config (dict of name → {enabled, config})
        self.toolsets_config: dict[str, Any] = raw.get("toolsets", {})

        # Custom toolset YAML paths
        self.custom_toolset_paths: list[str] = raw.get("custom_toolset_paths", [])

        # MCP servers config
        self.mcp_servers: dict[str, dict] = raw.get("mcp_servers", {})

        # Bash rules (extracted from toolsets.bash.config if present)
        bash_toolset = self.toolsets_config.get("bash", {})
        bash_cfg = bash_toolset.get("config", {})
        self.bash_rules = BashRules.from_config(bash_cfg)

        # Runbooks — auto-load all .md files from plugins/runbooks/ + any from config
        self.runbooks: list[str] = _load_runbooks(raw.get("runbooks", []))

        # Site knowledge base — loaded from a .md file on the PVC (not baked into image)
        # Contains infra-specific: instance names, alert→instance mappings, known working commands
        self.knowledge_path: str = _env("VK_KNOWLEDGE_PATH", raw.get("knowledge_path", "/data/knowledge.md"))
        self.knowledge: str = _load_knowledge(self.knowledge_path)

        # Alert deduplication window (seconds)
        self.dedup_window: int = int(
            _env("VK_DEDUP_WINDOW", str(raw.get("dedup_window", 300)))
        )

        # Cost report scheduler
        cost_cfg = raw.get("cost_report", {})
        self.cost_report = {
            "enabled": _env("VK_COST_REPORT_ENABLED",
                            str(cost_cfg.get("enabled", False))).lower() in ("true", "1", "yes"),
            "schedule_utc": cost_cfg.get("schedule_utc", "06:30"),
            "channel": _env("VK_COST_REPORT_CHANNEL", cost_cfg.get("channel", "")),
            "anomaly_threshold": float(cost_cfg.get("anomaly_threshold", 0.15)),
            "region": cost_cfg.get("region", "ap-south-1"),
        }

    # ── Factory methods ────────────────────────────────────────────────────────

    def make_llm(self) -> VishwakarmaLLM:
        """Create a fresh VishwakarmaLLM instance (one per request for cost tracking)."""
        return VishwakarmaLLM(self.llm)

    def make_toolset_manager(self) -> ToolsetManager:
        """Create and initialize the toolset manager."""
        return ToolsetManager(
            toolsets_config=self.toolsets_config,
            custom_toolset_paths=self.custom_toolset_paths,
            mcp_servers=self.mcp_servers,
        )

    def make_engine(
        self,
        llm: VishwakarmaLLM | None = None,
        toolset_manager: ToolsetManager | None = None,
    ) -> InvestigationEngine:
        """
        Create an InvestigationEngine.
        Pass pre-built llm/toolset_manager to share state across requests,
        or let it create new ones from config.
        """
        from vishwakarma.core.tools import ToolExecutor

        if llm is None:
            llm = self.make_llm()
        if toolset_manager is None:
            toolset_manager = self.make_toolset_manager()

        executor = ToolExecutor(toolsets=toolset_manager.active_toolsets())
        return InvestigationEngine(
            llm=llm,
            executor=executor,
            max_steps=self.max_steps,
            cluster_name=self.cluster_name,
            all_toolsets=toolset_manager.all_toolsets(),
            knowledge=self.knowledge,
        )

    # ── Loader ─────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path | None = None) -> "VishwakarmaConfig":
        """
        Load config from YAML file.
        If no path given, tries VK_CONFIG env var, then default paths.
        Falls back to empty config (env vars only) if no file found.
        """
        if path is None:
            env_path = os.environ.get("VK_CONFIG")
            if env_path:
                path = Path(env_path)
            else:
                for candidate in DEFAULT_CONFIG_PATHS:
                    if candidate.exists():
                        path = candidate
                        break

        if path and Path(path).exists():
            log.info(f"Loading config from {path}")
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
        else:
            if path:
                log.warning(f"Config file not found: {path} — using env vars only")
            else:
                log.debug("No config file found — using env vars only")
            raw = {}

        # Inject custom cert before anything tries HTTPS
        cert = os.environ.get("CERTIFICATE") or raw.get("certificate")
        if cert:
            from vishwakarma.utils.cert_utils import inject_custom_cert
            inject_custom_cert(cert)

        return cls(raw)

    @classmethod
    def from_env(cls) -> "VishwakarmaConfig":
        """Create a minimal config purely from environment variables."""
        return cls({})

    # ── Helpers ────────────────────────────────────────────────────────────────

    def is_slack_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)

    def summary(self) -> dict:
        """Human-readable config summary (no secrets)."""
        return {
            "model": self.llm.model,
            "api_base": self.llm.api_base or "(default)",
            "cluster": self.cluster_name or "(none)",
            "host": self.host,
            "port": self.port,
            "max_steps": self.max_steps,
            "db_path": self.db_path,
            "slack": "configured" if self.is_slack_configured() else "not configured",
            "bash_rules": self.bash_rules.to_dict(),
            "toolsets": list(self.toolsets_config.keys()),
            "custom_toolset_paths": self.custom_toolset_paths,
        }

    def __repr__(self) -> str:
        return f"VishwakarmaConfig(model={self.llm.model!r}, cluster={self.cluster_name!r})"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _env(key: str, fallback: Any = None) -> Any:
    """Return env var if set, else fallback."""
    val = os.environ.get(key)
    return val if val is not None else fallback


def _load_knowledge(path: str) -> str:
    """Load site-specific knowledge base from a markdown file (e.g. on PVC)."""
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Could not load knowledge base from {path}: {e}")
        return ""


def _load_runbooks(extra: list[str]) -> list[str]:
    """
    Auto-load all .md runbook files from vishwakarma/plugins/runbooks/
    plus any extra text snippets from config.yaml.
    """
    runbooks = []

    # Built-in runbooks shipped with the package
    runbooks_dir = Path(__file__).parent / "plugins" / "runbooks"
    if runbooks_dir.exists():
        for md_file in sorted(runbooks_dir.rglob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8").strip()
                if content:
                    runbooks.append(f"# Runbook: {md_file.stem}\n\n{content}")
            except Exception as e:
                log.warning(f"Could not load runbook {md_file}: {e}")

    # Extra snippets or file paths from config.yaml
    for item in extra:
        item = item.strip()
        if not item:
            continue
        # If it looks like a file path, load it
        p = Path(item)
        if p.exists() and p.suffix in (".md", ".txt"):
            try:
                runbooks.append(p.read_text(encoding="utf-8").strip())
            except Exception as e:
                log.warning(f"Could not load runbook file {p}: {e}")
        else:
            # Plain text snippet
            runbooks.append(item)

    if runbooks:
        log.debug(f"Loaded {len(runbooks)} runbooks")
    return runbooks


def load_matching_runbooks(alert_name: str, llm=None) -> list[str]:
    """
    Find the runbook(s) relevant to this alert.

    Two-stage matching:
    1. Fast: keyword match against agents.json — zero LLM cost, used for known alert patterns
    2. Fallback: if no keyword match, ask the LLM to classify from the agents.json catalog
       (one cheap classification call). If LLM also finds no match, investigate without runbook.
    """
    import json

    agents_path = Path(__file__).parent / "plugins" / "agents" / "agents.json"

    if not agents_path.exists():
        return []

    try:
        agents = json.loads(agents_path.read_text())["agents"]
    except Exception as e:
        log.warning(f"Could not load agents.json: {e}")
        return []

    alert_lower = alert_name.lower()

    def _load_runbook_for_entry(entry: dict) -> str | None:
        runbook_ref = entry.get("runbook", "")
        runbook_path = (agents_path.parent / runbook_ref).resolve()
        if runbook_path.exists():
            try:
                content = runbook_path.read_text(encoding="utf-8").strip()
                log.info(f"Matched runbook '{entry['id']}' for alert '{alert_name}'")
                return f"# Runbook: {runbook_path.stem}\n\n{content}"
            except Exception as e:
                log.warning(f"Could not load runbook {runbook_path}: {e}")
        return None

    # ── Stage 1: keyword match ────────────────────────────────────────────────
    matched = []
    for entry in agents:
        desc = entry.get("description", "").lower()
        keywords = [k.lower() for k in entry.get("keywords", [])]
        if alert_lower in desc or any(kw in alert_lower for kw in keywords):
            rb = _load_runbook_for_entry(entry)
            if rb:
                matched.append(rb)

    if matched:
        return matched

    # ── Stage 2: LLM classification fallback ─────────────────────────────────
    if llm is None:
        log.info(f"No keyword match for '{alert_name}' and no LLM available — investigating without runbook")
        return []

    log.info(f"No keyword match for '{alert_name}' — asking LLM to classify from agents catalog")

    catalog_lines = []
    for entry in agents:
        catalog_lines.append(f"- id: {entry['id']}\n  description: {entry.get('description', '')}")
    catalog_text = "\n".join(catalog_lines)

    prompt = (
        f"You are a runbook router. Given an alert name, pick the single most relevant runbook ID from the catalog below.\n\n"
        f"Alert: {alert_name}\n\n"
        f"Available runbooks:\n{catalog_text}\n\n"
        f"Reply with ONLY the runbook id (e.g. 'rds-investigation'), or 'none' if no runbook fits."
    )

    try:
        chosen_id = llm.summarize(prompt).strip().lower().strip("'\"")
        log.info(f"LLM classified '{alert_name}' → '{chosen_id}'")

        if chosen_id == "none" or not chosen_id:
            log.info(f"LLM found no matching runbook for '{alert_name}' — investigating without runbook")
            return []

        for entry in agents:
            if entry["id"].lower() == chosen_id:
                rb = _load_runbook_for_entry(entry)
                return [rb] if rb else []

        log.warning(f"LLM returned unknown agent id '{chosen_id}'")
    except Exception as e:
        log.warning(f"LLM runbook classification failed: {e}")

    return []
