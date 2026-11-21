"""事件表归档（2026-11）—— 与 TS ``ts/src/shared/storage/archive.ts`` 对称。

**共享归档入口（v1 / v3 共用）**：维护任务（``maintenance``）只依赖本模块。

- ``tool_calls``：analyze 调用记录（v1/v3 都写）；归档委托 ``Repository``（历史实现）；
- ``v3_proxy_events``：v3 gateway/proxy 拦截的原始事件。表名带 v3 是历史命名
  （只有 v3 会写），但归档属于共享的维护流程，统一在这里处理；
  表不存在（v1-only 部署）时静默跳过。

归档文件：``<db目录>/archive/<前缀>-<时间戳>.jsonl.gz``（JSONL + gzip，0600），
文件落盘成功后才删主库行。归档目录按前缀分别清理（各自的 大小/天数 配置，
互不影响）。

环境变量（0 = 关闭对应行为）：
- tool_calls：``AGENT_GATE_ARCHIVE_THRESHOLD/_KEEP/_THRESHOLD_MB/_KEEP_MB/_MAX_MB/_MAX_AGE_DAYS``
- v3_proxy_events：``AGENT_GATE_PROXY_ARCHIVE_THRESHOLD/_KEEP/_MAX_MB/_MAX_AGE_DAYS``
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone

from .db import get_db

PROXY_TABLE = "v3_proxy_events"
TOOL_CALLS_PREFIX = "tool_calls"
PROXY_PREFIX = "proxy_events"


# ── tool_calls（委托 Repository 的历史实现，保持统计同步逻辑不变） ──


def archive_tool_calls(keep: int, keep_bytes: int = 0) -> tuple[int, str]:
    """把最老的 tool_calls 归档到本地压缩文件。返回 ``(归档条数, 文件路径)``。"""
    from .repository import Repository

    return Repository().archive_old_records(keep, keep_bytes)


# ── v3_proxy_events ──


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PROXY_TABLE,)
    ).fetchone()
    return row is not None


def archive_proxy_events(keep: int = 0) -> tuple[int, str]:
    """把最老的 v3_proxy_events 归档到本地压缩文件，主库保留最近 ``keep`` 条。

    与 ``Repository.archive_old_records`` 同款语义：JSONL + gzip、0600、
    文件落盘成功后才删主库行。返回 ``(归档条数, 文件路径)``；表不存在或
    未超过保留窗口时返回 ``(0, "")``。
    """
    if keep <= 0:
        return 0, ""
    db = get_db()
    conn = db._get_connection()
    with db._write_lock:
        if not _table_exists(conn):
            return 0, ""
        total = conn.execute(f"SELECT COUNT(*) FROM {PROXY_TABLE}").fetchone()[0]
        if total <= keep:
            return 0, ""
        row = conn.execute(
            f"SELECT id FROM {PROXY_TABLE} ORDER BY id LIMIT 1 OFFSET ?",
            (total - keep - 1,),
        ).fetchone()
        if row is None:
            return 0, ""
        cutoff_id = row[0]
        rows = conn.execute(
            f"SELECT * FROM {PROXY_TABLE} WHERE id <= ? ORDER BY id", (cutoff_id,)
        ).fetchall()
        if not rows:
            return 0, ""

        archive_dir = db._db_path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        file_path = archive_dir / f"{PROXY_PREFIX}-{stamp}.jsonl.gz"
        payload = (
            "\n".join(json.dumps(dict(r), ensure_ascii=False, default=str) for r in rows)
            + "\n"
        )
        # 0600：归档里含完整命令 / prompt 原文，不能跟随 umask 变成 0644
        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(payload.encode("utf-8")))

        # 文件落盘后才删主库
        cur = conn.execute(
            f"DELETE FROM {PROXY_TABLE} WHERE id <= ?", (cutoff_id,)
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted, str(file_path)


# ── 归档目录清理（按前缀分组，互不影响） ──


def _cleanup_group(
    archive_dir, prefix: str, max_bytes: int, max_age_days: int
) -> dict:
    files = sorted(
        (p for p in archive_dir.glob(f"{prefix}-*.jsonl.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    now = time.time()
    removed = 0
    if max_age_days > 0:
        cutoff = now - max_age_days * 86400
        for p in list(files):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
                    files.remove(p)
            except OSError:
                continue
    total = sum(p.stat().st_size for p in files if p.exists())
    if max_bytes > 0:
        for p in files:
            if total <= max_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
    return {
        "deleted": removed,
        "files": len([p for p in files if p.exists()]),
        "bytes": max(0, total),
    }


def cleanup_archives(
    tool_max_bytes: int = 0,
    tool_max_age_days: int = 0,
    proxy_max_bytes: int = 0,
    proxy_max_age_days: int = 0,
) -> dict:
    """清理 ``archive/``：tool_calls-* 与 proxy_events-* 各自按配置删最老文件。

    返回合并统计，并附 ``tool`` / ``proxy`` 分项。
    """
    db = get_db()
    archive_dir = db._db_path.parent / "archive"
    if not archive_dir.exists():
        empty = {"deleted": 0, "files": 0, "bytes": 0}
        return {"deleted": 0, "files": 0, "bytes": 0, "tool": empty, "proxy": empty}
    tool = _cleanup_group(
        archive_dir, TOOL_CALLS_PREFIX, tool_max_bytes, tool_max_age_days
    )
    proxy = _cleanup_group(archive_dir, PROXY_PREFIX, proxy_max_bytes, proxy_max_age_days)
    return {
        "deleted": tool["deleted"] + proxy["deleted"],
        "files": tool["files"] + proxy["files"],
        "bytes": tool["bytes"] + proxy["bytes"],
        "tool": tool,
        "proxy": proxy,
    }
