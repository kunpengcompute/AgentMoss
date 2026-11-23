"""FastAPI route handlers for AgentGate service."""

import asyncio
import os
import socket
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from loguru import logger

from . import security as sp
from .dashboard import broadcast_event
from .models import (
    AgentGateInput,
    AgentGateResponse,
    DeleteToolCallsRequest,
    HealthResponse,
    ErrorResponse,
    SessionTraceResponse,
    ToolCallRecord,
)


def get_repo():
    """Dependency: get Repository instance."""
    from ..storage.repository import Repository
    return Repository()


def get_orchestrator():
    """Dependency: get CollectorOrchestrator."""
    from agent_gate.shared.collector import get_orchestrator as _get_orch
    return _get_orch()


def get_moss_client():
    """Dependency: get MossClient."""
    from ..client.moss_client import MossClient
    return MossClient()


def get_normalizer():
    """Dependency: get Normalizer with repository."""
    from agent_gate.shared.normalizer import Normalizer
    from ..storage.repository import Repository
    return Normalizer(repo=Repository())


router = APIRouter(prefix="/api/v1")


#: 低频回顾触发计数：每 _REVIEW_INTERVAL 条落库触发一次 ``/api/v1/review``。
#: 与 TS 版 ``ts/src/v1/routes.ts::v1ReviewCounter`` **一一对应** —— 此前只有
#: TS 侧实现，Python 侧从未触发过 review（双端不一致，2026-10 补齐）。
_REVIEW_INTERVAL = 100
_review_counter = 0

#: 持有 fire-and-forget 任务的强引用，防止被 asyncio 提前 GC。
_review_tasks: set = set()


def _maybe_trigger_review(moss_client, agent_id: str, repo) -> None:
    """低频回顾触发（fire-and-forget）。

    每 N 条落库投递一次，让 AgentMoss 直读本地存储重建链状态、提取待攻击
    前序。与实时判定完全解耦：任务不 await，失败只记日志。

    Args:
        moss_client: 本次请求注入的 MossClient（复用其连接配置）。
        agent_id: 目标 agent 标识（AgentMoss 状态库分桶用）。
        repo: 本次工具调用实际写入的 Repository。
    """
    global _review_counter
    _review_counter += 1
    if _review_counter % _REVIEW_INTERVAL != 0:
        return
    try:
        task = asyncio.create_task(
            moss_client.review(agent_id=agent_id, db_path=repo.db_path)
        )
        _review_tasks.add(task)
        task.add_done_callback(_review_tasks.discard)
    except Exception as exc:  # noqa: BLE001
        logger.debug("review trigger skipped: {}", exc)


def _agent_gate_version() -> str:
    """包版本（与 TS 版 AGENTGATE_VERSION 对齐）。

    此前 HealthResponse.version 硬编码 "0.2.2"，任何版本升级后看板页脚都显示
    错误版本。打包安装读分发元数据；源码运行退化为 0.0.0-dev。
    """
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("agent-gate-sec")
    except Exception:  # PackageNotFoundError / 其他元数据异常
        return "0.0.0-dev"


@router.get("/health", response_model=HealthResponse)
async def health_check(moss_client=Depends(get_moss_client)):
    """Health check endpoint（与 TS 版同构：网关状态 + 版本 + 身后 AgentMoss 状态）。"""
    moss = await moss_client.health()
    # 归属标识由启动方注入：同机多套 AgentGate 时供探测端区分是谁起的服务。
    return HealthResponse(
        instance=os.getenv("AGENT_GATE_INSTANCE", ""),
        version=_agent_gate_version(),
        # 实际监听端口（端口漂移后调用方靠它发现）
        port=int(os.getenv("AGENT_GATE_ACTUAL_PORT") or 0),
        moss_version=moss["moss_version"],
        moss_status=moss["moss_status"],
    )


@router.post("/analyze", response_model=AgentGateResponse)
async def analyze(
    input_data: AgentGateInput,
    request: Request,
    repo=Depends(get_repo),
    orchestrator=Depends(get_orchestrator),
    moss_client=Depends(get_moss_client),
    normalizer=Depends(get_normalizer),
):
    """Main analysis endpoint.

    Receives tool-call data from an Agent, collects system context,
    normalizes the data, calls AgentMoss every time (no decision reuse),
    and returns the security decision.
    """
    start = time.monotonic()
    # 0'. 与 TS 版对齐：只发 a_next（Python API 格式）的调用方，tool_name/command
    #     从 action_type/action_detail 兜底；两者都有时以顶层字段为准。
    _a_next_in = input_data.a_next or {}
    tool_name = input_data.tool_name or str(_a_next_in.get("action_type") or "unknown")
    command = input_data.command or str(_a_next_in.get("action_detail") or "")
    # 临时排障：记录 OpenDesk 送进来的原始 history（不提交）
    if os.environ.get("AGENT_GATE_DUMP"):
        try:
            import json as _json
            with open(os.environ["AGENT_GATE_DUMP"], "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(input_data.model_dump(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    # 0. 身份绑定：配了 AGENT_GATE_AGENT_TOKENS 时，自报的 agent_type 必须与
    #    凭证一致 —— 否则任何人拿自己的 token 声称是 opendesk，就能把脏数据
    #    写进别人的会话历史（"仿冒 agent"）。
    _principal = getattr(request.state, "principal", "") or ""
    _ok, _who = sp.bind_agent(_principal, input_data.agent_type)
    if not _ok:
        logger.warning("拒绝身份不匹配的请求: principal={} claimed={}", _principal, input_data.agent_type)
        raise HTTPException(status_code=403, detail=_who)

    # 1. Register agent & session in SQLite.
    #
    # Identity is taken from what the *caller* reports about itself, not from
    # this gateway's own host. Using socket.gethostname() filed every agent
    # under the gateway's machine name — an agent on another host (a Windows
    # QwenPaw talking to a gateway in WSL) was recorded as coming from the
    # gateway, and two agents of the same type collapsed into a single
    # ``agent_id`` with no way to tell them apart afterwards.
    #
    # Both fields are optional: callers that report nothing keep the previous
    # behaviour (fall back to the gateway's own view).
    metadata = input_data.metadata or {}
    hostname = str(metadata.get("hostname") or "") or socket.gethostname()
    os_type = str(metadata.get("os_type") or "")

    agent_id = repo.get_or_create_agent(
        agent_type=input_data.agent_type,
        hostname=hostname,
        agent_version=input_data.agent_version,
    )
    repo.get_or_create_session(
        session_id=input_data.session_id,
        agent_id=agent_id,
        cwd=input_data.cwd,
        user_prompt=input_data.user_prompt,
        os_type=os_type,
    )

    # 2. Collect system context (in background thread pool)
    context = orchestrator.collect()

    # 3. Normalize to AgentMoss format (includes DB action_history lookup)
    moss_request = await normalizer.normalize(input_data, context)

    # 4. Call AgentMoss
    #
    # 每次都真实分析，不复用历史判定。判定依赖的不只是命令文本：策略开关、
    # 单条规则启停、会话历史、脚本文件内容、L3 结果都会改变结论。缓存 Allow
    # 等于赌“这些都没变”，只要漏一项就是漏报；因此这里不再保留决策缓存。
    moss_result = await moss_client.analyze(moss_request)

    # 5. Write tool_call record
    tool_call_dict = {
        "tool_name": tool_name,
        "command": command,
        "description": input_data.description,
        "cwd": input_data.cwd,
        "action_type": tool_name,
        "action_detail": command or input_data.description,
    }
    tool_call_id = repo.insert_tool_call(
        session_id=input_data.session_id,
        tool_call=tool_call_dict,
        moss_result=moss_result,
    )

    # 6. Write system snapshot (deferred from step 3)
    context_dict = context.model_dump() if hasattr(context, "model_dump") else {}
    repo.insert_system_snapshot(
        session_id=input_data.session_id,
        tool_call_id=tool_call_id,
        context=context_dict,
    )

    elapsed = (time.monotonic() - start) * 1000
    logger.info(
        f"Analysis complete: {tool_name} → {moss_result.get('decision')} "
        f"({elapsed:.1f}ms total)"
    )

    # 7. Broadcast to dashboard WebSocket clients
    broadcast_event({
        "type": "analyze_complete",
        "agent_type": input_data.agent_type,
        "session_id": input_data.session_id,
        "tool_name": tool_name,
        "command": command,
        "decision": moss_result.get("decision", "Deny"),
        "risk_level": moss_result.get("risk_level", ""),
        "analysis_duration_ms": elapsed,
    })

    # 8. 低频回顾（fire-and-forget）：每 N 条落库触发一次，让 AgentMoss 直读
    #     本地存储重建链状态 + 提取待攻击前序。对齐 TS 版 v1/routes.ts；
    #     不阻塞本次响应，失败也不影响已算出的判定。
    _maybe_trigger_review(moss_client, agent_id, repo)

    return AgentGateResponse(
        decision=moss_result.get("decision", "Deny"),
        reason=moss_result.get("reason", ""),
        risk_level=moss_result.get("risk_level", ""),
        risk_type=moss_result.get("risk_type", ""),
        violated_layers=moss_result.get("violated_layers", []),
        policy=moss_result.get("policy", ""),
        confidence=moss_result.get("confidence", 0),
        analysis_duration_ms=elapsed,
        # 组合式拦截时的攻击链图：与 TS 版同字段，调用方无需再查库。
        violation_graph=moss_result.get("violation_graph"),
        # 能力降级/预警：**透传**给调用方（客户的 agent），不要吞掉
        degraded=moss_result.get("degraded") or [],
        # L3 结局：透传 AgentMoss 的稳定枚举，便于客户直接看「L3 有没有真判」
        l3_outcome=moss_result.get("l3_outcome") or "",
        collected_context=context,
        source="agent_moss",
    )


@router.get("/sessions/{session_id}/traces", response_model=SessionTraceResponse)
async def get_session_traces(
    session_id: str,
    request: Request,
    repo=Depends(get_repo),
):
    """Get full tool_call trace for a session."""
    # 跨会话批量访问信号（默认只告警）：这条接口返回用户 prompt + 该会话全部命令，
    # 短时间翻大量 session_id 就是在把别人的历史拼出来。
    _principal = getattr(request.state, "principal", "") or (
        "ip:" + (getattr(request.client, "host", "") or "-")
    )
    _n, _hit = sp.note_session_access(_principal, session_id)
    if _hit:
        if sp.session_sweep_action() == "deny":
            raise HTTPException(status_code=429, detail="会话访问过于集中，疑似批量扫描")
        logger.warning(
            "⚠️ 疑似跨会话扫描: key=%s 窗口内访问了 %d 个不同 session", _principal, _n,
        )

    session = repo.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    # 逐 agent token：只能读**自己 agent** 的会话。
    # 此前 check_token 对全局和 agent token 一视同仁 → agent A 能读 agent B 的
    # 全部命令原文和用户 prompt（跨 agent 数据泄露）。
    _bound = sp.bound_agent_of(getattr(request.state, "principal", "") or "")
    if _bound and (session.get("agent_type") or "").lower() != _bound.lower():
        raise HTTPException(
            status_code=403,
            detail="该会话不属于当前凭证绑定的 agent",
        )

    traces_raw = repo.get_session_traces(session_id)
    traces = [ToolCallRecord(**t) for t in traces_raw]

    return SessionTraceResponse(
        session_id=session_id,
        agent_type=session.get("agent_type", ""),
        agent_version=session.get("agent_version", ""),
        status=session.get("status", ""),
        cwd=session.get("cwd", ""),
        user_prompt=session.get("user_prompt", ""),
        created_at=str(session.get("created_at", "")),
        tool_call_count=session.get("tool_call_count", 0),
        tool_calls=traces,
    )


@router.get("/tool_calls")
async def query_tool_calls(
    request: Request,
    session_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1),
    repo=Depends(get_repo),
):
    """Query tool call records with filters. Returns {total, data} for pagination.

    ``limit`` 超过 500 时**静默截断**（与 TS 侧一致）：此前是 422，但两端行为
    不一致会让同一个客户端在不同实现上表现不同。
    """
    limit = min(limit, 500)
    # 逐 agent token：忽略调用方传的 agent_id，强制按凭证绑定的 agent_type
    # 过滤 —— 否则 agent A 能按 session_id/agent_id 枚举 agent B 的全部记录。
    _bound = sp.bound_agent_of(getattr(request.state, "principal", "") or "")
    if _bound:
        agent_id = None
    total = repo.count_tool_calls(
        agent_id=agent_id,
        agent_type=_bound or None,
        session_id=session_id,
        decision=decision,
        risk_level=risk_level,
        from_time=from_time,
        to_time=to_time,
    )
    records = repo.query_tool_calls(
        agent_id=agent_id,
        agent_type=_bound or None,
        session_id=session_id,
        decision=decision,
        risk_level=risk_level,
        from_time=from_time,
        to_time=to_time,
        offset=offset,
        limit=limit,
    )
    return {
        "total": total,
        "data": [dict(r) for r in records],
    }


# ==================== 会话流（sessionlog）====================
# 与 TS V1 的 /api/v1/sessionlog* 同路径（Python 侧原有 /dashboard/sessionlog*，
# 保留为向后兼容别名，内部复用同一实现）。
@router.get("/sessionlog")
async def api_list_sessionlogs():
    from .dashboard import list_sessionlogs
    return await list_sessionlogs()


@router.get("/sessionlog/{session_id}")
async def api_sessionlog_detail(session_id: str, request: Request):
    from .dashboard import get_sessionlog_flow
    return await get_sessionlog_flow(session_id, request)


@router.get("/agents")
async def get_agents(request: Request, repo=Depends(get_repo)):
    """每个 agent 的会话数 / 调用数（与 TS `agentsRoute` 同构）。

    逐 agent token 只能看到自己的那一条；全局 token 看全部。
    """
    _bound = sp.bound_agent_of(getattr(request.state, "principal", "") or "")
    return repo.get_agents_list(_bound or None)


@router.get("/timeline")
async def get_timeline_api(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    repo=Depends(get_repo),
):
    """按分钟聚合的 Allow/Deny 曲线。全局聚合视图，逐 agent token 一律 403。"""
    if sp.bound_agent_of(getattr(request.state, "principal", "") or ""):
        raise HTTPException(
            status_code=403,
            detail="该凭证绑定到 agent，无权查看全局时间线",
        )
    return repo.get_timeline(limit)


@router.get("/storage_stats")
async def get_storage_stats_api(request: Request, repo=Depends(get_repo)):
    """存储占用统计。全局聚合视图，逐 agent token 一律 403。"""
    if sp.bound_agent_of(getattr(request.state, "principal", "") or ""):
        raise HTTPException(
            status_code=403,
            detail="该凭证绑定到 agent，无权查看存储统计",
        )
    stats = repo.get_storage_stats()
    threshold = float(os.getenv("AGENT_GATE_CLEANUP_THRESHOLD_MB", "100") or "100")
    total_mb = (stats["dbBytes"] + stats["jsonBytes"]) / 1024 / 1024
    return {
        "stats": stats,
        "oversize": total_mb > threshold,
        "thresholdMB": threshold,
    }


@router.delete("/tool_calls")
async def delete_tool_calls_api(
    request: Request,
    body: Optional[DeleteToolCallsRequest] = Body(default=None),
    repo=Depends(get_repo),
):
    """删除 tool_call 记录。破坏性操作：只允许全局 token。

    - 不传 body：清空全部（兼容原有行为）；
    - ``{"ids": [1, 2]}``：只删除指定记录；
    - ``{"ids": []}``：不删除任何记录。
    """
    if sp.bound_agent_of(getattr(request.state, "principal", "") or ""):
        raise HTTPException(
            status_code=403,
            detail="该凭证绑定到 agent，无权清理记录",
        )
    if body is not None and body.ids is not None:
        return {"deleted": repo.delete_tool_calls(body.ids)}
    return {"deleted": repo.clear_tool_calls()}
