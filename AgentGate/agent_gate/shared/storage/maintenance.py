"""存储维护（2026-10）—— 镜像 TS `ts/src/shared/storage/maintenance.ts`。

触发条件：主库条数达到 ``AGENT_GATE_ARCHIVE_THRESHOLD``（默认 10 万），
或主库字节数达到 ``AGENT_GATE_ARCHIVE_THRESHOLD_MB``（默认 512MB）。
**清理与归档解耦（2026-11）**：只要配置了清理上限（大小或天数），每次维护都执行；
低于阈值 / 本次未归档时也会清，否则「调小上限后旧归档永不删除」。
顺序（**不能颠倒**）：

1. 先把最老的记录归档进本地压缩文件（JSONL + gzip，仍可 zcat 查）；
2. VACUUM 真正释放磁盘（DELETE 不会让文件变小）；
3. 用当前保留窗口刷新 AgentMoss 派生状态；
4. 清理老的归档文件（默认按总大小，按时间删除可选）。

放在后台线程里跑（启动时 + 每 6 小时），不碰 analyze 热路径。
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from loguru import logger

from ..server import security as sp
from .repository import Repository

#: 同一时刻只允许一个维护任务在跑（VACUUM 期间重复触发会互相等锁）。
_running = False
_thread: threading.Thread | None = None


def run_storage_maintenance(agent_id: str = "default") -> dict:
    """跑一次存储维护。任何失败都只记日志并返回，绝不影响正常请求。"""
    global _running
    if _running:
        return {"archived": 0, "file": "", "skipped": "running"}
    _running = True
    result = {"archived": 0, "file": "", "skipped": "error",
              "proxy_archived": 0, "proxy_file": "",
              "archive_cleanup": {}, "proxy_cleanup": {}}
    threshold = keep = records_at_check = None
    review = vacuum = "not-run"
    vacuum_sqlite_errorcode = vacuum_sqlite_errorname = None
    vacuum_error = ""
    proxy_error = ""
    error = ""
    try:
        # 0) 共享归档入口里先处理 v3_proxy_events（gateway/proxy 拦截的原始事件）。
        #    独立阈值开关：tool_calls 归档禁用不影响它，反之亦然。
        #    表不存在（v1-only 部署）时内部直接跳过。
        try:
            if sp.proxy_archive_threshold() > 0:
                from .archive import archive_proxy_events

                pa, pf = archive_proxy_events(sp.proxy_archive_keep())
                result["proxy_archived"], result["proxy_file"] = pa, pf
                if pa > 0:
                    # 立即释放 proxy 删除的空间（tool_calls 流程若也归档会再 vacuum 一次；
                    # 两表同时超阈值属罕见场景，重复一次可接受）
                    try:
                        Repository().vacuum()
                        vacuum = "completed"
                    except Exception as exc:  # noqa: BLE001
                        vacuum = type(exc).__name__
                        vacuum_error = str(exc)[:160]
        except Exception as exc:  # noqa: BLE001
            proxy_error = f"{type(exc).__name__}: {str(exc)[:160]}"

        threshold = sp.archive_threshold_records()
        threshold_bytes = sp.archive_threshold_bytes()
        keep = sp.archive_keep_records()
        keep_bytes = sp.archive_keep_bytes()

        # 清理与归档解耦（2026-11 修）：只要配置了清理上限（大小或天数），
        # 每次维护都执行——不能只在「本次真的归档了」之后才清。否则主库低于
        # 归档阈值时，已超限的归档文件永远不会被删除（用户调小上限/旧归档堆积）。
        def _run_cleanup() -> dict:
            try:
                from .archive import cleanup_archives

                c = cleanup_archives(
                    sp.archive_max_bytes(), sp.archive_max_age_days(),
                    sp.proxy_archive_max_bytes(), sp.proxy_archive_max_age_days(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("归档清理失败: {}", exc)
                return {}
            result["archive_cleanup"] = c.get("tool", c)
            result["proxy_cleanup"] = c.get("proxy", {})
            if c.get("deleted", 0) > 0:
                logger.info(
                    "归档清理: 删除 {} 个旧归档（tool {} / proxy {}）",
                    c["deleted"], c.get("tool", {}).get("deleted", 0),
                    c.get("proxy", {}).get("deleted", 0),
                )
            return c

        if threshold <= 0 and threshold_bytes <= 0:
            _run_cleanup()
            result["skipped"] = "disabled"
            return result

        repo = Repository()
        stats = repo.get_storage_stats()
        records_at_check = stats.get("toolCalls", 0)
        total_bytes = stats.get("dbBytes", 0) + stats.get("jsonBytes", 0)
        count_hit = threshold > 0 and records_at_check >= threshold
        bytes_hit = threshold_bytes > 0 and total_bytes >= threshold_bytes
        if not count_hit and not bytes_hit:
            cleanup = _run_cleanup()
            logger.info(
                "存储维护: 未达归档阈值（{} 条 / {} 字节），跳过归档；清理 {} 个",
                records_at_check, total_bytes, cleanup.get("deleted", 0),
            )
            result["skipped"] = "below-threshold"
            return result

        # 1) 先归档最老的记录。AgentMoss 只应看 AgentGate 当前保留窗口，
        #    不能在归档前把老数据 review 进去（否则旧数据仍留在链状态里）。
        from .archive import archive_tool_calls

        archived, file_path = archive_tool_calls(keep, keep_bytes)
        if archived <= 0:
            _run_cleanup()
            result["skipped"] = "nothing-to-archive"
            return result

        # 2) 真正释放磁盘
        try:
            repo.vacuum()
            vacuum = "completed"
        except Exception as exc:  # noqa: BLE001
            vacuum = type(exc).__name__
            vacuum_sqlite_errorcode = getattr(exc, "sqlite_errorcode", None)
            vacuum_sqlite_errorname = getattr(exc, "sqlite_errorname", None)
            if vacuum_sqlite_errorcode is None:
                vacuum_error = str(exc)[:160]

        # 3) 归档后，只用当前保留窗口刷新 AgentMoss 派生状态
        try:
            from ..client.moss_client import MossClient

            review = "attempted"
            asyncio.run(
                MossClient().review(agent_id=agent_id, db_path=str(repo._db._db_path))
            )
        except Exception as exc:  # noqa: BLE001
            review = type(exc).__name__

        # 4) 清理老的归档文件（tool_calls-* 与 proxy_events-* 各按自己的大小/天数配置）
        _run_cleanup()

        result.update(archived=archived, file=file_path, skipped="")
        return result
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return result
    finally:
        _running = False
        has_error = (
            bool(error)
            or bool(proxy_error)
            or review not in ("not-run", "attempted")
            or vacuum not in ("not-run", "completed")
        )
        logger.log(
            "WARNING" if has_error else "INFO",
            "storage_maintenance outcome={} records_at_check={} threshold={} keep_target={} "
            "archived={} archive_file={} review={} vacuum={} error={!r} "
            "vacuum_sqlite_errorcode={} vacuum_sqlite_errorname={} vacuum_error={!r} "
            "proxy_archived={} proxy_archive_file={} proxy_cleanup={} proxy_error={!r}",
            result["skipped"] or "archived", records_at_check, threshold, keep,
            result["archived"], Path(result["file"]).name or "-", review, vacuum, error,
            vacuum_sqlite_errorcode, vacuum_sqlite_errorname, vacuum_error,
            result["proxy_archived"], Path(result["proxy_file"]).name or "-",
            result["proxy_cleanup"], proxy_error,
        )


def start_storage_maintenance(agent_id: str = "default", interval_sec: int = 6 * 3600) -> None:
    """启动后台维护线程：立即跑一次，之后每 ``interval_sec`` 一次。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    def _loop() -> None:
        while True:
            try:
                run_storage_maintenance(agent_id)
            except Exception:  # noqa: BLE001
                logger.exception("存储维护线程异常")
            time.sleep(interval_sec)

    _thread = threading.Thread(target=_loop, name="agent-gate-storage-maintenance", daemon=True)
    _thread.start()
