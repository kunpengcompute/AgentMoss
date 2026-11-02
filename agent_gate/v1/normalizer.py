"""Normalizer: converts AgentGateInput + SystemContext → AgentMoss AnalyzeRequest.

Also integrates with Repository to auto-fill action_history from SQLite.
"""

import datetime
import socket
import time
from typing import Optional

from loguru import logger

from agent_gate.v1.multi_session import (
    multi_session_enabled,
    session_history_limit,
    stateful_incremental_enabled,
)
from agent_gate.v1.server.models import AgentGateInput, SystemContext
from agent_gate.v1.storage.repository import Repository


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
        if multi_session_enabled() and self._repo and input_data.session_id:
            try:
                # 注意：不按 agent 过滤（保守方向）——跨会话组合链的检测
                # 应覆盖全部历史 session，而非仅本 agent；agent_id 在 Repository
                # 里是复合键（agent_type:hostname），与输入 agent_type 不相等。
                session_history = await self._run_sync(
                    self._repo.get_recent_session_histories,
                    None,  # agent_id 不过滤
                    input_data.session_id,
                    # 默认 5，保持现有性能；需要更大实时覆盖面时用
                    # AGENT_GATE_SESSION_HISTORY_LIMIT 调大（上限 50）
                    session_history_limit(),
                )
            except Exception as e:
                logger.warning(f"Failed to get session_history from DB: {e}")

        # Map tool_name to a standard action_type that AgentMoss understands.
        # AgentMoss expects "bash" not "executeBash", "write" not "file_write", etc.
        _TOOL_TO_ACTION: dict[str, str] = {
            "executeBash": "bash",
            "executeZsh": "bash",
            "executePowerShell": "bash",
            "bash": "bash",
            "write_to_file": "write",
            "file_write": "write",
            "write": "write",
            "file_edit": "edit",
            "edit": "edit",
            "file_read": "read",
            "read_file": "read",
            "read": "read",
            "search_content": "grep",
            "search_file": "grep",
            "grep": "grep",
            "list_dir": "list_dir",
            "list_files": "list_dir",
            "glob": "glob",
            "web_fetch": "web_fetch",
            "web_search": "web_search",
            # QwenPaw（2026-09 对接）：工具名见 qwenpaw/agents/tools/*。
            # 缺这些映射时 action_type 保持 "execute_shell_command" 之类的原名，
            # AgentMoss 的 bash 类判定（action_type in ("bash", "executebash",
            # "terminal-executebash")）不成立 → read_before_write 等依赖 shell
            # 语义的 L2 规则整条失效。
            "execute_shell_command": "bash",
            "write_file": "write",
            "append_file": "write",
            "edit_file": "edit",
            "glob_search": "glob",
            "grep_search": "grep",
            "ast_search": "grep",
            "browser": "web_fetch",
        }
        action_type = _TOOL_TO_ACTION.get(input_data.tool_name, input_data.tool_name)

        # Prefer Agent-provided history, fallback to DB
        action_history = (
            input_data.action_history
            if input_data.action_history is not None
            else db_history
        )
        # Convert to dicts — and map the action_type through the SAME table as
        # the current action. History entries store the raw agent tool name
        # (routes.py writes `input_data.tool_name`), so without this an agent's
        # shell history arrives as "execute_shell_command" and AgentMoss does
        # not treat it as a shell action — read_before_write then sees no prior
        # command at all and its history-dependent verdicts silently degrade.
        action_history_list = [
            {
                "action_type": _TOOL_TO_ACTION.get(
                    h.action_type if hasattr(h, "action_type")
                    else str(h.get("action_type", "")),
                    h.action_type if hasattr(h, "action_type")
                    else str(h.get("action_type", "")),
                ),
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
        current_action = {
            "action_type": action_type,
            "action_detail": input_data.command or input_data.description,
            "called_at": datetime.datetime.now().isoformat(),
        }

        # Convert prompt_history turns to dicts (interleaved prompt + actions)
        prompt_history_list = [
            {
                "text": t.text,
                "actions": [
                    {
                        "action_type": a.action_type,
                        "action_detail": a.action_detail,
                        "output": getattr(a, "output", "") or "",
                        "called_at": getattr(a, "called_at", "") or "",
                    }
                    for a in t.actions
                ],
            }
            for t in input_data.prompt_history
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

        return {
            "session_id": input_data.session_id,
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
                **({"stateful_incremental": True} if stateful_incremental_enabled() else {}),
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
