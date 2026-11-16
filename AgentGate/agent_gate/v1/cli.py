"""AgentGate V1 CLI — 分析服务与数据库维护。

V1 是 SDK / Agent 主动上报模式：Agent 每次工具调用前调用
``POST /api/v1/analyze``，AgentGate 采集系统上下文并转发给 AgentMoss 判定。
"""

import argparse
import os
import sys
from pathlib import Path

from .. import __version__
from .config import get_config, resolve_storage_db_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-gate",
        description="AgentGate V1 - AI Agent Data Collection & Security Analysis Service",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # ---- server ----
    server_parser = subparsers.add_parser(
        "server", help="Start AgentGate V1 analysis server"
    )
    server_parser.add_argument(
        "--mode", choices=["http", "socket"], default="http",
        help="Server mode: http (TCP) or socket (Unix Domain Socket)",
    )
    server_parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind host (HTTP mode only, default: 0.0.0.0). "
             "网络可达性由鉴权把关（AGENT_GATE_AUTH_MODE=auto，没有凭证一律 401）",
    )
    server_parser.add_argument(
        "--port", type=int, default=9100,
        help="Bind port (HTTP mode only, default: 9100)",
    )
    server_parser.add_argument(
        "--socket", dest="socket_path",
        help="Unix socket path (socket mode only)",
    )
    server_parser.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"],
        default="info", help="Log level",
    )
    server_parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of worker processes",
    )
    server_parser.add_argument(
        "--config", type=Path, dest="config_path",
        help="Path to YAML config file",
    )
    server_parser.add_argument(
        "--db-path", type=str, dest="db_path",
        help="SQLite database path override",
    )
    server_parser.add_argument(
        "--security-url", dest="security_url",
        help="AgentMoss service URL (default: $AGENT_MOSS_URL / $MOSS_URL / config / 9090)",
    )
    server_parser.add_argument(
        "--moss-url", dest="moss_url", help="Alias of --security-url"
    )

    # ---- clean ----
    clean_parser = subparsers.add_parser("clean", help="Clean old database records")
    clean_parser.add_argument(
        "--retention-days", type=int, default=30,
        help="Days to retain records (default: 30)",
    )
    clean_parser.add_argument(
        "--config", type=Path, dest="config_path",
        help="Path to YAML config file",
    )

    return parser


def _run_server(args) -> None:
    cfg = get_config(args.config_path)
    server_cfg = cfg.server

    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 9100)
    mode = args.mode or server_cfg.get("mode", "http")
    socket_path = args.socket_path or server_cfg.get("socket_path")
    log_level = args.log_level or server_cfg.get("log_level", "info")
    workers = args.workers or server_cfg.get("workers", 1)
    # storage.db_path 默认是哨兵值 "auto"；这里先解析成绝对路径，
    # 避免 SQLite 在当前工作目录创建名为 auto 的文件。
    db_path = str(
        resolve_storage_db_path(args.db_path or cfg.storage.get("db_path"))
    )

    # AgentMoss 地址：--security-url > AGENT_MOSS_URL > MOSS_URL > 配置 > 9090。
    # MossClient 自己会读环境变量；这里把 CLI 显式传入的值落到环境里。
    security_url = getattr(args, "security_url", None) or getattr(args, "moss_url", None)
    if security_url:
        os.environ["AGENT_MOSS_URL"] = security_url

    from .server.app import run_server
    run_server(
        host=host,
        port=port,
        mode=mode,
        socket_path=socket_path,
        log_level=log_level,
        workers=workers,
        db_path=db_path,
    )


def _run_clean(args) -> None:
    cfg = get_config(args.config_path)
    db_path = str(resolve_storage_db_path(cfg.storage.get("db_path")))

    from loguru import logger

    from .storage.db import get_db
    from .storage.repository import Repository

    db = get_db(db_path)
    repo = Repository(db)

    deleted = repo.cleanup_old_records(args.retention_days)
    logger.info(f"Cleaned up {deleted} old records (retention: {args.retention_days} days)")
    db.close()


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "server":
        _run_server(args)
    elif args.command == "clean":
        _run_clean(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
