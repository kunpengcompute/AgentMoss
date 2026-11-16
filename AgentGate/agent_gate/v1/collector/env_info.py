"""Environment variable collector with security filtering.

Applies whitelist + blacklist to prevent leaking secrets.
"""

import os
import re

from agent_gate.v1.config import get_config


class EnvInfoCollector:
    """Collects environment variables, applying security filters."""

    def __init__(self):
        cfg = get_config().security
        self._blacklist: list[re.Pattern] = [
            re.compile(p) for p in cfg.get("env_blacklist", [])
        ]
        self._whitelist: list[re.Pattern] = [
            re.compile(p) for p in cfg.get("env_whitelist", [])
        ]

    def is_safe(self, key: str) -> bool:
        """Check if an env var key is safe to collect."""
        # Whitelist takes priority
        for pattern in self._whitelist:
            if pattern.match(key):
                return True
        # Then check blacklist
        for pattern in self._blacklist:
            if pattern.match(key):
                return False
        # Not in either list → don't collect (conservative)
        return False

    def collect(self) -> dict:
        result = {}
        for key, value in os.environ.items():
            if self.is_safe(key):
                # Truncate long values (>500 chars)
                result[key] = value[:500] if len(value) > 500 else value
        return {"env": result}


# Singleton
_env_collector: EnvInfoCollector | None = None


def get_env_collector() -> EnvInfoCollector:
    global _env_collector
    if _env_collector is None:
        _env_collector = EnvInfoCollector()
    return _env_collector
