"""Own one backend, one Gate runtime and their common tool middleware."""

import asyncio
import logging
from collections.abc import Mapping

import httpx
from deepagents.backends.local_shell import LocalShellBackend

from .backend_config import IntegrationConfig
from .middleware import AgentMossMiddleware
from .runtime import GateRuntime, drain


class DeepAgentsIntegration:
    def __init__(self, config, backend, runtime, middleware, http=None):
        self.config, self.backend, self.runtime = config, backend, runtime
        self.middleware = middleware
        self._http = http
        self._close_task = None
        self._closed = False

    @classmethod
    def load(cls, config: IntegrationConfig | Mapping) -> "DeepAgentsIntegration":
        """Synchronous startup; async applications initialize via asyncio.to_thread."""
        if not isinstance(config, IntegrationConfig):
            config = IntegrationConfig.from_mapping(config)
        backend = runtime = http = None
        try:
            if config.backend == "local":
                backend = LocalShellBackend(root_dir=config.local.root_dir, virtual_mode=config.local.virtual_mode)
            else:
                try:
                    from agent_sandbox import Sandbox
                except ModuleNotFoundError as error:
                    if error.name != "agent_sandbox":
                        raise
                    raise ImportError(
                        "Install the AIO dependencies from the AgentMoss commercial source checkout: "
                        "python -m pip install -e '/path/to/AgentMoss[deepagents-aio]'"
                    ) from None
                from .aio_backend import AIOSandboxBackend

                aio = config.aio
                token = aio.read_token()
                headers = {"Host": aio.host}
                if token:
                    headers["e2b-traffic-access-token"] = token
                http = httpx.Client(timeout=aio.hard_timeout + 15, trust_env=False, follow_redirects=False)
                client = Sandbox(
                    base_url=aio.proxy_url.rstrip("/"), headers=headers, httpx_client=http, follow_redirects=False
                )
                backend = AIOSandboxBackend(client, config=aio, secrets=(token, config.gate.read_token()))
            runtime = GateRuntime.load(config.gate)
            if config.backend == "aio":
                backend._secrets = tuple(s for s in (*backend._secrets, runtime.client.token) if s)
            middleware = AgentMossMiddleware(runtime, backend)
            return cls(config, backend, runtime, middleware, http)
        except BaseException:
            # No graph or async request has started yet; no live async HTTP sockets exist.
            if runtime is not None:
                runtime.closing = True
                try:
                    runtime.client._close_unstarted()
                except Exception:
                    logging.getLogger(__name__).warning("Gate startup cleanup failed")
            if backend is not None and config.backend == "aio":
                try:
                    backend.close()
                except Exception:
                    logging.getLogger(__name__).warning("AIO startup cleanup failed; inspect sandbox sessions")
            if http is not None:
                try:
                    http.close()
                except Exception:
                    logging.getLogger(__name__).warning("AIO HTTP startup cleanup failed")
            raise

    def status(self) -> dict:
        return {
            "backend": self.config.backend,
            "closed": self._closed,
            "gate": self.runtime.status(),
            "execution": self.backend.status() if self.config.backend == "aio" else {"cwd": str(self.backend.cwd)},
            "sandbox_moss_verified": False,
        }

    async def _close(self):
        errors = []
        try:
            if self.config.backend == "aio":
                try:
                    await asyncio.to_thread(self.backend.close)
                except Exception:
                    errors.append("AIO shell cleanup failed; inspect the old session")
        finally:
            try:
                if self._http is not None:
                    await asyncio.to_thread(self._http.close)
            except Exception:
                errors.append("AIO HTTP client cleanup failed")
            finally:
                try:
                    await self.runtime.aclose()
                except Exception:
                    errors.append("Gate client cleanup failed")
                self._closed = True
        if errors:
            raise RuntimeError("; ".join(errors))

    async def aclose(self):
        """Caller stops and drains graph executions first. Closing itself is cancellation-safe."""
        self.runtime.closing = True
        if self.config.backend == "aio":
            self.backend.begin_close()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await drain(self._close_task, propagate_cancel=True)
