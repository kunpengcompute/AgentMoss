"""LRU decision cache.

Only caches Allow decisions. Deny decisions are never cached.
Keys are SHA-256 hashes of (session_id, cwd, command).
"""

import hashlib
import time
from collections import OrderedDict
from typing import Optional


class DecisionCache:
    """LRU cache for Allow decisions with TTL-based expiration."""

    def __init__(self, max_size: int = 500, ttl: int = 300):
        self._max_size = max_size
        self._ttl = ttl  # seconds
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def _key(self, session_id: str, cwd: str, command: str) -> str:
        raw = f"{session_id}:{cwd}:{command}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, session_id: str, cwd: str, command: str) -> Optional[dict]:
        key = self._key(session_id, cwd, command)
        entry = self._cache.get(key)
        if entry is None:
            return None
        timestamp, result = entry
        if self._ttl > 0 and (time.time() - timestamp) > self._ttl:
            # Expired
            del self._cache[key]
            return None
        # Move to end (most recently used)
        self._cache.move_to_end(key)
        return result.copy()

    def set(self, session_id: str, cwd: str, command: str, result: dict) -> bool:
        """Cache a decision (only Allow is cached). Returns True if written."""
        if result.get("decision") != "Allow":
            return False
        key = self._key(session_id, cwd, command)
        if key in self._cache:
            self._cache.move_to_end(key)
            self._cache[key] = (time.time(), result.copy())
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)  # Remove least recently used
            self._cache[key] = (time.time(), result.copy())
        return True

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)
