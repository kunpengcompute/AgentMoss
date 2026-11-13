"""Smoke test the external Gate/Moss path and the AIO image's in-process Moss guard.

Set the environment variables documented in docs/tester-deployment.md, then run this
file with the Gate and Moss wheels installed. No model API is called.
"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from agent_gate.v1.sdk import GateClient
from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.history import KEY


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    _index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "agentmoss-deployment-smoke"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} before running the deployment smoke test")
    return value


def tool_call(command: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute", "args": {"command": command}, "id": call_id, "type": "tool_call"}],
    )


async def execute_case(integration: DeepAgentsIntegration, command: str, case: str):
    session_id = f"moss-deployment-{uuid4().hex}"
    agent = create_deep_agent(
        model=ScriptedModel(responses=[tool_call(command, case), AIMessage(content="done")]),
        backend=integration.backend,
        context_schema=dict,
        middleware=[integration.middleware],
    )
    result = await agent.ainvoke(
        {"messages": [("user", "Run the deployment smoke test")]},
        {"configurable": {"thread_id": session_id}, "recursion_limit": 60},
        context={"moss_info": {"session_id": session_id, "user_prompt": "Run the deployment smoke test"}},
    )
    event = next(item for item in result[KEY]["events"] if item["tool_call_id"] == case)
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage) and item.tool_call_id == case)
    return event, message


async def main() -> None:
    gate_url = required("MOSS_GATE_URL")
    gate_token_file = required("MOSS_GATE_TOKEN_FILE")
    aio_url = required("MOSS_AIO_URL")
    sandbox_id = required("MOSS_SANDBOX_ID")
    token = Path(gate_token_file).read_text().strip()
    if not token:
        raise RuntimeError("MOSS_GATE_TOKEN_FILE is empty")
    with GateClient(base_url=gate_url, timeout=30, api_token=token) as gate:
        health = gate.health()
    if health.get("status") != "healthy" or health.get("moss_status") != "healthy":
        raise RuntimeError(f"External Gate/Moss are not ready: {health}")
    print("PASS external Gate and Moss health")

    aio = {
        "proxy_url": aio_url,
        "sandbox_id": sandbox_id,
        "working_dir": os.environ.get("MOSS_WORKING_DIR", "/home/gem"),
        "default_timeout": 30,
        "hard_timeout": 30,
    }
    if os.environ.get("MOSS_AIO_ROUTE_HOST"):
        aio["route_host"] = os.environ["MOSS_AIO_ROUTE_HOST"]
    if os.environ.get("MOSS_AIO_TRAFFIC_TOKEN_FILE"):
        aio["traffic_token_file"] = os.environ["MOSS_AIO_TRAFFIC_TOKEN_FILE"]
    integration = await asyncio.to_thread(
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
    try:
        event, message = await execute_case(integration, "printf agentmoss-aio-ok", "normal")
        assert event["decision"] == "Allow" and event["status"] == "executed_success", event
        assert message.artifact["exit_code"] == 0 and "agentmoss-aio-ok" in message.content, message
        print("PASS DeepAgents → Gate → Moss → AIO shell: normal command")

        outer = "if false; then cat /etc/shadow; fi; printf outer-should-not-run"
        event, message = await execute_case(integration, outer, "outer")
        assert event["decision"] == "Deny" and event["status"] == "denied", event
        assert event["source"] != "fail_closed" and "outer-should-not-run" not in message.content, event
        print("PASS external Gate/Moss denial before AIO execution")

        script = aio["working_dir"].rstrip("/") + "/moss-inner-" + uuid4().hex + ".py"
        # If the image guard is missing, this probe only reads a local file and discards its output.
        content = b'import os\nos.system("cat /etc/shadow > /dev/null 2>&1")\nprint("inner-should-not-run")\n'
        uploaded = await integration.backend.aupload_files([(script, content)])
        assert len(uploaded) == 1 and uploaded[0].error is None, uploaded
        try:
            event, message = await execute_case(integration, "python3 " + script, "inner")
            assert event["decision"] == "Allow" and event["status"] == "executed_error", event
            assert message.artifact["exit_code"] == 126, message
            assert "AGENTMOSS_DENIED" in message.content and "inner-should-not-run" not in message.content, message
            print("PASS AIO shell.py → in-process Moss SDK: script-content denial")
        finally:
            await integration.backend.adelete(script)
    finally:
        await integration.aclose()


if __name__ == "__main__":
    asyncio.run(main())
