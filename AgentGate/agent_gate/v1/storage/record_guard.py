"""记录写入守卫（2026-10 存储治理）。

两个作用：

1. **单条上限**：命令原文正常只有几 KB，出现 MB 级说明异常（脚本内联、二进制误传）。
   超限部分截断，保留头部 + 长度/校验值，既能定位又不会一条吃满磁盘。
2. **磁盘降级**：剩余空间低于阈值时，新记录只存摘要（谁、何时、命令前 512 字节、结论）。
   **旧数据一条不删**，只是新数据不再记全量，避免把盘彻底写死。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ..server import security as sp

#: 降级模式下命令保留的字节数。
DEGRADE_COMMAND_BYTES = 512


def _byte_prefix(value: str, limit: int) -> str:
    """Keep complete UTF-8 characters within a byte budget."""
    return value.encode("utf-8", "surrogatepass")[:limit].decode("utf-8", "ignore")


def clip_text(value, limit: int | None = None) -> str:
    """保留至多 ``limit`` 字节的正文，另附长度与校验标记。"""
    if limit is None:
        limit = sp.max_record_bytes()
    s = "" if value is None else str(value)
    raw = s.encode("utf-8", "surrogatepass")
    if limit <= 0 or len(raw) <= limit:
        return s
    head = _byte_prefix(s, limit)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    removed = len(raw) - len(head.encode("utf-8"))
    return f"{head}…[truncated {removed}B sha256:{digest}]"


def disk_free_percent(target_path: str | Path) -> float:
    """目标路径所在文件系统的剩余空间百分比；拿不到时返回 100（不降级）。"""
    try:
        usage = shutil.disk_usage(str(Path(target_path).resolve().parent))
        if usage.total <= 0:
            return 100.0
        return usage.free / usage.total * 100.0
    except OSError:
        return 100.0


def should_degrade_writes(target_path: str | Path) -> bool:
    """当前是否应降级写入（磁盘剩余低于 degrade 阈值）。"""
    degrade = sp.disk_degrade_percent()
    if degrade <= 0:
        return False
    return disk_free_percent(target_path) < degrade


def disk_warning(target_path: str | Path) -> bool:
    """当前磁盘是否处于告警水位。"""
    warn = sp.disk_warn_percent()
    if warn <= 0:
        return False
    return disk_free_percent(target_path) < warn


def degrade_record(rec: dict) -> dict:
    """把一条记录降级成摘要：保留判定结论与命令头部，丢弃其余明细。"""
    command = str(rec.get("command") or "")
    clipped = (
        f"{_byte_prefix(command, DEGRADE_COMMAND_BYTES)}…[degraded]"
        if len(command.encode("utf-8", "surrogatepass")) > DEGRADE_COMMAND_BYTES
        else command
    )
    out = dict(rec)
    out["command"] = clipped
    out["action_detail"] = _byte_prefix(str(rec.get("action_detail") or ""), DEGRADE_COMMAND_BYTES)
    out["description"] = ""
    out["reason"] = _byte_prefix(str(rec.get("reason") or ""), 256)
    return out


def prepare_record_for_storage(rec: dict, target_path: str | Path) -> tuple[dict, bool]:
    """写库前的统一处理：先按单条上限截断，磁盘紧张时再降级为摘要。

    返回 ``(处理后的记录, 是否降级)``。
    """
    out = dict(rec)
    out["command"] = clip_text(rec.get("command"))
    out["action_detail"] = clip_text(rec.get("action_detail"))
    out["reason"] = clip_text(rec.get("reason"), 4096)
    out["description"] = clip_text(rec.get("description"), 4096)
    degraded = should_degrade_writes(target_path)
    if degraded:
        out = degrade_record(out)
    return out, degraded
