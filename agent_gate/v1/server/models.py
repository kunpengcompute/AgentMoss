"""AgentGate Pydantic data models.

Defines:
  - AgentGateInput:  what the Agent sends to AgentGate
  - AgentGateResponse: what AgentGate returns to the Agent
  - SystemContext:  aggregated system information collected by AgentGate
  - Sub-models for OS, resources, process, network, user info
"""

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Sub-models for collected system context ---

class OsInfo(BaseModel):
    system: str = ""                    # linux / windows / darwin
    distribution: str = ""              # Ubuntu 22.04 / openEuler 23.03
    kernel: str = ""                    # 5.15.0-91-generic
    hostname: str = ""                  # dev-server-01
    machine: str = ""                   # x86_64 / aarch64


class ResourceInfo(BaseModel):
    cpu_count: int = 0
    cpu_percent: float = 0.0
    memory_total_gb: float = 0.0
    memory_percent: float = 0.0
    disk_total_gb: float = 0.0
    disk_percent: float = 0.0


class ProcessInfo(BaseModel):
    pid: int = -1
    ppid: int = -1
    name: str = ""
    capabilities: list[str] = Field(default_factory=list)
    cgroup: str = ""


class NetworkInfo(BaseModel):
    interfaces: list[str] = Field(default_factory=list)
    active_connections: int = 0
    has_public_ip: bool = False


class UserInfo(BaseModel):
    # Windows 上 collector 取不到 uid/gid，回落到 -1（与 TS 端 Node
    # os.userInfo() 的平台行为一致）——保持 int 类型恒定，下游无需处理 null。
    uid: int = -1
    gid: int = -1
    username: str = ""
    groups: list[str] = Field(default_factory=list)


class SystemContext(BaseModel):
    """Aggregated system context from all collectors."""
    os: OsInfo = Field(default_factory=OsInfo)
    resources: ResourceInfo = Field(default_factory=ResourceInfo)
    process: ProcessInfo = Field(default_factory=ProcessInfo)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    user: UserInfo = Field(default_factory=UserInfo)
    env: dict[str, str] = Field(default_factory=dict)


# --- Request / Response models ---

class ActionHistoryItem(BaseModel):
    """A single entry in action_history.

    ``output`` 是工具的返回内容。对 ``ask_user_question`` 来说就是用户给出的
    回答 —— AgentMoss 的授权类规则（改密码 / 删用户）靠它判断「用户是否已确认」。
    此前模型里没有这个字段，Agent 传了也会被剥掉，导致这两类规则**必然**
    判成「未授权」。

    ``called_at`` 是动作发生时刻（ISO 8601）。AgentMoss 的 L2 授权规则用它
    校验「提问 → 回答 → 执行」的时效窗（默认 300s），防止拿很久以前的授权
    当依据。此前模型里没有这个字段，Agent 传了也会被剥掉。
    """
    action_type: str = ""
    action_detail: str = ""
    output: str = ""
    called_at: str = ""


class PromptTurn(BaseModel):
    """A single user prompt turn with the actions it triggered (interleaved time order).

    Expresses prompt1→tool1→tool2→prompt2→tool3 as [{text, actions:[...]}], so AgentMoss
    L2/L3 can see the full session intent, not just the latest user message (which may
    be "continue" after compaction replaced earlier history).
    """
    text: str = Field(default="", description="User prompt text at this turn")
    actions: list[ActionHistoryItem] = Field(
        default_factory=list,
        description="Actions triggered by this prompt, in order, until the next prompt"
    )


class AgentGateInput(BaseModel):
    """Input sent by the Agent to AgentGate for security analysis.

    Minimal required fields: agent_type, session_id, tool_name, command
    action_history is optional - AgentGate auto-fills from SQLite.
    """
    agent_type: str = Field(..., description="Agent type: opendes / claude-code / generic")
    agent_version: str = Field(default="", description="Agent version string")
    session_id: str = Field(..., description="Unique session identifier")
    cwd: str = Field(default="", description="Current working directory")
    tool_name: str = Field(..., description="Tool being called: bash / file_write / edit")
    command: str = Field(..., description="The command or action being executed")
    description: str = Field(default="", description="Human-readable description of the action")
    user_prompt: str = Field(default="", description="Original user task description")
    prompt_history: list[PromptTurn] = Field(
        default_factory=list,
        description="Full interleaved prompt history (ordered turns, each with its actions); optional, empty = fall back to user_prompt only"
    )
    action_history: Optional[list[ActionHistoryItem]] = Field(
        default=None,
        description="Optional history of previous actions; AgentGate auto-fills from SQLite if None"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extra metadata")


class AgentGateResponse(BaseModel):
    """Response from AgentGate after security analysis."""
    decision: str = Field(..., description="Allow / Deny")
    reason: str = Field(default="", description="Explanation for the decision")
    risk_level: str = Field(default="", description="low / medium / high / critical")
    risk_type: str = Field(default="", description="Risk category")
    violated_layers: list[str] = Field(default_factory=list)
    policy: str = Field(default="")
    confidence: int = Field(default=0, description="Confidence score 0-100")
    analysis_duration_ms: float = Field(default=0.0)
    collected_context: Optional[SystemContext] = Field(
        default=None, description="Collected system context (if requested)"
    )
    source: str = Field(default="agent_moss", description="Analysis source: agent_moss / cache / fail_closed")


# --- Trace query models ---

class ToolCallRecord(BaseModel):
    """A single tool_call record from the database."""
    id: int
    session_id: str
    seq: int
    tool_name: str
    command: str
    description: str = ""
    cwd: str = ""
    action_type: str
    action_detail: str
    decision: str
    risk_level: str = ""
    risk_type: str = ""
    violated_layers: str = "[]"
    reason: str = ""
    confidence: int = 0
    analysis_duration_ms: float = 0.0
    moss_response: str = "{}"
    called_at: str = ""


class SessionTraceResponse(BaseModel):
    """Response for GET /api/v1/sessions/{session_id}/traces."""
    session_id: str
    agent_type: str = ""
    agent_version: str = ""
    status: str = ""
    cwd: str = ""
    user_prompt: str = ""
    created_at: str = ""
    tool_call_count: int = 0
    traces: list[ToolCallRecord] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.2.2"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # 归属标识：由启动方注入 AGENT_GATE_INSTANCE（同机多套 AgentGate 时区分是谁起的），
    # 未注入时为空串。与 agent-moss 的 AGENT_MOSS_INSTANCE 同一套约定。
    instance: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
