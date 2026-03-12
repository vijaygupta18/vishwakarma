"""
Core data models for Vishwakarma.
All request/response types, tool call records, and LLM results.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Tool Call Models ──────────────────────────────────────────────────────────

class ToolStatus(str):
    SUCCESS = "success"
    ERROR = "error"
    NO_DATA = "no_data"
    APPROVAL_REQUIRED = "approval_required"


class ToolOutput(BaseModel):
    """Result of a single tool execution."""
    tool_call_id: str = ""
    tool_name: str = ""
    description: str = ""
    status: str = ToolStatus.SUCCESS
    output: Any = None          # structured data returned by tool
    error: str | None = None    # error message if status == ERROR
    invocation: str = ""        # the actual command/query that ran
    token_count: int = 0        # approximate token size of output
    params: dict = Field(default_factory=dict)  # original call params — used by LoopGuard history check


class PendingApproval(BaseModel):
    """A tool call waiting for user approval before execution."""
    tool_call_id: str
    tool_name: str
    description: str
    params: dict[str, Any]


class ApprovalDecision(BaseModel):
    """User's decision on a pending tool approval."""
    tool_call_id: str
    approved: bool
    remember_prefix: list[str] = []   # bash prefixes to auto-approve this session


# ── Follow-up Actions ─────────────────────────────────────────────────────────

class QuickAction(BaseModel):
    """Suggested follow-up questions shown after an investigation."""
    id: str
    label: str
    prompt: str
    loading_text: str = "Fetching..."


# ── Investigation Metadata ────────────────────────────────────────────────────

class InvestigationMeta(BaseModel):
    """Cost, token, and timing metadata for an investigation."""
    model: str = ""
    total_cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    steps_taken: int = 0
    compactions: int = 0
    duration_seconds: float = 0.0


# ── Chat API Models ───────────────────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    """
    POST /api/investigate

    Primary endpoint for asking Vishwakarma anything.
    """
    question: str = Field(..., description="What to investigate or ask")
    history: list[dict] = Field(default_factory=list, description="Prior conversation messages")
    model: str | None = None
    stream: bool = False
    require_approval: bool = False              # pause before each tool call
    approval_decisions: list[ApprovalDecision] = []
    extra_system_prompt: str | None = None      # append to system prompt
    images: list[dict] = []                     # vision inputs [{url, detail}]
    files: list[str] = []                       # file contents to attach to prompt
    runbooks: list[str] | None = None           # runbook content to include in prompt
    bash_always_allow: bool = False             # skip approval for all bash commands
    bash_always_deny: bool = False              # block all bash commands
    response_schema: dict | None = None         # JSON schema for structured output
    prompt_overrides: dict[str, bool] = {}      # toggle prompt components on/off
    trace_id: str | None = None


class InvestigationResult(BaseModel):
    """
    Response from POST /api/chat
    """
    analysis: str
    tool_outputs: list[ToolOutput] = []
    history: list[dict] = []
    quick_actions: list[QuickAction] = []
    pending_approvals: list[PendingApproval] = []
    meta: InvestigationMeta = Field(default_factory=InvestigationMeta)


# ── Internal LLM Result ───────────────────────────────────────────────────────

class LLMResult(BaseModel):
    """
    Internal result from the agentic loop.
    Not exposed directly — converted to InvestigationResult for API responses.
    """
    answer: str
    tool_outputs: list[ToolOutput] = []
    messages: list[dict] = []
    meta: InvestigationMeta = Field(default_factory=InvestigationMeta)
    pending_approvals: list[PendingApproval] = []


# ── Alert Models ──────────────────────────────────────────────────────────────

class AlertLabels(BaseModel):
    """Standard Prometheus/AlertManager alert labels."""
    alertname: str
    severity: str = "warning"
    namespace: str | None = None
    pod: str | None = None
    service: str | None = None

    model_config = {"extra": "allow"}   # allow arbitrary extra labels


class Alert(BaseModel):
    """Single alert from AlertManager webhook."""
    status: str                          # firing | resolved
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    starts_at: str = ""
    ends_at: str = ""
    generator_url: str = ""
    fingerprint: str = ""

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "UnknownAlert")

    @property
    def summary(self) -> str:
        return self.annotations.get("summary", "")

    @property
    def description(self) -> str:
        return self.annotations.get("description", "")

    @property
    def is_firing(self) -> bool:
        return self.status == "firing"


class AlertManagerPayload(BaseModel):
    """Incoming payload from AlertManager webhook."""
    alerts: list[Alert] = []
    version: str = "4"
    group_key: str = ""
    group_labels: dict[str, str] = {}
    common_labels: dict[str, str] = {}
    common_annotations: dict[str, str] = {}

    @property
    def firing(self) -> list[Alert]:
        return [a for a in self.alerts if a.is_firing]


# ── Health Check Models ───────────────────────────────────────────────────────

class CheckMode(str):
    MONITOR = "monitor"     # pass/fail, notify on failure only
    ANALYZE = "analyze"     # always analyze and report


class CheckRequest(BaseModel):
    """POST /api/checks/execute"""
    name: str
    query: str
    mode: str = CheckMode.MONITOR
    timeout: int = 60
    destination: dict | None = None


class CheckResult(BaseModel):
    name: str
    passed: bool
    rationale: str
    raw_analysis: str = ""
    meta: InvestigationMeta = Field(default_factory=InvestigationMeta)
