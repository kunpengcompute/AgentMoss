"""FastAPI application factory with HTTP and Unix Socket support."""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from . import security as sp
from .dashboard import dashboard_router, router as dash_api_router
from .middleware import RequestLoggingMiddleware
from .routes import router


#: 页面级鉴权自己处理的路径（浏览器没法把 Bearer 头交给 HTML 导航）。
_PUBLIC_PAGE_PATHS = ("/dashboard", "/dashboard/")


def _is_public_page(path: str) -> bool:
    """Dashboard 的 HTML 页面：凭证在页面处理器里换 Cookie。"""
    return path in _PUBLIC_PAGE_PATHS


class _PooledLimiter:
    """分层并发闸门：未认证全局 → 未认证单 IP → agent → 路径池。

    全部**快速拒绝**，不排队 —— 排队会让请求继续占着连接和内存，反而放大
    DoS（攻击者只要把队列灌满就能让正常请求全部超时）。

    计数按进程：多 worker 部署时每个进程各算各的，实际总配额会放大到
    ``worker 数 × 配置值``（文档已说明）。
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        async with self._lock:
            cur = self._counts.get(key, 0)
            if cur >= limit:
                return False
            self._counts[key] = cur + 1
            return True

    async def release(self, key: str) -> None:
        async with self._lock:
            cur = self._counts.get(key, 0)
            if cur <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = cur - 1


def _busy(detail: str, status: int = 503) -> JSONResponse:
    """并发超限响应。

    带 ``Connection: close``：让调用方处理完这次拒绝就断开，别把连接继续
    占在池子里 —— 这是应用层能对"连接被占满"做的最实际的一件事。
    """
    return JSONResponse(
        status_code=status,
        content={"detail": detail},
        headers={"Retry-After": "1", "Connection": "close"},
    )


def _unauth_ip_key(request) -> str:
    return "unauth:ip:" + (getattr(request.client, "host", "") or "-")


class _RateLimiter:
    """按主体的令牌桶。``qps<=0`` 时不生效（默认）。"""

    def __init__(self, qps: int, burst: Optional[int] = None):
        self._qps = qps
        self._burst = burst if burst is not None else max(qps, 1)
        self._buckets: dict[str, tuple[float, float]] = {}

    @property
    def enabled(self) -> bool:
        return self._qps > 0

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - last) * self._qps)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        if len(self._buckets) > 4096:
            cutoff = now - 60
            for k in [k for k, v in self._buckets.items() if v[1] < cutoff]:
                self._buckets.pop(k, None)
        return True


_pools = _PooledLimiter()
_limiter = _RateLimiter(sp.rate_limit_qps())


def _rate_key(request, principal: str) -> str:
    if principal:
        return principal
    host = getattr(request.client, "host", "") or "-"
    return "ip:" + host


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AgentGate",
        description="AI Agent Data Collection & Security Analysis Gateway",
        version="0.2.2",
    )

    # Middleware
    #
    # CORS 只回显白名单来源：以前是 ``allow_origins=["*"]`` + ``allow_credentials=True``，
    # 等于"任意网页都能读到 /api/v1/sessions/* 里的用户 prompt 和全部命令"。
    # Dashboard 是同源页面（相对路径 fetch），不需要 CORS；原生客户端也不发 Origin。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sp.allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.middleware("http")
    async def _security_guard(request, call_next):
        """来源校验 / 鉴权 / 体积上限 / 限流 / 有界并发。

        顺序刻意如此：先做最便宜的判断，被拒的请求不再进入分析路径 ——
        这本身就是 DoS 的第一道闸。
        """
        path = request.url.path
        method = request.method.upper()

        # 0) 预检交给 CORS 中间件处理（它决定是否回 allow-* 头）
        if method == "OPTIONS":
            return await call_next(request)

        # 1) 请求体大小上限
        max_body = sp.max_body_bytes()
        cl = request.headers.get("content-length")
        if cl and max_body > 0:
            try:
                if int(cl) > max_body:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"请求体超过上限 {max_body} 字节"},
                    )
            except ValueError:
                pass

        # 2) 浏览器跨源拦截（原生客户端不带 Origin → 不受影响）
        if sp.origin_check_enabled():
            origin = request.headers.get("origin", "")
            if origin and not sp.origin_allowed(origin, request.headers.get("host", "")):
                logger.warning(
                    "已拦截跨源请求: origin={} path={}（如需放行，把来源加入 "
                    "AGENT_GATE_CORS_ORIGINS）", origin, path,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"跨源来源未被允许: {origin}"},
                )

        # 3) 鉴权
        cookie = request.cookies.get(sp.DASHBOARD_COOKIE, "")
        # 看板（页面 + /dashboard/* API）与 WS 是**全局聚合视图**：逐 agent token
        # 只能证明"我是某个 agent"，看不到别人的数据 —— 这里直接拒绝，
        # agent 自查走读接口（按归属过滤），不走看板。
        require_global = path.startswith("/dashboard") or path.startswith("/ws/")
        ok_auth, principal = sp.check_request_auth(
            request.headers, path, cookie, require_global=require_global
        )
        if not ok_auth and not _is_public_page(path):
            if principal and sp.bound_agent_of(principal):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "该凭证绑定到 agent，无权访问看板；请使用全局 token"
                    },
                )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: invalid or missing Bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 身份透传给下游路由（analyze 的逐 agent 绑定要用它）。
        # ⚠️ 必须在鉴权成功后**立刻**设置：下面「有界并发」分支会直接
        # `return await call_next(request)`，如果放在函数末尾，开启并发后
        # `/api/v1/analyze` 永远拿不到 principal → bind_agent("") 恒放行，
        # 逐 agent token 的仿冒防护静默失效（2026-10 实测复现）。
        if principal:
            request.state.principal = principal

        # 4) 限流（0 = 不限，默认）
        if _limiter.enabled and (path.startswith("/api/") or path.startswith("/dashboard/")):
            if not await _limiter.allow(_rate_key(request, principal)):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后重试"},
                    headers={"Retry-After": "1"},
                )

        # 5) 分层并发闸门（快速拒绝，不排队）
        #    未认证全局 → 未认证单 IP → agent → 路径池，四层都拿到才放行。
        _keys: list[str] = []

        async def _take(key: str, limit: int) -> bool:
            if await _pools.try_acquire(key, limit):
                _keys.append(key)
                return True
            return False

        try:
            # 5.1 未认证配额。AUTH_MODE=off 时跳过：没有身份是配置选择，
            #     不是攻击，不能把正常业务全当成未认证。
            if not principal and sp.auth_enabled():
                if not await _take("unauth:total", sp.unauth_inflight()):
                    return _busy("未认证请求并发超限，请持凭证访问")
                if not await _take(_unauth_ip_key(request), sp.unauth_per_ip()):
                    return _busy("该来源的未认证请求并发超限")
            else:
                _bound = sp.bound_agent_of(principal)
                if _bound and not await _take(f"agent:{_bound}", sp.agent_inflight()):
                    return _busy(f"agent '{_bound}' 的并发超限，请稍后重试", 429)

            # 5.2 路径池：Dashboard 打满只占自己的 ops 池，不挤占 analyze 的 biz 池
            _pool = sp.pool_of(path)
            if not await _take(f"pool:{_pool}", sp.pool_limit(_pool)):
                # 并发被拒的频次统计：同一主体反复撞池上限，说明是在刷
                qkey = _rate_key(request, principal)
                q_n, abusive = sp.note_queue_reject(qkey)
                if abusive and sp.queue_abuse_action() == "deny":
                    return _busy("并发拒绝过于频繁，疑似恶意请求", 429)
                if abusive:
                    logger.warning(
                        "⚠️ 疑似恶意并发占用: key=%s 窗口内被拒 %s 次", qkey, q_n,
                    )
                return _busy("服务繁忙（并发已满），请稍后重试")

            return await call_next(request)
        finally:
            while _keys:
                await _pools.release(_keys.pop())

    # Routes
    app.include_router(router)               # /api/v1/*
    app.include_router(dash_api_router)       # /dashboard/stats, /dashboard/recent, etc.
    # /ws/events：**默认不注册**。V1 看板是 3 秒轮询，页面根本不用 WS；
    # 注册它等于白送一个攻击面（WS 不走 HTTP 中间件、不受连接层上限约束）。
    if sp.ws_enabled():
        app.include_router(dashboard_router)  # /ws/events（需 AGENT_GATE_ENABLE_WS=1）
    else:
        logger.info("WebSocket /ws/events 未注册（默认关闭；需要时设 AGENT_GATE_ENABLE_WS=1）")

    return app


def _print_dashboard_urls(host: str, port: int) -> None:
    """Print accessible dashboard URLs, detecting WSL environment."""
    import re
    urls = [f"http://localhost:{port}/dashboard/"]

    # Detect WSL
    is_wsl = False
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower() or "wsl" in f.read().lower():
                is_wsl = True
    except Exception:
        pass

    # Detect actual hostname for LAN access
    try:
        import socket as _sock
        hostname = _sock.gethostname()
        ip = _sock.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            urls.append(f"http://{ip}:{port}/dashboard/")
    except Exception:
        pass

    if is_wsl:
        urls.insert(0, "http://localhost:{}/dashboard/  ← 从 Windows 浏览器打开".format(port))
    else:
        urls[0] = f"  {urls[0]}"

    logger.info("Dashboard:")
    for u in urls:
        logger.info(f"  {u}")

    # 鉴权开启时，浏览器第一次必须靠 ?token= 换 Cookie（之后同源请求自动带上）。
    # 不打印 token 的话，运维只能自己去 cat 文件 —— 体验太差，且标准部署本来就是
    # 同一台机器的同一个用户。
    if sp.auth_enabled():
        tok = sp.auth_token()
        if tok:
            url = f"http://localhost:{port}/dashboard/?token={tok}"
            # 非交互输出（宿主把子进程日志转存到文件）打码：原样落盘等于把全局
            # token 写进可读日志。交互终端保留完整链接，方便直接打开。
            if not sys.stderr.isatty():
                import re as _re

                url = _re.sub(r"([?&]token=)[^&\s\"']+", r"\1***", url)
            logger.info("  首次打开（浏览器不支持自定义 Authorization 头，需用此链接换 Cookie）:")
            logger.info(f"    {url}")
            logger.info("  Cookie 有效期到浏览器关闭为止；换机器/换浏览器需重新带一次 token。")


def run_server(
    host: str = "0.0.0.0",
    port: int = 9100,
    mode: str = "http",
    socket_path: Optional[str] = None,
    log_level: str = "info",
    workers: int = 1,
    db_path: Optional[str] = None,
):
    """Start the AgentGate server.

    Args:
        host: Bind host (HTTP mode). **保持 0.0.0.0 不变** —— 改成回环会破坏跨机
            采集。访问控制由鉴权承担（``AGENT_GATE_AUTH_MODE=auto``，无凭证 401）。
        port: Bind port (HTTP mode).
        mode: 'http' or 'socket'.
        socket_path: Unix socket path (socket mode).
        log_level: Logging level.
        workers: Number of worker processes.
        db_path: SQLite database path override.
    """
    # Configure logger
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        level=log_level.upper(),
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )

    # Initialize database
    from ..storage.db import get_db
    db = get_db(db_path)
    logger.info(f"Database path: {db_path or 'default'}")
    # 库里是全量命令与用户 prompt，收紧到 0600 / 目录 0700
    sp.harden_db_files(db.db_path)

    logger.info(sp.describe())
    # 鉴权配置坏了（例如显式指定了 AUTH_TOKEN_FD 却读不到）就**不要带病运行**：
    # 要么全部 401（看着像客户端问题），要么悄悄退回无鉴权（安全问题）。
    sp.fail_fast_on_auth_error()

    app = create_app()

    # 存储维护：启动时跑一次 + 每 6 小时一次（条数到阈值才真正动作）。
    # 后台线程，daemon=True，不阻塞 uvicorn，也不影响请求热路径。
    try:
        from ..storage.maintenance import start_storage_maintenance

        start_storage_maintenance()
    except Exception as exc:  # noqa: BLE001
        logger.warning("存储维护启动失败: %s", exc)

    if mode == "socket":
        if socket_path is None:
            socket_path = "/var/run/agent_gate/agent_gate.sock"
        socket_dir = Path(socket_path).parent
        socket_dir.mkdir(parents=True, exist_ok=True)

        # Remove existing socket file
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        logger.info(f"Starting AgentGate in Unix Socket mode: {socket_path}")
        # socket 文件权限就是 UDS 模式唯一的内核级访问控制：以前跟随进程 umask
        # （常见 0777），同机任何用户都能连。这里用 umask 保证以
        # AGENT_GATE_SOCKET_MODE（默认 0600）创建；同组共用时设 660。
        _socket_mode = sp.socket_mode()
        _old_umask = os.umask(0o777 & ~_socket_mode)
        logger.info("Socket 权限: %04o（AGENT_GATE_SOCKET_MODE）", _socket_mode)
        try:
            uvicorn.run(
                app,
                uds=socket_path,
                log_level=log_level,
                limit_concurrency=sp.max_connections(),
                timeout_keep_alive=sp.keep_alive_sec(),
            )
        finally:
            os.umask(_old_umask)
    else:
        if host in ("0.0.0.0", "::"):
            # 只提示，不改行为：监听地址保持不变，访问控制交给鉴权。
            logger.info(
                f"监听 {host}（网络可达）。访问控制由鉴权承担："
                + ("已开启，无凭证一律 401" if sp.auth_enabled() else "⚠️ 当前未开启鉴权")
            )
        logger.info(f"Starting AgentGate in HTTP mode: {host}:{port}")
        _print_dashboard_urls(host, port)
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=log_level,
            workers=workers,
            limit_concurrency=sp.max_connections(),
            timeout_keep_alive=sp.keep_alive_sec(),
        )
