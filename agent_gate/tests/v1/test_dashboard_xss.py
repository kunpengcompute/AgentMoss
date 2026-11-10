"""看板渲染的 XSS 防护 + 安全响应头。

看板展示的 agent_type / session_id / tool_name / command / reason 全部**由调用方
上报**（`POST /api/v1/analyze` 的请求体字段），落地后再拼回 HTML —— 是不可信输入。

2026-10 之前的问题：
  - `escapeHtml` 只转义 `& < >`，**不含引号**；它被用在 `title="..."` 属性里，
    命令含 `" onmouseover="...` 就能冲出属性注入事件处理器；
  - 表格里 `agent_type` / `session_id` / `tool_name` **完全没转义**；
  - 页面没有任何安全响应头（可被 iframe 嵌入做点击劫持）。

这些断言是源码级的：跑真实 DOM 需要浏览器，CI 没有。每条都注明它防的是什么。
"""

import pytest

from agent_gate.v1.server.dashboard import DASHBOARD_HTML
from agent_gate.v1.server.security import DASHBOARD_SECURITY_HEADERS


def _script_block() -> str:
    return DASHBOARD_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]


# ----------------------------------------------------------------------
# 转义函数本身
# ----------------------------------------------------------------------


def test_escape_html_escapes_quotes():
    """引号必须转义 —— 否则属性上下文（title="..."）可被闭合。"""
    script = _script_block()
    assert "function escapeHtml" in script
    body = script.split("function escapeHtml", 1)[1][:600]
    assert "&quot;" in body, "escapeHtml 没有转义双引号，属性注入仍然可行"
    assert "&#39;" in body, "escapeHtml 没有转义单引号"


def test_table_cells_are_escaped():
    """表格里的三个不可信字段都要经过 escapeHtml。"""
    script = _script_block()
    assert "escapeHtml(r.agent_type" in script, "agent_type 未转义"
    assert "escapeHtml(r.tool_name" in script, "tool_name 未转义"
    assert "escapeHtml(r.session_id" in script, "session_id 未转义"
    assert "escapeHtml(r.command" in script, "command 未转义"


def test_no_raw_agent_fields_in_template():
    """裸插值 ${r.xxx} 不应再出现在模板里（转义后应为 ${escapeHtml(r.xxx...)}）。"""
    script = _script_block()
    for field in ("agent_type", "tool_name"):
        assert f"${{r.{field}" not in script, f"{field} 仍有未转义的裸插值"


# ----------------------------------------------------------------------
# 安全响应头
# ----------------------------------------------------------------------


def test_security_headers_present():
    csp = DASHBOARD_SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp, "缺少防点击劫持"
    assert "object-src 'none'" in csp
    assert DASHBOARD_SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert DASHBOARD_SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert DASHBOARD_SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"


def test_dashboard_page_ships_security_headers(monkeypatch):
    """实际请求 /dashboard/ 时响应头必须真的带上（不是只有常量定义）。"""
    from fastapi.testclient import TestClient

    from agent_gate.v1.server.app import create_app

    monkeypatch.setenv("AGENT_GATE_AUTH_MODE", "off")
    client = TestClient(create_app())
    resp = client.get("/dashboard/")

    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")
