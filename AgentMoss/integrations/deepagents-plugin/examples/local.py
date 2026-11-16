"""Application-owned model/checkpointer; this module does not invoke a model."""

import asyncio

from deepagents import create_deep_agent

from agentmoss_deepagents import DeepAgentsIntegration


async def create(model, checkpointer, workspace: str):
    integration = await asyncio.to_thread(
        DeepAgentsIntegration.load,
        {
            "backend": "local",
            # Missing gate block explicitly loads the existing DEEPAGENTS_GATE_* environment.
            "local": {"root_dir": workspace, "virtual_mode": False},
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
        # create_deep_agent supplies the same backend to declared subagents' filesystem middleware.
        return integration, agent
    except BaseException:
        await integration.aclose()
        raise
