"""AgentGate integration for the pinned DeepAgents runtime."""

__version__ = "0.5.1"


# Public imports stay lazy: config/actions can be used without assembling a graph.
def __getattr__(name):
    if name == "AgentMossMiddleware":
        from .middleware import AgentMossMiddleware

        return AgentMossMiddleware
    if name == "GateRuntime":
        from .runtime import GateRuntime

        return GateRuntime
    raise AttributeError(name)
