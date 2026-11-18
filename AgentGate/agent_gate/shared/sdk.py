"""Python SDK for AgentGate service.

Provides a simple, synchronous HTTP client for Agents to call AgentGate.

Usage:
    from agent_gate.shared.sdk import GateClient
    client = GateClient(base_url="http://127.0.0.1:9100")
    response = client.analyze(agent_type="claude-code", session_id="sess-001",
                               tool_name="bash", command="ls -la")
"""

import os
import time
from typing import Any, Optional

import httpx


class GateClient:
    """HTTP client for the AgentGate analysis API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9100",
        timeout: float = 60.0,
        api_token: str = "",
    ):
        """Create a client.

        Args:
            api_token: AgentGate 访问令牌。加固版默认开鉴权（``AGENT_GATE_AUTH_MODE=auto``），
                不带凭证会 401。留空时回退读环境变量 ``AGENT_GATE_AUTH_TOKEN`` ——
                **不会**去读服务端的 token 文件：调用方可能是另一台机器/另一个用户，
                静默读到别人的凭证反而是安全问题。
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_token = (api_token or os.environ.get("AGENT_GATE_AUTH_TOKEN", "")).strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_keepalive_connections=5),
        )

    def _headers(self) -> dict[str, str]:
        """鉴权头（未配置 token 时为空 —— 与加固前行为一致）。"""
        if not self._api_token:
            return {}
        return {"Authorization": f"Bearer {self._api_token}"}

    def analyze(
        self,
        agent_type: str,
        session_id: str,
        tool_name: str,
        command: str,
        agent_version: str = "",
        cwd: str = "",
        description: str = "",
        user_prompt: str = "",
        action_history: Optional[list[dict]] = None,
        prompt_history: Optional[list[dict]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Send a tool-call for security analysis.

        Args:
            agent_type: Type of agent (opendes, claude-code, generic, etc.)
            session_id: Unique session identifier.
            tool_name: Tool being called (bash, file_write, edit, etc.)
            command: The command or action being executed.
            agent_version: Version of the agent.
            cwd: Current working directory.
            description: Human-readable description.
            user_prompt: Original user task description.
            action_history: Optional history of previous actions.
                If not provided, AgentGate auto-fills from SQLite.
            prompt_history: Optional interleaved prompt history
                [{"text": "...", "actions": [{"action_type": "...", "action_detail": "..."}]}],
                ordered by time, so AgentMoss L2/L3 can see full session intent.
            metadata: Extra metadata.

        Returns:
            dict with keys: decision, reason, risk_level, risk_type,
            violated_layers, confidence, analysis_duration_ms, collected_context, source
        """
        payload = {
            "agent_type": agent_type,
            "agent_version": agent_version,
            "session_id": session_id,
            "cwd": cwd,
            "tool_name": tool_name,
            "command": command,
            "description": description,
            "user_prompt": user_prompt,
            "metadata": metadata or {},
        }
        if action_history is not None:
            payload["action_history"] = action_history
        if prompt_history is not None:
            payload["prompt_history"] = prompt_history

        response = self._client.post(
            f"{self._base_url}/api/v1/analyze",
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def health(self) -> dict:
        """Check AgentGate service health（``/api/v1/health`` 免鉴权，带头也无害）。"""
        response = self._client.get(
            f"{self._base_url}/api/v1/health",
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Legacy alias
AgentGateClient = GateClient
