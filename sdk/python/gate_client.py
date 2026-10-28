"""Python SDK for AgentGate — standalone client library.

This can be copied and used independently by any Python agent.
"""

import time
from typing import Any, Optional

import httpx


class GateClient:
    """HTTP client for the AgentGate analysis API.

    Usage:
        client = GateClient(base_url="http://127.0.0.1:9100")
        response = client.analyze(
            agent_type="claude-code",
            session_id="sess-001",
            tool_name="bash",
            command="ls -la",
        )
        if response["decision"] == "Allow":
            # Execute the command
            pass
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9100",
        timeout: float = 60.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_keepalive_connections=5),
        )

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
        metadata: Optional[dict] = None,
    ) -> dict:
        """Send a tool-call for security analysis.

        Returns:
            dict with: decision, reason, risk_level, risk_type,
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

        response = self._client.post(
            f"{self._base_url}/api/v1/analyze",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def health(self) -> dict:
        """Check AgentGate service health."""
        response = self._client.get(f"{self._base_url}/api/v1/health")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
