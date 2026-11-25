"""AgentGate Pydantic data models.

Defines:
  - AgentGateInput:  what the Agent sends to AgentGate
  - AgentGateResponse: what AgentGate returns to the Agent
  - SystemContext:  aggregated system information collected by AgentGate
  - Sub-models for OS, resources, process, network, user info
"""

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field


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

    Minimal required fields: agent_type, session_id, tool_name（+ command 或 a_next.action_detail）
    action_history is optional - AgentGate auto-fills from SQLite.

    与 TS 版 ``AgentGateInputSchema`` 对齐：``command`` 可选（只发 ``a_next`` 的
    调用方也能用）；``a_next`` / ``session_history`` 两个字段此前 Python 侧缺失，
    调用方传了会被静默丢弃。
    """
    agent_type: str = Field(..., description="Agent type: opendes / claude-code / generic")
    agent_version: str = Field(default="", description="Agent version string")
    session_id: str = Field(..., description="Unique session identifier")
    # 安装级实例 ID：仅用于 AgentMoss 展示层脱敏（同实例多会话不隐藏命令原文），
    # 不参与判定。缺省由网关生成持久化 ID（~/.config/agentgate/instance_id）。
    instance_id: str = Field(default="", description="Agent installation instance id (display-layer redaction only)")
    cwd: str = Field(default="", description="Current working directory")
    tool_name: str = Field(default="", description="Tool being called: bash / file_write / edit（缺省时取 a_next.action_type）")
    command: str = Field(default="", description="The command or action being executed（缺省时取 a_next.action_detail）")
    # Python API 格式的「当前动作」（与 TS 同构）：调用方只发这个字段时，
    # tool_name/command 从 action_type/action_detail 兜底。
    a_next: Optional[dict[str, Any]] = Field(
        default=None,
        description="Next action (Python API format): {action_type, action_detail, called_at}",
    )
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
    # 调用方显式提供的跨会话历史（与 TS 同构）；缺省时由网关从本地存储组装。
    session_history: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Optional multi-session history: [{session_id, action_history: [...]}]; auto-filled from storage if None",
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
    # 结构化攻击链图（组合式拦截时非空；前端弹窗画图用）。与 TS 版对齐：
    # 此前 Python 侧响应模型没有这个字段，调用方拿不到图（只能事后查库里的
    # moss_response）。
    violation_graph: Optional[dict[str, Any]] = Field(
        default=None, description="Structured attack-chain graph (present when a combo chain is blocked)"
    )
    collected_context: Optional[SystemContext] = Field(
        default=None, description="Collected system context (if requested)"
    )
    #: 能力降级/预警说明（2026-11）：**透传**自 AgentMoss。
    #: 空数组 = 本次判定由全部检测层完整完成；
    #: 非空 = 某一层没跑全或即将失效。调用方（客户的 agent）应记录/展示/上报，
    #: 不要静默丢弃 —— 否则客户永远不知道"这次判定缺了什么"。
    degraded: list[str] = Field(
        default_factory=list,
        description="Capability degradation/warning notes passed through from AgentMoss",
    )
    #: L3 结局（2026-11）：透传 AgentMoss 的稳定枚举（ok / reasoning_fallback /
    #: not_reached / skipped / fail_open / fail_closed / failed_but_blocked /
    #: circuit_open）。空 = 旧版 AgentMoss 未返回该字段。
    l3_outcome: str = Field(
        default="", description="L3 outcome passed through from AgentMoss"
    )
    #: Deny 但建议向用户确认（2026-11）。旧 agent 忽略此字段仍是 Deny；
    #: 新 agent 可据此把 DENY 升级为 ASK，避免超时/不可用造成硬阻断。
    ask_recommended: bool = Field(
        default=False, description="Deny but user confirmation is recommended"
    )
    ask_reason: str = Field(
        default="", description="Why user confirmation is recommended"
    )
    source: str = Field(default="agent_moss", description="Analysis source: agent_moss / fail_closed")


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
    """Response for GET /api/v1/sessions/{session_id}/traces.

    ``tool_calls`` 与 TS 版同名字段（此前叫 ``traces``，同一端点两种结构）。
    其余会话元信息两端一致。
    """
    session_id: str
    agent_type: str = ""
    agent_version: str = ""
    status: str = ""
    cwd: str = ""
    user_prompt: str = ""
    created_at: str = ""
    tool_call_count: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)

    @computed_field
    @property
    def traces(self) -> list[ToolCallRecord]:
        """兼容按旧接口路径读取 traces 的客户端，顺序与 tool_calls 相同。"""
        return self.tool_calls


class HealthResponse(BaseModel):
    """与 TS 版 health 同构：status=healthy、包版本、身后 AgentMoss 状态。"""
    status: str = "healthy"
    #: 实际监听端口（端口被占用自动漂移后，调用方从这里得知新端口）。
    port: int = 0
    version: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # 归属标识：由启动方注入 AGENT_GATE_INSTANCE（同机多套 AgentGate 时区分是谁起的），
    # 未注入时为空串。与 agent-moss 的 AGENT_MOSS_INSTANCE 同一套约定。
    instance: str = ""
    # 身后 AgentMoss 的版本与健康状态（TS 版一直有，Python 侧此前没有）。
    moss_version: str = ""
    moss_status: str = ""


class DeleteToolCallsRequest(BaseModel):
    """DELETE /api/v1/tool_calls 的可选请求体。

    - 不传 body → 清空全部（保持原行为）；
    - `{"ids": [...]}` → 只删除指定记录；
    - `{"ids": []}` → 不删除任何记录（避免空选择误清空）。
    """
    ids: Optional[list[int]] = Field(
        default=None,
        description="要删除的 tool_call id 列表；None 表示清空全部",
    )


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
