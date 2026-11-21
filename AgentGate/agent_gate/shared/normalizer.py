"""Normalizer: converts AgentGateInput + SystemContext → AgentMoss AnalyzeRequest.

Also integrates with Repository to auto-fill action_history from SQLite.
"""

import datetime
import socket
import time
from typing import Optional

from loguru import logger

from agent_gate.shared.multi_session import (
    multi_session_enabled,
    session_history_limit,
    stateful_incremental_enabled,
)
from agent_gate.shared.server.models import AgentGateInput, SystemContext
from agent_gate.shared.storage.repository import Repository
from agent_gate.shared.instance_id import get_instance_id
from agent_gate.shared.tool_map import resolve_action_type


class Normalizer:
    """Converts AgentGate input + collected context into AgentMoss AnalyzeRequest format."""

    def __init__(self, repo: Optional[Repository] = None):
        self._repo = repo

    async def normalize(
        self,
        input_data: AgentGateInput,
        context: SystemContext,
    ) -> dict:
        """Convert to AgentMoss-compatible AnalyzeRequest dict.

        Args:
            input_data: The AgentGateInput from the Agent.
            context: Collected system context.

        Returns:
            dict matching AgentMoss AnalyzeRequest schema.
        """
        # Build action_history from DB if Agent didn't provide it
        db_history: list[dict] = []
        if self._repo and input_data.session_id:
            try:
                db_history = await self._run_sync(
                    self._repo.get_action_history, input_data.session_id
                )
            except Exception as e:
                logger.warning(f"Failed to get action_history from DB: {e}")

        # Build session_history from DB: recent N sessions (excluding current),
        # each with full action_history — for AgentMoss L2.4 formal engine
        # cross-session combo-chain / sensitive-data-flow verification.
        # 向后兼容：Repository 不可用时留空，AgentMoss 退化为单 session（零回归）。
        # 开关默认开（AGENT_GATE_MULTI_SESSION，见 multi_session.py）；显式设 0
        # 时跳过，省掉跨会话查询开销。
        session_history: list[dict] = []
        history_source = "unavailable"
        db_limit = None
        # 调用方显式提供跨会话历史时优先使用（与 TS 版同契约；此前 Python
        # 模型没有 session_history 字段，调用方传了会被静默丢弃）。
        if input_data.session_history is not None:
            session_history = [dict(x) for x in input_data.session_history if isinstance(x, dict)]
            history_source = "caller"
        elif not multi_session_enabled():
            history_source = "disabled"
        elif self._repo and input_data.session_id:
            try:
                # 注意：不按 agent 过滤（对齐 V3 保守方向）——跨会话组合链的检测
                # 应覆盖全部历史 session，而非仅本 agent；agent_id 在 Repository
                # 里是复合键（agent_type:hostname），与输入 agent_type 不相等。
                db_limit = session_history_limit()
                session_history = await self._run_sync(
                    self._repo.get_recent_session_histories,
                    None,  # agent_id 不过滤
                    input_data.session_id,
                    # 默认 5，保持现有性能；需要更大实时覆盖面时用
                    # AGENT_GATE_SESSION_HISTORY_LIMIT 调大（上限 50）
                    db_limit,
                )
                history_source = "repository"
            except Exception as e:
                history_source = "repository_error"
                logger.warning(f"Failed to get session_history from DB: {e}")

        # 工具名 → action_type 归一化：显式配置 > 内置表 > 名称启发式 > 未知。
        # 详见 shared/tool_map.py（双端对称：TS ts/src/shared/tool-map.ts）。
        # 与 TS 版对齐：tool_name 缺省时取 a_next.action_type（只发 a_next 的
        # 调用方此前在 Python 侧会得到空 action_type，bash 类规则整条失效）。
        _raw_tool = input_data.tool_name or str((input_data.a_next or {}).get("action_type") or "unknown")
        action_type, _tool_recognized = resolve_action_type(_raw_tool)

        # Prefer Agent-provided history, fallback to DB
        action_history = (
            input_data.action_history
            if input_data.action_history is not None
            else db_history
        )

        def _mapped_history_type(entry) -> str:
            """历史动作存的是原始 agent 工具名，过同一套归一化（配置/内置/启发式）。"""
            raw = (
                entry.action_type if hasattr(entry, "action_type")
                else str(entry.get("action_type", ""))
            )
            mapped, _ = resolve_action_type(raw)
            return mapped

        # Convert to dicts — and map the action_type through the SAME rules as
        # the current action. History entries store the raw agent tool name
        # (routes.py writes `input_data.tool_name`), so without this an agent's
        # shell history arrives as "execute_shell_command" and AgentMoss does
        # not treat it as a shell action — read_before_write then sees no prior
        # command at all and its history-dependent verdicts silently degrade.
        action_history_list = [
            {
                "action_type": _mapped_history_type(h),
                "action_detail": (
                    h.action_detail if hasattr(h, "action_detail")
                    else str(h.get("action_detail", ""))
                ),
                # 工具返回内容。``ask_user_question`` 的 output = 用户回答，是
                # AgentMoss 授权类规则（改密码 / 删用户）的唯一判据；不带就会
                # 被判成「未授权」而拒绝。
                "output": str(
                    getattr(h, "output", "")
                    or (h.get("output", "") if isinstance(h, dict) else "")
                    or ""
                ),
                # 供 AgentMoss P4 时间窗口判定；Agent-provided 历史里通常没有
                # 这个字段，DB 补全的才有 —— 缺省空串，AgentMoss 退回旧行为。
                "called_at": str(
                    getattr(h, "called_at", "")
                    or (h.get("called_at", "") if isinstance(h, dict) else "")
                    or ""
                ),
            }
            for h in action_history
        ]

        # Current action as a_next
        # called_at：当前动作的发生时刻。AgentMoss 的 L2 授权规则用它和历史里
        # ask_user_question 的 called_at 比较，判断「用户刚才的确认是否仍在
        # 时效窗内」（默认 300s）。缺了它时效窗整条失效，只剩「最近 5 条」兜底。
        _a_next = input_data.a_next or {}
        current_action = {
            "action_type": action_type,
            "action_detail": (
                input_data.command
                or str(_a_next.get("action_detail") or "")
                or input_data.description
            ),
            # SQLite CURRENT_TIMESTAMP 历史按 UTC 记录；当前动作也须带明确时区，
            # 避免非 UTC 主机上的本地时间被 Moss 当成 UTC 而错过时效窗口。
            "called_at": str(_a_next.get("called_at") or "")
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        # Convert prompt_history turns to dicts (interleaved prompt + actions)。
        # 历史动作统一过归一化（与当前动作同一套：配置 > 内置 > 启发式），
        # 否则不同命名风格的历史动作会让 L2 的读写/组合链降级。
        prompt_history_list = [
            {
                "text": t.text,
                "actions": [
                    {
                        "action_type": _mapped_history_type(a),
                        "action_detail": a.action_detail,
                        "output": getattr(a, "output", "") or "",
                        "called_at": getattr(a, "called_at", "") or "",
                    }
                    for a in t.actions
                ],
            }
            for t in input_data.prompt_history
        ]

        # 跨会话历史的动作类型同样归一化（DB/调用方存的都是原始 agent 工具名）
        session_history = [
            {
                **s,
                "action_history": [
                    {**h, "action_type": _mapped_history_type(h)}
                    if isinstance(h, dict)
                    else h
                    for h in (s.get("action_history") or [])
                ],
            }
            if isinstance(s, dict)
            else s
            for s in session_history
        ]

        hostname = context.os.hostname if context.os else socket.gethostname()
        # 上游显式指定的 os_type 优先：跨平台部署时（如 agent 在 Windows、
        # 网关在 Linux），AgentGate 所在环境 ≠ 被管控 agent 所在环境，
        # 必须以 agent 侧上报的为准，否则会拿错规则集判定。
        os_type = ""
        if isinstance(input_data.metadata, dict):
            os_type = str(input_data.metadata.get("os_type") or "").lower()
        if not os_type:
            os_type = context.os.system if context.os else ""
        if not os_type:
            import platform
            os_type = platform.system().lower()

        inject_incremental = stateful_incremental_enabled()
        logger.debug(
            "session_history source={} count={} db_limit={} stateful_incremental_injected={}",
            history_source, len(session_history), db_limit, inject_incremental,
        )
        return {
            "session_id": input_data.session_id,
            # 安装级实例 ID：展示层脱敏按它判断「同实例不隐藏」（与 TS normalizer.ts
            # 对齐）。调用方没带时用网关自己的持久化实例 ID。
            "instance_id": input_data.instance_id or get_instance_id(),
            "prompt_session": input_data.user_prompt,
            "prompt_history": prompt_history_list,
            "a_next": current_action,
            "cwd": input_data.cwd,
            "reason": input_data.description,
            "os_type": os_type,
            "action_history": action_history_list,
            "session_history": session_history,
            "metadata": {
                **input_data.metadata,
                # L2.4 stateful incremental：让 AgentMoss 把每条 tool call 计入该
                # agent 的持久化时间线并即时推进链指针（跨 session/长时距组合链
                # 的最后一步当场 Deny）。由 AGENT_GATE_MULTI_SESSION 门控。
                # ⚠️ 双端对齐：TS `normalizer.ts` 一直是这么注入的，Python 侧此前
                # 漏了 —— 导致同一份配置下 TS 链路走增量、Python 链路不走。
                **({"stateful_incremental": True} if inject_incremental else {}),
                # 未知工具名上报：AgentMoss 据此在响应 degraded 中提示
                # "动作类 L2 规则可能降级"，提醒补充 AGENT_GATE_TOOL_NAME_MAP。
                # 未知动作**不享受任何安全豁免**（不被当作安全动作放行）。
                **({"unmapped_tool": _raw_tool} if not _tool_recognized else {}),
                "agent_type": input_data.agent_type,
                "agent_version": input_data.agent_version,
                "hostname": hostname,
                "timestamp": time.time(),
            },
        }

    async def _run_sync(self, func, *args):
        """Run a sync function asynchronously (for thread-safe DB operations)."""
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, func, *args)
