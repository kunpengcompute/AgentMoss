"""存储维护（2026-10）—— 镜像 TS `ts/src/shared/storage/maintenance.ts`。

触发条件：主库条数达到 ``AGENT_GATE_ARCHIVE_THRESHOLD``（默认 10 万）。
顺序（**不能颠倒**）：

1. 先让 AgentMoss 做一次全量回顾 —— 老数据要先进组合链时间线；
2. 再把最老的记录归档进本地压缩文件（JSONL + gzip，仍可 zcat 查）；
3. 最后 VACUUM 真正释放磁盘（DELETE 不会让文件变小）。

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
    result = {"archived": 0, "file": "", "skipped": "error"}
    threshold = keep = records_at_check = None
    review = vacuum = "not-run"
    vacuum_sqlite_errorcode = vacuum_sqlite_errorname = None
    vacuum_error = ""
    error = ""
    try:
        threshold = sp.archive_threshold_records()
        keep = sp.archive_keep_records()
        if threshold <= 0 or keep <= 0:
            result["skipped"] = "disabled"
            return result

        repo = Repository()
        records_at_check = repo.get_storage_stats().get("toolCalls", 0)
        if records_at_check < threshold:
            result["skipped"] = "below-threshold"
            return result

        db_path = str(repo._db._db_path)

        # 1) 归档前先全量回顾：老数据进了时间线，组合链才不会因为归档断掉
        try:
            from ..client.moss_client import MossClient

            review = "attempted"
            asyncio.run(MossClient().review(agent_id=agent_id, db_path=db_path))
        except Exception as exc:  # noqa: BLE001
            review = type(exc).__name__

        # 2) 归档最老的，主库留 keep 条
        archived, file_path = repo.archive_old_records(keep)
        if archived <= 0:
            result["skipped"] = "nothing-to-archive"
            return result

        # 3) 真正释放磁盘
        try:
            repo.vacuum()
            vacuum = "completed"
        except Exception as exc:  # noqa: BLE001
            vacuum = type(exc).__name__
            vacuum_sqlite_errorcode = getattr(exc, "sqlite_errorcode", None)
            vacuum_sqlite_errorname = getattr(exc, "sqlite_errorname", None)
            if vacuum_sqlite_errorcode is None:
                vacuum_error = str(exc)[:160]

        result.update(archived=archived, file=file_path, skipped="")
        return result
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return result
    finally:
        _running = False
        has_error = (
            bool(error)
            or review not in ("not-run", "attempted")
            or vacuum not in ("not-run", "completed")
        )
        logger.log(
            "WARNING" if has_error else "INFO",
            "storage_maintenance outcome={} records_at_check={} threshold={} keep_target={} "
            "archived={} archive_file={} review={} vacuum={} error={!r} "
            "vacuum_sqlite_errorcode={} vacuum_sqlite_errorname={} vacuum_error={!r}",
            result["skipped"] or "archived", records_at_check, threshold, keep,
            result["archived"], Path(result["file"]).name or "-", review, vacuum, error,
            vacuum_sqlite_errorcode, vacuum_sqlite_errorname, vacuum_error,
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
