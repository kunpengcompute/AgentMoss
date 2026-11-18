"""跨会话（多 session）分析的开关 —— 与 TS 版 ``shared/multi-session.ts`` 对称。

## 默认开启（2026-10 起）

组件默认给足检测能力，由宿主按场景关闭：

- **Python 版（招行等服务器部署）** 此前没有开关、始终组装 ``session_history``；
  默认开等于**保持它的现状**，不改变已测过的性能指标。
- **OpenDesk（桌面端）** 有可见的设置页开关，用户未点击时不应产生额外开销，
  因此 bridge 必须**显式**传 ``AGENT_GATE_MULTI_SESSION=0``。

关闭方式：显式设 ``AGENT_GATE_MULTI_SESSION=0``（也接受 false/no/off）。
关闭后不组装跨会话 ``session_history``，也不强开 ``metadata.stateful_incremental``。

## 升级提醒（OpenDesk）

OpenDesk 的 ``agent-gate-bridge.ts`` 旧实现是"开关为 true 才注入 1，否则什么都
不传"。默认值改成开之后，**旧 bridge + 新 agent-gate** 会让"从没点过开关"的
用户被动打开跨会话分析。升级 OpenDesk 依赖时必须同步把 bridge 改成总是显式
注入 ``'0'`` / ``'1'``。
"""

import os

#: 显式关闭的取值（小写）。空值 = 未设置 = 默认开。
_MULTI_SESSION_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def multi_session_enabled() -> bool:
    """跨会话分析是否开启（默认开，只有显式关闭才关）。

    解析规则与 TS 版 ``isMultiSessionEnabled`` 完全一致：
    - 未设置 / 空串 → ``True``（默认开）
    - ``0`` / ``false`` / ``no`` / ``off``（大小写不敏感）→ ``False``
    - 其它任何值 → ``True``
    """
    raw = (os.getenv("AGENT_GATE_MULTI_SESSION") or "").strip().lower()
    if raw == "":
        return True
    return raw not in _MULTI_SESSION_OFF_VALUES


def stateful_incremental_enabled() -> bool:
    """是否请求 AgentMoss 的 L2.4 有状态增量。

    与跨会话开关同源：它读的是同一批持久化状态，关掉跨会话时单独开着它只会
    白占内存，不会多检出任何东西。
    """
    return multi_session_enabled()


def session_history_limit() -> int:
    """跨会话分析取最近多少个会话。**默认 5，保持现有性能不变**。

    调用方（AgentGate）把这个数量传给 AgentMoss 的 ``session_history``，
    后者据此重建行为图跑 P1–P8。窗口越大，实时覆盖越广，建图开销也越大
    （O(节点数 × 性质数 × 边数)）。所以默认值不动，需要更大覆盖面的部署
    自行设 ``AGENT_GATE_SESSION_HISTORY_LIMIT``（上限 50，防止误配成
    全库导致每次 analyze 建超大图）。
    """
    raw = (os.getenv("AGENT_GATE_SESSION_HISTORY_LIMIT") or "").strip()
    try:
        n = int(raw) if raw else 5
    except ValueError:
        n = 5
    return max(1, min(n, 50))

