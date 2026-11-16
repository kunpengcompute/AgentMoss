"""Explicit backend selection; Gate configuration retains its existing semantics."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from .aio_paths import normalize_aio_path
from .config import GateConfig


def positive_timeout(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def _mapping(cls, values):
    if not isinstance(values, Mapping):
        raise ValueError(f"{cls.__name__} must be a mapping")
    raw = dict(values)
    if raw.keys() - {f.name for f in fields(cls)}:
        raise ValueError(f"Unknown {cls.__name__} fields")
    if raw.get("traffic_token_file") is not None:
        raw["traffic_token_file"] = Path(raw["traffic_token_file"])
    return cls(**raw)


@dataclass(frozen=True)
class LocalBackendConfig:
    root_dir: str
    virtual_mode: bool = False

    def __post_init__(self):
        if not isinstance(self.root_dir, str) or not self.root_dir.strip() or "\x00" in self.root_dir:
            raise ValueError("local.root_dir must be a nonempty path")
        if type(self.virtual_mode) is not bool:
            raise ValueError("local.virtual_mode must be a boolean")


@dataclass(frozen=True)
class AIOBackendConfig:
    proxy_url: str
    sandbox_id: str
    working_dir: str = "/home/gem"
    default_timeout: float = 30
    hard_timeout: float = 30
    route_host: str | None = None
    traffic_token: str = field(default="", repr=False)
    traffic_token_file: Path | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.proxy_url, str):
            raise ValueError("aio.proxy_url must be an HTTP(S) base URL")
        url = urlsplit(self.proxy_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(ord(c) < 33 for c in self.proxy_url)
        ):
            raise ValueError("aio.proxy_url must be an HTTP(S) base URL without credentials, query or fragment")
        if not isinstance(self.sandbox_id, str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", self.sandbox_id
        ):
            raise ValueError("aio.sandbox_id must be a DNS label")
        if not isinstance(self.working_dir, str) or not self.working_dir.startswith("/"):
            raise ValueError("aio.working_dir must be an absolute POSIX path")
        object.__setattr__(self, "working_dir", normalize_aio_path(self.working_dir, "/"))
        positive_timeout(self.default_timeout, "default_timeout")
        positive_timeout(self.hard_timeout, "hard_timeout")
        if self.route_host is not None and (
            not isinstance(self.route_host, str)
            or len(self.route_host) > 253
            or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p) for p in self.route_host.split(".")
            )
        ):
            raise ValueError("aio.route_host must be a DNS host name")
        if not isinstance(self.traffic_token, str):
            raise ValueError("aio.traffic_token must be text")
        if self.traffic_token and self.traffic_token_file is not None:
            raise ValueError("Specify traffic_token or traffic_token_file, not both")
        if self.traffic_token_file is not None and not isinstance(self.traffic_token_file, Path):
            raise ValueError("traffic_token_file must be a Path")
        self._validate_token(self.traffic_token, required=bool(self.traffic_token))

    @property
    def host(self) -> str:
        return self.route_host or f"8080-{self.sandbox_id}.cube.app"

    @staticmethod
    def _validate_token(token: str, *, required: bool) -> str:
        if required and not token:
            raise ValueError("AIO traffic token must not be empty")
        if any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("AIO traffic token must contain printable ASCII without whitespace")
        return token

    def read_token(self) -> str:
        if self.traffic_token_file is not None:
            try:
                token = self.traffic_token_file.read_text().strip()
            except (OSError, UnicodeError):
                raise ValueError("Cannot read AIO traffic token file") from None
            return self._validate_token(token, required=True)
        return self._validate_token(self.traffic_token, required=False)


@dataclass(frozen=True)
class IntegrationConfig:
    backend: str
    gate: GateConfig
    local: LocalBackendConfig | None = None
    aio: AIOBackendConfig | None = None

    def __post_init__(self):
        if self.backend not in {"local", "aio"}:
            raise ValueError("backend must be local or aio")
        if not isinstance(self.gate, GateConfig):
            raise ValueError("gate must be GateConfig")
        if self.backend == "local":
            valid = isinstance(self.local, LocalBackendConfig) and self.aio is None
        else:
            valid = isinstance(self.aio, AIOBackendConfig) and self.local is None
        if not valid:
            raise ValueError("Provide only the selected backend configuration")

    @classmethod
    def from_mapping(cls, values: Mapping) -> "IntegrationConfig":
        if not isinstance(values, Mapping):
            raise ValueError("Integration configuration must be a mapping")
        raw = dict(values)
        if raw.keys() - {"backend", "gate", "local", "aio"}:
            raise ValueError("Unknown integration configuration fields")
        backend = raw.get("backend")
        if backend not in {"local", "aio"}:
            raise ValueError("backend must be local or aio")
        if ("aio" in raw if backend == "local" else "local" in raw) or backend not in raw:
            raise ValueError("Provide only the selected backend configuration")
        gate = GateConfig.from_mapping(raw["gate"]) if "gate" in raw else GateConfig.from_env()
        selected = _mapping(LocalBackendConfig if backend == "local" else AIOBackendConfig, raw[backend])
        return cls(backend=backend, gate=gate, **{backend: selected})
