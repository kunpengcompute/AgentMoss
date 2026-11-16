"""Validated environment or application-owned configuration."""

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GateConfig:
    """Runtime policy and payload bounds for one application process."""

    api_token_file: Path | None = None
    enabled: bool = True
    gate_url: str = "http://127.0.0.1:9100"
    timeout_seconds: float = 5.0
    fail_closed: bool = True
    request_bytes: int = 256 * 1024
    history_actions: int = 200
    history_turns: int = 20
    history_bytes: int = 32 * 1024
    api_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.fail_closed) is not bool:
            raise ValueError("enabled and fail_closed must be booleans")
        url = urlsplit(self.gate_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("gate_url must be an HTTP(S) base URL without credentials")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        for name in ("request_bytes", "history_actions", "history_turns", "history_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.api_token_file is not None and not isinstance(self.api_token_file, Path):
            raise ValueError("api_token_file must be a Path")
        if not isinstance(self.api_token, str):
            raise ValueError("api_token must be text")
        if self.api_token.strip() and self.api_token_file is not None:
            raise ValueError("Specify api_token or api_token_file, not both")
        if self.enabled and not self.read_token():
            raise ValueError("Gate API credentials are required when enabled")

    def read_token(self) -> str:
        if not self.enabled:
            return ""
        return self.api_token_file.read_text().strip() if self.api_token_file is not None else self.api_token.strip()

    @classmethod
    def from_mapping(cls, values: Mapping) -> "GateConfig":
        """Use the application's own configuration without merging environment values."""
        if not isinstance(values, Mapping):
            raise ValueError("Gate configuration must be a mapping")
        raw = dict(values)
        unknown = raw.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        if raw.get("api_token_file") is not None:
            raw["api_token_file"] = Path(raw["api_token_file"])
        return cls(**raw)

    @classmethod
    def from_env(cls) -> "GateConfig":
        """Read DEEPAGENTS_GATE_* once at startup; never discover configuration files."""
        raw = {}
        for item in fields(cls):
            key = "DEEPAGENTS_GATE_" + ("URL" if item.name == "gate_url" else item.name.upper())
            if key not in os.environ:
                continue
            value = os.environ[key]
            if item.name in {"enabled", "fail_closed"}:
                normalized = value.strip().lower()
                if normalized not in {"true", "false", "1", "0"}:
                    raise ValueError(f"{key} must be true/false or 1/0")
                value = normalized in {"true", "1"}
            elif item.name == "timeout_seconds":
                value = float(value)
            elif item.name in {"request_bytes", "history_actions", "history_turns", "history_bytes"}:
                value = int(value)
            elif item.name == "api_token_file" and not value.strip():
                continue
            raw[item.name] = value
        return cls.from_mapping(raw)

    @classmethod
    def load(cls, path: Path) -> "GateConfig":
        """Compatibility for explicit legacy paths; new applications use env/mapping."""
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("Gate configuration must be an object")
        if raw.get("api_token_file") is not None:
            token_path = Path(raw["api_token_file"])
            raw["api_token_file"] = token_path if token_path.is_absolute() else path.parent / token_path
        return cls.from_mapping(raw)
