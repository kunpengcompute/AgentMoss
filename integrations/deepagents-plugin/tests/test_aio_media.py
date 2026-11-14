"""Read media through the real SDK and DeepAgents tool-message formatter."""

import asyncio
import base64

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from models import call
from test_aio_backend import sdk_backend as sdk_backend
from test_native_sdk import make_agent, run_args, setup

from agentmoss_deepagents.aio_errors import AIOExecutionUncertain, AIOUnavailable


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("name,content", [
    ("pixel.PNG", b"\x89PNG\r\n\x1a\n\xff"),
    ("document.pdf", b"%PDF-1.4\nASCII document\n%%EOF\n"),
    ("slides.pptx", b"PK\x03\x04\xff"),
])
async def test_media_tool_returns_exact_base64(tmp_path, sdk_backend, sync, name, content):
    backend, submitted, replies = sdk_backend

    def answer(request):
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=content)
        # Model the text API too, so the old implementation fails on behavior.
        return httpx.Response(200, json={"success": True, "data": {
            "file": "/home/gem/" + name, "content": content.decode("utf-8", errors="replace"),
        }})

    replies.append(answer)
    runtime, _ = setup(tmp_path)
    graph = make_agent(runtime, tmp_path, [call("read_file", {"file_path": "/home/gem/" + name}, "media"),
                                          AIMessage(content="done")], backend=backend)
    inputs, config, context = run_args()
    try:
        result = (await asyncio.to_thread(graph.invoke, inputs, config, context=context) if sync
                  else await graph.ainvoke(inputs, config, context=context))
        message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert message.status == "success"
        block = message.content_blocks[0]
        assert base64.b64decode(block["base64"], validate=True) == content
        assert submitted[0].url.path == "/v1/file/download"
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("name", [
    "photo.heic", "photo.heif", "video.mkv", "video.3gpp", "video.flv", "slides.ppt", "sound.aiff",
])
@pytest.mark.parametrize("offset,limit", [(10, 1), (0, 0)])
def test_media_is_not_line_sliced(sdk_backend, name, offset, limit):
    backend, submitted, replies = sdk_backend
    content = b"binary\ncontent\x00\xff"
    replies.append(httpx.Response(200, content=content))
    result = backend.read(name, offset=offset, limit=limit)
    assert result.error is None and result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"], validate=True) == content
    assert len(submitted) == 1 and submitted[0].url.path.endswith("/download")


def test_unknown_binary_extension_falls_back_after_decode_error(sdk_backend):
    backend, submitted, replies = sdk_backend
    replies.extend([
        httpx.Response(200, json={"success": False, "data": {
            "path": "/home/gem/data.bin", "operation": "read", "error_type": "decode_error",
            "message": "not UTF-8",
        }}),
        httpx.Response(200, content=b"\x00\xffbinary"),
    ])
    result = backend.read("data.bin")
    assert result.error is None and base64.b64decode(result.file_data["content"]) == b"\x00\xffbinary"
    assert [r.url.path for r in submitted] == ["/v1/file/read", "/v1/file/download"]


class MediaStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed, self.consumed = chunks, False, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def test_media_size_limit_closes_stream_without_disabling_session(sdk_backend, monkeypatch):
    from agentmoss_deepagents import aio_backend

    backend, _, replies = sdk_backend
    monkeypatch.setattr(aio_backend, "_MAX_MEDIA_BYTES", 8, raising=False)
    stream = MediaStream([b"a" * 9, b"must not be consumed"])
    replies.append(httpx.Response(200, stream=stream))
    result = backend.read("large.pdf")
    assert result.error and "size" in result.error.lower()
    assert stream.closed and stream.consumed == 1
    assert backend.status()["state"] == "ready"


@pytest.mark.parametrize("code,error", [(403, "permission_denied"), (404, "file_not_found")])
def test_media_rejection_is_recoverable(sdk_backend, code, error):
    backend, _, replies = sdk_backend
    replies.append(httpx.Response(code))
    assert backend.read("missing.png").error == error
    assert backend.status()["state"] == "ready"


def test_lost_media_response_closes_stream_and_quarantines(sdk_backend):
    backend, submitted, replies = sdk_backend
    stream = MediaStream([b"partial", httpx.ReadTimeout("secret-token")])
    replies.append(httpx.Response(200, stream=stream))
    with pytest.raises(AIOExecutionUncertain):
        backend.read("lost.pdf")
    assert stream.closed and backend.status()["state"] == "uncertain"
    with pytest.raises(AIOUnavailable):
        backend.read("another.pdf")
    assert len(submitted) == 1
