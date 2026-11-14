"""One shared run context for every instrumented graph in a root invocation."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from langgraph.config import get_config

from .history import KEY, RunHistory
from .runtime import GateRuntime

_RUN_KEY = "_agentmoss_run"


@dataclass(frozen=True)
class RunContext:
    owner: dict
    session_id: str
    user_prompt: str
    turn_id: str
    history: RunHistory

    @property
    def gate_session_id(self) -> str:
        return "deepagents:" + self.session_id


def moss_info(context: object, thread_id: str | None = None) -> dict:
    info = context.get("moss_info") if isinstance(context, Mapping) else getattr(context, "moss_info", None)
    if not isinstance(info, dict):
        raise ValueError("Official runtime context.moss_info must be a dictionary")
    session, prompt = info.get("session_id"), info.get("user_prompt")
    if not isinstance(session, str) or not session.strip() or not isinstance(prompt, str):
        raise ValueError("moss_info requires a nonempty session_id and text user_prompt")
    if thread_id is not None and str(thread_id) != session:
        raise ValueError("moss_info.session_id must match configurable.thread_id")
    return info


def current_run(context: object, thread_id: str | None = None) -> RunContext:
    info = moss_info(context, thread_id)
    run = info.get(_RUN_KEY)
    if not isinstance(run, RunContext) or run.owner is not info:
        raise ValueError("AgentMossMiddleware.before_agent has not initialized this run")
    if run.session_id != info["session_id"] or run.user_prompt != info["user_prompt"]:
        raise ValueError("moss_info changed during execution")
    return run


def begin_run(runtime_context: object, state: dict, gate_runtime: GateRuntime) -> RunContext:
    return ensure_run(runtime_context, state, gate_runtime, new_turn=True)


def ensure_run(
    runtime_context: object, state: dict, gate_runtime: GateRuntime, *, new_turn: bool = False
) -> RunContext:
    """Restore native continuations without requiring before_agent to run again."""
    thread_id = get_config().get("configurable", {}).get("thread_id")
    info = moss_info(runtime_context, thread_id)
    if _RUN_KEY in info:
        run = current_run(runtime_context, thread_id)
        run.history.merge(state.get(KEY, {}))
        return run
    history = RunHistory(info["session_id"], state.get(KEY, {}), gate_runtime.config)
    current = history.value["current_turn"]
    if not new_turn and current is None:
        raise ValueError("Cannot restore an AgentMoss run without a checkpoint turn")
    if not new_turn:
        prompt = next((turn for turn in history.value["turns"] if turn["id"] == current["id"]), None)
        if prompt is not None and prompt["text"] != gate_runtime.redact(info["user_prompt"]):
            raise ValueError("Resumed moss_info.user_prompt must match the original turn")
    run = RunContext(
        owner=info,
        session_id=info["session_id"],
        user_prompt=info["user_prompt"],
        turn_id=str(uuid4()) if new_turn else current["id"],
        history=history,
    )
    if new_turn:
        run.history.record_prompt(run.turn_id, gate_runtime.redact(run.user_prompt))
    info[_RUN_KEY] = run
    return run
