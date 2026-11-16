"""Patch the inspected AIO 1.11.0 shell API; reject any different upstream file."""

import hashlib
import sys
from pathlib import Path

EXPECTED_SHA256 = "14c8cc8616fcbe250cac781f430c81cbaf3bf56f23868996b289460b1fd6ad5a"


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("AIO shell.py does not contain the expected unique patch location")
    return source.replace(old, new, 1)


def patched_source(original):
    if hashlib.sha256(original).hexdigest() != EXPECTED_SHA256:
        raise ValueError("AIO shell.py SHA-256 differs from the inspected AIO 1.11.0 source; refusing to patch")
    source = original.decode("utf-8")
    source = replace_once(
        source,
        "from app.utils import validate_cwd",
        "from app.utils import validate_cwd\nfrom app.api.v1.moss_guard import check_command, execute_checked",
    )
    marker = (
        '    """\n    FIXME: sse output bug: output will stuck sse pipeline finished\n    """\n    # 支持 SSE 流式输出'
    )
    if source.count(marker) != 1:
        raise ValueError("AIO shell.py execution entry differs from the inspected source")
    prefix, rest = source.split(marker, 1)
    end = rest.index("\n\n@router.post(")
    body, following = rest[:end], rest[end:]
    if body.count("terminal_manager.execute_command(") != 4:
        raise ValueError("AIO shell.py execution branches differ from the inspected source")
    body = body.replace("terminal_manager.execute_command(", "submit_checked(")
    body = replace_once(
        body,
        "                except Exception as exc:\n                    yield _sse_data({'error': str(exc)})",
        "                except HTTPException as exc:\n"
        "                    yield _sse_data({'error': exc.detail, 'status_code': exc.status_code})\n"
        "                    return\n"
        "                except Exception as exc:\n                    yield _sse_data({'error': str(exc)})",
    )
    wrapper = (
        "    async def submit_checked(*args, **kwargs):\n"
        "        return await execute_checked(\n"
        "            session, request.command, working_dir,\n"
        "            terminal_manager.execute_command, *args, **kwargs,\n"
        "        )\n\n"
        "    if accept and 'text/event-stream' in accept:\n"
        "        await check_command(session, request.command, working_dir)\n\n"
    )
    source = prefix + wrapper + marker + body + following
    source = replace_once(
        source,
        "    await terminal_manager.execute_command(request.id, request.input, async_mode=True)",
        "    await execute_checked(\n"
        "        session, request.input, None, terminal_manager.execute_command,\n"
        "        request.id, request.input, async_mode=True,\n"
        "    )",
    )
    compile(source, "patched AIO shell.py", "exec")
    return source


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: patch_shell.py /path/to/app/api/v1/shell.py")
    path = Path(sys.argv[1])
    try:
        result = patched_source(path.read_bytes())
    except (ValueError, SyntaxError) as error:
        raise SystemExit(str(error)) from None
    path.write_text(result, encoding="utf-8")
    print("Patched AIO shell.py SHA-256:", hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
