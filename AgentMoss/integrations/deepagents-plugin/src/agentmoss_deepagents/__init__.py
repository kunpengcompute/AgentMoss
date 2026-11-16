"""AgentGate integration for the pinned DeepAgents runtime."""

__version__ = "0.6.0"


# Public imports stay lazy: config/actions can be used without assembling a graph.
def __getattr__(name):
    if name == "AgentMossMiddleware":
        from .middleware import AgentMossMiddleware

        return AgentMossMiddleware
    if name == "GateRuntime":
        from .runtime import GateRuntime

        return GateRuntime
    if name == "IntegrationConfig":
        from .backend_config import IntegrationConfig

        return IntegrationConfig
    if name == "DeepAgentsIntegration":
        from .integration import DeepAgentsIntegration

        return DeepAgentsIntegration
    if name == "AIOSandboxBackend":
        try:
            from .aio_backend import AIOSandboxBackend
        except ModuleNotFoundError as error:
            if error.name != "agent_sandbox":
                raise
            raise ImportError(
                "Install the AIO dependencies from the AgentMoss commercial source checkout: "
                "python -m pip install -e '/path/to/AgentMoss[deepagents-aio]'"
            ) from None

        return AIOSandboxBackend
    raise AttributeError(name)
