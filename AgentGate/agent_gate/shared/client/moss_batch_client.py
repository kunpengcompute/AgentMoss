"""AgentMoss batch client for v2 daemon — sends full session data on trigger.

Unlike v1's per-command analysis, v2 sends the complete session behavior
sequence + payload data only when a trigger rule matches.
"""

import json
import logging
import os
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_token() -> str:
    """复用主客户端的 token 解析链（显式 > 文件 > 自动生成的文件）。

    延迟导入：``moss_client`` 会拉 ``..config``，模块级导入容易把 CLI 的
    导入链拖长；这里失败也只返回空串（退化为"不带凭证"，与旧行为一致）。
    """
    try:
        from .moss_client import resolve_moss_token

        return resolve_moss_token()
    except Exception:  # noqa: BLE001
        return ""


class MossBatchClient:
    """Synchronous HTTP client for AgentMoss batch reporting.

    Used by the v2 daemon for trigger-based reporting. Unlike the v1 async
    client, this is synchronous to simplify daemon integration.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9090",
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._async_queue: list[dict] = []
        self._async_lock = threading.Lock()
        self._async_thread: Optional[threading.Thread] = None
        self._running = False

    def analyze_sync(self, request: dict) -> dict:
        """Send analysis request synchronously.

        Returns AgentMoss response or fail-closed Deny.
        """
        import urllib.request
        import urllib.error

        url = f"{self._base_url}/api/v1/analyze"
        data = json.dumps(request).encode('utf-8')

        last_error = ""
        for attempt in range(self._max_retries + 1):
            try:
                # AgentMoss 默认开鉴权（0.16.15+）：不带 Bearer 会 401，而本客户端
                # 是 **fail-closed** 的 —— 401 会被当成"服务不可用"直接 Deny，
                # 于是 L3/seccomp 路径上的所有动作都会被误杀。token 每轮重读，
                # 让 AgentMoss 晚于 AgentGate 首次生成 token 时能自愈。
                headers = {"Content-Type": "application/json"}
                token = _resolve_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                    logger.debug(
                        "AgentMoss response: decision=%s duration=%sms",
                        result.get('decision'),
                        result.get('analysis_duration_ms', 0),
                    )
                    return result

            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if 400 <= e.code < 500:
                    break
            except urllib.error.URLError as e:
                last_error = f"Connection failed: {e.reason}"
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                break

            logger.warning("AgentMoss attempt %d failed: %s", attempt + 1, last_error)
            if attempt < self._max_retries:
                time.sleep(1.0)

        logger.error("AgentMoss unreachable, returning Deny: %s", last_error)
        return self._fail_closed_response(last_error, request=request)

    def report_async(self, report: dict):
        """Queue a report for async sending (non-blocking).

        Used for local-block audit reporting that doesn't need to wait
        for AgentMoss response.
        """
        with self._async_lock:
            self._async_queue.append(report)

        if self._async_thread is None or not self._async_thread.is_alive():
            self._running = True
            self._async_thread = threading.Thread(target=self._flush_async, daemon=True)
            self._async_thread.start()

    def _flush_async(self):
        """Flush async report queue."""
        while True:
            with self._async_lock:
                if not self._async_queue:
                    break
                batch = list(self._async_queue)
                self._async_queue.clear()

            for report in batch:
                try:
                    self.analyze_sync(report)
                except Exception as e:
                    logger.error("Async report failed: %s", e)

    def build_session_report(
        self,
        session: dict,
        behavior_sequence: list[dict],
        payload_sequences: Optional[list[dict]] = None,
        trigger_type: str = "behavior",
        trigger_rule: str = "",
        trigger_detail: str = "",
    ) -> dict:
        """Build a batch report for AgentMoss from session data.

        This is the v2 equivalent of v1's per-command AnalyzeRequest.
        Instead of sending one event at a time, v2 sends the full session.
        """
        # Build action_history from behavior_sequence (AgentMoss compatible format)
        action_history = []
        for behavior in behavior_sequence:
            action_type = behavior.get("action", "unknown")
            # Map v2 action names to AgentMoss action types
            action_map = {
                "execute": "bash",
                "file_access": "read",
                "file_write": "write",
                "network_connect": "network",
                "subprocess_create": "bash",
                "process_exit": "bash",
            }
            action_history.append({
                "action_type": action_map.get(action_type, action_type),
                "action_detail": behavior.get("detail", ""),
            })

        return {
            "trigger_type": trigger_type,
            "trigger_rule": trigger_rule,
            "trigger_detail": trigger_detail,
            "session": session,
            "behavior_sequence": behavior_sequence,
            "payload_sequences": payload_sequences or [],
            "action_history": action_history,
        }

    def build_proxy_session_report(
        self,
        session: dict,
        proxy_events: list[dict],
        trigger_type: str = "behavior",
        trigger_rule: str = "",
        trigger_detail: str = "",
    ) -> dict:
        """Build a batch report from v3 proxy events.

        Converts LLM API tool_call events into AgentMoss action_history format.
        """
        action_history = []
        for evt in proxy_events:
            action = evt.get("action", {})
            target = evt.get("target", {})
            tool_name = action.get("tool_name", "unknown")
            command = target.get("command", "")
            args_raw = action.get("arguments_raw", "")
            detail = command or args_raw[:200]

            action_history.append({
                "action_type": tool_name,
                "action_detail": detail,
            })

        return {
            "trigger_type": trigger_type,
            "trigger_rule": trigger_rule,
            "trigger_detail": trigger_detail,
            "session": session,
            "behavior_sequence": [],
            "payload_sequences": [],
            "action_history": action_history,
        }

    def _fail_closed_response(self, error: str, request: dict | None = None) -> dict:
        mode = os.getenv("AGENT_GATE_MOSS_FAIL_MODE", "closed").strip().lower()
        if mode not in ("closed", "open", "readonly_open"):
            mode = "closed"
        action_type = ""
        if isinstance(request, dict):
            a_next = request.get("a_next") or {}
            action_type = str(
                request.get("action_type")
                or (a_next.get("action_type") if isinstance(a_next, dict) else "")
                or ""
            ).lower()
        readonly = action_type in ("read", "list_dir", "glob", "grep") or action_type.startswith("read")
        if mode == "open" or (mode == "readonly_open" and readonly):
            return {
                "decision": "Allow",
                "reason": f"AgentMoss unavailable — fail_open({mode}): {error}",
                "risk_level": "low",
                "risk_type": "service_unavailable",
                "violated_layers": [],
                "intercept_actions": [],
                "analysis_duration_ms": 0,
                "degraded": [f"gateway_moss_unavailable: {error}"],
                "ask_recommended": False,
                "ask_reason": "",
            }
        return {
            "decision": "Deny",
            "reason": f"AgentMoss unavailable — blocking by default: {error}",
            "risk_level": "high",
            "risk_type": "service_unavailable",
            "violated_layers": [],
            "intercept_actions": [],
            "analysis_duration_ms": 0,
            "ask_recommended": True,
            "ask_reason": "AgentMoss 不可用，无法完成安全检查；建议向用户确认是否继续。",
        }
