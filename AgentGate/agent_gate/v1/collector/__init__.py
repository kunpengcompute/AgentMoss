"""Collector orchestrator: runs all collectors in parallel via ThreadPoolExecutor."""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from loguru import logger

from agent_gate.v1.config import get_config
from agent_gate.v1.server.models import SystemContext, OsInfo, ResourceInfo, ProcessInfo, NetworkInfo, UserInfo
from .system_info import SystemInfoCollector
from .process_info import ProcessInfoCollector
from .env_info import get_env_collector
from .network_info import NetworkInfoCollector
from .user_info import UserInfoCollector


class CollectorOrchestrator:
    """Orchestrates parallel system context collection.

    Runs all 5 collectors in a thread pool with per-collector timeout.
    Falls back to empty data when a collector fails or times out.
    """

    def __init__(self):
        cfg = get_config().collector
        self._timeout = cfg.get("timeout", 5.0)
        self._pool_size = cfg.get("pool_size", 5)
        self._enabled = {
            "system_info": cfg.get("system_info", True),
            "process_info": cfg.get("process_info", True),
            "env_info": cfg.get("env_info", True),
            "network_info": cfg.get("network_info", True),
            "user_info": cfg.get("user_info", True),
        }

    def collect(self) -> SystemContext:
        start = time.monotonic()
        context = SystemContext()

        collectors = []
        if self._enabled["system_info"]:
            collectors.append(("system_info", SystemInfoCollector.collect))
        if self._enabled["process_info"]:
            collectors.append(("process_info", ProcessInfoCollector.collect))
        if self._enabled["env_info"]:
            collectors.append(("env_info", get_env_collector().collect))
        if self._enabled["network_info"]:
            collectors.append(("network_info", NetworkInfoCollector.collect))
        if self._enabled["user_info"]:
            collectors.append(("user_info", UserInfoCollector.collect))

        results = {}
        with ThreadPoolExecutor(max_workers=min(len(collectors), self._pool_size)) as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in collectors
            }
            for future in futures:
                name = futures[future]
                try:
                    results[name] = future.result(timeout=self._timeout)
                except FutureTimeoutError:
                    logger.warning(f"Collector '{name}' timed out after {self._timeout}s")
                except Exception as e:
                    logger.warning(f"Collector '{name}' failed: {e}")

        # Merge results into context
        for collector_name, data in results.items():
            if "os" in data:
                context.os = OsInfo(**data["os"]) if data["os"] else OsInfo()
            if "resources" in data:
                context.resources = ResourceInfo(**data["resources"]) if data["resources"] else ResourceInfo()
            if "process" in data:
                context.process = ProcessInfo(**data["process"]) if data["process"] else ProcessInfo()
            if "network" in data:
                context.network = NetworkInfo(**data["network"]) if data["network"] else NetworkInfo()
            if "user" in data:
                context.user = UserInfo(**data["user"]) if data["user"] else UserInfo()
            if "env" in data:
                context.env = data["env"]

        elapsed = (time.monotonic() - start) * 1000
        logger.debug(f"Collection completed in {elapsed:.1f}ms")
        return context


# Singleton
_orchestrator: CollectorOrchestrator | None = None


def get_orchestrator() -> CollectorOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CollectorOrchestrator()
    return _orchestrator
