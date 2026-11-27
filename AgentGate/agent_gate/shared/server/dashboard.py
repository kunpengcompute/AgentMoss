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
#: 定义在 ``security.py`` 里作为单一事实源 —— V3 看板（http.server）共用同一份。
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
    decision: Optional[str] = Query(None),
    agent_type: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    sort_by: str = Query("called_at"),
    sort_dir: str = Query("desc"),
    repo=Depends(get_repo),
):
    """Recent tool calls with session and agent info. Supports filter/sort/pagination."""
    db = repo._db
    conditions: list[str] = []
    params: list[str] = []
    if decision:
        conditions.append("tc.decision = ?")
        params.append(decision)
    if agent_type:
        conditions.append("a.agent_type = ?")
        params.append(agent_type)
    if session_id:
        conditions.append("tc.session_id = ?")
        params.append(session_id)
    if risk_level:
        conditions.append("tc.risk_level = ?")
        params.append(risk_level)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # Sort column is a whitelist (never interpolate request values directly).
    sort_columns = {
        "called_at": "tc.called_at",
        "agent_type": "a.agent_type",
        "session_id": "tc.session_id",
        "tool_name": "tc.tool_name",
        "command": "tc.command",
        "decision": "tc.decision",
        "risk_level": "tc.risk_level",
        "analysis_duration_ms": "tc.analysis_duration_ms",
        "reason": "tc.reason",
    }
    sort_col = sort_columns.get(sort_by, "tc.called_at")
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

    total_row = db.execute(
        f"""SELECT COUNT(*) as total
            FROM tool_calls tc
            JOIN sessions s ON tc.session_id = s.id
            JOIN agents a ON s.agent_id = a.id
            {where}""",
        tuple(params),
    ).fetchone()
    total = total_row["total"] if total_row else 0

    rows = db.execute(
        f"""SELECT tc.*, s.agent_id, s.user_prompt, s.os_type,
                   s.cwd AS session_cwd, a.agent_type
            FROM tool_calls tc
            JOIN sessions s ON tc.session_id = s.id
            JOIN agents a ON s.agent_id = a.id
            {where}
            ORDER BY {sort_col} {direction}, tc.id DESC
            LIMIT ? OFFSET ?""",
        tuple(params) + (limit, offset),
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
# Session log — payload data flow (inspired by dsh trajectory)
# ============================================================

def _sessionlog_dir() -> str:
    from agent_gate.shared.sessionlog.convert import DEFAULT_LOG_DIR
    return DEFAULT_LOG_DIR


@router.get("/sessionlog")
async def list_sessionlogs():
    """List persisted session logs with aggregate summaries (data flow view)."""
    import glob
    import os
    from agent_gate.shared.sessionlog import load, summarize

    log_dir = _sessionlog_dir()
    files = sorted(glob.glob(os.path.join(log_dir, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:50]:
        sid = os.path.basename(f)[:-len(".jsonl")]
        try:
            summary = summarize(load(f, session_id=sid))
        except Exception:
            summary = {}
        out.append({
            "session_id": sid,
            "file": f,
            "mtime": int(os.path.getmtime(f) * 1000),
            "summary": summary,
        })
    return {"total": len(out), "logs": out}


@router.get("/sessionlog/{session_id}")
async def get_sessionlog_flow(session_id: str, request: Request):
    """Assemble one session's payload data-flow records (trajectory view)."""
    import os
    from fastapi import HTTPException
    from agent_gate.shared.sessionlog import load, summarize
    from agent_gate.shared.sessionlog.trajectory import build_trajectory

    # 跨会话批量访问信号：正常 agent 只碰自己那一个 session；短时间翻大量
    # session_id 说明有人在扫库（默认只告警，不改判定）。
    _principal = getattr(request.state, "principal", "") or (
        "ip:" + (getattr(request.client, "host", "") or "-")
    )
    n, hit = sp.note_session_access(_principal, session_id)
    if hit:
        if sp.session_sweep_action() == "deny":
            raise HTTPException(status_code=429, detail="会话访问过于集中，疑似批量扫描")
        logger.warning(
            "⚠️ 疑似跨会话扫描: key={} 窗口内访问了 {} 个不同 session", _principal, n,
        )

    path = os.path.join(_sessionlog_dir(), f"{session_id}.jsonl")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"no session log for {session_id}")
    log = load(path, session_id=session_id)
    return {
        "session_id": session_id,
        "summary": summarize(log),
        "records": build_trajectory(log),
        "events": [e.to_dict() for e in log.events],
    }


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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title data-i18n="appTitle">AgentGate Dashboard</title>
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
.header { background:var(--card-bg); border-bottom:1px solid var(--border); padding:16px 24px; display:flex; align-items:center; }
.header h1 { font-size:16px; font-weight:700; letter-spacing:-0.01em; }
.lang-switch { margin-left:auto; display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.lang-switch button { border:none; background:transparent; color:var(--muted); padding:4px 10px; font-size:12px; cursor:pointer; }
.lang-switch button.active { background:var(--accent); color:#fff; }
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
.ag-pagination { display:flex; align-items:center; justify-content:center; flex-wrap:wrap; gap:8px; padding:10px 14px; font-size:12px; color:var(--muted); background:#FAFAFA; border-top:1px solid var(--border); }
.ag-pagination button { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--text); font-size:12px; cursor:pointer; }
.ag-pagination button:hover:not(:disabled) { background:var(--accent); color:#fff; border-color:var(--accent); }
.ag-pagination button:disabled { opacity:0.4; cursor:default; }
.ag-pagination .page-nav { display:inline-flex; align-items:center; gap:8px; }
.ag-pagination .page-size-group, .ag-pagination .page-jump-group { display:inline-flex; align-items:center; gap:6px; }
.ag-pagination select, .ag-pagination input { padding:3px 6px; border:1px solid var(--border); border-radius:6px; background:var(--card-bg); color:var(--text); font-size:12px; }
.ag-pagination input { width:64px; }
.filter-bar { display:flex; align-items:center; flex-wrap:wrap; gap:10px; padding:10px 16px; background:#FAFAFA; border-bottom:1px solid var(--border); font-size:12px; color:var(--muted); }
.filter-bar label { display:inline-flex; align-items:center; gap:5px; }
.filter-bar select, .filter-bar input { padding:3px 6px; border:1px solid var(--border); border-radius:6px; background:var(--card-bg); color:var(--text); font-size:12px; }
.filter-bar input { width:180px; }
.filter-bar button { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:var(--text); font-size:12px; cursor:pointer; }
.filter-bar button:hover { border-color:var(--accent); color:var(--accent); }
th.sortable { cursor:pointer; user-select:none; }
th.sortable:hover { color:var(--text); }
th.sortable[data-dir="asc"]::after { content:' ▲'; font-size:9px; }
th.sortable[data-dir="desc"]::after { content:' ▼'; font-size:9px; }
#recent-calls th { position:relative; }
#recent-calls td:not(.col-check) { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#recent-calls td.col-check { overflow:visible; text-overflow:clip; }
/* 这些 td 原有 max-width 会限制列宽，拖宽时看起来“拖不动”；进入列宽模式后交给列宽控制。 */
#recent-calls.col-resized .session-cell,
#recent-calls.col-resized .command-cell,
#recent-calls.col-resized .reason-cell { max-width:none; }
.col-resizer { position:absolute; right:-7px; top:0; width:14px; height:100%; cursor:col-resize; z-index:5; }
.col-resizer::after { content:''; position:absolute; left:6px; top:20%; width:2px; height:60%; background:var(--border); }
.col-resizer:hover::after { background:var(--accent); }
.tabs { display:flex; gap:0; padding:0 24px; background:#FAFAFA; }
.tab { padding:8px 20px; cursor:pointer; border:1px solid var(--border); border-bottom:none; background:var(--bg); color:var(--muted); border-radius:8px 8px 0 0; margin-right:-1px; font-weight:500; }
.tab.active { background:var(--card-bg); color:var(--text); border-bottom-color:var(--card-bg); font-weight:600; }
.tab:hover:not(.active) { color:var(--text); }
/* ── 多选操作（复制 / 删除） ── */
/* 入口按钮组靠 tabs 行右侧；tabs 是 flex，margin-left:auto 即可 */
.tab-actions { margin-left:auto; display:flex; align-items:center; gap:8px; align-self:center; }
.tab-actions .tab-action { margin-left:0; }
.tab-action { align-self:center; padding:5px 14px; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:var(--accent); font-size:12px; font-weight:500; cursor:pointer; }
.tab-action:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
.tab-action.danger { color:var(--red); border-color:rgba(255,59,48,.35); }
.tab-action.danger:hover { background:var(--red); color:#fff; border-color:var(--red); }
/* 多选工具栏：默认隐藏，进多选模式才展开 */
.copy-bar { display:none; align-items:center; gap:8px; padding:10px 16px; background:#FAFAFA; border-bottom:1px solid var(--border); font-size:12px; color:var(--muted); }
.copy-bar.on { display:flex; }
.copy-bar .spacer { flex:1; }
.copy-bar button { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:var(--text); font-size:12px; cursor:pointer; }
.copy-bar button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.copy-bar button:disabled { opacity:.4; cursor:default; }
.copy-bar button.ag-danger { color:var(--red); border-color:rgba(255,59,48,.35); }
.copy-bar button.ag-danger:hover:not(:disabled) { background:var(--red); color:#fff; border-color:var(--red); }
.bar-group { display:none; align-items:center; gap:8px; }
.copy-bar .fmt-group { display:none; }
.copy-bar.mode-copy .bar-group[data-mode="copy"],
.copy-bar.mode-copy .fmt-group,
.copy-bar.mode-delete .bar-group[data-mode="delete"] { display:inline-flex; }
.fmt-group { display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.fmt-group button { border:none; border-radius:0; padding:4px 10px; color:var(--muted); background:transparent; }
.fmt-group button.active { background:var(--accent); color:#fff; }
.fmt-group button:hover:not(:disabled):not(.active) { color:var(--text); }
/* 勾选框列：只在多选模式出现 */
.col-check { display:none; width:34px; min-width:34px; text-align:center; }
table.select-mode .col-check { display:table-cell; width:34px; min-width:34px; }
.col-check input { cursor:pointer; }
/* 轻提示 */
.toast { position:fixed; left:50%; bottom:32px; transform:translateX(-50%) translateY(8px); background:#1d1d1f; color:#fff; padding:8px 16px; border-radius:8px; font-size:13px; opacity:0; pointer-events:none; transition:opacity .18s, transform .18s; z-index:99; }
.toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
.toast.error { background:var(--red); }
.footer { padding:12px 24px; color:var(--muted); font-size:12px; border-top:1px solid var(--border); margin-top:20px; text-align:center; }
.flow-wrap { padding:12px 16px; max-height:520px; overflow-y:auto; }
.flow-row { display:flex; align-items:flex-start; gap:10px; padding:8px 0; border-bottom:1px solid #F5F5F7; }
.flow-row .f-kind { flex:0 0 76px; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; padding-top:2px; }
.flow-row.k-user .f-kind { color:var(--accent); }
.flow-row.k-llm .f-kind { color:var(--purple); }
.flow-row.k-tool .f-kind { color:var(--yellow); }
.flow-row.k-process .f-kind { color:var(--green); }
.flow-row.k-syscall .f-kind { color:var(--muted); }
.flow-row.k-system .f-kind { color:var(--muted); }
.flow-row .f-body { flex:1; min-width:0; font-size:13px; }
.flow-row .f-meta { font-size:12px; color:var(--muted); margin-top:2px; }
.flow-row .f-arrow { flex:0 0 18px; color:var(--muted); text-align:center; padding-top:2px; }
.f-ok { color:var(--green); }
.f-err { color:var(--red); }
.flow-btn { padding:2px 10px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--accent); font-size:12px; cursor:pointer; }
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
    <span style="display:flex;align-items:center;"><span class="live-dot"></span><span data-i18n="appTitle">AgentGate Dashboard</span></span>
    <span style="color:var(--muted);font-size:13px;font-weight:400" id="clock"></span>
  </h1>
  <div class="lang-switch" id="lang-switch">
    <button type="button" data-lang="zh">中文</button>
    <button type="button" data-lang="en">EN</button>
  </div>
</div>

<!-- Stats cards -->
<div class="grid">
  <div class="card"><div class="label" data-i18n="agents">Agents</div><div class="value" id="stat-agents">-</div></div>
  <div class="card"><div class="label" data-i18n="sessions">Sessions</div><div class="value" id="stat-sessions">-</div><div class="sub" id="stat-active"></div></div>
  <div class="card"><div class="label" data-i18n="toolCalls">Tool Calls</div><div class="value" id="stat-calls">-</div></div>
  <div class="card"><div class="label" data-i18n="allow">Allow</div><div class="value" style="color:var(--green)" id="stat-allow">-</div></div>
  <div class="card"><div class="label" data-i18n="deny">Deny</div><div class="value" style="color:var(--red)" id="stat-deny">-</div></div>
  <div class="card"><div class="label" data-i18n="avgLatency">Avg Latency</div><div class="value" id="stat-latency">-</div></div>
  <div class="card" style="grid-column:span 2">
    <div class="label" data-i18n="riskDistribution">Risk Distribution</div>
    <div class="risk-bar" id="risk-bar"></div>
    <div style="display:flex;gap:16px;margin-top:8px;font-size:12px" id="risk-legend"></div>
  </div>
</div>

<div class="ag-storage-banner" id="storage-banner">
  <span class="ag-sb-text" id="storage-banner-text"></span>
  <span class="ag-sb-actions">
    <span class="ag-sb-status" id="storage-banner-status"></span>
    <button class="ag-sb-btn" id="storage-clear-btn" data-i18n="storageClear">Clear History</button>
  </span>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="recent" data-i18n="recentCalls">Recent Calls</div>
  <div class="tab" data-tab="agents" data-i18n="agentsTab">Agents</div>
  <div class="tab" data-tab="timeline" data-i18n="timelineTab">Timeline</div>
  <div class="tab" data-tab="flow" data-i18n="dataFlowTab">Data Flow</div>
  <span class="tab-actions">
    <button class="tab-action" id="set-col-defaults" data-i18n="setColDefaults">Set as Default</button>
    <button class="tab-action" id="reset-col-widths" data-i18n="resetColWidths">Reset Column Widths</button>
    <button class="tab-action danger" id="enter-delete" data-i18n="deleteRecords">Delete Records</button>
    <button class="tab-action" id="enter-copy" data-i18n="copyRecords">Copy Records</button>
  </span>
</div>

<!-- Recent calls -->
<div class="main-area" id="tab-recent">
  <div class="panel" style="grid-column:1/-1">
    <div class="panel-header" data-i18n="recentToolCalls">Recent Tool Calls (auto-refresh every 3s)</div>
    <div class="filter-bar" id="filter-bar">
      <label><span data-i18n="filterDecision">Decision</span>
        <select id="filter-decision">
          <option value="" data-i18n="all">All</option>
          <option value="Allow" data-i18n="allow">Allow</option>
          <option value="Deny" data-i18n="deny">Deny</option>
        </select>
      </label>
      <label><span data-i18n="filterAgent">Agent</span>
        <select id="filter-agent"><option value="" data-i18n="all">All</option></select>
      </label>
      <label><span data-i18n="filterRisk">Risk</span>
        <select id="filter-risk">
          <option value="" data-i18n="all">All</option>
          <option value="low" data-i18n="riskLow">Low</option>
          <option value="medium" data-i18n="riskMedium">Medium</option>
          <option value="high" data-i18n="riskHigh">High</option>
          <option value="critical" data-i18n="riskCritical">Critical</option>
        </select>
      </label>
      <label><span data-i18n="filterSession">Session</span>
        <input id="filter-session" type="text" data-i18n-placeholder="sessionIdPlaceholder" placeholder="Session ID">
      </label>
      <button id="filter-apply" data-i18n="apply">Apply</button>
      <button id="filter-reset" data-i18n="reset">Reset</button>
    </div>
    <div class="copy-bar" id="copy-bar">
      <span id="copy-count">0 selected</span>
      <span class="fmt-group">
        <button id="fmt-tsv" class="active">TSV</button>
        <button id="fmt-json">JSON</button>
      </span>
      <span class="spacer"></span>
      <span class="bar-group" data-mode="delete">
        <button id="delete-selected" class="ag-danger" disabled data-i18n="deleteSelected">Delete Selected</button>
        <button id="delete-all" class="ag-danger" data-i18n="deleteAll">Delete All</button>
      </span>
      <span class="bar-group" data-mode="copy">
        <button id="copy-selected" disabled data-i18n="copySelected">Copy Selected</button>
        <button id="copy-page" data-i18n="copyCurrentPage">Copy Current Page</button>
      </span>
      <button id="exit-copy" data-i18n="cancel">Cancel</button>
    </div>
    <div class="table-wrap">
      <table id="recent-calls"><thead><tr>
        <th class="col-check"><input type="checkbox" id="select-all" disabled data-i18n-title="selectCurrentPage" title="Select current page"></th>
        <th class="sortable" data-sort="called_at" data-i18n="time">Time</th>
        <th class="sortable" data-sort="agent_type" data-i18n="agent">Agent</th>
        <th class="sortable" data-sort="session_id" data-i18n="session">Session</th>
        <th class="sortable" data-sort="tool_name" data-i18n="tool">Tool</th>
        <th class="sortable" data-sort="command" data-i18n="command">Command</th>
        <th class="sortable" data-sort="decision" data-i18n="decision">Decision</th>
        <th class="sortable" data-sort="risk_level" data-i18n="risk">Risk</th>
        <th class="sortable" data-sort="analysis_duration_ms" data-i18n="latency">Latency</th>
        <th class="sortable" data-sort="reason" data-i18n="reason">Reason</th>
        <th data-i18n="action">Action</th>
      </tr></thead><tbody id="recent-table"></tbody></table>
    </div>
    <div id="ag-pagination" class="ag-pagination"></div>
  </div>
</div>

<!-- Agents -->
<div class="agents" id="tab-agents" style="display:none">
  <div class="panel-header" style="border-bottom:none;padding-left:0" data-i18n="registeredAgents">Registered Agents</div>
  <div class="agent-grid" id="agent-list"></div>
</div>

<!-- Timeline -->
<div class="chart-area" id="tab-timeline" style="display:none">
  <div class="chart">
    <canvas id="timeline-canvas"></canvas>
    <div class="chart-empty" id="timeline-empty" style="display:none" data-i18n="timelineEmpty">No data in the last 60 minutes</div>
  </div>
</div>

<!-- Data Flow (sessionlog payload flow) -->
<div class="main-area" id="tab-flow" style="display:none">
  <div class="panel" style="grid-column:1/-1">
    <div class="panel-header" data-i18n="sessionLogs">Session Logs (payload data flow)</div>
    <div class="table-wrap">
      <table><thead><tr>
        <th data-i18n="flowSession">Session</th><th data-i18n="flowTurns">Turns</th><th data-i18n="flowLlm">LLM</th><th data-i18n="flowTools">Tools</th><th data-i18n="flowErrors">Errors</th>
        <th data-i18n="flowProc">Proc</th><th data-i18n="flowTokens">Tokens</th><th data-i18n="flowDuration">Duration</th><th></th>
      </tr></thead><tbody id="flow-sessions"></tbody></table>
    </div>
  </div>
  <div class="panel" style="grid-column:1/-1">
    <div class="panel-header" id="flow-title" data-i18n="selectSession">Select a session to view its data flow</div>
    <div id="flow-records" class="flow-wrap"><div class="empty" data-i18n="noSessionSelected">No session selected</div></div>
  </div>
</div>

<div class="footer" id="footer" data-i18n="loadingVersions">Loading versions...</div>

<div class="ag-modal" id="chain-modal">
  <div class="ag-modal-backdrop" id="chain-modal-backdrop"></div>
  <div class="ag-modal-box">
    <div class="ag-modal-head">
      <span class="ag-modal-title" id="chain-modal-title" data-i18n="comboChain">Combined Attack Chain</span>
      <button class="ag-modal-close" id="chain-modal-close" data-i18n-title="close" title="Close">×</button>
    </div>
    <div class="ag-modal-meta" id="chain-modal-meta"></div>
    <div class="ag-modal-expr" id="chain-modal-expr"></div>
    <div class="ag-modal-graph" id="chain-modal-graph"></div>
    <div class="ag-modal-reason" id="chain-modal-reason"></div>
  </div>
</div>

<script>
const BASE = '';

// ── 中英文切换 ─────────────────────────────────────────────────────────
// 每个词条：[英文, 中文]。页面默认跟随浏览器语言，选择结果写入 localStorage。
const I18N = {
  appTitle: ['AgentGate Dashboard', 'AgentGate 安全网关看板'],
  agents: ['Agents', '智能体'],
  sessions: ['Sessions', '会话'],
  toolCalls: ['Tool Calls', '工具调用'],
  allow: ['Allow', '放行'],
  deny: ['Deny', '拦截'],
  avgLatency: ['Avg Latency', '平均耗时'],
  riskDistribution: ['Risk Distribution', '风险分布'],
  storageClear: ['Clear History', '清理历史记录'],
  recentCalls: ['Recent Calls', '最近调用'],
  agentsTab: ['Agents', '智能体'],
  timelineTab: ['Timeline', '时间线'],
  dataFlowTab: ['Data Flow', '数据流'],
  copyRecords: ['Copy Records', '复制记录'],
  deleteRecords: ['Delete Records', '删除记录'],
  recentToolCalls: ['Recent Tool Calls (auto-refresh every 3s)', '最近工具调用（每 3 秒自动刷新）'],
  copySelected: ['Copy Selected', '复制所选'],
  copyCurrentPage: ['Copy Current Page', '复制当前页'],
  deleteSelected: ['Delete Selected', '删除所选'],
  deleteAll: ['Delete All', '全部删除'],
  cancel: ['Cancel', '取消'],
  selectCurrentPage: ['Select current page', '选择当前页'],
  filterDecision: ['Decision', '判定'],
  filterAgent: ['Agent', '智能体'],
  filterRisk: ['Risk', '风险'],
  filterSession: ['Session', '会话'],
  sessionIdPlaceholder: ['Session ID', '会话 ID'],
  all: ['All', '全部'],
  apply: ['Apply', '应用'],
  reset: ['Reset', '重置'],
  resetColWidths: ['Reset Column Widths', '重置列宽'],
  resetColWidthsDone: ['Column widths reset to default', '已恢复默认列宽'],
  setColDefaults: ['Set as Default', '设为默认列宽'],
  setColDefaultsDone: ['Current column widths saved as default', '当前列宽已设为默认'],
  time: ['Time', '时间'],
  agent: ['Agent', '智能体'],
  session: ['Session', '会话'],
  tool: ['Tool', '工具'],
  command: ['Command', '命令'],
  decision: ['Decision', '判定'],
  risk: ['Risk', '风险'],
  latency: ['Latency', '耗时'],
  reason: ['Reason', '原因'],
  action: ['Action', '操作'],
  registeredAgents: ['Registered Agents', '已注册智能体'],
  agentType: ['Agent Type', '智能体类型'],
  hostname: ['Hostname', '主机名'],
  sessionsCol: ['Sessions', '会话数'],
  calls: ['Calls', '调用数'],
  timelineEmpty: ['No data in the last 60 minutes', '最近 60 分钟无数据'],
  sessionLogs: ['Session Logs (payload data flow)', '会话日志（载荷数据流）'],
  flowSession: ['Session', '会话'],
  flowTurns: ['Turns', '轮次'],
  flowLlm: ['LLM', 'LLM'],
  flowTools: ['Tools', '工具'],
  flowErrors: ['Errors', '错误'],
  flowProc: ['Proc', '进程'],
  flowTokens: ['Tokens', 'Token'],
  flowDuration: ['Duration', '耗时'],
  selectSession: ['Select a session to view its data flow', '选择一个会话查看其数据流'],
  noSessionSelected: ['No session selected', '未选择会话'],
  loadingVersions: ['Loading versions...', '正在加载版本...'],
  comboChain: ['Combined Attack Chain', '组合式攻击链'],
  callChain: ['Call Chain', '调用链'],
  close: ['Close', '关闭'],
  activeCount: ['{count} active', '{count} 个活跃'],
  riskLow: ['Low', '低危'],
  riskMedium: ['Medium', '中危'],
  riskHigh: ['High', '高危'],
  riskCritical: ['Critical', '严重'],
  noToolCalls: ['No tool calls yet', '暂无调用记录'],
  viewChain: ['View Attack Chain', '查看攻击链'],
  selectedCount: ['{count} selected', '已选 {count} 项'],
  noAgents: ['No agents registered yet', '暂无注册的智能体'],
  agentSessions: ['Sessions: {count}', '会话数：{count}'],
  noSessionLogs: ['No session logs yet — run agent-gate with collectors enabled', '暂无会话日志——启用采集器后运行 agent-gate'],
  view: ['View', '查看'],
  dataFlowTitle: ['Data Flow — {session}', '数据流 — {session}'],
  noFlowRecords: ['No flow records in this session', '该会话没有数据流记录'],
  pageInfo: ['Page {page} / {total} ({count} records)', '第 {page} / {total} 页（共 {count} 条）'],
  prev: ['Prev', '上一页'],
  next: ['Next', '下一页'],
  perPage: ['Per page', '每页'],
  jumpTo: ['Go to page', '跳转到页'],
  go: ['Go', '跳转'],
  pageInvalid: ['Invalid page number', '页码无效'],
  copyFailed: ['Copy failed: {err}', '复制失败：{err}'],
  noRecordsSelected: ['No records selected', '未选择记录'],
  noRecordsOnPage: ['No records on this page', '当前页没有记录'],
  copied: ['Copied {count} record(s)', '已复制 {count} 条'],
  copiedPage: ['Copied {count} record(s) on this page', '已复制当前页 {count} 条'],
  deleted: ['Deleted {count} record(s)', '已删除 {count} 条'],
  deleteFailed: ['Delete failed: {err}', '删除失败：{err}'],
  noRecordsToDelete: ['No records to delete', '没有可删除的记录'],
  confirmDeleteSelected: ['Delete {count} selected record(s)? This cannot be undone.', '确定删除所选 {count} 条记录吗？此操作不可恢复。'],
  confirmDeleteAll: ['Delete all {count} record(s)? This cannot be undone.', '确定删除全部 {count} 条记录吗？此操作不可恢复。'],
  storageExceeded: ['Local storage exceeds threshold ({thr}): currently {mb} MB, {count} records. Consider clearing history.', '本地存储已超过阈值（{thr}）：当前占用 {mb} MB，共 {count} 条历史记录。建议清理历史记录，释放空间。'],
  storageClearing: ['Clearing…', '清理中…'],
  storageCleared: ['Cleared {count} record(s)', '清理完成，已删除 {count} 条记录'],
  storageClearFailed: ['Clear failed: {err}', '清理失败：{err}'],
  storageConfirm: ['Clear all tool call history? This cannot be undone.', '确定要清空全部历史调用记录吗？此操作不可恢复。'],
  mossUnreachable: ['AgentMoss N/A (unreachable)', 'AgentMoss N/A（不可达）'],
  propertyExpression: ['Property Expression', '性质表达式'],
  nodeLabel: ['Node', '节点'],
  violationProperty: ['Violation', '违反性质'],
  riskLabel: ['Risk', '风险'],
  typeLabel: ['Type', '类型'],
  layersLabel: ['Triggered Layers', '触发层'],
  sessionLabel: ['Session', '会话'],
  graphLegendTime: ['Solid line = action sequence', '实线 = 动作时序'],
  graphLegendData: ['Dashed line = data flow (shared file/carrier)', '虚线 = 数据流（共享同一文件/载体）'],
  graphLegendLabel: ['Edge label = shared carrier path', '连线标签 = 共享载体路径'],
  graphLegendWitness: ['Red box = violation witness', '红框 = 违例见证节点'],
  graphLegendIndex: ['Number = property expression node', '序号 = 性质表达式对应节点'],
  chainCarrier: ['Carrier: ', '载体：'],
  chainResources: ['Resources: ', '资源：'],
  roleSource: ['Data Source', '敏感数据源'],
  roleCarrier: ['Carrier', '数据载体'],
  roleSink: ['Exfil Point', '外传点'],
  roleWitness: ['Witness', '违例见证'],
  roleLISTEN: ['Foothold', '开据点'],
  roleEXEC: ['Remote Exec', '拿遥控器'],
  roleSCAN: ['Recon', '探路'],
  roleCRED: ['Credentials', '偷钥匙'],
  roleEXFIL: ['Exfil', '往外搬'],
  rolePERSIST: ['Persist', '赖着不走'],
  rolePRIVESC: ['PrivEsc', '抢 root'],
  propSensitiveDataFlow: ['Sensitive Data Exfiltration', '敏感数据传播外传'],
  propCrossSession: ['Cross-session Attack Chain', '跨会话组合链'],
  propNoExfilAfterPrivesc: ['Exfiltration after Privilege Escalation', '提权后外传'],
  propNoExfilAfterCredential: ['Exfiltration after Credential Access', '凭据访问后外传'],
  propLowIntegrityCommandExec: ['Low-integrity Command Execution', '低完整性命令执行'],
  propSensitiveDataAggregation: ['Sensitive Data Aggregation', '敏感数据聚合'],
  propLowIntegrityReachability: ['Low-integrity Reachability', '低完整性可达'],
  kindUser: ['User', '用户'],
  kindLlm: ['LLM', 'LLM'],
  kindTool: ['Tool', '工具'],
  kindProcess: ['Process', '进程'],
  kindSyscall: ['Syscall', '系统调用'],
  kindSystem: ['System', '系统'],
  summaryTurns: ['turns', '轮次'],
  summaryLlm: ['llm', 'LLM'],
  summaryTools: ['tools', '工具'],
  summaryErrors: ['errors', '错误'],
  summaryProcs: ['procs', '进程'],
  summaryTokens: ['tokens', 'Token'],
};

function _savedLang() {
  try {
    const saved = localStorage.getItem('agentgate.lang');
    if (saved === 'zh' || saved === 'en') return saved;
  } catch (e) { /* localStorage unavailable */ }
  return ((navigator.language || '').toLowerCase().indexOf('zh') === 0) ? 'zh' : 'en';
}
let _lang = _savedLang();

function tr(key, vars) {
  const entry = I18N[key];
  let text = entry ? (_lang === 'zh' ? entry[1] : entry[0]) : key;
  if (vars) Object.keys(vars).forEach(k => { text = text.split('{' + k + '}').join(String(vars[k])); });
  return text;
}

/** 只替换文字节点，保留表头里的列宽拖拽把手等子元素。 */
function _setI18nText(el, text) {
  for (let i = el.childNodes.length - 1; i >= 0; i--) {
    if (el.childNodes[i].nodeType === 3) el.removeChild(el.childNodes[i]);
  }
  el.appendChild(document.createTextNode(text));
}

function riskLabel(level) {
  const map = { low: 'riskLow', medium: 'riskMedium', high: 'riskHigh', critical: 'riskCritical' };
  return map[level] ? tr(map[level]) : level;
}

function chainRole(key) {
  const map = {
    source: 'roleSource', carrier: 'roleCarrier', sink: 'roleSink', witness: 'roleWitness',
    LISTEN: 'roleLISTEN', EXEC: 'roleEXEC', SCAN: 'roleSCAN', CRED: 'roleCRED',
    EXFIL: 'roleEXFIL', PERSIST: 'rolePERSIST', PRIVESC: 'rolePRIVESC',
  };
  return map[key] ? tr(map[key]) : key;
}

function flowKindLabel(kind) {
  const map = {
    user: 'kindUser', llm: 'kindLlm', tool: 'kindTool',
    process: 'kindProcess', syscall: 'kindSyscall', system: 'kindSystem',
  };
  return map[kind] ? tr(map[kind]) : kind;
}

function chainPropertyName(g) {
  const id = (g && g.property_id) || '';
  if (id === 'sensitive-data-flow') return tr('propSensitiveDataFlow');
  if (id.indexOf('cross-') === 0) return tr('propCrossSession');
  if (id === 'no-exfil-after-privesc') return tr('propNoExfilAfterPrivesc');
  if (id === 'no-exfil-after-credential') return tr('propNoExfilAfterCredential');
  if (id === 'low-integrity-command-exec') return tr('propLowIntegrityCommandExec');
  if (id === 'sensitive-data-aggregation') return tr('propSensitiveDataAggregation');
  if (id === 'low-integrity-reachability') return tr('propLowIntegrityReachability');
  return (g && g.property_name) || id;
}

function _savedPageSize() {
  try {
    const n = parseInt(localStorage.getItem('agentgate.pageSize') || '', 10);
    if ([10, 20, 50, 100].indexOf(n) >= 0) return n;
  } catch (e) { /* localStorage unavailable */ }
  return 20;
}

let activeTab = 'recent';
let _currentPage = 1;
let _pageSize = _savedPageSize();
let _totalRecords = 0;
let _lastRecentKey = null;   // 上次渲染用的数据，用于跳过无谓重建
let _lastPageSig = null;

// ── 多选操作（复制 / 删除）────────────────────────────────────────────
let _selectMode = 'none';        // 'none' | 'copy' | 'delete'
let _selectedIds = new Set();    // 已勾选记录的 id（跨刷新保持）
let _copyFormat = 'tsv';         // 'tsv' | 'json'
let _copying = false;
let _deleting = false;
let _currentPageRows = [];       // 当前页原始记录，复制/删除时直接读它（不读 DOM）
let _forceRecentRender = false;  // 进出多选模式要强刷一次（列结构变了）
let _filters = { decision: '', agent_type: '', risk_level: '', session_id: '' };
let _sortBy = 'called_at';
let _sortDir = 'desc';
let _agentFilterSig = '';

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
    document.getElementById('tab-flow').style.display = activeTab==='flow' ? '' : 'none';
    refresh();
  });
});

// ── 筛选与排序 ─────────────────────────────────────────────────────────
function _readFiltersFromDom() {
  _filters.decision = document.getElementById('filter-decision').value;
  _filters.agent_type = document.getElementById('filter-agent').value;
  _filters.risk_level = document.getElementById('filter-risk').value;
  _filters.session_id = document.getElementById('filter-session').value.trim();
}
function _applyFilters() {
  _readFiltersFromDom();
  _currentPage = 1;
  _lastPageSig = null;
  _lastRecentKey = null;
  _forceRecentRender = true;
  refresh();
}
function _resetFilters() {
  document.getElementById('filter-decision').value = '';
  document.getElementById('filter-agent').value = '';
  document.getElementById('filter-risk').value = '';
  document.getElementById('filter-session').value = '';
  _applyFilters();
}
function _updateAgentFilter(agentTypes) {
  const select = document.getElementById('filter-agent');
  if (!select) return;
  const current = _filters.agent_type || select.value || '';
  const types = Object.keys(agentTypes || {}).sort();
  const sig = _lang + '|' + types.join('\u0001') + '|' + current;
  if (sig === _agentFilterSig) return;
  _agentFilterSig = sig;
  select.innerHTML = '';
  const all = document.createElement('option');
  all.value = '';
  all.textContent = tr('all');
  all.dataset.i18n = 'all';
  select.appendChild(all);
  types.forEach(type => {
    const opt = document.createElement('option');
    opt.value = type;
    opt.textContent = type;
    select.appendChild(opt);
  });
  if (current && types.indexOf(current) < 0) {
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = current;
    select.appendChild(opt);
  }
  select.value = current;
}
function _updateSortIndicators() {
  document.querySelectorAll('#recent-calls th.sortable').forEach(th => {
    if (th.dataset.sort === _sortBy) th.dataset.dir = _sortDir;
    else delete th.dataset.dir;
  });
}

/** 只给“真的被裁掉”的单元格加原生 title，悬停即可看完整内容。 */
function _updateCellTitles() {
  const table = document.getElementById('recent-calls');
  if (!table) return;
  table.querySelectorAll('tbody td').forEach(td => {
    if (td.classList.contains('col-check')) return;
    const text = String(td.innerText || td.textContent || '').trim();
    if (text && td.scrollWidth > td.clientWidth + 1) td.title = text;
    else td.removeAttribute('title');
  });
}
document.querySelectorAll('#recent-calls th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    if (Date.now() < _resizeClickGuard) return;  // 拖列宽结束后的 click 不触发排序
    const key = th.dataset.sort;
    if (!key) return;
    if (_sortBy === key) _sortDir = _sortDir === 'asc' ? 'desc' : 'asc';
    else { _sortBy = key; _sortDir = 'asc'; }
    _currentPage = 1;
    _lastPageSig = null;
    _lastRecentKey = null;
    _forceRecentRender = true;
    _updateSortIndicators();
    refresh();
  });
});
document.getElementById('filter-decision').addEventListener('change', _applyFilters);
document.getElementById('filter-agent').addEventListener('change', _applyFilters);
document.getElementById('filter-risk').addEventListener('change', _applyFilters);
document.getElementById('filter-session').addEventListener('keydown', e => { if (e.key === 'Enter') _applyFilters(); });
document.getElementById('filter-apply').addEventListener('click', _applyFilters);
document.getElementById('filter-reset').addEventListener('click', _resetFilters);

// ── 列宽拖动（与 OpenDesk 看板同类的交互）──────────────────────────────
const COL_WIDTH_KEY = 'agentgate-calls-colwidths-v1';
const COL_DEFAULT_KEY = 'agentgate-calls-colwidths-default-v1';
let _defaultColWidths = [];
let _colThs = [];
let _resizeClickGuard = 0;

function _startColumnResize(th, startX) {
  const table = document.getElementById('recent-calls');
  if (!table) return;
  const startW = parseInt(th.style.width, 10) || th.offsetWidth;
  _resizeClickGuard = Date.now() + 600;
  const onMove = (ev) => {
    const w = Math.max(60, startW + (ev.clientX - startX));
    th.style.width = w + 'px';
    th.style.minWidth = w + 'px';
    th.style.maxWidth = w + 'px';
    const total = _colThs.reduce((sum, cell, idx) => idx === 0 ? sum : sum + (parseInt(cell.style.width, 10) || 0), 0);
    table.style.minWidth = total + 'px';
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    _resizeClickGuard = Date.now() + 300;
    _saveColumnWidths();
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function _applyColumnWidths(widths) {
  if (!_colThs.length) return;
  let total = 0;
  _colThs.forEach((th, i) => {
    if (i === 0) return;  // 勾选框列固定，不参与拖拽
    const w = Math.max(0, parseInt(widths[i], 10) || 0);
    th.style.width = w ? w + 'px' : '';
    th.style.minWidth = w ? w + 'px' : '';
    th.style.maxWidth = w ? w + 'px' : '';
    total += w;
  });
  const table = document.getElementById('recent-calls');
  if (table) {
    table.classList.add('col-resized');
    table.style.tableLayout = 'fixed';
    table.style.minWidth = total > 0 ? total + 'px' : '';
    _updateCellTitles();
  }
}

function _saveColumnWidths() {
  try {
    localStorage.setItem(COL_WIDTH_KEY, JSON.stringify(_colThs.map(th => parseInt(th.style.width, 10) || 0)));
  } catch (e) { /* localStorage unavailable */ }
}

function _initColumnResize() {
  const table = document.getElementById('recent-calls');
  if (!table || table.dataset.colResizeInit) return;
  const ths = Array.from(table.querySelectorAll('thead th'));
  if (!ths.length) return;
  table.dataset.colResizeInit = '1';
  _colThs = ths;
  let saved = {};
  let savedDefaults = {};
  try { saved = JSON.parse(localStorage.getItem(COL_WIDTH_KEY) || '{}') || {}; } catch (e) { saved = {}; }
  try { savedDefaults = JSON.parse(localStorage.getItem(COL_DEFAULT_KEY) || '{}') || {}; } catch (e) { savedDefaults = {}; }
  // 没有显式设过默认时，把用户当前已拖好的宽度直接视为默认（下次重置回到这里）。
  if (!Object.keys(savedDefaults).length && Object.keys(saved).length) {
    savedDefaults = saved;
    try { localStorage.setItem(COL_DEFAULT_KEY, JSON.stringify(savedDefaults)); } catch (e) { /* ignore */ }
  }
  _defaultColWidths = ths.map((th, i) => Math.max(60, parseInt(savedDefaults[String(i)], 10) || th.offsetWidth));
  ths.forEach((th, i) => {
    if (i === 0) return;  // 勾选框列不提供拖拽把手
    const resizer = document.createElement('span');
    resizer.className = 'col-resizer';
    th.appendChild(resizer);
    resizer.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      _startColumnResize(th, e.clientX);
    });
    resizer.addEventListener('click', (e) => e.stopPropagation());
    // 兜底：点在表头右边缘 10px 内拖当前列；点在左边缘 10px 内拖前一列。
    // 这样无论鼠标落在相邻两列的哪一侧，拖的都是用户看到的那条边框。
    th.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      const rect = th.getBoundingClientRect();
      let target = null;
      if (rect.right - e.clientX <= 10) target = th;
      else if (e.clientX - rect.left <= 10) {
        const prev = th.previousElementSibling;
        if (prev && !prev.classList.contains('col-check')) target = prev;
      }
      if (!target) return;
      e.preventDefault();
      e.stopPropagation();
      _startColumnResize(target, e.clientX);
    });
  });
  _applyColumnWidths(ths.map((_, i) => saved[String(i)] || _defaultColWidths[i]));
}

document.getElementById('set-col-defaults').addEventListener('click', () => {
  const widths = _colThs.map(th => parseInt(th.style.width, 10) || th.offsetWidth);
  _defaultColWidths = widths;
  try { localStorage.setItem(COL_DEFAULT_KEY, JSON.stringify(widths)); } catch (e) { /* ignore */ }
  toast(tr('setColDefaultsDone'), false);
});

document.getElementById('reset-col-widths').addEventListener('click', () => {
  try { localStorage.removeItem(COL_WIDTH_KEY); } catch (e) { /* ignore */ }
  if (_defaultColWidths.length) _applyColumnWidths(_defaultColWidths);
  toast(tr('resetColWidthsDone'), false);
});

function badge(d) {
  const map = {Allow:'badge-allow', Deny:'badge-deny', low:'badge-low', medium:'badge-medium', high:'badge-high', critical:'badge-critical'};
  let label = d;
  if (d === 'Allow') label = tr('allow');
  else if (d === 'Deny') label = tr('deny');
  else if (map[d]) label = riskLabel(d);
  // d 来自 AgentMoss 的判定字段；虽由服务端产生，仍按不可信内容转义（纵深防御）
  return `<span class="badge ${map[d]||''}">${escapeHtml(label)}</span>`;
}

// ==================== 存储超限横幅 + 清理（与 TS 版同构）====================
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
  _setText('storage-banner-status', busy ? tr('storageClearing') : '');
}
async function loadStorageStats(){
  try {
    const r = await fetch(BASE+'/api/v1/storage_stats');
    const st = await r.json();
    if (!st || typeof st !== 'object' || !st.oversize) { _showStorageBanner(false); return; }
    const s = st.stats || {};
    const cnt = typeof s.toolCalls === 'number' ? s.toolCalls : '?';
    const thr = typeof st.thresholdMB === 'number' ? st.thresholdMB + ' MB' : '?';
    _showStorageBanner(true, tr('storageExceeded', { thr: thr, mb: _getTotalMB(s), count: cnt }));
  } catch (e) { _showStorageBanner(false); console.error('[AgentGate] storage stats error:', e); }
}
(function bindStorageClear(){
  const btn = document.getElementById('storage-clear-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!window.confirm(tr('storageConfirm'))) return;
    _setStorageBusy(true);
    try {
      const r = await fetch(BASE+'/api/v1/tool_calls', { method: 'DELETE' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      _setText('storage-banner-status', tr('storageCleared', { count: (d && d.deleted != null ? d.deleted : 0) }));
      _showStorageBanner(false);
      refresh();
    } catch (e) {
      _setText('storage-banner-status', tr('storageClearFailed', { err: String(e.message || e) }));
    } finally { _setStorageBusy(false); }
  });
})();

// ==================== 页脚版本（与 TS 版同构：从 health 动态读取）====================
async function loadFooter(){
  try {
    const h = await fetch(BASE+'/api/v1/health').then(r=>r.json());
    const gateV = h.version || 'N/A';
    const mossV = h.moss_version || 'N/A';
    const mossS = h.moss_status || 'unknown';
    let mossLabel = mossV === 'N/A' ? tr('mossUnreachable') : 'AgentMoss v' + mossV;
    if (mossS === 'healthy') mossLabel += ' ✓';
    _setText('footer', 'AgentGate v' + gateV + ' + ' + mossLabel + ' (Python)');
  } catch (e) {
    _setText('footer', 'AgentGate N/A + ' + tr('mossUnreachable') + ' (Python)');
  }
}
loadFooter();

async function refresh() {
  try {
    const offset = (_currentPage - 1) * _pageSize;
    const params = new URLSearchParams();
    params.set('limit', String(_pageSize));
    params.set('offset', String(offset));
    if (_filters.decision) params.set('decision', _filters.decision);
    if (_filters.agent_type) params.set('agent_type', _filters.agent_type);
    if (_filters.risk_level) params.set('risk_level', _filters.risk_level);
    if (_filters.session_id) params.set('session_id', _filters.session_id);
    params.set('sort_by', _sortBy);
    params.set('sort_dir', _sortDir);
    const [stats, recentResp] = await Promise.all([
      fetch(BASE+'/dashboard/stats').then(r=>r.json()),
      fetch(BASE+'/dashboard/recent?'+params.toString()).then(r=>r.json()),
    ]);
    updateStats(stats);
    _updateSortIndicators();
    // Handle {total, data} pagination format
    const recentData = Array.isArray(recentResp) ? recentResp : (recentResp.data || []);
    _totalRecords = recentResp.total || recentData.length;
    updateRecent(recentData, _forceRecentRender);
    _forceRecentRender = false;
    updatePagination();
    if (activeTab === 'agents') updateAgents(stats);
    if (activeTab === 'timeline') updateTimeline();
    if (activeTab === 'flow') updateFlow();
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
  _updateAgentFilter(s.agent_types || {});
  _setText('stat-agents', s.total_agents);
  _setText('stat-sessions', s.total_sessions);
  _setText('stat-active', tr('activeCount', { count: s.active_sessions }));
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
    legend += `<span>${escapeHtml(riskLabel(level))}: ${count}</span>`;
  }
  document.getElementById('risk-bar').innerHTML = bar;
  document.getElementById('risk-legend').innerHTML = legend;
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
  if (isNaN(d.getTime())) return (ts||'').substring(11,19);
  return d.toLocaleTimeString(_lang === 'zh' ? 'zh-CN' : 'en-US', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
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

  const cols = _selectMode !== 'none' ? 11 : 10;
  if (!data.length) {
    tb.innerHTML = `<tr><td colspan="${cols}" class="empty">${escapeHtml(tr('noToolCalls'))}</td></tr>`;
    updateCopyBar();
    return;
  }
  tb.innerHTML = data.map((r, idx) => `
    <tr>
      ${_selectMode !== 'none' ? `<td class="col-check"><input type="checkbox" class="row-check" data-id="${r.id}"${_selectedIds.has(Number(r.id)) ? ' checked' : ''}></td>` : ''}
      <td style="white-space:nowrap;font-size:12px;color:var(--muted)">${fmtTime(r.called_at)}</td>
      <td>${escapeHtml(r.agent_type||'-')}</td>
      <td class="session-cell" title="${escapeHtml(r.session_id||'')}">${escapeHtml(r.session_id||'-')}</td>
      <td>${escapeHtml(r.tool_name)}</td>
      <td class="command-cell" title="${escapeHtml(r.command||'')}">${escapeHtml(r.command||'')}</td>
      <td>${badge(r.decision)}</td>
      <td>${r.risk_level ? badge(r.risk_level) : '-'}</td>
      <td style="font-size:12px;color:var(--muted)">${r.analysis_duration_ms||0}ms</td>
      <td class="reason-cell" title="${escapeHtml(r.reason||'')}">${escapeHtml(r.reason||'-')}</td>
      <td>${parseChainGraphFromRow(r) ? `<button class="ag-chain-btn" data-chain="${idx}">${escapeHtml(tr('viewChain'))}</button>` : ''}</td>
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
  _updateCellTitles();
}

// ==================== 组合式攻击链图（SVG） ====================
// 数据来源：tool_calls.moss_response（完整 AgentMoss 响应）→ violation_graph

/** 宽松解析 JSON 字段：兼容对象、JSON 字符串和空值。 */
function parseJsonValue(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') {
    try { return JSON.parse(value); } catch (e) { return null; }
  }
  return (typeof value === 'object') ? value : null;
}

/** 取 AgentMoss 原始响应（Python 存 moss_response；TS 也存）。 */
function parseMossResponse(r) {
  return parseJsonValue(r && r.moss_response) || {};
}

/** 取攻击链图：优先独立字段（TS），回退到 moss_response（Python）。 */
function parseChainGraphFromRow(r) {
  var g = parseJsonValue(r && r.violation_graph);
  if (!g || !Array.isArray(g.nodes) || g.nodes.length === 0) {
    var resp = parseMossResponse(r);
    g = parseJsonValue(resp && resp.violation_graph);
  }
  if (!g || !Array.isArray(g.nodes) || g.nodes.length === 0) return null;
  return g;
}

/** 复制/导出用的攻击链结构（不再依赖 moss_response 的其它大字段）。 */
function attackChainFromRow(r) {
  var g = parseChainGraphFromRow(r);
  if (!g) return null;
  var resp = parseMossResponse(r);
  return {
    property_id: g.property_id || null,
    property_short: g.property_short || null,
    property_name: g.property_name || null,
    nodes: g.nodes || [],
    edges: g.edges || [],
    property_expression: g.property_expression || [],
    violation_chain: Array.isArray(resp.violation_chain) ? resp.violation_chain : [],
  };
}

/** 攻击链压成一行，供 TSV/Excel 阅读。 */
function formatAttackChain(chain) {
  if (!chain) return '-';
  var pname = chainPropertyName(chain);
  var title = [chain.property_short, pname].filter(Boolean).join(' ');
  if (chain.property_id) title += (title ? ' · ' : '') + chain.property_id;
  var steps = (chain.nodes || []).map(function(n, i) {
    var roleKey = n.role || n.stage || '';
    var role = chainRole(roleKey);
    var cmd = String(n.action_detail || '').replace(/[\t\r\n]+/g, ' ');
    var res = (n.resources && n.resources.length) ? ' [' + tr('chainResources') + n.resources.join(', ') + ']' : '';
    return (i + 1) + (role ? '[' + role + ']' : '') + ' ' + cmd + res;
  });
  return (title ? title + ': ' : '') + (steps.join(' -> ') || '-');
}

/** violated_layers 在库里是 JSON 字符串；导出 JSON 时还原成数组。 */
function parseLayers(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') {
    try { var parsed = JSON.parse(value); return Array.isArray(parsed) ? parsed : []; } catch (e) { return []; }
  }
  return [];
}

/** violated_layers 在库里可能是 JSON 字符串，TSV 里转成可读文本。 */
function formatViolatedLayers(value) {
  var layers = parseLayers(value);
  return layers.length ? layers.join(', ') : '-';
}

function renderChainSvg(g) {
  var nodes = g.nodes || [], edges = g.edges || [];
  var NW = 220, NH = 78, GX = 48, GY = 32, PER_ROW = 4;
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
    var labelText = rawLabel ? (tr('chainCarrier') + rawLabel) : '';
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
    var roleText = chainRole(roleKey);
    var fullCmd = String(n.action_detail || '');
    var fullTitle = String((n.session_id || '-') + ' #' + n.seq);
    var tip = fullTitle + (roleText ? ' [' + roleText + ']' : '') + '\n' + fullCmd;
    if (n.resources && n.resources.length) tip += '\n' + tr('chainResources') + n.resources.join(', ');
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
      parts.push('<text x="' + (p.x + NW - 8) + '" y="' + (p.y + NH - 6) + '" text-anchor="end" font-size="8" font-weight="700" fill="#FF3B30">' + escapeHtml(tr('roleWitness')) + '</text>');
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
    var label = chainRole(t.token);
    return '<span class="ag-expr-token"><b>' + escapeHtml(t.token) + '</b><small>' + (label ? escapeHtml(label) + ' · ' : '') + escapeHtml(tr('nodeLabel')) + ' ' + nums + '</small></span>';
  });
  return '<span class="ag-expr-label">' + escapeHtml(tr('propertyExpression')) + '</span>' + parts.join('<span class="ag-expr-arrow">→</span>');
}

let _chainRow = null;
function openChainModal(row) {
  var g = parseChainGraphFromRow(row);
  if (!g) return;
  _chainRow = row;
  document.getElementById('chain-modal-title').textContent = row.decision === 'Deny' ? tr('comboChain') : tr('callChain');
  var meta = [];
  if (row.decision) meta.push('<span style="font-weight:700;color:' + (row.decision === 'Deny' ? 'var(--red)' : 'var(--green)') + '">' + escapeHtml(row.decision === 'Deny' ? tr('deny') : tr('allow')) + '</span>');
  if (g.property_short || g.property_id) {
    var pname = chainPropertyName(g);
    meta.push('<span class="ag-modal-property">' + escapeHtml(tr('violationProperty')) + '：' + escapeHtml(g.property_short || '') + ' · ' + escapeHtml(g.property_id || '') + (pname ? '（' + escapeHtml(pname) + '）' : '') + '</span>');
  }
  if (row.risk_level) meta.push('<span>' + escapeHtml(tr('riskLabel')) + '：' + escapeHtml(riskLabel(row.risk_level)) + '</span>');
  if (row.risk_type) meta.push('<span>' + escapeHtml(tr('typeLabel')) + '：' + escapeHtml(row.risk_type) + '</span>');
  var layers = row.violated_layers;
  if (typeof layers === 'string') { try { layers = JSON.parse(layers); } catch (e) { layers = []; } }
  if (Array.isArray(layers) && layers.length) meta.push('<span>' + escapeHtml(tr('layersLabel')) + '：' + escapeHtml(layers.join(', ')) + '</span>');
  meta.push('<span>' + escapeHtml(tr('sessionLabel')) + '：' + escapeHtml(row.session_id || '-') + '</span>');
  document.getElementById('chain-modal-meta').innerHTML = meta.join('');
  document.getElementById('chain-modal-expr').innerHTML = renderPropertyExpr(g);
  document.getElementById('chain-modal-graph').innerHTML = renderChainSvg(g)
    + '<div class="ag-modal-legend"><span>' + escapeHtml(tr('graphLegendTime')) + '</span><span>' + escapeHtml(tr('graphLegendData')) + '</span><span>' + escapeHtml(tr('graphLegendLabel')) + '</span><span style="color:var(--red)">' + escapeHtml(tr('graphLegendWitness')) + '</span><span>' + escapeHtml(tr('graphLegendIndex')) + '</span></div>';
  document.getElementById('chain-modal-reason').textContent = row.reason || '';
  document.getElementById('chain-modal').style.display = 'flex';
}

function closeChainModal() {
  var m = document.getElementById('chain-modal');
  if (m) m.style.display = 'none';
  _chainRow = null;
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
  const sig = _currentPage + '/' + totalPages + '/' + _totalRecords + '/' + _pageSize + '/' + _lang;
  if (sig === _lastPageSig) return;          // 翻页信息没变，别重建
  if (_userIsSelecting()) return;
  _lastPageSig = sig;
  el.innerHTML = '';

  const nav = document.createElement('span');
  nav.className = 'page-nav';
  const btnPrev = document.createElement('button');
  btnPrev.textContent = tr('prev');
  btnPrev.disabled = _currentPage <= 1;
  btnPrev.onclick = () => { _currentPage--; refresh(); };
  const label = document.createElement('span');
  label.textContent = tr('pageInfo', { page: _currentPage, total: totalPages, count: _totalRecords });
  const btnNext = document.createElement('button');
  btnNext.textContent = tr('next');
  btnNext.disabled = _currentPage >= totalPages;
  btnNext.onclick = () => { _currentPage++; refresh(); };
  nav.appendChild(btnPrev); nav.appendChild(label); nav.appendChild(btnNext);

  const sizeGroup = document.createElement('span');
  sizeGroup.className = 'page-size-group';
  const sizeLabel = document.createElement('span');
  sizeLabel.textContent = tr('perPage');
  const sizeSelect = document.createElement('select');
  sizeSelect.id = 'page-size-select';
  [10, 20, 50, 100].forEach(size => {
    const opt = document.createElement('option');
    opt.value = String(size);
    opt.textContent = String(size);
    if (size === _pageSize) opt.selected = true;
    sizeSelect.appendChild(opt);
  });
  sizeSelect.addEventListener('change', () => {
    const next = parseInt(sizeSelect.value, 10);
    if ([10, 20, 50, 100].indexOf(next) < 0) return;
    _pageSize = next;
    _currentPage = 1;
    _lastPageSig = null;
    try { localStorage.setItem('agentgate.pageSize', String(next)); } catch (e) { /* ignore */ }
    refresh();
  });
  sizeGroup.appendChild(sizeLabel); sizeGroup.appendChild(sizeSelect);

  const jumpGroup = document.createElement('span');
  jumpGroup.className = 'page-jump-group';
  const jumpLabel = document.createElement('span');
  jumpLabel.textContent = tr('jumpTo');
  const jumpInput = document.createElement('input');
  jumpInput.id = 'page-jump-input';
  jumpInput.type = 'number';
  jumpInput.min = '1';
  jumpInput.max = String(totalPages);
  jumpInput.value = String(_currentPage);
  jumpInput.setAttribute('aria-label', tr('jumpTo'));
  const goBtn = document.createElement('button');
  goBtn.textContent = tr('go');
  const doJump = () => {
    const target = parseInt(jumpInput.value, 10);
    if (!target || target < 1 || target > totalPages) {
      toast(tr('pageInvalid'), true);
      jumpInput.value = String(_currentPage);
      return;
    }
    _currentPage = target;
    _lastPageSig = null;
    refresh();
  };
  goBtn.addEventListener('click', doJump);
  jumpInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doJump(); });
  jumpGroup.appendChild(jumpLabel); jumpGroup.appendChild(jumpInput); jumpGroup.appendChild(goBtn);

  el.appendChild(nav);
  el.appendChild(sizeGroup);
  el.appendChild(jumpGroup);
}

function updateAgents(s) {
  const types = s.agent_types || {};
  const el = document.getElementById('agent-list');
  if (!Object.keys(types).length) {
    el.innerHTML = '<div class="empty">' + escapeHtml(tr('noAgents')) + '</div>';
    return;
  }
  el.innerHTML = Object.entries(types).map(([type, count]) => `
    <div class="agent-card">
      <div class="name">${escapeHtml(type)}</div>
      <div class="meta">${escapeHtml(tr('agentType'))}</div>
      <div class="stats">
        <div class="stat">${escapeHtml(tr('agentSessions', { count: count }))}</div>
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
  const tickStep = Math.max(1, Math.ceil(maxVal / 4));
  const scaleMax = Math.ceil(maxVal / tickStep) * tickStep;
  const left = 42, top = 28, bottom = H - 28;
  const chartW = Math.max(1, W - left - 14);
  const chartH = Math.max(1, bottom - top);
  const slotW = chartW / data.length;
  const barW = Math.max(0.5, slotW - Math.min(4, slotW / 5));

  // 显示计数和分钟刻度，让图中的柱高及时间可以直接读出。
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let value = 0; value <= scaleMax; value += tickStep) {
    const y = bottom - value / scaleMax * chartH;
    ctx.fillStyle = '#8E8E93';
    ctx.fillText(String(value), left - 6, y);
    ctx.strokeStyle = '#E5E5EA';
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + chartW, y); ctx.stroke();
  }
  const labelEvery = Math.max(1, Math.ceil(data.length / Math.max(1, Math.floor(chartW / 70))));
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  data.forEach((d, i) => {
    const x = left + i * slotW + (slotW - barW) / 2;
    // Allow bar
    const allowH = ((d.Allow||0) / scaleMax) * chartH;
    ctx.fillStyle = '#34C759';
    ctx.fillRect(x, bottom - allowH, barW/2, allowH);
    // Deny bar
    const denyH = ((d.Deny||0) / scaleMax) * chartH;
    ctx.fillStyle = '#FF3B30';
    ctx.fillRect(x + barW/2, bottom - denyH, barW/2, denyH);
    if (i % labelEvery === 0) {
      ctx.fillStyle = '#8E8E93';
      ctx.fillText(fmtTime(d.minute).slice(0, 5), x + barW/2, bottom + 6);
    }
  });

  // Legend
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#34C759'; ctx.fillRect(W-120, 8, 10, 10);
  ctx.fillStyle = '#8E8E93'; ctx.font='11px sans-serif'; ctx.textAlign='left';
  ctx.fillText(tr('allow'), W-106, 18);
  ctx.fillStyle = '#FF3B30'; ctx.fillRect(W-60, 8, 10, 10);
  ctx.fillText(tr('deny'), W-46, 18);
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

// ── Data Flow (sessionlog payload flow) ────────────────────────────────
let _flowSelected = null;

async function updateFlow() {
  try {
    const resp = await fetch(BASE+'/api/v1/sessionlog').then(r=>r.json());
    const logs = (resp && resp.logs) || [];
    const tb = document.getElementById('flow-sessions');
    if (!logs.length) {
      tb.innerHTML = '<tr><td colspan="9" class="empty">' + escapeHtml(tr('noSessionLogs')) + '</td></tr>';
      document.getElementById('flow-records').innerHTML = '<div class="empty">' + escapeHtml(tr('noSessionSelected')) + '</div>';
      return;
    }
    tb.innerHTML = logs.map(l => {
      const s = l.summary || {};
      return `<tr>
        <td style="font-size:12px;color:var(--muted)">${escapeHtml(l.session_id)}</td>
        <td>${s.turns ?? '-'}</td>
        <td>${s.llm_calls ?? '-'}</td>
        <td>${s.tool_calls ?? '-'}</td>
        <td>${(s.tool_errors||0) > 0 ? `<span class="f-err">${s.tool_errors}</span>` : (s.tool_errors ?? '-')}</td>
        <td>${s.process_execs ?? '-'}</td>
        <td style="font-size:12px">${(s.input_tokens||0)+(s.output_tokens||0)}</td>
        <td style="font-size:12px;color:var(--muted)">${s.duration_ms!=null ? s.duration_ms+'ms' : '-'}</td>
        <td><button class="flow-btn" data-session="${escapeHtml(l.session_id)}">${escapeHtml(tr('view'))}</button></td>
      </tr>`;
    }).join('');
    // 用事件委托而不是内联事件处理器：后者要把不可信字符串塞进 JS 字面量，
    // HTML 转义（&#39;）会被浏览器在解析属性时还原成引号，仍能闭合字符串执行
    // 任意 JS。data-* + addEventListener 让数据始终是数据，不进入代码上下文。
    tb.querySelectorAll('button.flow-btn[data-session]').forEach(btn => {
      btn.addEventListener('click', () => showFlow(btn.dataset.session));
    });
  } catch(e) { console.error(e); }
}

async function showFlow(sessionId) {
  _flowSelected = sessionId;
  try {
    const data = await fetch(BASE+'/api/v1/sessionlog/'+encodeURIComponent(sessionId)).then(r=>r.json());
    document.getElementById('flow-title').textContent = tr('dataFlowTitle', { session: sessionId });
    const recs = data.records || [];
    const s = data.summary || {};
    const wrap = document.getElementById('flow-records');
    if (!recs.length) { wrap.innerHTML = '<div class="empty">' + escapeHtml(tr('noFlowRecords')) + '</div>'; return; }
    wrap.innerHTML = recs.map(r => {
      const kind = r.kind || 'system';
      let body = '', meta = '';
      if (kind === 'user') { body = escapeHtml(String(r.content||'')); }
      else if (kind === 'llm') {
        const u = r.usage || {};
        meta = `model=${escapeHtml(String(r.model||'-'))} · in=${u.input_tokens||u.prompt_tokens||0} out=${u.output_tokens||u.completion_tokens||0}${r.duration_ms!=null?' · '+r.duration_ms+'ms':''}`;
        body = escapeHtml(String(r.content||'')).substring(0,200);
      } else if (kind === 'tool') {
        const ok = !r.error || Object.keys(r.error||{}).length===0;
        meta = `${escapeHtml(String(r.call_id||''))}${r.duration_ms!=null?' · '+r.duration_ms+'ms':''} · src=${escapeHtml(String(r.source||'llm'))}`;
        body = `<span class="${ok?'f-ok':'f-err'}">${escapeHtml(String(r.name||''))}</span> ${escapeHtml(String(r.arguments||'')).substring(0,120)}`;
        if (r.result!==null && r.result!==undefined) body += ` <span class="f-arrow">→</span> <span class="${ok?'f-ok':'f-err'}">${escapeHtml(String(r.result)).substring(0,160)}</span>`;
      } else if (kind === 'process') {
        meta = `pid=${r.pid??'-'}${r.exit_code!=null?' · exit='+r.exit_code:''}${r.duration_ms!=null?' · '+r.duration_ms+'ms':''}`;
        body = escapeHtml(String(r.command||'')).substring(0,160);
      } else if (kind === 'syscall') {
        meta = `pid=${r.pid??'-'}`;
        body = escapeHtml(String(r.syscall||''));
      } else {
        body = escapeHtml(String(r.detail||r.content||''));
      }
      return `<div class="flow-row k-${escapeHtml(kind)}"><div class="f-kind">${escapeHtml(flowKindLabel(kind))}</div><div class="f-body">${body}${meta?'<div class="f-meta">'+meta+'</div>':''}</div></div>`;
    }).join('');
    // Summary strip
    const strip = document.createElement('div');
    strip.className = 'f-meta';
    strip.style.cssText = 'padding:0 16px 8px;';
    strip.textContent = `${tr('summaryTurns')}=${s.turns??0} · ${tr('summaryLlm')}=${s.llm_calls??0} · ${tr('summaryTools')}=${s.tool_calls??0} · ${tr('summaryErrors')}=${s.tool_errors??0} · ${tr('summaryProcs')}=${s.process_execs??0} · ${tr('summaryTokens')}=${(s.input_tokens||0)+(s.output_tokens||0)} · ${s.duration_ms!=null?s.duration_ms+'ms':''}`;
    wrap.prepend(strip);
  } catch(e) { console.error(e); }
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
    return JSON.stringify(rows.map(r => {
      const resp = parseMossResponse(r);
      return {
        id: r.id ?? null,
        called_at: fmtTimeFull(r.called_at),
        agent_type: r.agent_type ?? null,
        session_id: r.session_id ?? null,
        tool_name: r.tool_name ?? null,
        command: r.command ?? null,
        description: r.description ?? null,
        cwd: r.cwd ?? null,
        action_type: r.action_type ?? null,
        action_detail: r.action_detail ?? null,
        decision: r.decision ?? null,
        risk_level: r.risk_level ?? null,
        risk_type: r.risk_type ?? null,
        violated_layers: parseLayers(r.violated_layers),
        confidence: r.confidence ?? null,
        analysis_duration_ms: r.analysis_duration_ms ?? null,
        reason: r.reason ?? null,
        session_context: {
          user_prompt: r.user_prompt ?? null,
          os_type: r.os_type ?? null,
          cwd: r.session_cwd ?? null,
        },
        attack_chain: attackChainFromRow(r),
        degraded: Array.isArray(resp.degraded) ? resp.degraded : [],
        l3_outcome: resp.l3_outcome ?? '',
      };
    }), null, 2);
  }
  const header = [
    'Time','Agent','Session','Tool','Command','Decision','Risk','Latency','Reason',
    'Risk Type','Violated Layers','Action Detail','User Prompt','CWD','Session CWD',
    'OS','Degraded','L3 Outcome','Attack Chain','Attack Chain JSON',
  ].join('\t');
  const lines = rows.map(r => {
    const resp = parseMossResponse(r);
    const chain = attackChainFromRow(r);
    return [
      fmtTimeFull(r.called_at),
      r.agent_type || '-',
      r.session_id || '-',
      r.tool_name || '-',
      r.command || '-',
      r.decision || '-',
      r.risk_level || '-',
      Math.round(r.analysis_duration_ms || 0) + 'ms',
      r.reason || '-',
      r.risk_type || '-',
      formatViolatedLayers(r.violated_layers),
      r.action_detail || '-',
      r.user_prompt || '-',
      r.cwd || '-',
      r.session_cwd || '-',
      r.os_type || '-',
      (Array.isArray(resp.degraded) && resp.degraded.length) ? resp.degraded.join(', ') : '-',
      resp.l3_outcome || '-',
      formatAttackChain(chain),
      chain ? JSON.stringify(chain) : '-',
    ].map(cell).join('\t');
  });
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
    toast(tr('copyFailed', { err: e }), true);
  } finally {
    _copying = false; updateCopyBar();
  }
}

function setCopyFormat(fmt) {
  _copyFormat = fmt;
  document.getElementById('fmt-tsv').classList.toggle('active', fmt === 'tsv');
  document.getElementById('fmt-json').classList.toggle('active', fmt === 'json');
}

function _selecting() { return _selectMode !== 'none'; }

function _updateSelectAll() {
  const cb = document.getElementById('select-all');
  if (!cb) return;
  const ids = _currentPageRows.map(r => Number(r.id));
  const picked = ids.filter(id => _selectedIds.has(id)).length;
  cb.disabled = !_selecting() || ids.length === 0;
  cb.checked = ids.length > 0 && picked === ids.length;
  cb.indeterminate = picked > 0 && picked < ids.length;
}

function setSelectMode(mode) {
  _selectMode = mode;
  if (!_selecting()) _selectedIds.clear();
  const bar = document.getElementById('copy-bar');
  bar.classList.toggle('on', _selecting());
  bar.classList.toggle('mode-copy', mode === 'copy');
  bar.classList.toggle('mode-delete', mode === 'delete');
  document.getElementById('enter-copy').style.display = _selecting() ? 'none' : '';
  document.getElementById('enter-delete').style.display = _selecting() ? 'none' : '';
  document.getElementById('recent-calls').classList.toggle('select-mode', _selecting());
  _forceRecentRender = true;   // 列结构变了，必须重建表格
  refresh();
}

function updateCopyBar() {
  const bar = document.getElementById('copy-bar');
  if (!bar) return;
  const n = _selectedIds.size;
  document.getElementById('copy-count').textContent = tr('selectedCount', { count: n });
  document.getElementById('copy-selected').disabled = _copying || n === 0;
  document.getElementById('copy-page').disabled = _copying || _currentPageRows.length === 0;
  document.getElementById('fmt-tsv').disabled = _copying;
  document.getElementById('fmt-json').disabled = _copying;
  document.getElementById('delete-selected').disabled = _deleting || n === 0;
  document.getElementById('delete-all').disabled = _deleting || _totalRecords === 0;
  _updateSelectAll();
}

document.getElementById('enter-copy').addEventListener('click', () => setSelectMode('copy'));
document.getElementById('enter-delete').addEventListener('click', () => setSelectMode('delete'));
document.getElementById('exit-copy').addEventListener('click', () => setSelectMode('none'));
document.getElementById('fmt-tsv').addEventListener('click', () => setCopyFormat('tsv'));
document.getElementById('fmt-json').addEventListener('click', () => setCopyFormat('json'));
document.getElementById('select-all').addEventListener('change', (e) => {
  const checked = !!e.target.checked;
  _currentPageRows.forEach(r => {
    const id = Number(r.id);
    if (checked) _selectedIds.add(id); else _selectedIds.delete(id);
  });
  document.querySelectorAll('#recent-table input.row-check').forEach(cb => {
    cb.checked = checked;
  });
  updateCopyBar();
});
document.getElementById('copy-selected').addEventListener('click', async () => {
  const want = new Set(_selectedIds);
  const rows = _currentPageRows.filter(r => want.has(Number(r.id)));
  if (!rows.length) { toast(tr('noRecordsSelected'), true); return; }
  await doCopy(rows, tr('copied', { count: rows.length }));
});
document.getElementById('copy-page').addEventListener('click', async () => {
  const rows = _currentPageRows.slice();
  if (!rows.length) { toast(tr('noRecordsOnPage'), true); return; }
  await doCopy(rows, tr('copiedPage', { count: rows.length }));
});

async function doDelete(ids) {
  if (_deleting) return;
  _deleting = true;
  updateCopyBar();
  try {
    const opts = { method: 'DELETE' };
    if (ids) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ ids: ids });
    }
    const r = await fetch(BASE + '/api/v1/tool_calls', opts);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const count = d && d.deleted != null ? d.deleted : 0;
    toast(tr('deleted', { count: count }), false);
    _currentPage = 1;
    setSelectMode('none');
  } catch (e) {
    toast(tr('deleteFailed', { err: String(e.message || e) }), true);
  } finally {
    _deleting = false;
    updateCopyBar();
  }
}

document.getElementById('delete-selected').addEventListener('click', async () => {
  const ids = Array.from(_selectedIds);
  if (!ids.length) { toast(tr('noRecordsSelected'), true); return; }
  if (!window.confirm(tr('confirmDeleteSelected', { count: ids.length }))) return;
  await doDelete(ids);
});
document.getElementById('delete-all').addEventListener('click', async () => {
  if (!_totalRecords) { toast(tr('noRecordsToDelete'), true); return; }
  if (!window.confirm(tr('confirmDeleteAll', { count: _totalRecords }))) return;
  await doDelete(null);
});

// Clock
function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleString(_lang === 'zh' ? 'zh-CN' : 'en-US');
}
setInterval(updateClock, 1000); updateClock();

function applyLanguage() {
  document.documentElement.lang = _lang === 'zh' ? 'zh-CN' : 'en';
  document.title = tr('appTitle');
  document.querySelectorAll('[data-i18n]').forEach(el => { _setI18nText(el, tr(el.dataset.i18n)); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = tr(el.dataset.i18nTitle); });
  document.querySelectorAll('#lang-switch button[data-lang]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === _lang);
  });
  updateClock();
  loadFooter();
  _lastRecentKey = null;
  _lastPageSig = null;
  _forceRecentRender = true;
  updateRecent(_currentPageRows, true);
  updatePagination();
  if (activeTab === 'timeline') updateTimeline();
  if (activeTab === 'flow') {
    updateFlow();
    if (_flowSelected) showFlow(_flowSelected);
  }
  const modal = document.getElementById('chain-modal');
  if (_chainRow && modal && modal.style.display === 'flex') openChainModal(_chainRow);
  refresh();
}

document.getElementById('lang-switch').addEventListener('click', (event) => {
  const btn = event.target.closest ? event.target.closest('button[data-lang]') : null;
  if (!btn) return;
  const next = btn.dataset.lang;
  if (next !== 'zh' && next !== 'en') return;
  _lang = next;
  try { localStorage.setItem('agentgate.lang', next); } catch (e) { /* ignore */ }
  applyLanguage();
});

// Init
applyLanguage();
_initColumnResize();
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
