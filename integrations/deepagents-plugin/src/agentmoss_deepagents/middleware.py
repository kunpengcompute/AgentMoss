"""Bridge DeepAgents tool calls to Gate; security verdicts belong to Gate/Moss."""

import asyncio
import json
import logging
import platform
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage, convert_to_messages
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import Command

from . import __version__
from .actions import ActionAdapter
from .client import GateUnavailable
from .context import RunContext, begin_run, current_run, ensure_run, moss_info
from .history import KEY, AgentMossState, make_event_id
from .operation import async_tool_operation, tool_operation
from .runtime import GateRuntime, drain

logger = logging.getLogger(__name__)

ToolResult = ToolMessage | Command
ToolHandler = Callable[[ToolCallRequest], ToolResult]
AsyncToolHandler = Callable[[ToolCallRequest], Awaitable[ToolResult]]


class AgentMossMiddleware(AgentMiddleware):
    state_schema = AgentMossState

    def __init__(self, runtime: GateRuntime, backend) -> None:
        self.runtime, self.adapter = runtime, ActionAdapter(backend)

    def before_agent(self, state: dict, runtime: Runtime) -> dict | None:
        if self.runtime.closing:
            raise RuntimeError("GateRuntime is closed")
        if not self.runtime.config.enabled:
            return None
        run = begin_run(runtime.context, state, self.runtime)
        return {KEY: run.history.value}

    async def abefore_agent(self, state: dict, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    def after_agent(self, state: dict, runtime: Runtime) -> dict | None:
        if not self.runtime.config.enabled:
            return None
        run = ensure_run(runtime.context, state, self.runtime)
        run.history.merge(state.get(KEY, {}))
        return {KEY: run.history.value}

    async def aafter_agent(self, state: dict, runtime: Runtime) -> dict | None:
        return self.after_agent(state, runtime)

    def _run(self, request: ToolCallRequest) -> RunContext:
        # Validate the explicit tool runtime thread before restoring from its
        # state. An existing context still undergoes the ownership checks below.
        info = moss_info(request.runtime.context, request.runtime.config.get("configurable", {}).get("thread_id"))
        if "_agentmoss_run" not in info:
            ensure_run(request.runtime.context, request.state, self.runtime)
        return current_run(request.runtime.context, request.runtime.config.get("configurable", {}).get("thread_id"))

    def _error(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        return ToolMessage(
            content=self.runtime.redact("AgentMoss: " + reason)[:2000],
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )

    def _result(self, request: ToolCallRequest, result: ToolResult) -> Command:
        view = self._run(request).history
        if isinstance(result, Command):
            updates = self._updates(result)
            for key, value in updates:
                if key == KEY:
                    view.merge(value)
            if result.update is None or isinstance(result.update, dict):
                update = {**(result.update or {}), KEY: view.value}
            else:
                # Keep duplicate update keys/order and parent routing intact.
                update = [*updates, (KEY, view.value)]
            return replace(result, update=update)
        return Command(update={"messages": [result], KEY: view.value})

    def _integration_error(self, request: ToolCallRequest, error: Exception) -> ToolResult:
        message = self._error(request, "local integration error: " + str(error))
        try:
            _ = self._run(request)
        except ValueError:
            return message
        # Keep the attempted/unknown call even when the handler raises before
        # returning a normal ToolMessage; never copy its error body into context.
        return self._result(request, message)

    def _prepare(self, request: ToolCallRequest) -> tuple[dict, dict] | Command:
        run = self._run(request)
        view = run.history
        execution_info = getattr(request.runtime, "execution_info", None)
        event_id = make_event_id(
            run.session_id, getattr(execution_info, "checkpoint_ns", None), request.tool_call["id"]
        )
        if request.state.get(KEY):
            view.merge(request.state[KEY])
        name, args = request.tool_call["name"], request.tool_call["args"]
        action = self.adapter.adapt(name, args)
        # Context carries previous tool calls, not inferred successful reads.
        snapshot = view.snapshot()
        event = view.begin(
            run,
            request.tool_call["id"],
            replace(
                action, command=self.runtime.redact(action.command), metadata=self.runtime.redact_value(action.metadata)
            ),
            name,
            event_id=event_id,
        )
        try:
            messages = request.state.get("messages", [])
            reason = next(
                (
                    m.content
                    for m in reversed(messages)
                    if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content
                ),
                "",
            )
            payload = {
                "agent_type": "deepagents",
                "agent_version": "deepagents-plugin-" + __version__,
                "session_id": run.gate_session_id,
                "cwd": action.cwd,
                "tool_name": action.tool_name,
                "command": action.command,
                "description": reason,
                "user_prompt": self.runtime.redact(run.user_prompt),
                "action_history": snapshot["action_history"],
                "prompt_history": snapshot["prompt_history"],
                "metadata": {
                    **action.metadata,
                    "hostname": platform.node(),
                    "os_type": action.metadata.get("os_type", platform.system().lower()),
                    **(
                        {"client_os_type": platform.system().lower()} if action.metadata.get("backend") == "aio" else {}
                    ),
                    "source": "deepagents-plugin",
                    "original_tool_name": name,
                    "tool_call_id": request.tool_call["id"],
                    "history_truncated": snapshot["history_truncated"],
                    "history_semantics": "tool_calls",
                    "history_statuses": snapshot["history_statuses"],
                    "history_arguments": snapshot["history_arguments"],
                },
            }
            if len(json.dumps(payload, ensure_ascii=False).encode()) > self.runtime.config.request_bytes:
                raise ValueError("Analysis request exceeds configured limit; action was not truncated")
            return event, payload
        except Exception as error:
            view.finish(event, "denied", decision="Deny", source="plugin")
            return self._result(request, self._error(request, str(error)))

    def _fault(self, error: GateUnavailable) -> dict:
        closed = self.runtime.config.fail_closed
        logger.warning("AgentGate unavailable (%s); %s", type(error).__name__, "fail_closed" if closed else "bypass")
        return {
            "decision": "Deny" if closed else "Bypass",
            "reason": "AgentGate unavailable",
            "source": "fail_closed" if closed else "bypass",
        }

    def _check(self, request: ToolCallRequest, event: dict, verdict: dict) -> Command | None:
        if verdict["decision"] == "Deny":
            self._run(request).history.finish(
                event, "denied", decision="Deny", source=verdict.get("source", "agent_moss")
            )
            return self._result(request, self._error(request, str(verdict.get("reason", "Rejected"))))
        return None

    def _record(
        self, request: ToolCallRequest, event: dict, result: ToolResult, verdict: dict, *, uncertain: bool = False
    ) -> Command:
        view = self._run(request).history
        if isinstance(result, Command):
            messages = []
            for key, value in self._updates(result):
                if key == KEY:
                    view.merge(value)
                elif key == "messages":
                    messages = value if isinstance(value, list) else [value]
        else:
            messages = [result]
        message = next(
            (
                item
                for item in reversed(convert_to_messages(messages))
                if isinstance(item, ToolMessage) and item.tool_call_id == request.tool_call["id"]
            ),
            None,
        )
        status = "unknown"
        if isinstance(message, ToolMessage) and not uncertain:
            if message.status == "error":
                status = "executed_error"
            elif request.tool_call["name"] == "execute":
                code = message.artifact.get("exit_code") if isinstance(message.artifact, dict) else None
                status = "unknown" if code is None else "executed_success" if code == 0 else "executed_error"
            else:
                status = "executed_success"
        view.finish(event, status, decision=verdict["decision"], source=verdict.get("source", "agent_moss"))
        return self._result(request, result)

    @staticmethod
    def _updates(result: Command) -> list[tuple[str, Any]]:
        if result.update is None:
            return []
        if isinstance(result.update, dict):
            return list(result.update.items())
        if isinstance(result.update, (list, tuple)):
            return list(result.update)
        raise ValueError("Unsupported tool Command update")

    @staticmethod
    def _policy_request(request: ToolCallRequest) -> ToolCallRequest:
        # Native batched HITL keeps the original AIMessage and stores edits in
        # graph state. Its inner tool wrapper substitutes them only at execution.
        # Inspect that same final action, leaving SDK execution/annotation intact.
        edited = request.state.get("hitl_edited_tool_calls") or {}
        action = edited.get(request.tool_call["id"])
        if action is None:
            return request
        if (
            not isinstance(action, dict)
            or not isinstance(action.get("name"), str)
            or not isinstance(action.get("args"), dict)
        ):
            raise ValueError("Invalid approved tool action")
        return request.override(tool_call={**request.tool_call, "name": action["name"], "args": action["args"]})

    def wrap_tool_call(self, request: ToolCallRequest, handler: ToolHandler) -> ToolResult:
        if self.runtime.closing:
            return self._error(request, "GateRuntime is closed")
        if not self.runtime.config.enabled:
            return handler(request)
        try:
            execution_request = request
            request = self._policy_request(request)
            view = self._run(request).history
            lock = nullcontext() if request.tool_call["name"] == "task" else view.leaf_lock()
            with lock:
                prepared = self._prepare(request)
                if isinstance(prepared, Command):
                    return prepared
                event, payload = prepared
                try:
                    verdict = self.runtime.client.analyze(payload)
                except GateUnavailable as error:
                    verdict = self._fault(error)
                if (denial := self._check(request, event, verdict)) is not None:
                    return denial
                try:
                    with tool_operation() as outcome:
                        result = handler(execution_request)
                except BaseException as error:
                    view.finish(
                        event,
                        "interrupted" if isinstance(error, GraphInterrupt) else "unknown",
                        decision=verdict["decision"],
                        source=verdict.get("source", "agent_moss"),
                    )
                    raise
                return self._record(request, event, result, verdict, uncertain=outcome.uncertain)
        except GraphBubbleUp:
            raise
        except Exception as error:
            return self._integration_error(request, error)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: AsyncToolHandler) -> ToolResult:
        if self.runtime.closing:
            return self._error(request, "GateRuntime is closed")
        if not self.runtime.config.enabled:
            return await handler(request)
        try:
            execution_request = request
            request = self._policy_request(request)
            view = self._run(request).history
            lock = nullcontext() if request.tool_call["name"] == "task" else view.async_leaf_lock()
            async with lock:
                prepared = self._prepare(request)
                if isinstance(prepared, Command):
                    return prepared
                event, payload = prepared
                try:
                    verdict = await self.runtime.client.aanalyze(payload)
                except GateUnavailable as error:
                    verdict = self._fault(error)
                except asyncio.CancelledError:
                    view.finish(event, "unknown", source="cancelled_analysis")
                    raise
                if (denial := self._check(request, event, verdict)) is not None:
                    return denial
                try:
                    async with async_tool_operation() as outcome:
                        if request.tool_call["name"] == "task":
                            result = await handler(execution_request)
                        else:
                            result = await drain(handler(execution_request), propagate_cancel=True)
                except BaseException as error:
                    view.finish(
                        event,
                        "interrupted" if isinstance(error, GraphInterrupt) else "unknown",
                        decision=verdict["decision"],
                        source=verdict.get("source", "agent_moss"),
                    )
                    raise
                return self._record(request, event, result, verdict, uncertain=outcome.uncertain)
        except GraphBubbleUp:
            raise
        except Exception as error:
            return self._integration_error(request, error)
