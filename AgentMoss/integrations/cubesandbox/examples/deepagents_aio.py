"""Connect an application's existing model to a protected CubeSandbox instance."""

import asyncio

from agentmoss_deepagents import DeepAgentsIntegration
from agentmoss_deepagents.runtime import drain
from deepagents import create_deep_agent


async def run_once(model, checkpointer, session_id, user_text, *, config):
    """Use an existing sandbox; its creation and deletion belong to the application."""
    starting = asyncio.create_task(asyncio.to_thread(DeepAgentsIntegration.load, config))
    try:
        integration = await asyncio.shield(starting)
    except asyncio.CancelledError:
        # Cancelling the caller does not stop the worker from creating a session.
        integration = await drain(starting)
        await integration.aclose()
        raise
    try:
        agent = create_deep_agent(
            model=model,
            backend=integration.backend,
            context_schema=dict,
            middleware=[integration.middleware],
            checkpointer=checkpointer,
            subagents=[
                {
                    "name": "general-purpose",
                    "description": "在沙箱中执行任务的通用子代理",
                    "middleware": [integration.middleware],
                }
            ],
        )
        return await agent.ainvoke(
            {"messages": [("user", user_text)]},
            {"configurable": {"thread_id": session_id}},
            context={
                "moss_info": {
                    "session_id": session_id,
                    "user_prompt": user_text,
                }
            },
        )
    finally:
        await integration.aclose()
