"""AIO search limits and explicit input errors must not quarantine the session."""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from models import call
from test_aio_backend import sdk_backend as sdk_backend
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.aio_errors import AIOExecutionUncertain, AIOUnavailable
from agentmoss_deepagents.history import KEY


@pytest.mark.parametrize(
    "max_count,total,expected_count,truncated",
    [
        (1, 1, 1, False),
        (1, 2, 1, True),
        (9999, 9999, 9999, False),
        (9999, 10000, 9999, True),
        (10000, 10000, 10000, False),
        (10000, 10001, 10000, True),
        (20000, 10000, 10000, False),
        (20000, 10001, 10000, True),
    ],
)
def test_grep_respects_aio_limit_and_preserves_truncation(sdk_backend, max_count, total, expected_count, truncated):
    backend, _, replies = sdk_backend

    def search(request):
        body = json.loads(request.content)
        # Official AIO FileGrepRequest accepts max_results in [1, 10000].
        if not 1 <= body["max_results"] <= 10000:
            return httpx.Response(
                422,
                json={
                    "detail": [
                        {"loc": ["body", "max_results"], "msg": "must be at most 10000", "type": "less_than_equal"}
                    ]
                },
            )
        count = min(body["max_results"], total)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "path": body["path"],
                    "pattern": body["pattern"],
                    "matches": [
                        {"file": "/home/gem/log.txt", "line_number": i + 1, "line_content": "needle"}
                        for i in range(count)
                    ],
                    "match_count": count,
                    "files_searched": 1,
                    "files_matched": 1,
                    "truncated": count < total,
                },
            },
        )

    replies.append(search)
    result = backend.grep("needle", "/home/gem", max_count=max_count)
    assert result.error is None
    assert len(result.matches) == expected_count
    assert result.truncated is truncated
    assert backend.status()["state"] == "ready"


@pytest.mark.parametrize("code", [400, 422])
@pytest.mark.parametrize("sync", [False, True])
async def test_rejected_grep_keeps_next_graph_command_usable(sdk_backend, tmp_path, code, sync):
    backend, submitted, replies = sdk_backend

    def execute(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "session_id": body["id"],
                    "command": body["command"],
                    "status": "completed",
                    "exit_code": 0,
                    "output": "next-command-ok",
                },
            },
        )

    error_body = (
        {"detail": [{"loc": ["body", "glob"], "msg": "invalid: traffic-secret", "type": "value_error"}]}
        if code == 422
        else {"detail": "invalid glob: traffic-secret"}
    )
    replies.extend([httpx.Response(code, json=error_body), execute])
    runtime, _ = setup(tmp_path)
    graph = make_agent(
        runtime,
        tmp_path,
        [
            call("grep", {"pattern": "needle", "glob": "["}, "search"),
            call("execute", {"command": "printf next-command-ok"}, "next"),
            AIMessage(content="done"),
        ],
        backend=backend,
    )
    inputs, config, context = run_args()
    try:
        result = (
            await asyncio.to_thread(graph.invoke, inputs, config, context=context)
            if sync
            else await graph.ainvoke(inputs, config, context=context)
        )
        messages = {m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)}
        assert messages["search"].status == "error"
        assert messages["next"].status == "success" and "next-command-ok" in messages["next"].content
        assert "traffic-secret" not in str(messages)
        statuses = {event["tool_call_id"]: event["status"] for event in result[KEY]["events"]}
        assert statuses == {"search": "executed_error", "next": "executed_success"}
        assert backend.status()["state"] == "ready" and len(submitted) == 2 and not replies
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("failure", ["timeout", "server_error", "malformed_validation", "malformed_json"])
def test_unconfirmed_grep_failure_still_stops_session(sdk_backend, failure):
    backend, submitted, replies = sdk_backend

    def timeout(request):
        raise httpx.ReadTimeout("traffic-secret")

    if failure == "malformed_json":
        replies.append(httpx.Response(422, content=b"{", headers={"content-type": "application/json"}))
    else:
        replies.append(
            timeout
            if failure == "timeout"
            else httpx.Response(500 if failure == "server_error" else 422, json={"detail": "malformed: traffic-secret"})
        )
    with pytest.raises(AIOExecutionUncertain) as error:
        backend.grep("needle")
    assert "traffic-secret" not in str(error.value)
    assert backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        backend.execute("must-not-submit")
    assert len(submitted) == 1 and not replies
