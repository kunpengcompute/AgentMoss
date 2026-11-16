"""Check AIO shell submissions with the in-process AgentMoss SDK."""

import asyncio
import hashlib
import logging
import os
import threading
import time

# This image deliberately uses the built-in static rules, without a model service.
os.environ["AGENT_MOSS_DISABLE_LLM"] = "1"
os.environ["AGENT_MOSS_RULES_MODE"] = "builtin"

from agent_moss.engine.coordinator import judge_security  # noqa: E402
from agent_moss.infra.config import Config  # noqa: E402
from agent_moss.profiles import get_profile  # noqa: E402
from fastapi import HTTPException  # noqa: E402

logger = logging.getLogger(__name__)
PROFILE = get_profile("linux")
CONFIG = Config()
SDK_LOCK = threading.Lock()


def _judge(command, cwd, history):
    # The SDK shares rule caches. Serialize analysis across shell sessions.
    with SDK_LOCK:
        return judge_security(
            prompt_session="",
            action_history=history,
            a_next={"action_type": "bash", "action_detail": command},
            reason="",
            config=CONFIG,
            profile=PROFILE,
            cwd=cwd,
        )


# Imported by shell.py during service startup, before accepting requests.
if _judge("true", "/tmp", []).allowed is not True:
    raise RuntimeError("AgentMoss startup check failed")


def _session_lock(session):
    lock = getattr(session, "_moss_lock", None)
    if lock is None:
        # No await between reading and assigning: one lock per AIO event-loop session.
        lock = session._moss_lock = asyncio.Lock()
    return lock


async def _check_locked(session, command, working_dir, *, record):
    if getattr(session, "has_active_command", False) or getattr(session, "active_command_count", 0):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENTMOSS_SESSION_BUSY",
                "reason": "Wait for the active command before submitting another command",
            },
        )
    cwd = working_dir or getattr(getattr(session, "bash_session", None), "cwd", None) or session.working_dir
    history = getattr(session, "_moss_history", [])
    started = time.monotonic()
    try:
        judgment = await asyncio.to_thread(_judge, command, cwd, list(history))
        allowed = judgment.allowed is True
        detail = {
            "code": "AGENTMOSS_DENIED",
            "decision": "Allow" if allowed else "Deny",
            "reason": judgment.reason,
            "source": judgment.source,
            "violated_layers": judgment.violated_layers,
            "session_id": session.id,
        }
    except Exception:
        logger.exception("AgentMoss check failed; command not submitted")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENTMOSS_UNAVAILABLE",
                "reason": "Security check failed; command not submitted",
            },
        ) from None
    logger.info(
        "agentmoss decision=%s session=%s command_sha256=%s elapsed_ms=%.2f source=%s",
        detail["decision"],
        session.id,
        hashlib.sha256(command.encode()).hexdigest(),
        (time.monotonic() - started) * 1000,
        judgment.source,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=detail)
    if record:
        # Allowed submission history, not a record of successful completion.
        session._moss_history = (history + [{"action_type": "bash", "action_detail": command}])[-128:]


async def check_command(session, command, working_dir):
    """SSE preflight before response headers; actual execution must check again."""
    async with _session_lock(session):
        await _check_locked(session, command, working_dir, record=False)


async def execute_checked(session, command, working_dir, submit, /, *args, **kwargs):
    """Keep the session locked from analysis through the manager's submission."""
    async with _session_lock(session):
        await _check_locked(session, command, working_dir, record=True)
        return await submit(*args, **kwargs)
