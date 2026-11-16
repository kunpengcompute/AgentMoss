"""AgentMoss HTTP/Socket client with Fail-Closed semantics.

Makes async HTTP calls to AgentMoss for security analysis.
If AgentMoss is unreachable, returns Deny (security-first).
"""

import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from ..config import get_config

#: AgentMoss 的 token 解析结果缓存：(值, 过期时间)。用 TTL 而不是永久缓存，
#: 这样 AgentMoss 首次启动生成 token 晚于 AgentGate 时能自愈，不用重启。
_MOSS_TOKEN_CACHE: tuple[str, float] = ("", 0.0)
_MOSS_TOKEN_TTL = 60.0


def _moss_token_path() -> Path:
    root = os.getenv("AGENT_MOSS_DATA_DIR", "").strip()
    if root:
        return Path(root) / "auth_token"
    # root（sudo/systemd/直接启动）下 Path.home() 就是 /root —— 与 AgentMoss
    # `get_data_root()` 完全一致：**不再**按 SUDO_USER 猜测真实用户目录。
    # 非 root 服务要访问 root 上 AgentMoss 的 token 时，必须显式配置
    # AGENT_GATE_MOSS_TOKEN / AGENT_MOSS_AUTH_TOKEN_FILE。
    return Path.home() / ".config" / "agentmoss" / "auth_token"


def resolve_moss_token(force: bool = False) -> str:
    """AgentMoss 默认开启鉴权（0.16.15+），AgentGate 调它时必须带凭证。

    优先级与 AgentMoss 侧一致：
    ``AGENT_MOSS_AUTH_TOKEN`` > ``AGENT_MOSS_AUTH_TOKEN_FILE`` > ``<数据目录>/auth_token``。
    另外支持 AgentGate 自己的覆盖 ``AGENT_GATE_MOSS_TOKEN``。
    """
    global _MOSS_TOKEN_CACHE
    now = time.monotonic()
    if not force and _MOSS_TOKEN_CACHE[1] > now:
        return _MOSS_TOKEN_CACHE[0]

    tok = os.getenv("AGENT_GATE_MOSS_TOKEN", "").strip()
    if not tok:
        tok = os.getenv("AGENT_MOSS_AUTH_TOKEN", "").strip()
    if not tok:
        f = os.getenv("AGENT_MOSS_AUTH_TOKEN_FILE", "").strip()
        if f:
            try:
                tok = Path(f).read_text(encoding="utf-8").strip()
            except OSError:
                tok = ""
    if not tok:
        token_path = _moss_token_path()
        try:
            tok = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "AgentMoss 鉴权 token 未找到（尝试路径: %s）；"
                "跨用户/跨机部署请用 AGENT_GATE_MOSS_TOKEN 或 "
                "AGENT_MOSS_AUTH_TOKEN_FILE 显式指定",
                token_path,
            )
            tok = ""

    _MOSS_TOKEN_CACHE = (tok, now + _MOSS_TOKEN_TTL)
    return tok


class MossClient:
    """Async HTTP client for AgentMoss security analysis service.

    Supports both HTTP TCP and Unix Domain Socket connections.
    Implements Fail-Closed: returns Deny when AgentMoss is unavailable.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ):
        cfg = get_config()
        moss_cfg = cfg.agent_moss

        # 地址解析（与 V3 CLI 的 _resolve_moss_url、TS 版 moss-client 同序）：
        #   显式参数 > AGENT_MOSS_URL > MOSS_URL > 配置文件 agent_moss.url > 默认
        # V1 的 server 子命令不走 _resolve_moss_url（它没有 --security-url），
        # 所以这里必须自己读环境变量 —— 否则 V1 部署在分离机器上时，
        # `AGENT_MOSS_URL=http://...` 会被静默忽略，一直连默认的 127.0.0.1:9090。
        resolved = base_url or os.environ.get("AGENT_MOSS_URL") or os.environ.get("MOSS_URL")
        self._base_url = (resolved or moss_cfg.get("url", "http://127.0.0.1:9090")).rstrip("/")
        # timeout 默认 100s：需覆盖 AgentMoss 总超时（90s）+ 余量，避免 AgentMoss
        # 还在分析时被 AgentGate 抢断后 fail-closed 误杀。
        self._timeout = timeout if timeout is not None else moss_cfg.get("timeout", 100.0)
        # max_retries 默认 0：AgentMoss 内部已有重试，AgentGate 这层不再重试，
        # 否则单次失败会翻倍耗时。用显式 None 判断避免 0 被 `or` 当 falsy。
        self._max_retries = (
            max_retries if max_retries is not None else moss_cfg.get("max_retries", 0)
        )
        self._retry_delay = moss_cfg.get("retry_delay", 1.0)

        # Determine transport
        if self._base_url.startswith("unix://"):
            socket_path = self._base_url[len("unix://"):]
            transport = httpx.AsyncHTTPTransport(uds=socket_path)
            self._base_url = "http://localhost"  # Placeholder host for UDS
        else:
            transport = None

        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self._timeout),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def health(self, timeout: float = 3.0) -> dict:
        """探活 AgentMoss（供 /api/v1/health 汇总，与 TS 的 fetchMossHealth 同构）。

        任何失败都不抛异常：网关自身健康与否不能因为身后服务没起来而 500。
        """
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/health", timeout=timeout,
            )
            if resp.status_code != 200:
                return {"moss_version": "N/A", "moss_status": "error"}
            data = resp.json()
            return {
                "moss_version": str(data.get("version") or "unknown"),
                "moss_status": str(data.get("status") or "unknown"),
            }
        except Exception:  # noqa: BLE001 — 探活失败只回报状态
            return {"moss_version": "N/A", "moss_status": "unreachable"}

    async def analyze(self, request: dict) -> dict:
        """Send analysis request to AgentMoss.

        Returns:
            dict with decision/reason/risk_level fields.
            On failure, returns Deny with error details (Fail-Closed).
        """
        last_error = ""
        attempt = 0
        max_attempts = self._max_retries + 1
        auth_retried = False
        while attempt < max_attempts:
            display_attempt = attempt + 1
            attempt += 1
            headers = {}
            token = resolve_moss_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = await self._client.post(
                    f"{self._base_url}/api/v1/analyze",
                    json=request,
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()
                logger.debug(
                    f"AgentMoss response: decision={result.get('decision')}, "
                    f"duration={result.get('analysis_duration_ms', 0)}ms"
                )
                return result

            except httpx.TimeoutException as e:
                last_error = f"AgentMoss timeout after {self._timeout}s"
                logger.warning(f"{last_error} (attempt {display_attempt})")

            except httpx.ConnectError as e:
                last_error = f"AgentMoss connection failed: {e}"
                logger.warning(f"{last_error} (attempt {display_attempt})")

            except httpx.HTTPStatusError as e:
                last_error = f"AgentMoss HTTP {e.response.status_code}"
                # 401：AgentMoss 可能刚首次启动生成了 token（我们缓存里还是空），
                # 或 token 被轮换。强制重读一次再试，不占用正常重试次数。
                if e.response.status_code == 401 and not auth_retried:
                    auth_retried = True
                    max_attempts += 1
                    resolve_moss_token(force=True)
                    logger.warning("AgentMoss 返回 401，已重新读取访问 token，重试一次")
                    continue
                logger.warning(f"{last_error} (attempt {display_attempt})")
                # Don't retry on 4xx
                if 400 <= e.response.status_code < 500:
                    break

            except Exception as e:
                last_error = f"AgentMoss unexpected error: {e}"
                logger.warning(f"{last_error} (attempt {display_attempt})")
                break

            # Wait before retry
            if attempt < max_attempts:
                import asyncio
                await asyncio.sleep(self._retry_delay)

        # All retries exhausted → Fail-Closed
        logger.error(f"AgentMoss failed, returning Deny: {last_error}")
        # 区分超时和连接失败，返回不同的 risk_type
        is_timeout = "timeout" in last_error.lower()
        risk_type = "analysis_timeout" if is_timeout else "service_unavailable"
        reason_prefix = "AgentMoss analysis timeout" if is_timeout else "AgentMoss security service unavailable"
        return self._fail_closed_response(last_error, risk_type, reason_prefix)

    async def review(self, agent_id: str = "", db_path: str = "") -> None:
        """低频回顾：让 AgentMoss 从 AgentGate 本地存储直读全量 tool_calls。

        与 TS 版 ``callAgentMossReview`` **一一对应**（此前只有 TS 实现，Python
        侧缺失属双端不一致，2026-10 补齐）：

        - **fire-and-forget**：调用方用 ``asyncio.create_task`` 触发、不 await；
          失败只记日志，绝不影响实时判定路径；
        - **30s 超时**：回顾是全量重打，比单条 analyze 快，但比健康检查慢；
        - **需携带 Bearer**：AgentMoss 默认开鉴权，复用 ``resolve_moss_token``。

        Args:
            agent_id: 目标 agent 标识（决定 AgentMoss 状态库分桶）。
            db_path: AgentGate 存储路径；空串直接跳过（与 TS 版一致）。
        """
        if not db_path:
            return

        url = f"{self._base_url}/api/v1/review"
        headers = {"Content-Type": "application/json"}
        token = resolve_moss_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = await self._client.post(
                url,
                json={"agent_id": agent_id, "db_path": db_path},
                headers=headers,
                # 单次覆盖：client 默认 100s 是给 analyze 留的，回顾不需要那么久
                timeout=httpx.Timeout(30.0),
            )
            if resp.status_code >= 400:
                logger.warning("AgentMoss review HTTP %s", resp.status_code)
            else:
                # 结果出口（2026-10）：此前只检查状态码，review 算出的全量性质
                # 违规（P3–P8 等）被直接丢弃 —— 全量范围等于静默漏报。这里把
                # violations 打出来，至少让"历史中存在完整攻击链"可见。
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    data = {}
                violations = data.get("violations") or []
                if violations:
                    logger.warning(
                        "AgentMoss 全量回顾发现 %d 条性质违规（agent=%s）: %s",
                        len(violations), agent_id,
                        ", ".join(str(v.get("property_id", "?")) for v in violations[:8]),
                    )
        except Exception as exc:  # noqa: BLE001
            # 与 analyze 不同：回顾失败**不降级为 Deny**，实时路径完全不受影响
            logger.warning("AgentMoss review unreachable: %s", exc)

    def _fail_closed_response(self, error: str, risk_type: str = "service_unavailable", reason_prefix: str = "AgentMoss security service unavailable") -> dict:
        return {
            "decision": "Deny",
            "reason": f"{reason_prefix} — blocking by default: {error}",
            "risk_level": "high",
            "risk_type": risk_type,
            "violated_layers": [],
            "policy": "",
            "violated_policy": "",
            "confidence": 100,
            "analysis_duration_ms": 0,
        }

    async def close(self) -> None:
        await self._client.aclose()
