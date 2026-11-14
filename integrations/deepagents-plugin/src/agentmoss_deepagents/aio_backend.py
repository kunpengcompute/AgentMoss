"""AIO backend adapted from agent-infra/sandbox's Apache-2.0 example.

Upstream: 7f1afaf8d82bd30531a19caeb1a24dfebbc97d8c,
examples/langgraph-deepagents-integration/sandbox_backend.py.
Modified: modern DeepAgents results, explicit paths, no retries, lifecycle and
uncertain-execution handling. See THIRD_PARTY_NOTICES.md and LICENSES/.
"""

from __future__ import annotations

import asyncio
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
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from .aio_errors import AIOExecutionUncertain, AIOStartupError, AIOUnavailable
from .aio_paths import normalize_aio_path
from .backend_config import AIOBackendConfig, positive_timeout
from .runtime import drain


class _AIOFileFailure(AIOUnavailable):
    """Known structured file failure, including error unions returned with HTTP 200."""


class AIOSandboxBackend(BaseSandbox):
    """Use SDK I/O and BaseSandbox file algorithms; own one serialized shell session."""

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
            data = self._data(
                client.shell.create_session(
                    id=self.session_id,
                    exec_dir=self.working_dir,
                    request_options={"max_retries": 0},
                )
            )
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
        if getattr(response, "success", None) is not True or getattr(response, "data", None) is None:
            raise AIOUnavailable("AIO returned an invalid or unsuccessful response")
        data = response.data
        if getattr(data, "error_type", None) is not None:
            category = str(data.error_type).lower()
            errno_name = getattr(data, "errno_name", None)
            if category in {"file_not_found", "not_found"} or errno_name == "ENOENT":
                raise _AIOFileFailure("file_not_found")
            if category in {"permission_denied", "access_denied"} or errno_name in {"EACCES", "EPERM"}:
                raise _AIOFileFailure("permission_denied")
            raise _AIOFileFailure("AIO rejected the file operation")
        return data

    def _ready(self):
        if self._closing.is_set() or self._state != "ready":
            raise AIOUnavailable("AIO session is not ready; inspect and clean up the old session before recreating")

    @contextmanager
    def _operation(self):
        with self._lock:
            self._ready()
            try:
                yield
            except _AIOFileFailure:
                raise
            except ApiError as error:
                if error.status_code not in (403, 404):
                    self._state = "uncertain"
                raise
            except Exception:
                self._state = "uncertain"
                raise AIOExecutionUncertain(
                    "AIO file operation result is unknown; inspect the old session before retrying"
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
        with self._lock:
            self._ready()
            try:
                result = self.client.shell.exec_command(
                    command=command,
                    id=self.session_id,
                    exec_dir=self.working_dir,
                    strict=True,
                    async_mode=False,
                    timeout=limit + 5,
                    hard_timeout=limit,
                    request_options={"max_retries": 0, "timeout_in_seconds": limit + 15},
                )
                data = self._data(result)
                if data.status != "completed" or type(data.exit_code) is not int or not isinstance(data.output, str):
                    raise AIOExecutionUncertain("Incomplete AIO command result")
                return ExecuteResponse(
                    output=data.output,
                    exit_code=data.exit_code,
                    truncated=bool(getattr(data, "full_output_file_path", None)),
                )
            except ApiError as error:
                if error.status_code in (403, 409, 503):
                    # A session-busy response also means we cannot safely submit another command.
                    if error.status_code == 409:
                        self._state = "uncertain"
                    return ExecuteResponse(
                        output=f"AIO HTTP {error.status_code}: {self._redact(error.body)}", exit_code=126
                    )
                self._state = "uncertain"
                raise AIOExecutionUncertain(
                    "AIO command response unavailable; inspect the old session before retrying"
                ) from None
            except Exception:
                self._state = "uncertain"
                raise AIOExecutionUncertain(
                    "AIO execution state is unknown or timed out; "
                    "inspect and clean up the old session before recreating"
                ) from None

    def ls(self, path: str) -> LsResult:
        path = self.normalize_path(path)
        with self._operation():
            try:
                data = self._data(
                    self.client.file.list_path(path=path, include_size=True, request_options={"max_retries": 0})
                )
            except (ApiError, _AIOFileFailure) as error:
                return LsResult(error=self._file_error(error))
            entries = []
            if data.files is None:
                raise AIOUnavailable("AIO listing omitted files")
            for item in data.files:
                info = {"path": self.normalize_path(item.path), "is_dir": bool(item.is_directory)}
                if item.size is not None:
                    info["size"] = item.size
                if item.modified_time is not None:
                    info["modified_at"] = item.modified_time
                entries.append(info)
            return LsResult(entries=entries)

    def ls_info(self, path: str) -> list:
        result = self.ls(path)
        if result.error:
            raise AIOUnavailable(result.error)
        return result.entries

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        path = self.normalize_path(file_path)
        if type(offset) is not int or type(limit) is not int:
            raise ValueError("offset and limit must be integers")
        if limit <= 0:
            return ReadResult(file_data={"content": "", "encoding": "utf-8"}, no_lines_requested=True)
        with self._operation():
            try:
                data = self._data(
                    self.client.file.read_file(
                        file=path,
                        start_line=max(0, offset),
                        end_line=max(0, offset) + limit,
                        request_options={"max_retries": 0},
                    )
                )
            except (ApiError, _AIOFileFailure) as error:
                return ReadResult(error=self._file_error(error))
            if not isinstance(data.content, str):
                raise AIOUnavailable("AIO read omitted text content")
            return ReadResult(file_data={"content": data.content, "encoding": "utf-8"})

    def write(self, file_path: str, content: str) -> WriteResult:
        path = self.normalize_path(file_path)
        with self._operation():
            try:
                # Find is exact-match filtered: filenames containing glob metacharacters are not patterns here.
                found = self._data(
                    self.client.file.find_files(
                        path=posixpath.dirname(path),
                        glob=globlib.escape(posixpath.basename(path)),
                        request_options={"max_retries": 0},
                    )
                )
                if found.files is None:
                    raise AIOUnavailable("AIO existence check omitted files")
                if any(self.normalize_path(p) == path for p in found.files):
                    return WriteResult(error="File already exists")
                self._data(self.client.file.write_file(file=path, content=content, request_options={"max_retries": 0}))
                return WriteResult(path=path)
            except (ApiError, _AIOFileFailure) as error:
                return WriteResult(error=self._file_error(error))

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        path = self.normalize_path(file_path)
        if not old_string:
            return EditResult(error="old_string must not be empty")
        with self._operation():
            try:
                values = {
                    "command": "str_replace",
                    "path": path,
                    "old_str": old_string,
                    "new_str": new_string,
                    "request_options": {"max_retries": 0},
                }
                if replace_all:
                    values["replace_mode"] = "ALL"
                data = self._data(self.client.file.str_replace_editor(**values))
                if getattr(data, "error", None):
                    return EditResult(error=self._redact(data.error))
                old = getattr(data, "old_content", None)
                return EditResult(path=path, occurrences=old.count(old_string) if isinstance(old, str) else None)
            except (ApiError, _AIOFileFailure) as error:
                # AIO uses structured HTTP errors for replacement mismatch too; surface, do not claim not-found.
                return EditResult(error=self._file_error(error, edit=True))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        path = self.normalize_path(self.working_dir if path is None else path)
        with self._operation():
            return super().glob(pattern, path)

    def glob_info(self, pattern: str, path: str = "/") -> list:
        result = self.glob(pattern, path)
        if result.error:
            raise AIOUnavailable(result.error)
        return result.matches

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        path = self.normalize_path(self.working_dir if path is None else path)
        if max_count is not None and (type(max_count) is not int or max_count < 1):
            raise ValueError("max_count must be a positive integer")
        # AIO's PTY removes the NUL separators used by BaseSandbox's shell parser.
        options = {
            "path": path,
            "pattern": pattern,
            "fixed_strings": True,
            "recursive": True,
            "request_options": {"max_retries": 0},
        }
        if glob is not None:
            options["include"] = [glob]
        if self.grep_exclude_dirs:
            options["exclude"] = list(self.grep_exclude_dirs)
        if max_count is not None:
            options["max_results"] = max_count + 1
        with self._operation():
            try:
                data = self._data(self.client.file.grep_files(**options))
            except (ApiError, _AIOFileFailure) as error:
                return GrepResult(error=self._file_error(error))
            if data.matches is None:
                if getattr(data, "match_count", None) != 0:
                    raise AIOUnavailable("AIO grep omitted matches")
                matches = []
            else:
                matches = [
                    {"path": self.normalize_path(m.file), "line": m.line_number, "text": m.line_content}
                    for m in data.matches
                ]
            truncated = bool(getattr(data, "truncated", False))
            if max_count is not None and len(matches) > max_count:
                matches = matches[:max_count]
                truncated = True
            return GrepResult(matches=matches, truncated=truncated)

    def grep_raw(self, pattern: str, path: str | None = None, glob: str | None = None):
        result = self.grep(pattern, path, glob)
        return result.error if result.error else result.matches

    def delete(self, file_path: str) -> DeleteResult:
        import shlex

        path = self.normalize_path(file_path)
        result = self.execute("rm -r -- " + shlex.quote(path))
        return (
            DeleteResult(path=path)
            if result.exit_code == 0
            else DeleteResult(error=result.output or "AIO delete failed")
        )

    def _file_error(self, error, *, edit: bool = False) -> str:
        if isinstance(error, _AIOFileFailure):
            return str(error)
        if error.status_code == 404:
            return "file_not_found"
        if error.status_code == 403:
            return "permission_denied"
        if edit and error.status_code in (400, 409, 422):
            return "AIO edit rejected: " + self._redact(error.body)
        raise AIOUnavailable("AIO file service unavailable or returned an unexpected response") from None

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for original, content in files:
            try:
                path = self.normalize_path(original)
                with self._operation():
                    self._data(
                        self.client.file.write_file(
                            file=path,
                            content=base64.b64encode(content).decode("ascii"),
                            encoding="base64",
                            request_options={"max_retries": 0},
                        )
                    )
                results.append(FileUploadResponse(path=original))
            except Exception as error:
                results.append(FileUploadResponse(path=original, error=self._transfer_error(error)))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for original in paths:
            try:
                path = self.normalize_path(original)
                with self._operation():
                    content = b"".join(self.client.file.download_file(path=path, request_options={"max_retries": 0}))
                results.append(FileDownloadResponse(path=original, content=content))
            except Exception as error:
                results.append(FileDownloadResponse(path=original, error=self._transfer_error(error)))
        return results

    def _transfer_error(self, error: Exception) -> str:
        if isinstance(error, ValueError):
            return "invalid_path"
        if isinstance(error, (ApiError, _AIOFileFailure)):
            try:
                return self._file_error(error)
            except AIOUnavailable:
                pass
        return "AIO file transfer failed; result not confirmed"

    # BaseSandbox's async methods bypass the SDK/path overrides below.
    async def _async(self, method, *args, **kwargs):
        return await drain(asyncio.to_thread(method, *args, **kwargs), propagate_cancel=True)

    async def aexecute(self, command: str, *, timeout: int | None = None):
        return await self._async(self.execute, command, timeout=timeout)

    async def als(self, path: str):
        return await self._async(self.ls, path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        return await self._async(self.read, file_path, offset, limit)

    async def awrite(self, file_path: str, content: str):
        return await self._async(self.write, file_path, content)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        return await self._async(self.edit, file_path, old_string, new_string, replace_all)

    async def aglob(self, pattern: str, path: str | None = None):
        return await self._async(self.glob, pattern, path)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ):
        return await self._async(self.grep, pattern, path, glob, max_count=max_count)

    async def adelete(self, file_path: str):
        return await self._async(self.delete, file_path)

    async def aupload_files(self, files: list[tuple[str, bytes]]):
        return await self._async(self.upload_files, files)

    async def adownload_files(self, paths: list[str]):
        return await self._async(self.download_files, paths)

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
                response = self.client.shell.cleanup_session(
                    session_id=self.session_id, request_options={"max_retries": 0}
                )
                if getattr(response, "success", None) is not True:
                    raise AIOUnavailable("AIO shell cleanup was not confirmed")
            except ApiError as error:
                if error.status_code != 404:
                    self._state = "cleanup_failed"
                    raise AIOUnavailable(
                        "AIO shell cleanup failed; inspect the old session before discarding its ID"
                    ) from None
            except Exception:
                self._state = "cleanup_failed"
                raise AIOUnavailable(
                    "AIO shell cleanup failed; inspect the old session before discarding its ID"
                ) from None
            self._state = "closed"
