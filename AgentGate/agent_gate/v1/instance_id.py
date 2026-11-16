"""AgentGate 安装级实例 ID（与 TS 版 ``ts/src/shared/instance-id.ts`` 对齐）。

用途：AgentMoss 的「展示层脱敏」按这个 ID 判断节点是否来自同一实例 ——
同一实例的多个会话不隐藏命令原文，跨实例才隐藏。**不参与组合链判定**
（判定始终是全局的，见 AGENT_MOSS_CHAIN_SCOPE=system）。

持久化：``~/.config/agentgate/instance_id``（0600），首次启动生成 UUID；
不能用进程 UUID（重启后历史节点的实例归属会断）。
可覆盖：``AGENT_GATE_INSTANCE_ID``（显式指定）或 ``AGENT_GATE_INSTANCE_ID_PATH``
（自定义文件路径）。
"""

import os
import uuid
from pathlib import Path

_cached = ""


def get_instance_id() -> str:
    """返回本安装实例的持久化 ID（进程内缓存）。"""
    global _cached
    if _cached:
        return _cached
    env = os.getenv("AGENT_GATE_INSTANCE_ID", "").strip()
    if env:
        _cached = env
        return _cached
    file_path = Path(
        os.getenv("AGENT_GATE_INSTANCE_ID_PATH")
        or Path.home() / ".config" / "agentgate" / "instance_id"
    )
    try:
        if file_path.exists():
            val = file_path.read_text(encoding="utf-8").strip()
            if val:
                _cached = val
                return _cached
        val = str(uuid.uuid4())
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(val + "\n", encoding="utf-8")
        os.chmod(file_path, 0o600)
        _cached = val
        return _cached
    except OSError:
        # 落盘失败（只读 HOME 等）→ 进程内临时 ID，不阻断分析
        _cached = f"ephemeral-{uuid.uuid4()}"
        return _cached
