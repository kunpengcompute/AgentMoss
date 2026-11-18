"""AgentGate V1 CLI — 薄入口，共用实现全部来自 agent_gate.shared。

客户只走「agent hook → AgentGate /api/v1/analyze → AgentMoss」这条链路时，
用这个入口即可；V3 的 proxy / gateway / mcp / pty / eBPF 等能力不在这里。
"""

import argparse
import os
import sys
from pathlib import Path

from ..version import __version__
from ..shared.config import get_config, resolve_storage_db_path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-gate",
        description="AgentGate V1 — analyze gateway (hook → AgentGate → AgentMoss)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    server_parser = subparsers.add_parser("server", help="Start AgentGate server")
    server_parser.add_argument(
        "--mode", choices=["http", "socket"], default="http",
        help="Server mode: http (TCP) or socket (Unix Domain Socket)",
    )
    server_parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind host (HTTP mode only, default: 0.0.0.0)",
    )
    server_parser.add_argument(
        "--port", type=int, default=9100,
        help="Bind port (HTTP mode only, default: 9100)",
    )
    server_parser.add_argument(
        "--socket", dest="socket_path", help="Unix socket path (socket mode only)"
    )
    server_parser.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"],
        default="info", help="Log level",
    )
    server_parser.add_argument(
        "--workers", type=int, default=1, help="Number of worker processes"
    )
    server_parser.add_argument(
        "--config", type=Path, dest="config_path", help="Path to YAML config file"
    )
    server_parser.add_argument(
        "--db-path", type=str, dest="db_path", help="SQLite database path override"
    )
    server_parser.add_argument(
        "--security-url", dest="security_url",
        help="AgentMoss service URL (default: $AGENT_MOSS_URL / $MOSS_URL / config / 9090)",
    )
    server_parser.add_argument(
        "--moss-url", dest="moss_url", help="Alias of --security-url"
    )

    clean_parser = subparsers.add_parser("clean", help="Clean old database records")
    clean_parser.add_argument(
        "--retention-days", type=int, default=30,
        help="Days to retain records (default: 30)",
    )
    clean_parser.add_argument(
        "--config", type=Path, dest="config_path", help="Path to YAML config file"
    )

    args = parser.parse_args()
    if args.command == "server":
        _run_server(args)
    elif args.command == "clean":
        _run_clean(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_server(args) -> None:
    cfg = get_config(args.config_path)
    server_cfg = cfg.server

    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 9100)
    mode = args.mode or server_cfg.get("mode", "http")
    socket_path = args.socket_path or server_cfg.get("socket_path")
    log_level = args.log_level or server_cfg.get("log_level", "info")
    workers = args.workers or server_cfg.get("workers", 1)
    db_path = str(
        resolve_storage_db_path(args.db_path or cfg.storage.get("db_path"))
    )

    security_url = getattr(args, "security_url", None) or getattr(args, "moss_url", None)
    if security_url:
        os.environ["AGENT_MOSS_URL"] = security_url

    from ..shared.server.app import run_server

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
    db_path = cfg.storage.get("db_path")

    from loguru import logger

    from ..shared.storage.db import get_db
    from ..shared.storage.repository import Repository

    db = get_db(db_path)
    repo = Repository(db)
    deleted = repo.cleanup_old_records(args.retention_days)
    logger.info(
        f"Cleaned up {deleted} old records (retention: {args.retention_days} days)"
    )
    db.close()


if __name__ == "__main__":
    main()
