# Deep Agents 接入 AgentMoss

插件在 Deep Agents 调用工具前，把工具名、参数、当前用户请求和会话历史发给 AgentGate。AgentGate 向 AgentMoss 获取判定。收到 `Allow` 后，工具才会执行。收到 `Deny` 时，Agent 会看到拒绝原因。请将这两个服务部署在可以接收应用数据的环境中。

下面以工具在应用所在机器上执行为例。运行位置由 `backend` 决定。本地模式使用应用进程的系统权限，工作目录只决定命令从哪里开始执行。需要让命令在沙箱里执行时，见 [CubeSandbox 接入](../cubesandbox/README.md)。

## 安装

先按[项目快速开始](../../../README.md#快速开始)启动 AgentMoss 和 AgentGate，并确认一次分析请求可以成功返回。在 Deep Agents 应用所在的机器上，从本仓库根目录执行以下命令。插件要求 Python 3.11 及以上，以下插件要求 Python 3.11 及以上，示例使用 Linux、uv 和 Python 3.12。

```bash
uv venv --python 3.12 .venv-deepagents
uv pip install --python .venv-deepagents/bin/python './AgentMoss[deepagents-aio]'
source .venv-deepagents/bin/activate
```

已有 Deep Agents 应用时，在它的虚拟环境中安装同一个组件即可。`deepagents-aio` 包含本地和沙箱两种执行方式的依赖。AgentGate 服务使用自己的虚拟环境。如果应用曾单独安装过 `agentmoss-deepagents`，请在新的应用环境里按上面的命令安装，避免两个发行包同时提供 `agentmoss_deepagents` 模块。

## 连接 AgentGate

在启动应用的终端设置 Gate 地址和令牌文件。以下路径适用于 Linux 上同一用户启动的默认部署。

```bash
export DEEPAGENTS_GATE_URL=http://127.0.0.1:9100
export DEEPAGENTS_GATE_API_TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/agentgate/auth_token"
export DEEPAGENTS_GATE_TIMEOUT_SECONDS=30
```

应用与 AgentGate 分开部署时，填写应用可以访问的 Gate 地址，以及**应用机器上**存放 Gate 令牌的文件路径。通过环境注入令牌时，改用 `DEEPAGENTS_GATE_API_TOKEN`。两种令牌设置只选一种。示例给一次送检预留 30 秒，请根据服务实际耗时调整。详见[服务与凭据配置](docs/deployment.md)。

## 在应用中接入

在原有的 `create_deep_agent()` 调用中使用插件提供的 `backend` 和 `middleware`。下面的函数可放进已有应用。首次接入时，也可以将它保存为仓库根目录下的 `deep_agent.py`。调用方传入支持工具调用的 `model`、用于保存对话状态的 `checkpointer`、已存在的工作目录 `workspace`、本次对话编号和用户原文。

```python
import asyncio
from pathlib import Path

from deepagents import create_deep_agent
from agentmoss_deepagents import DeepAgentsIntegration


async def ask(model, checkpointer, workspace, session_id, user_text):
    integration = await asyncio.to_thread(
        DeepAgentsIntegration.load,
        {"backend": "local", "local": {"root_dir": str(Path(workspace).resolve())}},
    )
    try:
        agent = create_deep_agent(
            model=model,
            checkpointer=checkpointer,
            backend=integration.backend,
            middleware=[integration.middleware],
            context_schema=dict,
            subagents=[{
                "name": "general-purpose",
                "description": "通用子代理",
                "middleware": [integration.middleware],
            }],
        )
        return await agent.ainvoke(
            {"messages": [("user", user_text)]},
            {"configurable": {"thread_id": session_id}},
            context={"moss_info": {"session_id": session_id, "user_prompt": user_text}},
        )
    finally:
        await integration.aclose()
```

### 发起第一次工具调用

如果已经有模型和 checkpointer，可直接调用上面的 `ask()`。从零开始时，可用 Deep Agents 自带的 Anthropic 适配器，并把下面的入口追加到 `deep_agent.py` 末尾。

```python
if __name__ == "__main__":
    import os
    from uuid import uuid4

    from langchain_anthropic import ChatAnthropic
    from langgraph.checkpoint.memory import InMemorySaver

    workspace = Path("agent-workspace").resolve()
    workspace.mkdir(exist_ok=True)
    session_id = uuid4().hex
    model = ChatAnthropic(model=os.environ["ANTHROPIC_MODEL"])
    result = asyncio.run(ask(
        model, InMemorySaver(), workspace, session_id,
        "请调用 execute 执行 pwd，并告诉我返回的工作目录。",
    ))
    print(result["messages"][-1].text)
    print(f"Gate 会话编号为 deepagents:{session_id}")
```

在已经激活应用环境并设置 Gate 变量的 Bash 终端运行。输入你账户可用、支持工具调用的 Anthropic 模型 ID 及其 API Key。Key 输入时不会显示。

```bash
read -r -p 'Anthropic 模型 ID：' ANTHROPIC_MODEL
read -r -s -p 'Anthropic API Key：' ANTHROPIC_API_KEY && echo
export ANTHROPIC_MODEL ANTHROPIC_API_KEY
python deep_agent.py
```

如果模型按请求调用了 `execute`，工具会返回本机的 `agent-workspace` 绝对路径。用程序输出的会话编号在 AgentGate 看板找到这条记录，核对命令和判定。登录方式见 [AgentGate 使用说明](../../../AgentGate/README.md)。

### 在应用中复用

`InMemorySaver` 只保留当前进程中的状态。已有应用可传入自己的持久化 checkpointer。继续一段对话时使用同一个 `session_id`，同一会话的调用按顺序执行。每次调用都由应用新建 `moss_info`，其中的 `session_id` 与 `thread_id` 相同。

主代理和每个隔离的子代理都要注册中间件，包括默认的 `general-purpose`。应用自行编译的子代理图也需要绑定对应的 backend 和中间件。

长期运行的服务可以在启动时创建一次 integration 和 agent，复用到退出前。先等待进行中的请求结束，再调用 `await integration.aclose()`。示例代码见[本地执行](examples/local.py)和[沙箱执行](examples/aio.py)。

## 自定义工具与审批

将自定义工具传入 `create_deep_agent(tools=[...])` 后，该工具和内置工具一样经过中间件。需要它的子代理也要在自己的 `tools` 中注册。插件把 `execute` 转成 `bash` 动作，把 `read_file` 转成 `read` 动作。普通自定义工具按工具名和参数 JSON 送检，函数内部的操作不会单独送检。直接调用 backend 方法也不会触发中间件。

使用 Deep Agents 的人工审批时，为 agent 配置 checkpointer 和需要审批的工具，例如 `interrupt_on={"execute": True}`。恢复调用时，继续使用原来的会话编号和本轮用户原文，为 `moss_info` 创建新字典。以下为单个待审批动作的恢复参数。

```python
from langgraph.types import Command

result = await agent.ainvoke(
    Command(resume={"decisions": [{"type": "approve"}]}),
    {"configurable": {"thread_id": session_id}},
    context={"moss_info": {"session_id": session_id, "user_prompt": user_text}},
)
```

多个待审批动作按 SDK 的顺序逐个提供决定。审批修改过的工具参数也要经过 Gate 检查。能改写工具参数的中间件，以及按调用审批的 `HumanInTheLoopMiddleware(interrupt_mode="per_call", ...)`，都应排在插件中间件之前。

## 连接故障与会话历史

Gate 请求默认在 5 秒后超时，可用 `DEEPAGENTS_GATE_TIMEOUT_SECONDS` 调整。默认 `DEEPAGENTS_GATE_FAIL_CLOSED=true`，连接失败、鉴权失败或响应缺少有效判定时会阻止执行。显式设为 `false` 会在 Gate 故障时放行。Gate 已返回的 `Deny` 始终生效。

插件与应用的 checkpointer 一起保存工具调用历史，默认最多保留 200 个动作、20 次用户输入和 32 KiB 历史数据。历史被裁剪后，默认策略会停止后续工具调用。遇到 `history_truncated`，请先核对该会话的审计记录和已经执行的动作。使用 `fail_closed=false` 时，后续判定只能参考保留下来的历史。

从使用 v3/v4 历史格式的旧版本升级前，先结束活动请求并备份 checkpoint。已完成的会话会自动转换为 v5。有 `attempted`、`unknown` 或 `interrupted` 记录的会话，需要先核对未完成的调用。回退到旧版本时使用升级前的备份。

如需把设置放进应用配置，可在 `DeepAgentsIntegration.load()` 的配置字典中传入 `gate`，字段包括 `gate_url`、`api_token_file`、`timeout_seconds` 和 `fail_closed`。传入 `gate` 后，插件只读取该字典。省略它才读取 `DEEPAGENTS_GATE_*` 环境变量。完整选项见[插件配置](src/agentmoss_deepagents/config.py)。
