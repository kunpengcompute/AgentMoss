"""Translate policy inputs without mutating requests sent to actual tools."""

import json
import shlex
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.utils import validate_path


def delete_command(path: str) -> str:
    """Express the recursive delete contract for both Gate and AIO execution."""
    return "rm -r -- " + shlex.quote(path)


@dataclass(frozen=True)
class Action:
    """An action expressed in the existing AgentGate policy vocabulary."""

    tool_name: str
    command: str
    cwd: str
    metadata: dict = field(default_factory=dict)


class ActionAdapter:
    """Serialize Gate actions, resolving paths with the selected backend's semantics."""

    def __init__(self, backend):
        self.local = type(backend) is LocalShellBackend
        if not self.local:
            from .aio_backend import AIOSandboxBackend

            if type(backend) is not AIOSandboxBackend:
                raise ValueError("Only LocalShellBackend or AIOSandboxBackend is supported")
        self.backend = backend

    def adapt(self, name: str, args: dict) -> Action:
        """Build a complete, untruncated analysis action."""
        values = dict(args)
        backend = self.backend
        cwd = str(backend.cwd) if self.local else backend.working_dir
        metadata = {
            "original_arguments": deepcopy(args),
        }
        if self.local:
            metadata["path_mode"] = "virtual" if backend.virtual_mode else "native"
        else:
            metadata.update(
                backend="aio",
                path_mode="sandbox",
                sandbox_id=backend.config.sandbox_id,
                shell_session_id=backend.session_id,
                execution_cwd=cwd,
                os_type="linux",
            )
        tool_name = {
            "execute": "bash",
            "read_file": "read",
            "write_file": "write",
            "edit_file": "edit",
            "ls": "list_dir",
            "delete": "bash",
        }.get(name, name)
        if name == "execute":
            command = values["command"]
            if not isinstance(command, str):
                raise ValueError("execute.command must be a string")
        elif name in {"read_file", "write_file", "edit_file", "delete", "ls", "glob", "grep"}:
            key = "file_path" if name in {"read_file", "write_file", "edit_file", "delete"} else "path"
            raw_path = values.pop(key, None)
            path = ("/" if self.local else cwd) if raw_path is None else raw_path
            if not isinstance(path, str):
                raise ValueError("file path must be a string")
            # Use the same public normalization as the official file tools.
            # Backend authorization still runs in the original SDK handler.
            normalized = validate_path(path)
            default_search = name in {"glob", "grep"} and raw_path is None
            if not self.local:
                real = backend.normalize_path(normalized)
            elif default_search or (name == "glob" and normalized == "/"):
                # LocalShellBackend searches cwd for a pathless grep/glob,
                # and for glob('/') even in native path mode.
                real = str(Path(self.backend.cwd).resolve())
            else:
                candidate = Path(normalized.lstrip("/")) if self.backend.virtual_mode else Path(normalized)
                real = str((Path(self.backend.cwd) / candidate).resolve())
            metadata.update(virtual_path=path, real_path=real)
            if name == "delete":
                command = delete_command(real)
                metadata["synthetic_action"] = True
            else:
                # Keep the real path visible to the existing Gate/Moss adapter,
                # and encode parameter names/boundaries rather than flatten strings.
                command = shlex.quote(real)
                if values:
                    command += "\n" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))

        else:
            command = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            if name in {"task", "write_todos"}:
                metadata["orchestration"] = True
        return Action(tool_name, command, cwd, metadata)
