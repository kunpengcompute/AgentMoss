"""Check Gate/Moss and the AIO image using fixed tool calls, without a model API.

Required: MOSS_GATE_URL, MOSS_GATE_TOKEN_FILE, MOSS_AIO_URL, MOSS_SANDBOX_ID.
Optional: MOSS_WORKING_DIR, MOSS_AIO_ROUTE_HOST, MOSS_AIO_TRAFFIC_TOKEN_FILE.
See README.md in this directory for deployment and expected results.
"""

import asyncio
import os
import shlex
from pathlib import Path
from uuid import uuid4

import httpx
from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.history import KEY
from agentmoss_deepagents.runtime import drain
from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class ScriptedModel(BaseChatModel):
    """Return a predetermined tool call; never send a request to a model service."""

    responses: list[AIMessage]
    _index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "agentmoss-cubesandbox-check"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"请先设置环境变量 {name}")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def wait_for_session_cleanup(config, session_id: str, *, timeout: float = 10) -> None:
    """AIO acknowledges deletion before its background cleanup actually finishes."""
    headers = {"Host": config.host}
    token = config.read_token()
    if token:
        headers["e2b-traffic-access-token"] = token
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        while loop.time() < deadline:
            response = await client.get(
                config.proxy_url.rstrip("/") + "/v1/shell/sessions",
                headers=headers,
                timeout=max(0.001, min(5, deadline - loop.time())),
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            sessions = data.get("sessions") if isinstance(data, dict) else None
            require(
                isinstance(payload, dict) and payload.get("success") is True and isinstance(sessions, dict),
                "AIO 返回的会话列表无效，无法确认清理结果",
            )
            if session_id not in sessions:
                return
            await asyncio.sleep(min(0.1, max(0, deadline - loop.time())))
    raise RuntimeError(f"会话清理尚未确认，请检查沙箱内的 shell 会话 {session_id}")


async def execute_case(integration: DeepAgentsIntegration, command: str, case: str):
    session_id = f"moss-cubesandbox-{uuid4().hex}"
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": command},
                        "id": case,
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=integration.backend,
        context_schema=dict,
        middleware=[integration.middleware],
    )
    result = await agent.ainvoke(
        {"messages": [("user", "检查沙箱接入是否正常")]},
        {"configurable": {"thread_id": session_id}, "recursion_limit": 60},
        context={"moss_info": {"session_id": session_id, "user_prompt": "检查沙箱接入是否正常"}},
    )
    event = next(item for item in result[KEY]["events"] if item["tool_call_id"] == case)
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage) and item.tool_call_id == case)
    return event, message


async def main() -> None:
    gate_url = required("MOSS_GATE_URL")
    gate_token_file = required("MOSS_GATE_TOKEN_FILE")
    token = Path(gate_token_file).read_text().strip()
    require(bool(token), "Gate 令牌文件为空")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.get(
            gate_url.rstrip("/") + "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        health = response.json()
    require(
        health.get("status") == "healthy" and health.get("moss_status") == "healthy",
        "Gate 或 Moss 尚未就绪，请检查服务地址和访问令牌",
    )
    print("PASS 外部 AgentGate 和 AgentMoss 服务正常")

    aio = {
        "proxy_url": required("MOSS_AIO_URL"),
        "sandbox_id": required("MOSS_SANDBOX_ID"),
        "working_dir": os.environ.get("MOSS_WORKING_DIR", "/home/gem"),
        "default_timeout": 30,
        "hard_timeout": 30,
    }
    if os.environ.get("MOSS_AIO_ROUTE_HOST"):
        aio["route_host"] = os.environ["MOSS_AIO_ROUTE_HOST"]
    if os.environ.get("MOSS_AIO_TRAFFIC_TOKEN_FILE"):
        aio["traffic_token_file"] = os.environ["MOSS_AIO_TRAFFIC_TOKEN_FILE"]
    starting = asyncio.create_task(
        asyncio.to_thread(
            DeepAgentsIntegration.load,
            {
                "backend": "aio",
                "gate": {
                    "gate_url": gate_url,
                    "api_token_file": gate_token_file,
                    "fail_closed": True,
                    "timeout_seconds": 30,
                },
                "aio": aio,
            },
        )
    )
    try:
        integration = await asyncio.shield(starting)
    except asyncio.CancelledError:
        integration = await drain(starting)
        await integration.aclose()
        raise
    try:
        event, message = await execute_case(integration, "printf agentmoss-aio-ok", "normal")
        require(
            event["decision"] == "Allow"
            and event["status"] == "executed_success"
            and message.artifact["exit_code"] == 0
            and "agentmoss-aio-ok" in message.content,
            "普通命令未成功执行，请检查 Gate、Moss 和 AIO 连接",
        )
        print("PASS 普通命令经过检查后，在沙箱中成功执行")

        outer = "if false; then cat /etc/shadow; fi; printf outer-should-not-run"
        event, message = await execute_case(integration, outer, "outer")
        require(
            event["decision"] == "Deny"
            and event["status"] == "denied"
            and event["source"] != "fail_closed"
            and "outer-should-not-run" not in message.content,
            "外部规则拦截未生效，或实际返回的是服务故障拒绝",
        )
        print("PASS 外部检查拒绝风险命令，命令未送到沙箱执行")

        script = aio["working_dir"].rstrip("/") + "/moss-inner-" + uuid4().hex + ".py"
        # Even without the guard, the shell condition prevents sensitive-file access.
        content = b'import os\nos.system("if false; then cat /etc/shadow; fi")\nprint("inner-should-not-run")\n'
        # Direct upload deliberately prepares a fixture outside the Agent tool flow.
        try:
            uploaded = await integration.backend.aupload_files([(script, content)])
            require(len(uploaded) == 1 and uploaded[0].error is None, "无法上传临时检查脚本")
            event, message = await execute_case(integration, "python3 " + shlex.quote(script), "inner")
            require(
                event["decision"] == "Allow"
                and event["status"] == "executed_error"
                and message.artifact["exit_code"] == 126
                and "AGENTMOSS_DENIED" in message.content
                and "inner-should-not-run" not in message.content,
                "沙箱内部脚本检查未生效，请确认实例使用了本目录构建的镜像",
            )
            print("PASS 沙箱内部读到脚本内容并拒绝执行")
        finally:
            cleanup = await integration.backend.adelete(script)
            require(cleanup.error is None, f"临时文件清理失败，请检查沙箱内的 {script}")
    finally:
        await integration.aclose()
        await wait_for_session_cleanup(integration.config.aio, integration.backend.session_id)
    print("PASS 临时文件和本次 shell 会话已清理")


if __name__ == "__main__":
    asyncio.run(main())
