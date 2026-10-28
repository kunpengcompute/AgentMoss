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
.command-cell { max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:'SF Mono',Menlo,monospace; font-size:12px; }
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
        <th>Decision</th><th>Risk</th><th>Latency</th>
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

<div class="footer">AgentGate V1 · Data refreshes automatically</div>

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

  const cols = _copyMode ? 9 : 8;
  if (!data.length) {
    tb.innerHTML = `<tr><td colspan="${cols}" class="empty">No tool calls yet</td></tr>`;
    updateCopyBar();
    return;
  }
  tb.innerHTML = data.map(r => `
    <tr>
      ${_copyMode ? `<td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}"${_selectedIds.has(Number(r.id)) ? ' checked' : ''}></td>` : ''}
      <td style="white-space:nowrap;font-size:12px;color:var(--muted)">${fmtTime(r.called_at)}</td>
      <td>${escapeHtml(r.agent_type||'-')}</td>
      <td style="font-size:12px;color:var(--muted)">${escapeHtml((r.session_id||'').substring(0,12))}</td>
      <td>${escapeHtml(r.tool_name)}</td>
      <td class="command-cell" title="${escapeHtml(r.command||'')}">${escapeHtml(r.command||'')}</td>
      <td>${badge(r.decision)}</td>
      <td>${r.risk_level ? badge(r.risk_level) : '-'}</td>
      <td style="font-size:12px;color:var(--muted)">${r.analysis_duration_ms||0}ms</td>
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
