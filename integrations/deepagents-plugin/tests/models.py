"""Network-free models exercise the actual SDK graph and tool executors."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    seen_system: list[str] = []
    bound_tool_names: list[str] = []
    _index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self):
        return "agentmoss-scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.get("name", "") if isinstance(tool, dict) else tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_system.extend(str(message.content) for message in messages if message.type == "system")
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def call(name, args, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier, "type": "tool_call"}])
