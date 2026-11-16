"""Authenticated Gate transport with explicit analysis-failure semantics."""

import httpx

from .config import GateConfig


class GateUnavailable(Exception):
    """No usable Allow/Deny decision was received."""


class GateClient:
    """Own separate synchronous and asynchronous HTTP clients, with no retries."""

    def __init__(self, config: GateConfig, *, transport=None):
        self.token = config.read_token()
        options = {
            "base_url": config.gate_url.rstrip("/") + "/",
            "timeout": config.timeout_seconds,
            "headers": {"Authorization": "Bearer " + self.token},
            "trust_env": False,
        }
        if transport is not None:
            options["transport"] = transport
        self._sync = httpx.Client(**options)
        self._async = httpx.AsyncClient(**options)

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("decision") not in {"Allow", "Deny", "allow", "deny"}:
            raise ValueError("Invalid AgentGate decision")
        value["decision"] = value["decision"].capitalize()
        return value

    def analyze(self, payload: dict) -> dict:
        try:
            return self._parse(self._sync.post("api/v1/analyze", json=payload))
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise GateUnavailable(type(error).__name__) from error

    async def aanalyze(self, payload: dict) -> dict:
        try:
            return self._parse(await self._async.post("api/v1/analyze", json=payload))
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise GateUnavailable(type(error).__name__) from error

    def close(self) -> None:
        self._sync.close()

    def _close_unstarted(self) -> None:
        """Rollback synchronous initialization, before any async request exists."""
        self._sync.close()

    async def aclose(self) -> None:
        await self._async.aclose()
        self.close()
