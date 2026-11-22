"""低频回顾（``POST /api/v1/review``）的 Python 实现。

- ``MossClient.review`` 发对地址/字段/凭证，且**失败不抛异常**（fire-and-forget）；
- V1 路由按「每 100 条触发一次」节流；
- ``get_agent_gate_db_path`` 返回 AgentMoss 可直读的存储路径。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from agent_gate.shared.client.moss_client import MossClient
from agent_gate.shared.storage.db import get_agent_gate_db_path


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


async def test_review_skips_without_db_path():
    """没给存储路径就不发请求。"""
    client = MossClient(base_url="http://127.0.0.1:9999", timeout=5.0)
    post = AsyncMock()
    try:
        with patch.object(client._client, "post", post):
            await client.review(agent_id="a", db_path="")
    finally:
        await client.close()

    post.assert_not_called()


# ----------------------------------------------------------------------
# 触发节流
# ----------------------------------------------------------------------


async def test_v1_routes_triggers_every_100_calls():
    from agent_gate.shared.server import routes

    routes._review_counter = 0
    fake = MagicMock()
    fake.review = AsyncMock()

    with patch(
        "agent_gate.shared.storage.db.get_agent_gate_db_path", return_value="/tmp/ag.db"
    ):
        for _ in range(99):
            routes._maybe_trigger_review(fake, "agent-1")
        assert fake.review.await_count == 0, "第 99 条不应触发"

        routes._maybe_trigger_review(fake, "agent-1")
        await __import__("asyncio").sleep(0)

    assert fake.review.await_count == 1, "第 100 条应触发一次"
    assert fake.review.await_args.kwargs["agent_id"] == "agent-1"


# ----------------------------------------------------------------------
# 存储路径
# ----------------------------------------------------------------------


def test_db_path_points_at_sqlite_file():
    path = get_agent_gate_db_path()
    assert path.endswith(".db")
    assert "agent_gate" in path
