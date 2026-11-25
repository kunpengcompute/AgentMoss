"""AgentGate 自身的安全策略 —— 来源校验 / 鉴权 / 限额 / 数据权限。

设计原则与 AgentMoss 的 ``agent_moss/infra/security_policy.py`` **完全一致**：

- 默认值要么不改变存量行为，要么**原文本身就是漏洞**（本文件处理的正是后者）；
- 把 ``AGENT_GATE_AUTH_MODE=off`` 设上，鉴权整体退回 0.7.6 的行为；
- 想完全恢复旧行为，再叠加 ``AGENT_GATE_ORIGIN_CHECK=0``。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| ``AGENT_GATE_AUTH_MODE`` | ``auto`` | ``auto``：首次启动自动生成 token 写到 ``<data_root>/auth_token``（0600），之后所有非 health 请求都要带它；``off``：完全关闭鉴权（旧行为） |
| ``AGENT_GATE_AUTH_TOKEN`` | 空 | 显式指定全局 token（优先级最高） |
| ``AGENT_GATE_AUTH_TOKEN_FD`` | 空 | 从**继承的 fd** 读 token（不落盘/不进 environ） |
| ``AGENT_GATE_AUTH_TOKEN_FILE`` | 空 | 从文件读 token |
| ``AGENT_GATE_AGENT_TOKENS`` | 空 | 每 agent 一个 token，``opendesk=t1,qwenpaw=t2``；启用后请求自报的 ``agent_type`` 必须与 token 匹配 —— 这是"仿冒 agent"的根治 |
| ``AGENT_GATE_ORIGIN_CHECK`` | ``1`` | 浏览器跨源请求拦截（**默认开**：只影响网页，不影响原生客户端） |
| ``AGENT_GATE_CORS_ORIGINS`` | 空 | 额外允许的跨源来源，逗号分隔 |
| ``AGENT_GATE_MAX_BODY_BYTES`` | 16MB | 请求体上限 |
| ``AGENT_GATE_MAX_INFLIGHT`` | 16 | 同时处理的最大请求数（**默认开启**）；0 = 不限（仅调试用） |
| ``AGENT_GATE_MAX_QUEUE`` | 256 | 超出并发后允许排队的数量 |
| ``AGENT_GATE_QUEUE_WAIT_SEC`` | 5 | 最多排队多少秒 |
| ``AGENT_GATE_MAX_CONNECTIONS`` | 256 | 连接层并发上限（uvicorn ``limit_concurrency``，**只覆盖 HTTP**） |
| ``AGENT_GATE_ENABLE_WS`` | ``0`` | 是否注册 ``/ws/events``（**默认不注册**：V1 看板走轮询，用不到 WS） |
| ``AGENT_GATE_MAX_WS_CONNECTIONS`` | 64 | WebSocket 同时在线连接上限（开启 WS 时生效；WS 不受 uvicorn 限制） |
| ``AGENT_GATE_KEEP_ALIVE_SEC`` | 5 | keep-alive 空闲上限 |
| ``AGENT_GATE_RATE_LIMIT_QPS`` | 100 | 单主体每秒请求数（**默认开启**）；0 = 不限（仅调试用） |
| ``AGENT_GATE_QUEUE_ABUSE_THRESHOLD`` | 10 | 窗口内排队被拒次数阈值（0 = 关闭） |
| ``AGENT_GATE_QUEUE_ABUSE_WINDOW`` | 60 | 排队拒绝统计窗口（秒） |
| ``AGENT_GATE_QUEUE_ABUSE_ACTION`` | ``log`` | ``log`` 只告警 / ``deny`` 返回 429 |
| ``AGENT_GATE_SESSION_SWEEP_THRESHOLD`` | 50 | 窗口内单主体访问的不同 session 数阈值（0 = 关闭） |
| ``AGENT_GATE_SESSION_SWEEP_WINDOW`` | 300 | 跨会话统计窗口（秒） |
| ``AGENT_GATE_SESSION_SWEEP_ACTION`` | ``log`` | ``log`` 只告警 / ``deny`` 返回 429 |

⚠️ 双端对称：TS 侧为 ``ts/src/shared/security.ts``，变量名与默认值必须一致。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("agent_gate.security")


# ==================== 基础读取 ====================

TRUTHY = ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _split(raw: str) -> list[str]:
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


# ==================== 来源校验（浏览器跨源） ====================

def origin_check_enabled() -> bool:
    return _env("AGENT_GATE_ORIGIN_CHECK", "1").lower() in TRUTHY


def allowed_origins() -> list[str]:
    """额外允许的来源（同源永远允许，不需要列在这里）。"""
    return _split(_env("AGENT_GATE_CORS_ORIGINS"))


def normalize_origin(origin: str) -> str:
    return (origin or "").strip().rstrip("/")


def origin_allowed(origin: str, host_header: str, extra: list[str] | None = None) -> bool:
    """跨源请求是否放行。

    - 没有 Origin 头 → 放行（原生客户端不发 Origin）
    - Origin 与 Host 同源 → 放行（Dashboard 页面自己）
    - 在 ``AGENT_GATE_CORS_ORIGINS`` 里 → 放行
    - 其他 → 拒绝
    """
    o = normalize_origin(origin)
    if not o:
        return True
    entries = [normalize_origin(x) for x in (extra if extra is not None else allowed_origins())]
    if o in entries:
        return True
    host = (host_header or "").strip()
    if not host:
        return False
    parsed = urlparse(o)
    return bool(parsed.netloc) and parsed.netloc == host


# ==================== 鉴权 ====================

#: 探活端点永远放行（部署探针/负载均衡用）。
PUBLIC_PATHS = ("/api/v1/health",)

#: Dashboard 页面本身：鉴权在页面处理器里做（支持 ``?token=`` 换 Cookie），
#: 中间件不拦，否则浏览器没法把 token 交上来。
DASHBOARD_PAGE_PATHS = ("/dashboard", "/dashboard/")

#: Dashboard 的 Cookie 名。浏览器 fetch 同源请求会自动带上，WebSocket 握手也会带。
DASHBOARD_COOKIE = "agent_gate_token"

#: Dashboard **HTML 页面**的安全响应头（JSON API 不加）。单一事实源：
#: V1 看板（FastAPI）与 V3 看板（http.server）共用同一份，避免两边漂移。
#:
#: - **CSP**：页面不引用任何外部资源（无 CDN / 外部字体 / 外部脚本），所以
#:   ``default-src`` 可以收到 ``'self'``。内联 ``<script>`` / ``<style>`` /
#:   ``onclick`` 必须保留 ``'unsafe-inline'`` —— 主要防线仍是渲染时的 HTML
#:   转义，CSP 负责挡住"注入 ``<script src=外部>`` / ``<iframe>`` / ``<object>``"
#:   这类绕过路径。
#: - **frame-ancestors 'none' + X-Frame-Options**：防点击劫持。看板承载全部
#:   命令原文与用户 prompt，被恶意页面 iframe 覆盖后诱导点击风险很高。
#: - **nosniff**：阻止浏览器把 JSON 当 HTML 解析。
#: - **no-referrer**：避免带 token 的地址经 Referer 外泄。
DASHBOARD_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

AUTH_MODE_AUTO = "auto"
AUTH_MODE_OFF = "off"

#: 解析结果缓存：(cache_key, token, agent_tokens)。键里只放**环境变量字符串**
#: （不含 auto_token_path() —— 那要走 resolve_data_path()，是本不该出现在每请求热路径的开销）。
_TOKEN_CACHE: tuple[tuple, str, dict[str, str], str] | None = None


def auth_mode() -> str:
    return (_env("AGENT_GATE_AUTH_MODE", AUTH_MODE_AUTO) or AUTH_MODE_AUTO).lower()


def _home_for_config() -> Path:
    """尊重 sudo：systemd/sudo 起的服务，HOME 是 /root，但凭据要落在真实用户目录。"""
    sudo_user = _env("SUDO_USER")
    if sudo_user:
        candidate = Path(f"/home/{sudo_user}")
        if candidate.exists():
            return candidate
    return Path.home()


def auth_token_path() -> Path:
    """凭据统一放 ``~/.config/<产品>/auth_token``。

    与 AgentMoss 的 ``~/.config/agentmoss/auth_token`` 同一约定 —— 两个产品的
    凭证都在 ``~/.config`` 下，运维只需要记一条规则。

    ⚠️ **数据目录不动**：SQLite 仍在 ``~/.local/share/agent_gate/``（XDG 数据目录）。
    那里是存量数据，挪了等于让用户以为历史丢了。token 是本版本新增的文件，
    没有存量包袱，所以可以放在语义更合适的 ``.config`` 下。
    """
    xdg = _env("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else _home_for_config() / ".config"
    return root / "agentgate" / "auth_token"


def _current_uid() -> int | None:
    """当前进程 uid；Windows 没有 ``os.getuid()``，返回 None。

    Windows 的文件权限是 ACL，``st_uid`` 与 POSIX 属主语义不对应；此时跳过
    属主校验，只做 chmod（尽力而为），否则鉴权 token 初始化会直接 AttributeError
    → 服务拒绝启动（2026-11 Windows 实测）。
    """
    getter = getattr(os, "getuid", None)
    return getter() if getter is not None else None


def ensure_private_dir(path: Path, mode: int = 0o700) -> bool:
    """目录不存在就建（0700）；已存在且属于自己但不是 0700 时收紧。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        st = path.stat()
        uid = _current_uid()
        if (uid is None or st.st_uid == uid) and (st.st_mode & 0o777) != mode:
            os.chmod(path, mode)
        return True
    except OSError as e:
        logger.debug("创建/收紧目录权限失败 %s: %s", path, e)
        return False


def harden_permissions(path: Path, mode: int) -> None:
    """文件权限收紧到 ``mode``；不属于自己或是符号链接时跳过。"""
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            return
        uid = _current_uid()
        if uid is not None and st.st_uid != uid:
            return
        if (st.st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except OSError as e:
        logger.debug("收紧文件权限失败 %s: %s", path, e)


#: 从继承的 fd 读到的 token（fd → token）。fd 只能读一次，缓存起来。
_FD_TOKEN_CACHE: dict[int, str] = {}


def _read_fd_token() -> str:
    """从**继承的文件描述符**读 token（``AGENT_GATE_AUTH_TOKEN_FD=3``）。

    写进文件/env 的 token，同 OS 用户的任何进程都能读（文件 → cat；
    env → /proc/<pid>/environ）。走 fd 时只有分配它的父进程和本进程持有，
    其他进程在文件系统和 environ 里都看不到。
    """
    raw = _env("AGENT_GATE_AUTH_TOKEN_FD")
    if not raw:
        return ""
    try:
        fd = int(raw)
    except ValueError:
        return ""
    if fd in _FD_TOKEN_CACHE:
        return _FD_TOKEN_CACHE[fd]
    try:
        data = os.read(fd, 8192)
    except OSError as e:
        logger.error("从 fd %d 读取访问 token 失败: %s", fd, e)
        _FD_TOKEN_CACHE[fd] = ""
        return ""
    try:
        os.close(fd)  # 读完即关，避免 fd 泄漏
    except OSError:
        pass
    tok = data.decode("utf-8", errors="ignore").strip()
    _FD_TOKEN_CACHE[fd] = tok
    if not tok:
        logger.error("fd %d 里没有读到访问 token", fd)
    return tok


def _read_explicit_token_result() -> tuple[str, str]:
    """读取显式 token，返回 ``(token, error)``。

    ``AGENT_GATE_AUTH_TOKEN_FILE`` 配了却读不到，不能静默回退到自动 token：
    那会把「配置写错」伪装成「鉴权已开启」，最坏情况下自动 token 也失败，
    服务就以无鉴权状态跑起来。显式配置错误必须由启动门禁拒绝。
    """
    tok = _env("AGENT_GATE_AUTH_TOKEN")
    if tok:
        return tok, ""
    tok = _read_fd_token()
    if tok:
        return tok, ""
    path = _env("AGENT_GATE_AUTH_TOKEN_FILE")
    if path:
        try:
            tok = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            return "", (
                f"AGENT_GATE_AUTH_TOKEN_FILE 已设置但无法读取 {path}: {e}。"
                "请确认文件存在、运行用户可读；或改用 AGENT_GATE_AUTH_TOKEN / "
                "AGENT_GATE_AUTH_TOKEN_FD；不需要鉴权则显式设 "
                "AGENT_GATE_AUTH_MODE=off"
            )
        if not tok:
            return "", (
                f"AGENT_GATE_AUTH_TOKEN_FILE 指向的文件为空: {path}。"
                "请写入 token，或显式设 AGENT_GATE_AUTH_MODE=off"
            )
        return tok, ""
    return "", ""


def _read_explicit_token() -> str:
    return _read_explicit_token_result()[0]


def _ensure_auto_token() -> str:
    """首次启动生成 token（0600）；已存在则读取。

    失败必须抛出异常，由 ``_resolve_auth`` 记入 ``auth_config_error``，最终由
    ``fail_fast_on_auth_error`` 拒绝启动。**不能返回空串继续运行** —— 那会
    静默退化成无鉴权服务。
    """
    path = auth_token_path()
    try:
        if path.is_file() and not path.is_symlink():
            tok = path.read_text(encoding="utf-8").strip()
            if tok:
                harden_permissions(path, stat.S_IRUSR | stat.S_IWUSR)
                return tok
        tok = secrets.token_urlsafe(32)
        if not ensure_private_dir(path.parent):
            raise RuntimeError(f"无法创建/收紧 token 目录（权限或属主异常）: {path.parent}")
        # 原子创建（O_EXCL）：多 worker 同时启动时只有一个能创建成功，其余读同一份 ——
        # 否则每个 worker 各生成一个，客户端只能猜中一个。
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, 0o600)
            try:
                os.write(fd, (tok + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            tok = path.read_text(encoding="utf-8").strip()
            if not tok:
                raise RuntimeError(f"自动 token 文件已存在但为空: {path}")
            return tok
        logger.warning(
            "已生成访问 token 并写入 %s（0600）。客户端需带 "
            "`Authorization: Bearer <token>`；查看：cat %s；"
            "如需关闭鉴权：AGENT_GATE_AUTH_MODE=off",
            path, path,
        )
        return tok
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"自动访问 token 生成/读取失败（{path}）：{e}") from e


def _parse_agent_tokens(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in _split(raw):
        if "=" in pair:
            agent, tok = pair.split("=", 1)
            if agent.strip() and tok.strip():
                out[agent.strip()] = tok.strip()
    return out


def _resolve_auth() -> tuple[str, dict[str, str], str]:
    """返回 ``(全局 token, 每 agent token, 配置错误信息)``，解析一次并缓存。

    缓存键只由环境变量字符串组成（见 ``_TOKEN_CACHE``）。
    """
    global _TOKEN_CACHE
    raw_agents = _env("AGENT_GATE_AGENT_TOKENS")
    key = (
        auth_mode(),
        _env("AGENT_GATE_AUTH_TOKEN"),
        _env("AGENT_GATE_AUTH_TOKEN_FILE"),
        _env("AGENT_GATE_AUTH_TOKEN_FD"),
        raw_agents,
        _env("AGENT_GATE_DATA_DIR"),
        _env("XDG_CONFIG_HOME"),
        _env("SUDO_USER"),
    )
    if _TOKEN_CACHE is not None and _TOKEN_CACHE[0] == key:
        return _TOKEN_CACHE[1], _TOKEN_CACHE[2], _TOKEN_CACHE[3]

    error = ""
    if auth_mode() == AUTH_MODE_OFF:
        # off 是显式关闭鉴权：token 配置即使坏了也不影响启动/请求。
        token = ""
    else:
        token, error = _read_explicit_token_result()

    # 显式指定了 fd 却读不到 —— **绝不静默降级**去磁盘生成 token。
    # 降级的后果是双输：调用方拿着自己的 token 全部 401（服务等于挂了），
    # 同时磁盘上多出一份同用户任何进程都能读的凭证（安全目标也丢了）。
    # 正确做法是明确报错、拒绝启动（见 fail_fast_on_auth_error）。
    if not token and not error and _env("AGENT_GATE_AUTH_TOKEN_FD") and auth_mode() != AUTH_MODE_OFF:
        error = (
            "AGENT_GATE_AUTH_TOKEN_FD 已设置，但没能从该文件描述符读到 token。"
            "拒绝降级到磁盘 token（那会在本机留下一份同用户可读的凭证）。"
        )
        logger.error(error)

    if not token and not error and auth_mode() != AUTH_MODE_OFF:
        try:
            token = _ensure_auto_token()
        except Exception as e:  # noqa: BLE001
            error = (
                f"{e}。请确认数据目录可写、权限正确、磁盘未满；"
                "不需要鉴权请显式设 AGENT_GATE_AUTH_MODE=off"
            )
            logger.error("鉴权配置错误，拒绝启动: %s", error)

    agents = _parse_agent_tokens(raw_agents)
    _TOKEN_CACHE = (key, token, agents, error)
    return token, agents, error


def auth_token() -> str:
    """当前的全局 token（``auto`` 模式下首次调用会生成）。"""
    return _resolve_auth()[0]


def agent_tokens() -> dict[str, str]:
    """``AGENT_GATE_AGENT_TOKENS`` → ``{agent_type: token}``。"""
    return _resolve_auth()[1]


def auth_config_error() -> str:
    """鉴权配置是否坏了（fd / 显式 token 文件 / 自动 token 生成失败）。空串 = 正常。"""
    return _resolve_auth()[2]


def fail_fast_on_auth_error() -> None:
    """启动时调用：鉴权配置错误就**直接退出**，不要带病运行。

    带病运行的两种表现都比退出更难排查：要么全部 401（看着像客户端问题），
    要么悄悄退回无鉴权（安全问题）。
    """
    err = auth_config_error()
    if err:
        logger.error("鉴权配置错误，拒绝启动: %s", err)
        raise SystemExit(1)


def auth_enabled() -> bool:
    if auth_mode() == AUTH_MODE_OFF:
        return False
    tok, agents, err = _resolve_auth()
    # 配置错误也属于「鉴权开启」：启动会被 fail_fast 拒绝，运行期请求也会
    # fail-closed；不能因为 token 为空就误判成「无需鉴权」。
    return bool(err) or bool(tok or agents)


def _bearer(headers) -> str:
    value = headers.get("authorization", "") if hasattr(headers, "get") else ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def principal_of(token: str) -> str:
    """凭证 → 统计主体。agent token 直接用 ``agent:<名字>``，便于按 agent 区分。"""
    if not token:
        return ""
    _tok, agents, _err = _resolve_auth()
    for agent, atok in agents.items():
        if _const_eq(token, atok):
            return f"agent:{agent}"
    return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def check_token(token: str) -> bool:
    """这个 token 是不是有效的（全局或任一 agent）。"""
    if not token:
        return False
    global_tok, agents, _err = _resolve_auth()
    if global_tok and _const_eq(token, global_tok):
        return True
    return any(_const_eq(token, t) for t in agents.values())


def is_global_token(token: str) -> bool:
    """这个 token 是不是**全局（管理员）** token。

    逐 agent token 只能证明"我是某个 agent"，不能证明"我是管理员"。
    """
    if not token:
        return False
    global_tok, _agents, _err = _resolve_auth()
    return bool(global_tok) and _const_eq(token, global_tok)


def bound_agent_of(principal: str) -> str:
    """principal → 绑定的 agent 名；全局 token / 未知返回空串。"""
    return principal[len("agent:"):] if principal.startswith("agent:") else ""


def check_request_auth(
    headers, path: str, cookie: str = "", require_global: bool = False
) -> tuple[bool, str]:
    """返回 ``(是否放行, 主体)``。

    - ``AUTH_MODE=off`` 或没有任何 token → 放行（保持历史行为）
    - 鉴权配置坏了（fd 读不到）→ **除 health 外全部拒绝**（fail-closed）
    - ``/api/v1/health`` 永远放行（探活）
    - Dashboard 的读接口接受 **Cookie** 作为凭证（浏览器 fetch 带不上自定义头）
    - 其余一律要求 ``Authorization: Bearer <token>``
    - ``require_global=True``：**逐 agent token 不放行**（看板是全局聚合视图，
      agent 只能看自己的数据，走读接口的归属校验，不走看板）
    """
    if auth_mode() == AUTH_MODE_OFF:
        return True, ""
    is_health = path.rstrip("/") in PUBLIC_PATHS
    global_tok, agents, err = _resolve_auth()
    if err:
        return (True, "") if is_health else (False, "")
    if not global_tok and not agents:
        return True, ""
    if is_health:
        return True, ""
    if cookie_path_allowed(path) and cookie and check_token(cookie):
        principal = principal_of(cookie)
        if require_global and not is_global_token(cookie):
            return False, principal
        return True, principal
    tok = _bearer(headers)
    if not tok:
        return False, ""
    if global_tok and _const_eq(tok, global_tok):
        return True, principal_of(tok)
    for _agent, atok in agents.items():
        if _const_eq(tok, atok):
            principal = principal_of(tok)
            if require_global:
                return False, principal
            return True, principal
    return False, ""


def bind_agent(principal: str, agent_type: str) -> tuple[bool, str]:
    """每 agent token 模式下，请求自报的 ``agent_type`` 必须与 token 绑定的一致。

    - 没配 ``AGENT_GATE_AGENT_TOKENS`` → 不校验（旧行为）
    - 用的是全局 token（控制台/运维）→ 不校验，它本来就有全量权限
    - 用的是 agent token → ``agent_type`` 必须等于绑定的名字
    """
    if auth_mode() == AUTH_MODE_OFF:
        return True, ""
    _tok, agents, _err = _resolve_auth()
    if not agents:
        return True, ""
    if not principal.startswith("agent:"):
        return True, ""
    bound = principal[len("agent:"):]
    claimed = (agent_type or "").strip()
    if not claimed:
        return False, f"该凭证绑定到 agent '{bound}'，请求必须声明 agent_type"
    if claimed.lower() != bound.lower():
        return False, f"agent_type '{claimed}' 与凭证绑定的 '{bound}' 不一致"
    return True, bound


def cookie_path_allowed(path: str) -> bool:
    """Cookie 能替代 Bearer 的路径。

    Dashboard / WebSocket / 只读 API 用 Cookie（浏览器 fetch 加不上自定义头），
    但 ``/api/v1/analyze`` 这个安全关键写路径**只认 Bearer**。
    CSRF 由 Cookie 的 ``SameSite=Strict`` 兜底。
    """
    if path == "/api/v1/analyze" or path.startswith("/api/v1/analyze/"):
        return False
    return (
        path == "/dashboard"
        or path.startswith("/dashboard/")
        or path.startswith("/ws/")
        or path.startswith("/api/")
    )


# ==================== 请求体 / 并发 / 限流 ====================

def max_body_bytes() -> int:
    return _env_int("AGENT_GATE_MAX_BODY_BYTES", 16 * 1024 * 1024)


# ==================== 存储治理（2026-10） ====================
#
# 目标：不让 tool_calls 无限增长撑爆磁盘，同时**不丢可查的旧数据**。
#   1. 单条上限：正常命令几 KB，超出上限只存头部 + 校验值；
#   2. 归档：攒够 N 条后把最老的整批挪进本地压缩文件（仍可查），主库只留热数据；
#   3. 磁盘水位：剩余空间低于 warn% 告警、低于 degrade% 时新记录降级为摘要。

def max_record_bytes() -> int:
    """单条命令/动作明细的字节上限；超出部分截断并附校验值。"""
    return _env_int("AGENT_GATE_MAX_RECORD_BYTES", 1024 * 1024)


def archive_threshold_records() -> int:
    """归档触发条数：主库超过这个条数时，把最老的归档走。0 = 不归档。"""
    return _env_int("AGENT_GATE_ARCHIVE_THRESHOLD", 100000)


def archive_keep_records() -> int:
    """归档后主库保留的条数（热数据）。"""
    return _env_int("AGENT_GATE_ARCHIVE_KEEP", 50000)


def archive_threshold_bytes() -> int:
    """主库达到多少字节开始归档（默认 512MB）。0 = 不按大小触发。"""
    return max(0, _env_int("AGENT_GATE_ARCHIVE_THRESHOLD_MB", 512)) * 1024 * 1024


def archive_keep_bytes() -> int:
    """归档后主库尽量不超过多少字节（默认 256MB）。0 = 不按大小保留。"""
    return max(0, _env_int("AGENT_GATE_ARCHIVE_KEEP_MB", 256)) * 1024 * 1024


def archive_max_bytes() -> int:
    """归档目录总大小上限（默认 512MB）。超过后删最老的归档。0 = 不按大小删。"""
    return max(0, _env_int("AGENT_GATE_ARCHIVE_MAX_MB", 512)) * 1024 * 1024


def archive_max_age_days() -> int:
    """归档文件最长保留天数（默认 0 = 不按时间删）。"""
    return max(0, _env_int("AGENT_GATE_ARCHIVE_MAX_AGE_DAYS", 0))


# ── v3_proxy_events（gateway/proxy 拦截的原始事件）归档 ──
# 与 tool_calls 同库不同表；事件体积大（arguments/prompt/response 原文），
# 长期运行会无限膨胀，因此单独一套阈值与保留窗口。


def proxy_archive_threshold() -> int:
    """v3_proxy_events 归档触发条数（默认 10 万）。0 = 不归档。"""
    return max(0, _env_int("AGENT_GATE_PROXY_ARCHIVE_THRESHOLD", 100000))


def proxy_archive_keep() -> int:
    """归档后 v3_proxy_events 保留条数（默认 5 万）。"""
    return max(0, _env_int("AGENT_GATE_PROXY_ARCHIVE_KEEP", 50000))


def proxy_archive_max_bytes() -> int:
    """proxy 归档目录总大小上限（默认 512MB）。超过后删最老的归档。0 = 不按大小删。"""
    return max(0, _env_int("AGENT_GATE_PROXY_ARCHIVE_MAX_MB", 512)) * 1024 * 1024


def proxy_archive_max_age_days() -> int:
    """proxy 归档文件最长保留天数（默认 0 = 不按时间删）。"""
    return max(0, _env_int("AGENT_GATE_PROXY_ARCHIVE_MAX_AGE_DAYS", 0))


def disk_warn_percent() -> int:
    """磁盘剩余空间告警水位（%）。"""
    return _env_int("AGENT_GATE_DISK_WARN_PERCENT", 5)


def disk_degrade_percent() -> int:
    """磁盘剩余空间低于此值时，新记录降级为摘要（%）。0 = 不降级。"""
    return _env_int("AGENT_GATE_DISK_DEGRADE_PERCENT", 2)



def max_inflight() -> int:
    """业务池并发上限（兼容别名，等价 ``AGENT_GATE_POOL_BIZ``）。"""
    return pool_limit("biz")


#: 路径 → 池名。池之间互不影响：Dashboard 打满只占自己的池。
_POOL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("biz", ("/api/v1/analyze",)),
    ("read", ("/api/v1/sessions", "/api/v1/agents", "/api/v1/tool_calls")),
    ("ops", ("/dashboard", "/ws/")),
    ("health", ("/api/v1/health",)),
)


def pool_of(path: str) -> str:
    """请求走哪个并发池。"""
    for name, prefixes in _POOL_RULES:
        for p in prefixes:
            if path == p or path.startswith(p + "/") or (p.endswith("/") and path.startswith(p)):
                return name
    return "other"


#: 池默认值。``biz`` 兼容旧的 ``AGENT_GATE_MAX_INFLIGHT``。
_POOL_DEFAULTS = {"biz": 16, "read": 8, "ops": 4, "health": 4, "other": 4}


def pool_limit(name: str) -> int:
    """某个池的并发上限。0 = 不限（仅调试用）。"""
    default = _POOL_DEFAULTS.get(name, 4)
    if name == "biz":
        default = _env_int("AGENT_GATE_MAX_INFLIGHT", default)
    return _env_int(f"AGENT_GATE_POOL_{name.upper()}", default)


def unauth_inflight() -> int:
    """**未认证请求**的全局并发上限。默认 4。

    未认证请求没有身份，只能靠一个很小的全局配额兜住：攻击者最多占 4 条，
    剩下的连接/并发留给带 token 的正常业务。``AUTH_MODE=off`` 时不生效
    （否则会把所有正常请求都当成未认证）。
    """
    return _env_int("AGENT_GATE_MAX_UNAUTH_INFLIGHT", 4)


def unauth_per_ip() -> int:
    """单个来源 IP 的未认证并发上限。默认 2（同机回环会共享这一份额度）。

    跨机部署时它是第二层：一个外部 IP 最多占 2 条，吃不满全局的 4 条。
    """
    return _env_int("AGENT_GATE_MAX_UNAUTH_PER_IP", 2)


def agent_inflight() -> int:
    """**单个 agent** 的并发上限（已认证请求按 principal 分桶）。默认 8。"""
    return _env_int("AGENT_GATE_MAX_AGENT_INFLIGHT", 8)


def max_queue() -> int:
    return _env_int("AGENT_GATE_MAX_QUEUE", 256)


def queue_wait_sec() -> float:
    return _env_float("AGENT_GATE_QUEUE_WAIT_SEC", 5.0)


def max_connections() -> int:
    return _env_int("AGENT_GATE_MAX_CONNECTIONS", 256)


def ws_enabled() -> bool:
    """是否注册 ``/ws/events``。**默认关**。

    V1 看板页面是 3 秒轮询（``/dashboard/stats`` 等），**根本不用 WebSocket** ——
    这个路由是设计文档里的实时推送能力，页面没接、TS 版也没实现。默认注册它
    等于白送一个独立的攻击面：WS 不走 HTTP 中间件，不受连接层上限约束。

    需要用实时推送的部署显式设 ``AGENT_GATE_ENABLE_WS=1``。
    """
    return _env("AGENT_GATE_ENABLE_WS", "0").lower() in TRUTHY


def max_ws_connections() -> int:
    """WebSocket 同时在线连接上限。**默认 64**；0 = 不限（仅调试用）。

    ⚠️ 这是独立的一层：uvicorn 的 ``limit_concurrency`` 只在 HTTP 协议实现里
    检查，**WebSocket 不受它约束** —— 不加这层，攻击者可以开任意多个 WS 连接
    把 asyncio task 和内存打满，而 HTTP 侧的 256 连接上限完全看不到这些连接。
    多 worker 部署时每个进程各自计数（上限按单进程算）。
    """
    return _env_int("AGENT_GATE_MAX_WS_CONNECTIONS", 64)


def socket_mode() -> int:
    """Unix socket 文件权限（八进制字符串，默认 ``600``）。

    UDS 模式没有网络面，socket 文件的权限就是唯一的内核级访问控制：以前跟随
    进程 umask（常见 0777），同机任何用户都能连。收紧到 0600 后只有属主能连。
    需要同组多用户共用一个 socket 时设 ``AGENT_GATE_SOCKET_MODE=660``。
    """
    raw = _env("AGENT_GATE_SOCKET_MODE", "600")
    try:
        mode = int(raw, 8)
    except ValueError:
        logger.warning("AGENT_GATE_SOCKET_MODE=%r 不是八进制，回退 600", raw)
        return 0o600
    if not 0 <= mode <= 0o777:
        logger.warning("AGENT_GATE_SOCKET_MODE=%r 超出范围，回退 600", raw)
        return 0o600
    return mode


def keep_alive_sec() -> int:
    return _env_int("AGENT_GATE_KEEP_ALIVE_SEC", 5)


def rate_limit_qps() -> int:
    """单主体每秒请求数上限。**默认 100（开启）**。

    限流防的是**调用方**把 AgentGate 打满（连接、内存、线程池、SQLite 写锁），
    与 AgentGate 在等谁、下游是什么负载特性无关。0 = 不限（仅调试用）。
    """
    return _env_int("AGENT_GATE_RATE_LIMIT_QPS", 100)


# ==================== 窗口计数（恶意排队 / 跨会话扫描） ====================

_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, dict] = {}


def _bump(bucket: str, key: str, window: float, extra: str | None = None) -> int:
    """在 ``window`` 秒窗口内给 ``key`` 计一次数，返回窗口内次数。"""
    now = time.monotonic()
    k = f"{bucket}|{key}"
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(k)
        if entry is None or now - entry["start"] >= window:
            entry = {"start": now, "n": 0, "seen": set()}
            _COUNTERS[k] = entry
        entry["n"] += 1
        if extra is not None:
            entry["seen"].add(extra)
        # 顺手清理过期桶，避免长期运行内存增长
        if len(_COUNTERS) > 4096:
            for kk in [x for x, v in _COUNTERS.items() if now - v["start"] >= window]:
                _COUNTERS.pop(kk, None)
        return len(entry["seen"]) if extra is not None else entry["n"]


def note_session_access(key: str, session_id: str) -> tuple[int, bool]:
    """记录一次会话访问，返回 ``(窗口内不同会话数, 是否超过阈值)``。"""
    threshold = _env_int("AGENT_GATE_SESSION_SWEEP_THRESHOLD", 50)
    if threshold <= 0:
        return 0, False
    window = _env_float("AGENT_GATE_SESSION_SWEEP_WINDOW", 300.0)
    n = _bump("sweep", key, window, extra=session_id or "-")
    return n, n > threshold


def session_sweep_action() -> str:
    return (_env("AGENT_GATE_SESSION_SWEEP_ACTION", "log") or "log").lower()


def note_queue_reject(key: str) -> tuple[int, bool]:
    """记录一次"排队被拒"，返回 ``(窗口内次数, 是否疑似恶意排队)``。"""
    threshold = _env_int("AGENT_GATE_QUEUE_ABUSE_THRESHOLD", 10)
    if threshold <= 0:
        return 0, False
    window = _env_float("AGENT_GATE_QUEUE_ABUSE_WINDOW", 60.0)
    n = _bump("queue", key, window)
    return n, n > threshold


def queue_abuse_action() -> str:
    return (_env("AGENT_GATE_QUEUE_ABUSE_ACTION", "log") or "log").lower()


def reset_counters() -> None:
    """测试用：清空窗口计数。"""
    with _COUNTERS_LOCK:
        _COUNTERS.clear()
    global _TOKEN_CACHE
    _TOKEN_CACHE = None


# ==================== 数据/配置落盘权限 ====================

def harden_files_enabled() -> bool:
    """默认开。同一台机器上 AgentMoss 与 AgentGate 用**不同用户**运行时，
    AgentMoss 的 ``/api/v1/review`` 直读这个库会因 0600 被拒 —— 那种部署设
    ``AGENT_GATE_HARDEN_FILES=0`` 恢复原权限（并自行用用户组控制访问）。"""
    return _env("AGENT_GATE_HARDEN_FILES", "1").lower() in TRUTHY


def harden_db_files(db_path) -> None:
    """SQLite 主库 + WAL/SHM 存的是**全部 agent 的命令与用户 prompt**。

    正常 umask 下它们是 0644 —— 同机任何用户都能直接拷走。这里收紧到 0600，
    目录收紧到 0700；不属于自己就跳过（不是我们该改的）。
    """
    if not harden_files_enabled():
        return
    p = Path(db_path)
    harden_permissions(p, stat.S_IRUSR | stat.S_IWUSR)
    for suffix in ("-wal", "-shm"):
        harden_permissions(Path(str(p) + suffix), stat.S_IRUSR | stat.S_IWUSR)
    # 只收紧 AgentGate 自己的数据目录（.../agent_gate/）。绝不 chmod 任意父目录 ——
    # 测试/临时部署里 db 可能就放在 /tmp，那会改掉共享目录的权限。
    try:
        if p.parent.name == "agent_gate":
            st = p.parent.stat()
            uid = _current_uid()
            if (uid is None or st.st_uid == uid) and (st.st_mode & 0o777) not in (0o700, 0o750):
                os.chmod(p.parent, 0o700)
    except OSError:
        pass


def describe() -> str:
    """启动时打印一行，让运维一眼看到当前安全姿态。"""
    err = auth_config_error()
    if err:
        auth = f"⚠️ 配置错误（拒绝启动）: {err}"
    elif auth_mode() == AUTH_MODE_OFF:
        auth = "off（不鉴权，等价加固前行为）"
    elif auth_enabled():
        agents = agent_tokens()
        auth = f"auto（token: {auto_token_hint()}）"
        if agents:
            auth += f"，每 agent 绑定: {','.join(sorted(agents))}"
    else:
        auth = "auto（token 生成失败，本次不鉴权）"
    return (
        f"安全策略: auth={auth} | origin_check={'on' if origin_check_enabled() else 'off'} "
        f"| 请求体上限={max_body_bytes()}B | 限流={rate_limit_qps() or 'off'}"
    )


def auto_token_hint() -> str:
    if auth_mode() == AUTH_MODE_OFF:
        return "-"
    if _env("AGENT_GATE_AUTH_TOKEN"):
        return "env"
    if _env("AGENT_GATE_AUTH_TOKEN_FD"):
        return "fd"
    if _env("AGENT_GATE_AUTH_TOKEN_FILE"):
        return _env("AGENT_GATE_AUTH_TOKEN_FILE")
    return str(auth_token_path())
