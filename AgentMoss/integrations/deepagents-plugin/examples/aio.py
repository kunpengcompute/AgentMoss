"""Attach to an existing CubeSandbox; sandbox allocation belongs to the application."""

import asyncio

from deepagents import create_deep_agent

from agentmoss_deepagents import DeepAgentsIntegration


async def create(
    model, checkpointer, *, proxy_url: str, sandbox_id: str, gate_token_file: str, traffic_token_file: str | None = None
):
    aio = {
        "proxy_url": proxy_url,
        "sandbox_id": sandbox_id,
        "working_dir": "/home/gem",
        "default_timeout": 30,
        "hard_timeout": 30,
    }
    if traffic_token_file is not None:
        aio["traffic_token_file"] = traffic_token_file
    integration = await asyncio.to_thread(
        DeepAgentsIntegration.load,
        {
            "backend": "aio",
            "gate": {"gate_url": "http://127.0.0.1:9100", "api_token_file": gate_token_file, "fail_closed": True},
            "aio": aio,
        },
    )
    try:
        agent = create_deep_agent(
            model=model,
            backend=integration.backend,
            context_schema=dict,
            middleware=[integration.middleware],
            checkpointer=checkpointer,
            subagents=[
                {"name": "general-purpose", "description": "通用子代理", "middleware": [integration.middleware]}
            ],
        )
        return integration, agent
    except BaseException:
        await integration.aclose()
        raise
