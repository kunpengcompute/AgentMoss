"""Gate configuration and HTTP resources; the application owns graph execution."""

import asyncio
import re
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, Self

from . import __version__
from .client import GateClient
from .config import GateConfig


async def drain(awaitable: Awaitable[Any]) -> Any:
    """Wait through repeated cancellation; a thread-backed leaf may still be live."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


class GateRuntime:
    def __init__(self, config: GateConfig, client: GateClient) -> None:
        self.config, self.client = config, client
        self.closing = False

    @classmethod
    def load(cls, config: GateConfig | Mapping | Path | None = None, *, config_path: Path | None = None) -> Self:
        """Default to environment, or use explicit application configuration."""
        if config_path is not None:
            if config is not None:
                raise ValueError("Specify config or legacy config_path, not both")
            config = config_path
        if config is None:
            config = GateConfig.from_env()
        elif isinstance(config, Path):
            config = GateConfig.load(config)
        elif not isinstance(config, GateConfig):
            config = GateConfig.from_mapping(config)
        return cls(config, GateClient(config))

    def redact(self, text: str) -> str:
        if self.client.token:
            text = text.replace(self.client.token, "[redacted]")
        return re.sub(r"sk-[^\s\"'<>]+", "[redacted]", text)

    def redact_value(self, value: Any) -> Any:
        """Redact context copies without changing original execution parameters."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(str(key)): self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "version": __version__,
            "agent_type": "deepagents",
            "failure_mode": "fail_closed" if self.config.fail_closed else "fail_open",
            "coverage": "main_and_synchronous_subagents",
            "layers": ["L1", "L2"],
            "history": "agent_state",
            "policy": "agentgate",
            "history_semantics": "tool_calls",
            "state_key": "agentgate_history",
        }

    async def aclose(self) -> None:
        # The application drains its graph executions before closing the client.
        self.closing = True
        await self.client.aclose()
