"""Dashboard API — statistics, WebSocket events, and static page serving."""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from ..storage.repository import Repository
from . import security as sp


router = APIRouter(prefix="/dashboard")
dashboard_router = APIRouter()

#: 安全响应头（HTML 页面才需要，JSON API 不加）。
#: 定义在 ``security.py`` 里作为单一事实源。
_DASHBOARD_SECURITY_HEADERS = sp.DASHBOARD_SECURITY_HEADERS

# WebSocket connections pool
_ws_connections: set[WebSocket] = set()


def get_repo():
    return Repository()


# ============================================================
# WebSocket — real-time event push
# ============================================================

def _ws_token(ws: WebSocket) -> str:
    """WebSocket 握手的凭证：Cookie（浏览器）或 Authorization 头（原生客户端）。"""
    tok = ws.cookies.get(sp.DASHBOARD_COOKIE, "")
    if tok:
        return tok
    header = ws.headers.get("authorization", "") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _ws_origin_allowed(ws: WebSocket) -> bool:
    """WebSocket 握手的跨源校验（CSWSH 纵深防御）。

    ⚠️ 这层不能省：``@app.middleware("http")`` 只处理 HTTP 请求，WebSocket
    握手**根本不经过** ``_security_guard`` —— Origin 校验、QPS 限流、体积
    检查对 WS 全部不生效，必须在 WS 路由里自己做。
    """
    if not sp.origin_check_enabled():
        return True
    return sp.origin_allowed(
        ws.headers.get("origin", ""),
        ws.headers.get("host", ""),
    )


@dashboard_router.websocket("/ws/events")
async def websocket_events(ws: WebSocket):
    # 这条链路实时推送**每条命令的原文**。以前是完全开放的 —— 同网段任何人
    # 连上就能持续收到所有 agent 的动作流。
    #
    # ⚠️ WebSocket 不走 HTTP 中间件（见 _ws_origin_allowed），下面三道闸
    # 全部要在这里自己做，漏一道就是一个独立的攻击面。
    # 1) 跨源校验
    if not _ws_origin_allowed(ws):
        await ws.close(code=1008)
        return
    # 2) 鉴权
    if sp.auth_enabled() and not sp.check_token(_ws_token(ws)):
        await ws.close(code=1008)
        return
    # 3) 在线连接数上限。uvicorn 的 limit_concurrency 只在 HTTP 协议实现里
    #    检查，**WebSocket 不受它约束** —— 不加这层可以无限开连接把 task
    #    和内存打满，而 HTTP 侧的连接上限完全看不到这些连接。
    limit = sp.max_ws_connections()
    if limit > 0 and len(_ws_connections) >= limit:
        await ws.close(code=1013)  # Try Again Later
        return
    await ws.accept()
    _ws_connections.add(ws)
    # accept 与 add 之间没有 await；检查放这里兜住并发握手的竞态，
    # 最多超限 1 个。
    if limit > 0 and len(_ws_connections) > limit:
        _ws_connections.discard(ws)
        await ws.close(code=1013)
        return
    try:
        while True:
            # Keep alive
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_connections.discard(ws)


def broadcast_event(event: dict):
    """Push an event to all connected dashboard clients."""
    if not _ws_connections:
        return
    msg = json.dumps(event, ensure_ascii=False)
    dead: set[WebSocket] = set()
    for ws in list(_ws_connections):
        try:
            asyncio.ensure_future(ws.send_text(msg))
        except Exception:
            dead.add(ws)
    _ws_connections.difference_update(dead)


# ============================================================
# Stats API
# ============================================================

@router.get("/stats")
async def get_stats(repo=Depends(get_repo)):
    """Aggregated statistics for dashboard overview."""
    db = repo._db

    # Total agents
    agents = db.execute("SELECT COUNT(*) as c FROM agents").fetchone()
    total_agents = agents["c"]

    # Total sessions (active + closed)
    sessions = db.execute("SELECT COUNT(*) as c FROM sessions").fetchone()
    total_sessions = sessions["c"]

    # Active sessions
    active = db.execute(
        "SELECT COUNT(*) as c FROM sessions WHERE status = 'active'"
    ).fetchone()
    active_sessions = active["c"]

    # Total tool calls
    tc = db.execute("SELECT COUNT(*) as c FROM tool_calls").fetchone()
    total_tool_calls = tc["c"]

    # Allow / Deny counts
    decisions = db.execute(
        "SELECT decision, COUNT(*) as c FROM tool_calls GROUP BY decision"
    ).fetchall()
    allow_count = 0
    deny_count = 0
    for row in decisions:
        if row["decision"] == "Allow":
            allow_count = row["c"]
        elif row["decision"] == "Deny":
            deny_count = row["c"]

    # Average analysis duration
    avg_dur = db.execute(
        "SELECT AVG(analysis_duration_ms) as avg_dur FROM tool_calls"
    ).fetchone()
    avg_duration = round(avg_dur["avg_dur"] or 0, 1)

    # Risk level distribution
    risk_levels = db.execute(
        "SELECT risk_level, COUNT(*) as c FROM tool_calls "
        "WHERE risk_level != '' GROUP BY risk_level ORDER BY c DESC"
    ).fetchall()

    # Agent type distribution
    agent_types = db.execute(
        "SELECT a.agent_type, COUNT(*) as c FROM sessions s "
        "JOIN agents a ON s.agent_id = a.id GROUP BY a.agent_type"
    ).fetchall()

    return {
        "total_agents": total_agents,
        "total_sessions": total_sessions,
        "active_sessions": active_sessions,
        "total_tool_calls": total_tool_calls,
        "allow_count": allow_count,
        "deny_count": deny_count,
        "avg_analysis_duration_ms": avg_duration,
        "risk_levels": {r["risk_level"]: r["c"] for r in risk_levels},
        "agent_types": {r["agent_type"]: r["c"] for r in agent_types},
    }


@router.get("/recent")
async def get_recent_calls(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    repo=Depends(get_repo),
):
    """Recent tool calls with session and agent info. Supports pagination."""
    db = repo._db
    # Count total records for pagination
    total_row = db.execute(
        """SELECT COUNT(*) as total FROM tool_calls"""
    ).fetchone()
    total = total_row["total"] if total_row else 0

    rows = db.execute(
        """SELECT tc.*, s.agent_id, a.agent_type
           FROM tool_calls tc
           JOIN sessions s ON tc.session_id = s.id
           JOIN agents a ON s.agent_id = a.id
           ORDER BY tc.called_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "data": [dict(r) for r in rows],
    }


@router.get("/timeline")
async def get_timeline(
    minutes: int = Query(60, ge=1, le=1440),
    repo=Depends(get_repo),
):
    """Tool calls timeline grouped by minute."""
    db = repo._db
    rows = db.execute(
        """SELECT
              strftime('%Y-%m-%dT%H:%M:00', called_at) as minute,
              decision,
              COUNT(*) as c
           FROM tool_calls
           WHERE called_at >= datetime('now', ? || ' minutes')
           GROUP BY minute, decision
           ORDER BY minute ASC""",
        (f"-{minutes}",),
    ).fetchall()

    # Build timeline data
    timeline: dict[str, dict] = {}
    for row in rows:
        m = row["minute"]
        if m not in timeline:
            timeline[m] = {"minute": m, "Allow": 0, "Deny": 0}
        timeline[m][row["decision"]] = row["c"]

    return list(timeline.values())


# ============================================================
# Dashboard HTML page
# ============================================================

# 注意：这里是 **raw string**（r"""），不是普通字符串。
# 页面里嵌着 JS，而 JS 里到处是 `\t` `\n` `\r`（正则、join 分隔符等）。普通
# 字符串会把这些反斜杠提前解释成真正的制表符/换行 —— 实测踩过一次：正则
# /[\t\r\n]+/ 变成含真实换行的 /[<TAB><CR><LF>]+/，整段脚本直接
# "Invalid regular expression: missing /" 解析失败，页面上所有交互（刷新、
# 切标签、复制）全部静默失灵。加/去掉 r 前缀前请先想清楚这一点。
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgentGate Dashboard</title>
<style>
:root {
  --bg: #FAFAFA; --card-bg: #fff; --border: #F0F0F0;
  --text: #1d1d1f; --muted: #8E8E93; --accent: #007AFF;
  --green: #34C759; --red: #FF3B30; --yellow: #FF9500;
  --orange: #FF9500; --purple: #AF52DE;
}
* { margin:0; padding:0; box-sizing:border-box; }
/* 始终为竖向滚动条预留位置。标签页高度差很大（Recent Calls 20 行 ≈1230px，
   Timeline 只有一张图 ≈873px），切换时滚动条出现/消失会让视口宽度在
   1585↔1600 之间跳，下面 .grid 用的 auto-fit 据此重算列宽 —— 实测卡片宽度
   182↔184、最后一个卡片的 x 从 1186 跳到 1197，整排跟着"挪"。预留位置后
   宽度恒定，切标签页卡片不再动。 */
html { scrollbar-gutter: stable; }
body { background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Segoe UI',system-ui,sans-serif; -webkit-font-smoothing:antialiased; }
.header { background:var(--card-bg); border-bottom:1px solid var(--border); padding:36px 14px 16px 24px; display:flex; align-items:center; }
.header h1 { font-size:16px; font-weight:700; letter-spacing:-0.01em; }
.header .live-dot { width:8px; height:8px; border-radius:50%; background:var(--green); display:inline-block; margin-right:6px; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; padding:20px 24px; }
.card { background:var(--card-bg); border:1px solid var(--border); border-radius:10px; padding:16px; }
.card .label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; font-weight:600; }
.card .value { font-size:28px; font-weight:700; margin-top:4px; color:var(--text); }
.card .sub { font-size:12px; color:var(--muted); margin-top:2px; }
.risk-bar { display:flex; height:6px; border-radius:3px; overflow:hidden; margin-top:8px; background:#F2F2F7; }
.risk-bar span { display:block; }
.main-area { display:grid; grid-template-columns:2fr 1fr; gap:12px; padding:0 24px 20px; }
.panel { background:var(--card-bg); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
.panel-header { padding:12px 16px; border-bottom:1px solid var(--border); font-weight:600; font-size:14px; color:var(--text); background:#FAFAFA; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; padding:8px 12px; font-size:12px; font-weight:600; color:var(--muted); border-bottom:1px solid var(--border); background:#FAFAFA; }
td { padding:8px 12px; font-size:13px; border-bottom:1px solid #F5F5F7; color:var(--text); }
tr:hover { background:#F5F5F7; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:600; }
.badge-allow { background:rgba(52,199,89,.12); color:var(--green); }
.badge-deny { background:rgba(255,59,48,.12); color:var(--red); }
.badge-low { background:rgba(52,199,89,.12); color:var(--green); }
.badge-medium { background:rgba(255,149,0,.12); color:var(--yellow); }
.badge-high { background:rgba(255,149,0,.12); color:var(--orange); }
.badge-critical { background:rgba(255,59,48,.12); color:var(--red); }
.agents { padding:0 24px 20px; }
.agent-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:12px; margin-top:8px; }
.agent-card { background:var(--card-bg); border:1px solid var(--border); border-radius:10px; padding:14px; }
.agent-card .name { font-weight:600; font-size:15px; color:var(--text); }
.agent-card .meta { font-size:12px; color:var(--muted); margin-top:4px; }
.agent-card .stats { display:flex; gap:12px; margin-top:10px; }
.agent-card .stat { font-size:13px; }
.agent-card .stat strong { color:var(--accent); }
.chart-area { padding:0 24px 20px; }
.chart { background:var(--card-bg); border:1px solid var(--border); border-radius:10px; padding:16px; position:relative; height:280px; overflow:hidden; }
.chart canvas { width:100%; height:100%; display:block; }
/* Timeline 空态用 DOM 文本，不画在 canvas 上 —— canvas 是位图，被 CSS 拉伸后
   文字会糊成一团（见 drawTimeline 注释）。 */
.chart-empty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:14px; }
.empty { text-align:center; color:var(--muted); padding:40px; }
.ag-storage-banner { display:none; align-items:center; justify-content:space-between; gap:12px; margin:12px 24px 0; padding:10px 14px; border:1px solid rgba(255,149,0,.35); background:rgba(255,204,0,.14); border-radius:8px; font-size:12px; }
.ag-storage-banner.show { display:flex; }
.ag-sb-text { color:var(--text); }
.ag-sb-text b { color:var(--orange); }
.ag-sb-actions { display:flex; align-items:center; gap:8px; flex:none; }
.ag-sb-status { font-size:11px; color:var(--muted); }
.ag-sb-btn { padding:5px 14px; border-radius:6px; border:1px solid var(--orange); background:var(--orange); color:#fff; font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap; }
.ag-sb-btn:hover { background:#e08600; border-color:#e08600; }
.ag-sb-btn:disabled { opacity:.5; cursor:default; }
.command-cell { max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:'SF Mono',Menlo,monospace; font-size:12px; }
.session-cell { max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; color:var(--muted); }
.reason-cell { max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; color:var(--muted); }
.ag-pagination { display:flex; align-items:center; justify-content:center; gap:8px; padding:10px 14px; font-size:12px; color:var(--muted); background:#FAFAFA; border-top:1px solid var(--border); }
.ag-pagination button { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--text); font-size:12px; cursor:pointer; }
.ag-pagination button:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
.ag-pagination button:disabled { opacity:0.4; cursor:default; }
.tabs { display:flex; gap:0; padding:0 24px; background:#FAFAFA; }
.tab { padding:8px 20px; cursor:pointer; border:1px solid var(--border); border-bottom:none; background:var(--bg); color:var(--muted); border-radius:8px 8px 0 0; margin-right:-1px; font-weight:500; }
.tab.active { background:var(--card-bg); color:var(--text); border-bottom-color:var(--card-bg); font-weight:600; }
.tab:hover:not(.active) { color:var(--text); }
/* ── 复制记录 ── */
/* 入口靠 tabs 行右侧；tabs 是 flex，margin-left:auto 即可 */
.tab-action { margin-left:auto; align-self:center; padding:5px 14px; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:var(--accent); font-size:12px; font-weight:500; cursor:pointer; }
.tab-action:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
/* 多选工具栏：默认隐藏，进复制模式才展开 */
.copy-bar { display:none; align-items:center; gap:8px; padding:10px 16px; background:#FAFAFA; border-bottom:1px solid var(--border); font-size:12px; color:var(--muted); }
.copy-bar.on { display:flex; }
.copy-bar .spacer { flex:1; }
.copy-bar button { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:var(--text); font-size:12px; cursor:pointer; }
.copy-bar button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.copy-bar button:disabled { opacity:.4; cursor:default; }
.fmt-group { display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.fmt-group button { border:none; border-radius:0; padding:4px 10px; color:var(--muted); background:transparent; }
.fmt-group button.active { background:var(--accent); color:#fff; }
.fmt-group button:hover:not(:disabled):not(.active) { color:var(--text); }
/* 勾选框列：只在复制模式出现 */
.col-check { display:none; width:34px; text-align:center; }
table.copy-mode .col-check { display:table-cell; }
.col-check input { cursor:pointer; }
/* 轻提示 */
.toast { position:fixed; left:50%; bottom:32px; transform:translateX(-50%) translateY(8px); background:#1d1d1f; color:#fff; padding:8px 16px; border-radius:8px; font-size:13px; opacity:0; pointer-events:none; transition:opacity .18s, transform .18s; z-index:99; }
.toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
.toast.error { background:var(--red); }
.footer { padding:12px 24px; color:var(--muted); font-size:12px; border-top:1px solid var(--border); margin-top:20px; text-align:center; }
.flow-btn:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
.ag-chain-btn { padding:2px 8px; border-radius:5px; border:1px solid var(--accent); background:transparent; color:var(--accent); font-size:11px; font-weight:600; cursor:pointer; white-space:nowrap; }
.ag-chain-btn:hover { background:var(--accent); color:#fff; }
.ag-modal { position:fixed; inset:0; display:none; align-items:center; justify-content:center; z-index:100; }
.ag-modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,.35); }
.ag-modal-box { position:relative; background:var(--card-bg); border-radius:12px; max-width:92vw; max-height:88vh; overflow:auto; padding:16px 18px; box-shadow:0 12px 48px rgba(0,0,0,.22); min-width:520px; }
.ag-modal-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }
.ag-modal-title { font-size:15px; font-weight:700; }
.ag-modal-close { border:none; background:transparent; font-size:22px; line-height:1; color:var(--muted); cursor:pointer; padding:0 4px; }
.ag-modal-close:hover { color:var(--text); }
.ag-modal-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; font-size:11px; color:var(--muted); }
.ag-modal-property { display:inline-flex; align-items:center; gap:6px; padding:2px 8px; border-radius:6px; background:rgba(255,59,48,.08); color:var(--red); font-size:11px; font-weight:700; }
.ag-modal-expr { display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin:6px 0 4px; font-size:11px; }
.ag-expr-label { color:var(--muted); font-size:10px; font-weight:600; margin-right:4px; }
.ag-expr-token { display:inline-flex; flex-direction:column; align-items:center; padding:3px 8px; border-radius:6px; background:#F5F5F7; border:1px solid var(--border); }
.ag-expr-token b { font-size:11px; }
.ag-expr-token small { font-size:9px; color:var(--muted); }
.ag-expr-arrow { color:var(--muted); font-size:12px; }
.ag-modal-graph { overflow:auto; padding:8px 0; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }
.ag-modal-legend { margin-top:6px; font-size:10px; color:var(--muted); display:flex; gap:14px; flex-wrap:wrap; }
.ag-modal-reason { margin-top:10px; font-size:12px; color:var(--text); line-height:1.5; word-break:break-word; }
</style>
</head>
<body>
<div class="header">
  <h1 style="display:flex;align-items:center;gap:12px;">
    <span style="display:flex;align-items:center;"><span class="live-dot"></span>AgentGate Dashboard</span>
    <span style="color:var(--muted);font-size:13px;font-weight:400" id="clock"></span>
  </h1>
</div>

<!-- Stats cards -->
<div class="grid">
  <div class="card"><div class="label">Agents</div><div class="value" id="stat-agents">-</div></div>
  <div class="card"><div class="label">Sessions</div><div class="value" id="stat-sessions">-</div><div class="sub" id="stat-active"></div></div>
  <div class="card"><div class="label">Tool Calls</div><div class="value" id="stat-calls">-</div></div>
  <div class="card"><div class="label">Allow</div><div class="value" style="color:var(--green)" id="stat-allow">-</div></div>
  <div class="card"><div class="label">Deny</div><div class="value" style="color:var(--red)" id="stat-deny">-</div></div>
  <div class="card"><div class="label">Avg Latency</div><div class="value" id="stat-latency">-</div></div>
  <div class="card" style="grid-column:span 2">
    <div class="label">Risk Distribution</div>
    <div class="risk-bar" id="risk-bar"></div>
    <div style="display:flex;gap:16px;margin-top:8px;font-size:12px" id="risk-legend"></div>
  </div>
</div>

<div class="ag-storage-banner" id="storage-banner">
  <span class="ag-sb-text" id="storage-banner-text"></span>
  <span class="ag-sb-actions">
    <span class="ag-sb-status" id="storage-banner-status"></span>
    <button class="ag-sb-btn" id="storage-clear-btn">清理历史记录</button>
  </span>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="recent">Recent Calls</div>
  <div class="tab" data-tab="agents">Agents</div>
  <div class="tab" data-tab="timeline">Timeline</div>
  <button class="tab-action" id="enter-copy">Copy Records</button>
</div>

<!-- Recent calls -->
<div class="main-area" id="tab-recent">
  <div class="panel" style="grid-column:1/-1">
    <div class="panel-header">Recent Tool Calls (auto-refresh every 3s)</div>
    <div class="copy-bar" id="copy-bar">
      <span id="copy-count">0 selected</span>
      <span class="fmt-group">
        <button id="fmt-tsv" class="active">TSV</button>
        <button id="fmt-json">JSON</button>
      </span>
      <span class="spacer"></span>
      <button id="copy-selected" disabled>Copy Selected</button>
      <button id="copy-page">Copy Current Page</button>
      <button id="exit-copy">Cancel</button>
    </div>
    <div class="table-wrap">
      <table id="recent-calls"><thead><tr>
        <th class="col-check"></th>
        <th>Time</th><th>Agent</th><th>Session</th><th>Tool</th><th>Command</th>
        <th>Decision</th><th>Risk</th><th>Latency</th><th>Reason</th><th>Action</th>
      </tr></thead><tbody id="recent-table"></tbody></table>
    </div>
    <div id="ag-pagination" class="ag-pagination"></div>
  </div>
</div>

<!-- Agents -->
<div class="agents" id="tab-agents" style="display:none">
  <div class="panel-header" style="border-bottom:none;padding-left:0">Registered Agents</div>
  <div class="agent-grid" id="agent-list"></div>
</div>

<!-- Timeline -->
<div class="chart-area" id="tab-timeline" style="display:none">
  <div class="chart">
    <canvas id="timeline-canvas"></canvas>
    <div class="chart-empty" id="timeline-empty" style="display:none">No data in the last 60 minutes</div>
  </div>
</div>

<div class="footer" id="footer">Loading versions...</div>

<div class="ag-modal" id="chain-modal">
  <div class="ag-modal-backdrop" id="chain-modal-backdrop"></div>
  <div class="ag-modal-box">
    <div class="ag-modal-head">
      <span class="ag-modal-title" id="chain-modal-title">组合式攻击链</span>
      <button class="ag-modal-close" id="chain-modal-close" title="关闭">×</button>
    </div>
    <div class="ag-modal-meta" id="chain-modal-meta"></div>
    <div class="ag-modal-expr" id="chain-modal-expr"></div>
    <div class="ag-modal-graph" id="chain-modal-graph"></div>
    <div class="ag-modal-reason" id="chain-modal-reason"></div>
  </div>
</div>


<script>
const BASE = '';
let activeTab = 'recent';
let _currentPage = 1;
let _pageSize = 20;
let _totalRecords = 0;
let _lastRecentKey = null;   // 上次渲染用的数据，用于跳过无谓重建
let _lastPageSig = null;

// ── 复制记录 ───────────────────────────────────────────────────────────
let _copyMode = false;           // 多选模式开关
let _selectedIds = new Set();    // 已勾选记录的 id（跨刷新保持）
let _copyFormat = 'tsv';         // 'tsv' | 'json'
let _copying = false;
let _currentPageRows = [];       // 当前页原始记录，复制时直接读它（不读 DOM，避免被列宽省略号截断）
let _forceRecentRender = false;  // 进出复制模式要强刷一次（列结构变了）

// 刷新是每 3 秒一次，而重建 DOM（innerHTML=…）会把正在进行的文字选择一并清掉
// ——实测：在 Recent Calls 里选中命令后，不到 1 秒选区就没了，根本复制不了。
// 所以重建前先看"数据真的变了吗 / 用户是不是正在选字"，两者任一成立就先不动 DOM。
function _userIsSelecting() {
  const sel = window.getSelection();
  return !!sel && sel.toString().length > 0;
}

// Tab switching
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    activeTab = t.dataset.tab;
    document.getElementById('tab-recent').style.display = activeTab==='recent' ? '' : 'none';
    document.getElementById('tab-agents').style.display = activeTab==='agents' ? '' : 'none';
    document.getElementById('tab-timeline').style.display = activeTab==='timeline' ? '' : 'none';
    refresh();
  });
});

function badge(d, cls) {
  const map = {Allow:'badge-allow', Deny:'badge-deny', low:'badge-low', medium:'badge-medium', high:'badge-high', critical:'badge-critical'};
  // d 来自 AgentMoss 的判定字段；虽由服务端产生，仍按不可信内容转义（纵深防御）
  return `<span class="badge ${map[d]||''}">${escapeHtml(d)}</span>`;
}

// ==================== 存储超限横幅 + 清理 ====================
let _storageVisible = false;
function _fmtMB(n){ return (n / 1048576).toFixed(1); }
function _getTotalMB(stats){ return _fmtMB((stats.dbBytes || 0) + (stats.jsonBytes || 0)); }
function _showStorageBanner(oversize, text){
  _storageVisible = !!oversize && !!text;
  const el = document.getElementById('storage-banner');
  if (el) el.classList.toggle('show', _storageVisible);
  if (text) _setText('storage-banner-text', text);
}
function _setStorageBusy(busy){
  const btn = document.getElementById('storage-clear-btn');
  if (btn) btn.disabled = busy;
  _setText('storage-banner-status', busy ? '清理中…' : '');
}
async function loadStorageStats(){
  try {
    const r = await fetch(BASE+'/api/v1/storage_stats');
    const st = await r.json();
    if (!st || typeof st !== 'object' || !st.oversize) { _showStorageBanner(false); return; }
    const s = st.stats || {};
    const cnt = typeof s.toolCalls === 'number' ? s.toolCalls : '?';
    const thr = typeof st.thresholdMB === 'number' ? st.thresholdMB + ' MB' : '?';
    _showStorageBanner(true,
      '本地存储已超过阈值（' + thr + '）：当前占用 ' + _getTotalMB(s) + ' MB，共 ' + cnt +
      ' 条历史记录。建议清理历史记录，释放空间。');
  } catch (e) { _showStorageBanner(false); console.error('[AgentGate] storage stats error:', e); }
}
(function bindStorageClear(){
  const btn = document.getElementById('storage-clear-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!window.confirm('确定要清空全部历史调用记录吗？此操作不可恢复。')) return;
    _setStorageBusy(true);
    try {
      const r = await fetch(BASE+'/api/v1/tool_calls', { method: 'DELETE' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      _setText('storage-banner-status', '清理完成，已删除 ' + (d && d.deleted != null ? d.deleted : 0) + ' 条记录');
      _showStorageBanner(false);
      refresh();
    } catch (e) {
      _setText('storage-banner-status', '清理失败：' + String(e.message || e));
    } finally { _setStorageBusy(false); }
  });
})();

// ==================== 页脚版本（从 health 动态读取）====================
(async function loadFooter(){
  try {
    const h = await fetch(BASE+'/api/v1/health').then(r=>r.json());
    const gateV = h.version || 'N/A';
    const mossV = h.moss_version || 'N/A';
    const mossS = h.moss_status || 'unknown';
    let mossLabel = mossV === 'N/A' ? 'AgentMoss N/A (unreachable)' : 'AgentMoss v' + mossV;
    if (mossS === 'healthy') mossLabel += ' ✓';
    _setText('footer', 'AgentGate v' + gateV + ' + ' + mossLabel + ' (Python)');
  } catch (e) {
    _setText('footer', 'AgentGate N/A + AgentMoss N/A (Python)');
  }
})();

async function refresh() {
  try {
    const offset = (_currentPage - 1) * _pageSize;
    const [stats, recentResp] = await Promise.all([
      fetch(BASE+'/dashboard/stats').then(r=>r.json()),
      fetch(BASE+'/dashboard/recent?limit='+_pageSize+'&offset='+offset).then(r=>r.json()),
    ]);
    updateStats(stats);
    // Handle {total, data} pagination format
    const recentData = Array.isArray(recentResp) ? recentResp : (recentResp.data || []);
    _totalRecords = recentResp.total || recentData.length;
    updateRecent(recentData, _forceRecentRender);
    _forceRecentRender = false;
    updatePagination();
    if (activeTab === 'agents') updateAgents(stats);
    if (activeTab === 'timeline') updateTimeline();
    loadStorageStats();
  } catch(e) { console.error(e); }
}

// 只在值真的变了时写 textContent —— 赋值会替换文本节点，选区同样会被清掉。
function _setText(id, value) {
  const el = document.getElementById(id);
  const text = String(value);
  if (el && el.textContent !== text) el.textContent = text;
}

function updateStats(s) {
  _setText('stat-agents', s.total_agents);
  _setText('stat-sessions', s.total_sessions);
  _setText('stat-active', s.active_sessions + ' active');
  _setText('stat-calls', s.total_tool_calls);
  _setText('stat-allow', s.allow_count);
  _setText('stat-deny', s.deny_count);
  _setText('stat-latency', s.avg_analysis_duration_ms + 'ms');

  // Risk bar
  const rl = s.risk_levels || {};
  const total = Object.values(rl).reduce((a,b)=>a+b, 0) || 1;
  const colors = {low:'var(--green)', medium:'var(--yellow)', high:'var(--orange)', critical:'var(--red)'};
  let bar = '', legend = '';
  for (const [level, count] of Object.entries(rl)) {
    const pct = (count/total*100).toFixed(1);
    bar += `<span style="width:${pct}%;background:${colors[level]||'var(--muted)'}"></span>`;
    legend += `<span>${level}: ${count}</span>`;
  }
  document.getElementById('risk-bar').innerHTML = bar;
  document.getElementById('risk-legend').innerHTML = legend;
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
  if (isNaN(d.getTime())) return (ts||'').substring(11,19);
  return d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
function updateRecent(data, force) {
  const tb = document.getElementById('recent-table');
  const key = JSON.stringify(data);
  if (!force) {
    if (key === _lastRecentKey) return;      // 数据没变，重建纯属白干（还会清掉选区）
    if (_userIsSelecting()) return;          // 正在选字，等选完再重建
  }
  _lastRecentKey = key;
  _currentPageRows = data.slice();           // 复制时读这份原始数据，不读 DOM

  const cols = _copyMode ? 11 : 10;
  if (!data.length) {
    tb.innerHTML = `<tr><td colspan="${cols}" class="empty">No tool calls yet</td></tr>`;
    updateCopyBar();
    return;
  }
  tb.innerHTML = data.map((r, idx) => `
    <tr>
      ${_copyMode ? `<td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}"${_selectedIds.has(Number(r.id)) ? ' checked' : ''}></td>` : ''}
      <td style="white-space:nowrap;font-size:12px;color:var(--muted)">${fmtTime(r.called_at)}</td>
      <td>${escapeHtml(r.agent_type||'-')}</td>
      <td class="session-cell" title="${escapeHtml(r.session_id||'')}">${escapeHtml(r.session_id||'-')}</td>
      <td>${escapeHtml(r.tool_name)}</td>
      <td class="command-cell" title="${escapeHtml(r.command||'')}">${escapeHtml(r.command||'')}</td>
      <td>${badge(r.decision)}</td>
      <td>${r.risk_level ? badge(r.risk_level) : '-'}</td>
      <td style="font-size:12px;color:var(--muted)">${r.analysis_duration_ms||0}ms</td>
      <td class="reason-cell" title="${escapeHtml(r.reason||'')}">${escapeHtml(r.reason||'-')}</td>
      <td>${parseChainGraphFromRow(r) ? `<button class="ag-chain-btn" data-chain="${idx}">查看攻击链</button>` : ''}</td>
    </tr>
  `).join('');
  // 勾选状态跨刷新保留（重建后按 _selectedIds 还原，这里只负责绑定事件）
  tb.querySelectorAll('input.row-check').forEach(cb => {
    cb.addEventListener('change', () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) _selectedIds.add(id); else _selectedIds.delete(id);
      updateCopyBar();
    });
  });
  updateCopyBar();
}

// ==================== 组合式攻击链图（SVG） ====================
// 数据来源：tool_calls.moss_response（完整 AgentMoss 响应）→ violation_graph
const CHAIN_ROLE_LABEL = {
  source: '敏感数据源', carrier: '数据载体', sink: '外传点',
  witness: '违例见证',
  LISTEN: '开据点', EXEC: '拿遥控器', SCAN: '探路',
  CRED: '偷钥匙', EXFIL: '往外搬', PERSIST: '赖着不走', PRIVESC: '抢 root'
};

function parseChainGraphFromRow(r) {
  var resp = r && r.moss_response;
  if (!resp) return null;
  if (typeof resp === 'string') { try { resp = JSON.parse(resp); } catch (e) { return null; } }
  var g = resp && resp.violation_graph;
  if (!g || !Array.isArray(g.nodes) || g.nodes.length === 0) return null;
  return g;
}

function renderChainSvg(g) {
  var nodes = g.nodes || [], edges = g.edges || [];
  var NW = 160, NH = 78, GX = 48, GY = 32, PER_ROW = 4;
  var rows = Math.max(1, Math.ceil(nodes.length / PER_ROW));
  var cols = Math.min(nodes.length, PER_ROW);
  var W = 40 + cols * NW + (cols - 1) * GX;
  var H = 40 + rows * NH + (rows - 1) * GY;
  var pos = nodes.map(function(_, i) {
    var r = Math.floor(i / PER_ROW), c = i % PER_ROW;
    return { x: 20 + c * (NW + GX), y: 20 + r * (NH + GY) };
  });
  var parts = [];
  parts.push('<defs><marker id="ag-chain-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#8E8E93"/></marker></defs>');
  // 边：同行直线；换行用正交折线
  edges.forEach(function(e) {
    var a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    var dash = e.type === 'data' ? ' stroke-dasharray="6 4"' : '';
    var rawLabel = e.label || '';
    if (rawLabel.length > 24) rawLabel = rawLabel.slice(0, 23) + '…';
    var labelText = rawLabel ? ('载体: ' + rawLabel) : '';
    var sameRow = Math.abs(a.y - b.y) < 1;
    var d, lx, ly;
    if (sameRow) {
      var x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      d = 'M' + x1 + ' ' + y1 + ' L' + (x2 - 6) + ' ' + y2;
      lx = (x1 + x2) / 2; ly = y1 - 7;
    } else {
      var ax = a.x + NW / 2, bx = b.x + NW / 2;
      var midY = (a.y + NH + b.y) / 2;
      d = 'M' + ax + ' ' + (a.y + NH) + ' L' + ax + ' ' + midY +
          ' L' + bx + ' ' + midY + ' L' + bx + ' ' + (b.y - 6);
      lx = (ax + bx) / 2; ly = midY - 5;
    }
    parts.push('<path d="' + d + '" fill="none" stroke="#8E8E93" stroke-width="1.5" stroke-linejoin="round"' + dash + ' marker-end="url(#ag-chain-arrow)"/>');
    if (labelText) {
      parts.push('<text x="' + lx + '" y="' + ly + '" text-anchor="middle" font-size="9" fill="#8E8E93">' + escapeHtml(labelText) + '</text>');
    }
  });
  // 节点
  nodes.forEach(function(n, i) {
    var p = pos[i], last = i === nodes.length - 1;
    var witness = !!n.witness || last;
    var stroke = witness ? '#FF3B30' : '#D1D1D6';
    var badgeColor = witness ? '#FF3B30' : '#007AFF';
    var roleKey = n.role || n.stage || '';
    var roleText = CHAIN_ROLE_LABEL[roleKey] || roleKey;
    var fullCmd = String(n.action_detail || '');
    var fullTitle = String((n.session_id || '-') + ' #' + n.seq);
    var tip = fullTitle + (roleText ? ' [' + roleText + ']' : '') + '\n' + fullCmd;
    if (n.resources && n.resources.length) tip += '\n资源: ' + n.resources.join(', ');
    parts.push('<g><title>' + escapeHtml(tip) + '</title>');
    parts.push('<rect x="' + p.x + '" y="' + p.y + '" width="' + NW + '" height="' + NH + '" rx="7" fill="#fff" stroke="' + stroke + '" stroke-width="' + (witness ? 2 : 1) + '"/>');
    parts.push('<circle cx="' + (p.x + 13) + '" cy="' + (p.y + 13) + '" r="8" fill="' + badgeColor + '" opacity="0.12"/>');
    parts.push('<text x="' + (p.x + 13) + '" y="' + (p.y + 16) + '" text-anchor="middle" font-size="9" font-weight="700" fill="' + badgeColor + '">' + (i + 1) + '</text>');
    var shortTitle = fullTitle.length > 15 ? fullTitle.slice(0, 14) + '…' : fullTitle;
    parts.push('<text x="' + (p.x + 25) + '" y="' + (p.y + 16) + '" font-size="9" font-weight="700" fill="#1d1d1f">' + escapeHtml(shortTitle) + '</text>');
    if (roleText) {
      parts.push('<text x="' + (p.x + NW - 8) + '" y="' + (p.y + 16) + '" text-anchor="end" font-size="7" font-weight="700" fill="' + (witness ? '#FF3B30' : '#007AFF') + '">' + escapeHtml(roleText) + '</text>');
    }
    var lines = [];
    for (var k = 0; k < fullCmd.length && lines.length < 2; k += 22) lines.push(fullCmd.slice(k, k + 22));
    if (fullCmd.length > 44) lines[1] = lines[1].slice(0, 18) + '...';
    lines.forEach(function(line, li) {
      parts.push('<text x="' + (p.x + 8) + '" y="' + (p.y + 34 + li * 12) + '" font-size="9" fill="#444" font-family="monospace">' + escapeHtml(line) + '</text>');
    });
    if (n.resources && n.resources.length) {
      var res = String(n.resources[0]);
      if (res.length > 22) res = '…' + res.slice(-21);
      parts.push('<text x="' + (p.x + 8) + '" y="' + (p.y + NH - 6) + '" font-size="8" fill="#8E8E93">' + escapeHtml(res) + '</text>');
    }
    if (witness) {
      parts.push('<text x="' + (p.x + NW - 8) + '" y="' + (p.y + NH - 6) + '" text-anchor="end" font-size="8" font-weight="700" fill="#FF3B30">违例见证</text>');
    }
    parts.push('</g>');
  });
  return '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">' + parts.join('') + '</svg>';
}

function renderPropertyExpr(g) {
  var expr = g.property_expression || [];
  if (!expr.length) return '';
  var parts = expr.map(function(t) {
    var idx = Array.isArray(t.node_index) ? t.node_index : [t.node_index];
    var nums = idx.map(function(i) { return String(i + 1); }).join(',');
    var label = CHAIN_ROLE_LABEL[t.token] || '';
    return '<span class="ag-expr-token"><b>' + escapeHtml(t.token) + '</b><small>' + (label ? escapeHtml(label) + ' · ' : '') + '节点 ' + nums + '</small></span>';
  });
  return '<span class="ag-expr-label">性质表达式</span>' + parts.join('<span class="ag-expr-arrow">→</span>');
}

function openChainModal(row) {
  var g = parseChainGraphFromRow(row);
  if (!g) return;
  document.getElementById('chain-modal-title').textContent = row.decision === 'Deny' ? '组合式攻击链' : '调用链';
  var meta = [];
  if (row.decision) meta.push('<span style="font-weight:700;color:' + (row.decision === 'Deny' ? 'var(--red)' : 'var(--green)') + '">' + escapeHtml(row.decision) + '</span>');
  if (g.property_short || g.property_id) {
    var pname = g.property_name ? '（' + g.property_name + '）' : '';
    meta.push('<span class="ag-modal-property">违反性质：' + escapeHtml(g.property_short || '') + ' · ' + escapeHtml(g.property_id || '') + escapeHtml(pname) + '</span>');
  }
  if (row.risk_level) meta.push('<span>风险：' + escapeHtml(row.risk_level) + '</span>');
  if (row.risk_type) meta.push('<span>类型：' + escapeHtml(row.risk_type) + '</span>');
  var layers = row.violated_layers;
  if (typeof layers === 'string') { try { layers = JSON.parse(layers); } catch (e) { layers = []; } }
  if (Array.isArray(layers) && layers.length) meta.push('<span>触发层：' + escapeHtml(layers.join(', ')) + '</span>');
  meta.push('<span>会话：' + escapeHtml(row.session_id || '-') + '</span>');
  document.getElementById('chain-modal-meta').innerHTML = meta.join('');
  document.getElementById('chain-modal-expr').innerHTML = renderPropertyExpr(g);
  document.getElementById('chain-modal-graph').innerHTML = renderChainSvg(g)
    + '<div class="ag-modal-legend"><span>—— 实线 = 动作时序</span><span>- - 虚线 = 数据流（共享同一文件/载体）</span><span>连线标签 = 共享载体路径</span><span style="color:var(--red)">红框 = 违例见证节点</span><span>① 序号 = 性质表达式对应节点</span></div>';
  document.getElementById('chain-modal-reason').textContent = row.reason || '';
  document.getElementById('chain-modal').style.display = 'flex';
}

function closeChainModal() {
  var m = document.getElementById('chain-modal');
  if (m) m.style.display = 'none';
}

const _chainTb = document.getElementById('recent-table');
if (_chainTb) _chainTb.addEventListener('click', function(ev) {
  const btn = ev.target && ev.target.closest ? ev.target.closest('[data-chain]') : null;
  if (!btn) return;
  const i = Number(btn.getAttribute('data-chain'));
  if (_currentPageRows[i]) openChainModal(_currentPageRows[i]);
});
const _chainClose = document.getElementById('chain-modal-close');
if (_chainClose) _chainClose.addEventListener('click', closeChainModal);
const _chainBackdrop = document.getElementById('chain-modal-backdrop');
if (_chainBackdrop) _chainBackdrop.addEventListener('click', closeChainModal);
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeChainModal(); });

function updatePagination() {
  const totalPages = Math.max(1, Math.ceil(_totalRecords / _pageSize));
  const el = document.getElementById('ag-pagination');
  if (!el) return;
  const sig = _currentPage + '/' + totalPages + '/' + _totalRecords;
  if (sig === _lastPageSig) return;          // 翻页信息没变，别重建
  if (_userIsSelecting()) return;
  _lastPageSig = sig;
  el.innerHTML = '';
  const btnPrev = document.createElement('button');
  btnPrev.textContent = 'Prev';
  btnPrev.disabled = _currentPage <= 1;
  btnPrev.onclick = () => { _currentPage--; refresh(); };
  const btnNext = document.createElement('button');
  btnNext.textContent = 'Next';
  btnNext.disabled = _currentPage >= totalPages;
  btnNext.onclick = () => { _currentPage++; refresh(); };
  const label = document.createElement('span');
  label.textContent = `Page ${_currentPage} / ${totalPages} (${_totalRecords} records)`;
  el.appendChild(btnPrev);
  el.appendChild(label);
  el.appendChild(btnNext);
}

function updateAgents(s) {
  const types = s.agent_types || {};
  const el = document.getElementById('agent-list');
  if (!Object.keys(types).length) {
    el.innerHTML = '<div class="empty">No agents registered yet</div>';
    return;
  }
  el.innerHTML = Object.entries(types).map(([type, count]) => `
    <div class="agent-card">
      <div class="name">${escapeHtml(type)}</div>
      <div class="meta">Agent Type</div>
      <div class="stats">
        <div class="stat">Sessions: <strong>${count}</strong></div>
      </div>
    </div>
  `).join('');
}

async function updateTimeline() {
  try {
    const data = await fetch(BASE+'/dashboard/timeline?minutes=60').then(r=>r.json());
    drawTimeline(data);
  } catch(e) {}
}

function drawTimeline(data) {
  const canvas = document.getElementById('timeline-canvas');
  const empty = document.getElementById('timeline-empty');

  // 空态走 DOM 文本，不画在 canvas 上。canvas 是位图：空数据分支以前既没设
  // canvas.width/height（一直是默认 300×150），又用 fillText 写字，再被 CSS
  // 的 width:100%;height:100% 拉伸到容器大小（实测 300×150 → 1518×246，横向
  // 放大约 5 倍）——于是那句提示糊成一团、还被拉长。改用 DOM 文本后清晰且可选中。
  if (!data.length) {
    canvas.style.display = 'none';
    empty.style.display = 'flex';
    return;
  }
  canvas.style.display = '';
  empty.style.display = 'none';

  // 位图分辨率对齐 CSS 显示尺寸 × devicePixelRatio，否则高 DPI 屏上整张图也是糊的。
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0,0,W,H);

  const maxVal = Math.max(1, ...data.map(d=>Math.max(d.Allow||0, d.Deny||0)));
  const barW = Math.max(2, (W - 40) / data.length - 2);
  const chartH = H - 40;

  data.forEach((d, i) => {
    const x = 20 + i * (barW + 2);
    // Allow bar
    const allowH = ((d.Allow||0) / maxVal) * chartH;
    ctx.fillStyle = '#34C759';
    ctx.fillRect(x, chartH - allowH + 10, barW/2, allowH);
    // Deny bar
    const denyH = ((d.Deny||0) / maxVal) * chartH;
    ctx.fillStyle = '#FF3B30';
    ctx.fillRect(x + barW/2, chartH - denyH + 10, barW/2, denyH);
  });

  // Legend
  ctx.fillStyle = '#34C759'; ctx.fillRect(W-120, 8, 10, 10);
  ctx.fillStyle = '#8E8E93'; ctx.font='11px sans-serif'; ctx.textAlign='left';
  ctx.fillText('Allow', W-106, 18);
  ctx.fillStyle = '#FF3B30'; ctx.fillRect(W-60, 8, 10, 10);
  ctx.fillText('Deny', W-46, 18);
}

// 转义 HTML 特殊字符。**必须包含引号**：本函数同时用于文本上下文和属性上下文
// （如 title="${...}"）；只转 &<> 时，命令里的 `" onmouseover="` 能冲出属性注入
// 事件处理器 —— 命令/agent_type/session_id/tool_name 全部来自调用方，属不可信输入。
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── 复制记录 ───────────────────────────────────────────────────────────
// 复制内容直接从 _currentPageRows 的原始字段构造，**不读 DOM** —— 表格里的
// Command 列被 ellipsis 裁成一行，但原始数据是完整的，这样复制出来不截断。

/** 完整时间戳（TSV 用；表格里只显示到秒，粘到表格里需要日期）。 */
function fmtTimeFull(ts) {
  if (!ts) return '-';
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
  if (isNaN(d.getTime())) return String(ts);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 记录数组 → 文本。TSV 便于粘进表格，JSON 保留原始字段便于程序处理。 */
function buildCopyPayload(rows, fmt) {
  // 值里的 tab/换行会破坏 TSV 的列结构，替换成空格
  const cell = v => String(v ?? '').replace(/[\t\r\n]+/g, ' ');
  if (fmt === 'json') {
    return JSON.stringify(rows.map(r => ({
      id: r.id ?? null,
      called_at: r.called_at ?? null,
      agent_type: r.agent_type ?? null,
      session_id: r.session_id ?? null,
      tool_name: r.tool_name ?? null,
      command: r.command ?? null,
      description: r.description ?? null,
      cwd: r.cwd ?? null,
      decision: r.decision ?? null,
      risk_level: r.risk_level ?? null,
      risk_type: r.risk_type ?? null,
      violated_layers: r.violated_layers ?? null,
      confidence: r.confidence ?? null,
      analysis_duration_ms: r.analysis_duration_ms ?? null,
      reason: r.reason ?? null,
    })), null, 2);
  }
  const header = ['Time','Agent','Session','Tool','Command','Decision','Risk','Latency','Reason'].join('\t');
  const lines = rows.map(r => [
    fmtTimeFull(r.called_at),
    r.agent_type || '-',
    r.session_id || '-',
    r.tool_name || '-',
    r.command || '-',
    r.decision || '-',
    r.risk_level || '-',
    Math.round(r.analysis_duration_ms || 0) + 'ms',
    r.reason || '-',
  ].map(cell).join('\t'));
  return [header, ...lines].join('\n');
}

/** 写剪贴板：优先 navigator.clipboard（仅在安全上下文可用 —— 直接开
 *  http://<WSL-IP>:9100 时不算安全上下文），失败回退 execCommand。 */
async function writeClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch (e) { /* 落到兜底 */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none;';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
}

let _toastTimer = null;
function toast(text, isError) {
  let el = document.getElementById('dash-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'dash-toast'; el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.toggle('error', !!isError);
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

async function doCopy(rows, okMsg) {
  if (_copying) return;
  _copying = true; updateCopyBar();
  try {
    await writeClipboard(buildCopyPayload(rows, _copyFormat));
    toast(okMsg, false);
  } catch (e) {
    toast('Copy failed: ' + e, true);
  } finally {
    _copying = false; updateCopyBar();
  }
}

function setCopyFormat(fmt) {
  _copyFormat = fmt;
  document.getElementById('fmt-tsv').classList.toggle('active', fmt === 'tsv');
  document.getElementById('fmt-json').classList.toggle('active', fmt === 'json');
}

function setCopyMode(on) {
  _copyMode = on;
  if (!on) _selectedIds.clear();
  document.getElementById('copy-bar').classList.toggle('on', on);
  document.getElementById('enter-copy').style.display = on ? 'none' : '';
  document.getElementById('recent-calls').classList.toggle('copy-mode', on);
  _forceRecentRender = true;   // 列结构变了，必须重建表格
  refresh();
}

function updateCopyBar() {
  const bar = document.getElementById('copy-bar');
  if (!bar) return;
  const n = _selectedIds.size;
  document.getElementById('copy-count').textContent = n + ' selected';
  document.getElementById('copy-selected').disabled = _copying || n === 0;
  document.getElementById('copy-page').disabled = _copying || _currentPageRows.length === 0;
  document.getElementById('fmt-tsv').disabled = _copying;
  document.getElementById('fmt-json').disabled = _copying;
}

document.getElementById('enter-copy').addEventListener('click', () => setCopyMode(true));
document.getElementById('exit-copy').addEventListener('click', () => setCopyMode(false));
document.getElementById('fmt-tsv').addEventListener('click', () => setCopyFormat('tsv'));
document.getElementById('fmt-json').addEventListener('click', () => setCopyFormat('json'));
document.getElementById('copy-selected').addEventListener('click', async () => {
  const want = new Set(_selectedIds);
  const rows = _currentPageRows.filter(r => want.has(Number(r.id)));
  if (!rows.length) { toast('No records selected', true); return; }
  await doCopy(rows, `Copied ${rows.length} record(s)`);
});
document.getElementById('copy-page').addEventListener('click', async () => {
  const rows = _currentPageRows.slice();
  if (!rows.length) { toast('No records on this page', true); return; }
  await doCopy(rows, `Copied ${rows.length} record(s) on this page`);
});

// Clock
function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleString();
}
setInterval(updateClock, 1000); updateClock();

// Init
refresh();
setInterval(refresh, 3000);

// Resize handler for timeline chart
window.addEventListener('resize', () => { if (activeTab==='timeline') updateTimeline(); });
</script>
</body>
</html>"""


_UNAUTHORIZED_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>AgentGate Dashboard</title>
<style>body{font:14px/1.6 -apple-system,'Segoe UI',sans-serif;background:#FAFAFA;color:#1d1d1f;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#fff;border:1px solid #eee;border-radius:10px;padding:28px 32px;max-width:560px}
code{background:#F5F5F7;padding:1px 6px;border-radius:4px;font-size:13px}
p{margin:8px 0;color:#555}</style></head><body>
<div class="box">
<h3 style="margin-top:0">需要访问凭证</h3>
<p>Dashboard 现在需要令牌。浏览器无法自动带上 <code>Authorization</code> 头，请用下面这种地址打开一次：</p>
<p><code>/dashboard/?token=&lt;token&gt;</code></p>
<p>令牌位置：<code>~/.config/agentgate/auth_token</code>
（或启动日志里打印的那一行）。换 Cookie 后就只需打开 <code>/dashboard/</code>。</p>
<p>如需恢复旧行为：<code>AGENT_GATE_AUTH_MODE=off</code>。</p>
</div></body></html>"""


_AGENT_FORBIDDEN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>AgentGate Dashboard</title>
<style>body{font:14px/1.6 -apple-system,'Segoe UI',sans-serif;background:#FAFAFA;color:#1d1d1f;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#fff;border:1px solid #eee;border-radius:10px;padding:28px 32px;max-width:560px}
code{background:#F5F5F7;padding:1px 6px;border-radius:4px;font-size:13px}
p{margin:8px 0;color:#555}</style></head><body>
<div class="box">
<h3 style="margin-top:0">该凭证无权访问看板</h3>
<p>你使用的是 <b>agent 专属 token</b>。看板是全局聚合视图，只接受全局（管理员）token ——
否则 agent A 就能看到 agent B 的全部命令原文和用户 prompt。</p>
<p>agent 自查请用读接口（会按凭证绑定的 agent 自动过滤）：
<code>/api/v1/sessions/&lt;id&gt;/traces</code>、<code>/api/v1/tool_calls</code>。</p>
<p>运维请使用全局 token：<code>~/.config/agentgate/auth_token</code>。</p>
</div></body></html>"""


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, token: str = Query("")):
    """Serve the dashboard HTML page.

    鉴权开启后，浏览器无法给 HTML 导航加 ``Authorization`` 头，所以：
      - **浏览器导航**：``?token=<token>`` → 下发 HttpOnly + SameSite=Strict 的
        Cookie → 302 回 ``/dashboard/``（URL 里的 token 随即从地址栏消失）；
      - **fetch 客户端**（Electron 内嵌看板等）：直接带 ``Authorization: Bearer``，
        命中后原地渲染。这类客户端跨源，302 后的 Cookie 会被 SameSite=Strict
        挡掉，只能走这条路径。
    """
    if not sp.auth_enabled():
        return HTMLResponse(DASHBOARD_HTML, headers=_DASHBOARD_SECURITY_HEADERS)
    # 看板是全局聚合视图：只接受**全局（管理员）token**。
    # 逐 agent token 能换 Cookie 的话，agent A 就能看到 agent B 的全部命令
    # 原文和用户 prompt —— 这是跨 agent 数据泄露。
    auth_header = request.headers.get("authorization", "")
    bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    cookie = request.cookies.get(sp.DASHBOARD_COOKIE, "")
    if bearer and sp.is_global_token(bearer):
        # fetch 客户端：直接渲染，不重定向（跨源下 Cookie 不可用）
        return HTMLResponse(DASHBOARD_HTML, headers=_DASHBOARD_SECURITY_HEADERS)
    if token and sp.is_global_token(token):
        resp = RedirectResponse(
            url="/dashboard/", status_code=302, headers=_DASHBOARD_SECURITY_HEADERS,
        )
        resp.set_cookie(
            sp.DASHBOARD_COOKIE, token,
            httponly=True, samesite="strict", path="/",
        )
        return resp
    if cookie and sp.is_global_token(cookie):
        return HTMLResponse(DASHBOARD_HTML, headers=_DASHBOARD_SECURITY_HEADERS)
    # 有效但**不是全局**的凭证 → 403（agent token 打不开看板）；
    # 无效凭证 → 401（不知道自己是谁）。
    if (
        (bearer and sp.check_token(bearer))
        or (token and sp.check_token(token))
        or (cookie and sp.check_token(cookie))
    ):
        return HTMLResponse(
            _AGENT_FORBIDDEN_HTML, status_code=403, headers=_DASHBOARD_SECURITY_HEADERS,
        )
    return HTMLResponse(
        _UNAUTHORIZED_HTML, status_code=401, headers=_DASHBOARD_SECURITY_HEADERS,
    )
