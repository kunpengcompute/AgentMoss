"""Translate AIO tool arguments without consulting the client filesystem."""

import json
import shlex
from copy import deepcopy

from deepagents.backends.utils import validate_path

from .actions import Action


class AIOActionAdapter:
    def __init__(self, backend):
        self.backend = backend

    def adapt(self, name: str, args: dict) -> Action:
        backend = self.backend
        values = dict(args)
        metadata = {
            "backend": "aio",
            "path_mode": "sandbox",
            "original_arguments": deepcopy(args),
            "sandbox_id": backend.config.sandbox_id,
            "shell_session_id": backend.session_id,
            "execution_cwd": backend.working_dir,
            "os_type": "linux",
        }
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
            path = values.pop(key, None)
            # FilesystemMiddleware applies validate_path before calling the backend.
            # Mirror that exact pre-processing; do not inspect a different cwd-relative file.
            real = backend.normalize_path(backend.working_dir if path is None else validate_path(path))
            metadata.update(virtual_path=path, real_path=real)
            if name == "delete":
                command = "rm -- " + shlex.quote(real)
                metadata["synthetic_action"] = True
            else:
                command = shlex.quote(real)
                if values:
                    command += "\n" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        else:
            command = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            if name in {"task", "write_todos"}:
                metadata["orchestration"] = True
        return Action(tool_name, command, backend.working_dir, metadata)
