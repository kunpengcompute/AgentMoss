"""A fixed exclusive-create operation for AIO's official Python code API."""

import base64
import json

from .aio_errors import AIOUnavailable

_CREATE = """
errors = {
    errno.EEXIST: 'already_exists', errno.ENOENT: 'file_not_found',
    errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied',
    errno.ENOTDIR: 'invalid_target', errno.EISDIR: 'invalid_target',
    errno.EROFS: 'read_only_filesystem', errno.EINVAL: 'invalid_path',
}
try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stream = open(path, 'xb')
except OSError as error:
    if error.errno not in errors:
        raise
    print(json.dumps({'status': 'rejected', 'error': errors[error.errno]}))
else:
    # Once created, any write/close failure may have left partial content.
    # Do not turn it into a confirmed rejection or delete a replaced path.
    with stream:
        stream.write(content.encode('utf-8'))
    print(json.dumps({'status': 'created', 'path': path}))
"""

_REJECTIONS = {
    "already_exists", "file_not_found", "permission_denied", "invalid_target", "read_only_filesystem", "invalid_path",
}


def create_code(path: str, content: str) -> str:
    if not isinstance(content, str):
        raise ValueError("content must be text")
    content.encode("utf-8")  # Validate before any remote mutation.
    payload = base64.b64encode(json.dumps([path, content], ensure_ascii=True).encode()).decode("ascii")
    return (
        "import base64, json, os, errno\n"
        f"path, content = json.loads(base64.b64decode({payload!r}))\n"
        + _CREATE
    )


def creation_error(data, path: str) -> str | None:
    """Return a confirmed pre-creation rejection; reject all ambiguous replies."""
    if data.language != "python" or data.status != "ok" or data.exit_code not in (None, 0):
        raise AIOUnavailable("AIO atomic creation did not complete")
    outputs = data.outputs or []
    if data.traceback or any(output.get("output_type") == "error" for output in outputs):
        raise AIOUnavailable("AIO atomic creation failed")
    text = data.stdout
    if text is None:
        # Python runtimes can expose stdout as Jupyter stream outputs and have
        # no process exit code. The script's exact receipt is still required.
        text = "".join(
            output["text"] for output in outputs
            if output.get("output_type") == "stream" and output.get("name") == "stdout"
        )
    receipt = json.loads(text)
    if receipt == {"status": "created", "path": path}:
        return None
    if (
        isinstance(receipt, dict) and receipt.keys() == {"status", "error"}
        and receipt["status"] == "rejected" and receipt["error"] in _REJECTIONS
    ):
        return receipt["error"]
    raise AIOUnavailable("AIO atomic creation acknowledgement was invalid")
