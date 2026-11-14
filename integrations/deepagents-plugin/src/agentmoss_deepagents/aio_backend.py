"""AIO backend adapted from agent-infra/sandbox's Apache-2.0 example.

Upstream: 7f1afaf8d82bd30531a19caeb1a24dfebbc97d8c,
examples/langgraph-deepagents-integration/sandbox_backend.py.
Modified: modern DeepAgents results, explicit paths, no retries, lifecycle and
uncertain-execution handling. See THIRD_PARTY_NOTICES.md and LICENSES/.
"""

from __future__ import annotations

import base64
import glob as globlib
import posixpath
import threading
import uuid
from contextlib import contextmanager

from agent_sandbox.core.api_error import ApiError
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox
from pydantic import ValidationError

from .actions import delete_command
from .aio_errors import AIOExecutionUncertain, AIOStartupError, AIOUnavailable
from .aio_paths import normalize_aio_path
from .backend_config import AIOBackendConfig, positive_timeout
from .operation import mark_uncertain


class _AIOFileFailure(AIOUnavailable):
    """Confirmed file rejection returned with HTTP 200 and success=false."""


def _file_error(data) -> str | None:
    kind = data.get("error_type") if isinstance(data, dict) else getattr(data, "error_type", None)
    errno = data.get("errno_name") if isinstance(data, dict) else getattr(data, "errno_name", None)
    if kind in ("file_not_found", "not_found") or errno == "ENOENT":
        return "file_not_found"
    if kind in ("permission_denied", "access_denied") or errno in ("EACCES", "EPERM"):
        return "permission_denied"
    return None


class AIOSandboxBackend(SandboxBackendProtocol):
    """Translate SDK results to DeepAgents; inherit its async forwarding methods."""

    grep_exclude_dirs = (".git",)

    def __init__(self, client, *, config: AIOBackendConfig, secrets: tuple[str, ...] = ()):
        self.client, self.config = client, config
        self.working_dir = config.working_dir
        self._secrets = tuple(s for s in (config.traffic_token, *secrets) if s)
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._state = "initializing"
        self._id = str(uuid.uuid4())
        # Client-generated ID permits cleanup even if the creation response is lost.
        self.session_id = "moss-" + self._id
        try:
            data = self._data(client.shell.create_session(id=self.session_id, exec_dir=self.working_dir))
            if data.session_id != self.session_id:
                raise AIOUnavailable("AIO returned an unexpected shell session")
        except Exception as error:
            try:
                self.close()
            except AIOUnavailable:
                pass
            raise AIOStartupError(
                sandbox_id=config.sandbox_id,
                session_id=self.session_id,
                cleanup_confirmed=self._state == "closed",
                failure_type=type(error).__name__,
            ) from None
        self._state = "ready"

    @property
    def id(self) -> str:
        return self._id

    def normalize_path(self, path: str) -> str:
        return normalize_aio_path(path, self.working_dir)

    def _redact(self, value) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text

    @staticmethod
    def _data(response):
        data = getattr(response, "data", None)
        if getattr(response, "success", None) is not True or data is None:
            if failure := _file_error(data):
                raise _AIOFileFailure(failure)
            raise AIOUnavailable("AIO returned an invalid or unsuccessful response")
        return data

    def _ready(self):
        if self._closing.is_set() or self._state != "ready":
            raise AIOUnavailable("AIO session is not ready; inspect and clean up the old session before recreating")

    @contextmanager
    def _operation(self, result):
        """Serialize SDK calls and distinguish confirmed rejection from lost results."""
        with self._lock:
            self._ready()
            try:
                yield
            except Exception as error:
                code = error.status_code if isinstance(error, ApiError) else None
                shell = isinstance(result, ExecuteResponse)
                failure = str(error) if isinstance(error, _AIOFileFailure) else None
                if isinstance(error, ValidationError):
                    # SDK 0.0.30 models success data only; expected file errors omit those fields.
                    failure = next(
                        (
                            _file_error(e["input"])
                            for e in error.errors()
                            if e["type"] == "missing" and len(e["loc"]) == 2 and e["loc"][0] == "data"
                        ),
                        None,
                    )
                if shell and code in (403, 409, 503):
                    result.output, result.exit_code = f"AIO HTTP {code}: {self._redact(error.body)}", 126
                    if code == 409:
                        self._state = "uncertain"
                elif not shell and failure:
                    result.error = failure
                elif not shell and code in (403, 404):
                    result.error = "permission_denied" if code == 403 else "file_not_found"
                elif isinstance(result, EditResult) and code in (400, 409, 422):
                    result.error = "AIO edit rejected: " + self._redact(error.body)
                else:
                    self._state = "uncertain"
                    mark_uncertain()
                    raise AIOExecutionUncertain(
                        "AIO operation result is unknown; inspect the old session before retrying"
                    ) from None

    def status(self) -> dict:
        with self._lock:
            return {
                "state": "closing" if self._closing.is_set() and self._state == "ready" else self._state,
                "sandbox_id": self.config.sandbox_id,
                "shell_session_id": self.session_id,
                "working_dir": self.working_dir,
                "os_type": "linux",
            }

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not isinstance(command, str):
            raise ValueError("command must be text")
        limit = min(
            positive_timeout(self.config.default_timeout if timeout is None else timeout, "timeout"),
            self.config.hard_timeout,
        )
        result = ExecuteResponse(output="", exit_code=None)
        with self._operation(result):
            data = self._data(
                self.client.shell.exec_command(
                    command=command,
                    id=self.session_id,
                    exec_dir=self.working_dir,
                    strict=True,
                    async_mode=False,
                    timeout=limit + 5,
                    hard_timeout=limit,
                    request_options={"timeout_in_seconds": limit + 15},
                )
            )
            if data.status != "completed" or type(data.exit_code) is not int or not isinstance(data.output, str):
                raise AIOExecutionUncertain("Incomplete AIO command result")
            result.output, result.exit_code = data.output, data.exit_code
            result.truncated = bool(getattr(data, "full_output_file_path", None))
        return result

    def ls(self, path: str) -> LsResult:
        path = self.normalize_path(path)
        result = LsResult()
        with self._operation(result):
            data = self._data(self.client.file.list_path(path=path, include_size=True))
            result.entries = self._entries(data.files)
        return result

    def _entries(self, files):
        entries = []
        for item in files:
            info = {"path": self.normalize_path(item.path), "is_dir": bool(item.is_directory)}
            if item.size is not None:
                info["size"] = item.size
            if item.modified_time is not None:
                info["modified_at"] = item.modified_time
            entries.append(info)
        return entries

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        path = self.normalize_path(file_path)
        if type(offset) is not int or type(limit) is not int:
            raise ValueError("offset and limit must be integers")
        if limit <= 0:
            return ReadResult(file_data={"content": "", "encoding": "utf-8"}, no_lines_requested=True)
        result = ReadResult()
        with self._operation(result):
            data = self._data(
                self.client.file.read_file(file=path, start_line=max(0, offset), end_line=max(0, offset) + limit)
            )
            if not data.content and offset > 0:
                return ReadResult(error=f"Requested offset {offset} is beyond the end of the file")
            result.file_data = {"content": data.content, "encoding": "utf-8"}
            if data.content:
                lines = data.content.count("\n") + (not data.content.endswith("\n"))
                result.start_line = max(0, offset) + 1
                result.end_line = max(0, offset) + lines
                if lines == limit:
                    result.next_offset = result.end_line
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        path = self.normalize_path(file_path)
        result = WriteResult()
        with self._operation(result):
            found = self._data(
                self.client.file.find_files(path=posixpath.dirname(path), glob=globlib.escape(posixpath.basename(path)))
            )
            if any(self.normalize_path(p) == path for p in found.files):
                return WriteResult(error="File already exists")
            self._data(self.client.file.write_file(file=path, content=content))
            result.path = path
        return result

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        path = self.normalize_path(file_path)
        if not old_string:
            return EditResult(error="old_string must not be empty")
        result = EditResult()
        with self._operation(result):
            data = self._data(
                self.client.file.str_replace_editor(
                    command="str_replace",
                    path=path,
                    old_str=old_string,
                    new_str=new_string,
                    **({"replace_mode": "ALL"} if replace_all else {}),
                )
            )
            if getattr(data, "error", None):
                return EditResult(error=self._redact(data.error))
            old = getattr(data, "old_content", None)
            result.path, result.occurrences = path, old.count(old_string) if isinstance(old, str) else None
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        path = self.normalize_path(self.working_dir if path is None else path)
        # Match the tool schema's recursive basename and brace patterns.
        return BaseSandbox.glob(self, pattern, path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        path = self.normalize_path(self.working_dir if path is None else path)
        if max_count is not None and (type(max_count) is not int or max_count < 1):
            raise ValueError("max_count must be a positive integer")
        options = {
            "path": path,
            "pattern": pattern,
            "fixed_strings": True,
            "recursive": True,
        }
        if glob is not None:
            options["include"] = [glob]
        if self.grep_exclude_dirs:
            options["exclude"] = list(self.grep_exclude_dirs)
        if max_count is not None:
            options["max_results"] = max_count + 1
        result = GrepResult()
        with self._operation(result):
            data = self._data(self.client.file.grep_files(**options))
            if data.matches is None and getattr(data, "match_count", None) != 0:
                raise AIOUnavailable("AIO grep omitted matches")
            result.matches = [
                {"path": self.normalize_path(m.file), "line": m.line_number, "text": m.line_content}
                for m in data.matches or []
            ]
            result.truncated = bool(getattr(data, "truncated", False))
            if max_count is not None and len(result.matches) > max_count:
                result.matches = result.matches[:max_count]
                result.truncated = True
        return result

    def delete(self, file_path: str) -> DeleteResult:
        path = self.normalize_path(file_path)
        result = self.execute(delete_command(path))
        return (
            DeleteResult(path=path)
            if result.exit_code == 0
            else DeleteResult(error=result.output or "AIO delete failed")
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for original, content in files:
            result = FileUploadResponse(path=original)
            try:
                path = self.normalize_path(original)
                with self._operation(result):
                    self._data(
                        self.client.file.write_file(
                            file=path,
                            content=base64.b64encode(content).decode("ascii"),
                            encoding="base64",
                        )
                    )
            except Exception as error:
                result.error = "invalid_path" if isinstance(error, ValueError) else "AIO upload result not confirmed"
            results.append(result)
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for original in paths:
            result = FileDownloadResponse(path=original)
            try:
                path = self.normalize_path(original)
                with self._operation(result):
                    result.content = b"".join(self.client.file.download_file(path=path))
            except Exception as error:
                result.error = "invalid_path" if isinstance(error, ValueError) else "AIO download result not confirmed"
            results.append(result)
        return results

    def begin_close(self):
        """Stop admission immediately, before waiting for an existing operation's lock."""
        self._closing.set()

    def close(self):
        self.begin_close()
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
            try:
                response = self.client.shell.cleanup_session(session_id=self.session_id)
                if getattr(response, "success", None) is not True:
                    raise AIOUnavailable("AIO shell cleanup was not confirmed")
            except Exception as error:
                if not isinstance(error, ApiError) or error.status_code != 404:
                    self._state = "cleanup_failed"
                    raise AIOUnavailable(
                        "AIO shell cleanup failed; inspect the old session before discarding its ID"
                    ) from None
            self._state = "closed"
