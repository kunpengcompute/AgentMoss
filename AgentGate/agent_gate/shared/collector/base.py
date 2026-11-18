"""Unified BehaviorCollector interface — abstract base for all data sources.

All data sources (Proxy, eBPF, MCP) implement this interface and emit
NormalizedEvent objects. Upper layers (session, trigger, DB) consume only
NormalizedEvent, unaware of the underlying source.

Usage:
    collector = LLMAPIProxyCollector(config)
    collector.subscribe(on_event)
    collector.start()
"""

import datetime
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class NormalizedEvent:
    """Unified event schema — all data sources emit this.

    This is the single source of truth for event format.
    Both eBPF normalizer and proxy normalizer produce this.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_ns: int = 0
    timestamp_iso: str = ""
    source_collector: str = ""  # "llm_api_proxy" | "ebpf" | "mcp"
    source_os: str = "linux"
    session_id: str = ""
    agent_type: str = "unknown"

    # Action block
    action_type: str = ""  # "tool_call" | "process_exec" | "file_access" | ...
    action_detail: dict = field(default_factory=dict)

    # Target block
    target: dict = field(default_factory=dict)

    # Payload block (optional, content data)
    payload: dict = field(default_factory=dict)

    # Process block (for eBPF events)
    process: dict = field(default_factory=dict)

    # OS context
    os_context: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp_ns:
            now = datetime.datetime.now(datetime.timezone.utc)
            self.timestamp_ns = int(now.timestamp() * 1_000_000_000)
        if not self.timestamp_iso:
            self.timestamp_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization and DB storage."""
        return {
            "event_id": self.event_id,
            "timestamp": {"timestamp_ns": self.timestamp_ns, "iso": self.timestamp_iso},
            "source_os": self.source_os,
            "source_collector": self.source_collector,
            "session_id": self.session_id,
            "agent_type": self.agent_type,
            "action": {"action_type": self.action_type, **self.action_detail},
            "target": self.target,
            "payload": self.payload,
            "process": self.process,
            "os_context": self.os_context,
        }


# Type alias for event callbacks
EventCallback = Callable[[NormalizedEvent], None]


class BehaviorCollector(ABC):
    """Abstract base class for all behavior data sources.

    Implementations:
    - LLMAPIProxyCollector: intercepts LLM API traffic via mitmproxy
    - EBpfCollector: captures syscalls via eBPF tracepoints (Linux only)
    - MCPCollector: receives events via MCP protocol (future)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Collector name (e.g., 'llm_api_proxy', 'ebpf')."""

    @property
    @abstractmethod
    def available(self) -> bool:
        """Check if this collector can run on the current platform."""

    @abstractmethod
    def start(self) -> None:
        """Start collecting events. Non-blocking if possible."""

    @abstractmethod
    def stop(self) -> None:
        """Stop collecting events."""

    @abstractmethod
    def subscribe(self, callback: EventCallback) -> None:
        """Register a callback for new events."""


class CollectorOrchestratorV3:
    """Manages multiple BehaviorCollectors.

    Starts/stops collectors, routes events to a unified callback.
    Used by the CLI to coordinate proxy + eBPF.
    """

    def __init__(self):
        self._collectors: list[BehaviorCollector] = []
        self._callback: Optional[EventCallback] = None
        self._running = False

    def add(self, collector: BehaviorCollector) -> None:
        """Add a collector to the orchestrator."""
        self._collectors.append(collector)
        if self._callback:
            collector.subscribe(self._callback)

    def subscribe(self, callback: EventCallback) -> None:
        """Set the unified event callback for all collectors."""
        self._callback = callback
        for c in self._collectors:
            c.subscribe(callback)

    def start(self) -> dict:
        """Start all available collectors. Returns status dict."""
        results = {}
        for c in self._collectors:
            if c.available:
                try:
                    c.start()
                    results[c.name] = "started"
                except Exception as e:
                    results[c.name] = f"error: {e}"
            else:
                results[c.name] = "unavailable"
        self._running = True
        return results

    def stop(self) -> None:
        """Stop all collectors."""
        for c in self._collectors:
            try:
                c.stop()
            except Exception:
                pass
        self._running = False

    @property
    def active_collectors(self) -> list[str]:
        return [c.name for c in self._collectors if c.available]
