"""低频回顾（``POST /api/v1/review``）的 Python 实现。

- ``MossClient.review`` 发对地址/字段/凭证，且**失败不抛异常**（fire-and-forget）；
- V1 路由按「每 100 条触发一次」节流；
- 周期回顾和归档后回顾传递实际写入的 SQLite 文件。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger

from agent_gate.shared.client.moss_client import MossClient
from agent_gate.shared.normalizer import Normalizer
from agent_gate.shared.server import routes
from agent_gate.shared.server.models import SystemContext
from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.repository import Repository


# ----------------------------------------------------------------------
# MossClient.review
# ----------------------------------------------------------------------


async def test_review_posts_expected_payload():
    """地址、JSON 字段与凭证携带方式保持稳定。"""
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    post = AsyncMock(return_value=httpx.Response(200, json={}))
    try:
        with patch.object(client._client, "post", post):
            await client.review(agent_id="qwenpaw:host-1", db_path="/tmp/ag.db")
    finally:
        await client.close()

    args, kwargs = post.call_args
    assert args[0] == "http://127.0.0.1:9999/api/v1/review"
    assert kwargs["json"] == {"agent_id": "qwenpaw:host-1", "db_path": "/tmp/ag.db"}


async def test_review_carries_bearer_token():
    """AgentMoss 默认开鉴权，回顾请求同样必须带 Bearer。"""
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    post = AsyncMock(return_value=httpx.Response(200, json={}))
    try:
        with patch.object(client._client, "post", post), patch(
            "agent_gate.shared.client.moss_client.resolve_moss_token",
            return_value="tok-abc",
        ):
            await client.review(agent_id="a", db_path="/tmp/ag.db")
    finally:
        await client.close()

    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-abc"


async def test_review_is_fire_and_forget_on_failure():
    """回顾失败不能冒泡 —— 它跑在实时判定路径旁边，抛异常会污染响应。"""
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    try:
        with patch.object(
            client._client, "post", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await client.review(agent_id="a", db_path="/tmp/ag.db")  # 不应抛出
    finally:
        await client.close()


@pytest.mark.parametrize("db_path", ["", ":memory:"])
async def test_review_skips_without_shared_file(db_path):
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    post = AsyncMock()
    try:
        with patch.object(client._client, "post", post):
            await client.review(agent_id="a", db_path=db_path)
    finally:
        await client.close()

    post.assert_not_called()


# ----------------------------------------------------------------------
# 触发节流
# ----------------------------------------------------------------------


@pytest.fixture
def custom_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "default-data"))
    db = Database("custom #1.db")
    db.init_schema()
    try:
        yield db
    finally:
        db.close()


async def test_v1_routes_triggers_every_100_calls(custom_db, monkeypatch):
    monkeypatch.setattr(routes, "_review_counter", 0)
    repo = Repository(custom_db)
    moss = AsyncMock()
    moss.analyze.return_value = {"decision": "Allow", "reason": "ok"}
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_repo] = lambda: repo
    app.dependency_overrides[routes.get_moss_client] = lambda: moss
    app.dependency_overrides[routes.get_orchestrator] = lambda: MagicMock(
        collect=SystemContext
    )
    app.dependency_overrides[routes.get_normalizer] = lambda: Normalizer(repo=repo)
    payload = {
        "agent_type": "test",
        "metadata": {"hostname": "gate"},
        "session_id": "s",
        "instance_id": "test-instance",
        "tool_name": "bash",
        "action_history": [],
        "session_history": [],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gate"
    ) as client:
        for index in range(100):
            response = await client.post(
                "/api/v1/analyze", json={**payload, "command": f"echo {index}"}
            )
            assert response.status_code == 200
            if index < 99:
                moss.review.assert_not_called()
    await asyncio.gather(*routes._review_tasks)
    written = custom_db.execute("PRAGMA database_list").fetchone()["file"]
    assert custom_db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 100
    moss.review.assert_awaited_once_with(agent_id="test:gate", db_path=written)


def test_maintenance_reviews_actual_database_after_archive(custom_db, monkeypatch):
    from agent_gate.shared.storage import db as db_module
    from agent_gate.shared.storage.maintenance import run_storage_maintenance

    monkeypatch.setattr(db_module, "_db", custom_db)
    monkeypatch.setenv("AGENT_GATE_ARCHIVE_THRESHOLD", "2")
    monkeypatch.setenv("AGENT_GATE_ARCHIVE_KEEP", "1")
    repo = Repository(custom_db)
    agent_id = repo.get_or_create_agent("test", "gate")
    repo.get_or_create_session("s", agent_id)
    for command in ("echo first", "echo second"):
        repo.insert_tool_call("s", {"command": command}, {"decision": "Allow"})

    with patch("agent_gate.shared.client.moss_client.MossClient") as mock_client:
        review = AsyncMock()
        mock_client.return_value.review = review
        result = run_storage_maintenance(agent_id)
    assert result["archived"] == 1
    assert custom_db.execute("SELECT command FROM tool_calls").fetchone()[0] == "echo second"
    written = custom_db.execute("PRAGMA database_list").fetchone()["file"]
    review.assert_awaited_once_with(agent_id=agent_id, db_path=written)


@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        (httpx.Response(404), None, "review HTTP 404"),
        (None, httpx.ReadTimeout(""), "review unreachable: ReadTimeout"),
    ],
)
async def test_review_logs_failure(response, error, expected):
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    post = AsyncMock(return_value=response, side_effect=error)
    messages = []
    sink = logger.add(messages.append, format="{message}", level="WARNING")
    try:
        with patch.object(client._client, "post", post):
            await client.review(agent_id="test", db_path="/tmp/test.db")
    finally:
        logger.remove(sink)
        await client.close()

    assert any(expected in message for message in messages)
