"""AgentGate configuration management.

Loads from YAML file with environment variable override support.
Environment variables use AGENT_GATE_ prefix with uppercase dotted path.
Example: AGENT_GATE_SERVER_PORT overrides server.port
"""

import os
import re
from pathlib import Path
from typing import Optional

import yaml


DEFAULT_CONFIG_PATHS = [
    Path("config/agent_gate.yaml"),
    Path("/etc/agent_gate/config.yaml"),
    Path.home() / ".config" / "agent_gate" / "config.yaml",
]


class Config:
    """AgentGate configuration with YAML loading + env var override."""

    def __init__(self, config_path: Optional[Path] = None):
        self._data = self._load_defaults()
        self._load_yaml(config_path)
        self._apply_env_overrides()

    # ---- file loading ----

    def _load_defaults(self) -> dict:
        return {
            "server": {
                # 监听地址**保持不变**（0.0.0.0）—— 改成回环会让跨机采集直接连不上，
                # 属于功能变化。真正的闸门是鉴权（默认 auto）：监听 0.0.0.0 但没凭证
                # 只能拿到 401，数据不会出去。
                "host": "0.0.0.0",
                "port": 9100,
                "mode": "http",
                "socket_path": "/var/run/agent_gate/agent_gate.sock",
                "workers": 1,
                "log_level": "info",
            },
            "agent_moss": {
                "url": "http://127.0.0.1:9090",
                "timeout": 30.0,
                "max_retries": 2,
                "retry_delay": 1.0,
            },
            "collector": {
                "system_info": True,
                "process_info": True,
                "env_info": True,
                "network_info": True,
                "user_info": True,
                "timeout": 5.0,
                "pool_size": 5,
            },
            "audit": {
                "log_dir": "auto",   # auto → resolve_data_dir("logs")
                "enabled": True,
                "retention_days": 30,
            },
            "storage": {
                "db_path": "auto",   # auto → resolve_data_dir("agent_gate.db")
                "retention_days": 30,
                "max_query_limit": 500,
            },
            "proxy": {
                "host": "127.0.0.1",
                "port": 9110,
                "intercept_hosts": [
                    "api.openai.com",
                    "api.anthropic.com",
                ],
                "agent_type": "auto",
                "session_timeout": 300,
                "redact": True,
                "upstream_timeout": 60.0,
                "debug_payload": False,
                "level": 1,
                "l2_timeout": 10.0,
                "deny_action": "inform",  # "inform" | "block"
            },
            "gateway": {
                "host": "127.0.0.1",
                "port": 9110,
                "model_routes": [],  # empty = use --upstream single-url mode
            },
            "privacy": {
                "mode": "auto",  # "auto" | "cloud_only" | "local_only"
                "redact_api_keys": True,
                "redact_passwords": True,
                "hash_file_content": True,
                "truncate_system_prompt": 200,
                "truncate_user_prompt": 500,
                "max_payload_bytes": 10240,
            },
            "security": {
                "env_blacklist": [
                    ".*_TOKEN$",
                    ".*_KEY$",
                    ".*_SECRET$",
                    ".*_PASSWORD$",
                    ".*_PASSWD$",
                    ".*_CREDENTIALS$",
                    "AWS_.*",
                    "AZURE_.*",
                    "GCP_.*",
                ],
                "env_whitelist": [
                    "^PATH$",
                    "^HOME$",
                    "^USER$",
                    "^SHELL$",
                    "^LANG$",
                    "^LC_.*",
                    "^PYTHON.*",
                    "^NODE_.*",
                    "^JAVA_HOME$",
                    "^GOPATH$",
                    "^CARGO_HOME$",
                    "^VIRTUAL_ENV$",
                    "^CONDA_.*",
                    "^CUDA_.*",
                ],
            },
            "l3": {
                # L3 backend selection: "auto" (try eBPF, fall back to harmony),
                # "ebpf" (Linux only, fail if unavailable), "harmony" (force).
                "backend": "auto",
                # /proc polling interval in ms (harmony backend only).
                "poll_interval_ms": 500,
                # Re-scan full /proc every N polls to catch new agent roots.
                "full_scan_every": 5,
                # Enable inotify watcher on shared user dir (cross-sandbox file signal).
                "enable_watcher": True,
                # Shared dirs to watch (harmony backend). Defaults cover the
                # common agent data dirs on HarmonyOS PC.
                "shared_dirs": [
                    "~/.atomcode",
                    "~/.workbuddy",
                    "~/.agentmoss",
                    "~/.opendesk",
                    "~/.config",
                ],
                # Instance identifier for multi-space deployment (each sandbox
                # runs its own AgentGate instance; events are tagged with this).
                "instance_id": "default",
            },
        }

    def _load_yaml(self, config_path: Optional[Path]) -> None:
        if config_path and config_path.exists():
            path = config_path
        else:
            path = None
            for candidate in DEFAULT_CONFIG_PATHS:
                if candidate.exists():
                    path = candidate
                    break

        if path is None:
            return

        try:
            with open(path, "r") as f:
                yaml_data = yaml.safe_load(f) or {}
            self._deep_merge(self._data, yaml_data)
        except Exception:
            pass

    def _apply_env_overrides(self) -> None:
        """``AGENT_GATE_<SECTION>_<KEY>`` 形式的环境变量覆盖配置项。

        匹配 section 时**按名字长度倒序**：``agent_moss_url`` 必须先命中
        ``agent_moss``。旧实现按**第一个下划线**切分，得到
        ``["agent", "moss_url"]`` —— 而 ``_data`` 里根本没有 ``agent`` 这一节，
        于是 ``AGENT_GATE_AGENT_MOSS_URL`` 设了也**静默不生效**（2026-10 修）。

        未识别的变量保持静默忽略：``AGENT_GATE_AUTH_TOKEN`` 这类由
        ``security.py`` 直接读取的变量不该在这里报错。
        """
        prefix = "AGENT_GATE_"
        sections = sorted(self._data, key=len, reverse=True)
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            config_key = key[len(prefix):].lower()
            for section in sections:
                if not config_key.startswith(section + "_"):
                    continue
                sub_key = config_key[len(section) + 1:]
                target = self._data[section]
                if isinstance(target, dict) and sub_key in target:
                    target[sub_key] = self._coerce_type(value, target[sub_key])
                break

    # ---- helpers ----

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                Config._deep_merge(base[key], value)
            else:
                base[key] = value

    @staticmethod
    def _coerce_type(value: str, reference):
        if isinstance(reference, bool):
            return value.lower() in ("true", "1", "yes")
        elif isinstance(reference, int):
            return int(value)
        elif isinstance(reference, float):
            return float(value)
        return value

    # ---- accessors ----

    @property
    def server(self) -> dict:
        return self._data["server"]

    @property
    def agent_moss(self) -> dict:
        return self._data["agent_moss"]

    @property
    def collector(self) -> dict:
        return self._data["collector"]

    @property
    def audit(self) -> dict:
        return self._data["audit"]

    @property
    def storage(self) -> dict:
        return self._data["storage"]

    @property
    def security(self) -> dict:
        return self._data["security"]

    @property
    def proxy(self) -> dict:
        return self._data["proxy"]

    @property
    def privacy(self) -> dict:
        return self._data["privacy"]

    @property
    def gateway(self) -> dict:
        return self._data["gateway"]

    def get(self, dotted_key: str, default=None):
        keys = dotted_key.split(".")
        node = self._data
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return default
        return node


# ---- Path resolution ----

def resolve_data_path(relative_name: str = "") -> Path:
    """Resolve data file path following XDG/FHS conventions.

    Priority:
      1. Explicit path in config (not "auto")
      2. `data/` directory if running from project root (development)
      3. ``XDG_DATA_HOME`` (typically ~/.local/share/agent_gate/) — user install
      4. ``/var/lib/agent_gate/`` — system install (if writable)
    """
    cfg = get_config()

    # Check if storage.db_path is explicitly set (not "auto")
    db_path = cfg.storage.get("db_path", "auto")
    if db_path != "auto" and not relative_name:
        return Path(db_path)

    # Determine the real user's home (respects sudo: SUDO_USER → original user)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        user_home = Path(f"/home/{sudo_user}")
        if not user_home.exists():
            user_home = Path.home()
    else:
        user_home = Path.home()

    # XDG_DATA_HOME (defaults to ~/.local/share/agent_gate/)
    xdg_data = os.environ.get("XDG_DATA_HOME", str(user_home / ".local" / "share"))
    data_dir = Path(xdg_data) / "agent_gate"

    if not relative_name:
        return data_dir

    target = data_dir / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def resolve_storage_db_path(configured: Optional[str] = None) -> Path:
    """Resolve ``storage.db_path``, where ``"auto"`` is a sentinel.

    ``DEFAULT_CONFIG`` ships ``storage.db_path = "auto"`` meaning "pick the
    standard location".  Handing that literal string to ``sqlite3.connect``
    made SQLite create a file literally named ``auto`` in the process working
    directory: the records then moved with the CWD, and a restart from another
    directory appeared to have lost all history.

    An explicit path (CLI flag or config) always wins; ``None``, empty and
    ``"auto"`` all resolve to the standard location.
    """
    if configured and configured != "auto":
        return Path(configured)
    return resolve_data_path("agent_gate.db")


# Singleton
_config: Optional[Config] = None


def get_config(config_path: Optional[Path] = None) -> Config:
    global _config
    if _config is None:
        _config = Config(config_path)
    return _config


def reload_config(config_path: Optional[Path] = None) -> Config:
    global _config
    _config = Config(config_path)
    return _config
