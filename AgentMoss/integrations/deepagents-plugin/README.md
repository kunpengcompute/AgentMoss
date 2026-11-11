# AgentMoss

Moss 负责安全检测并返回判定，Gate 负责接收 Agent 请求、调用 Moss，并保存调用和判定记录。本插件将 DeepAgents 的工具调用发送给 Gate，根据返回的 Allow / Deny 执行或拒绝。

当前工具参数和模型调用说明按原文送检，Gate/Moss 需作为可信服务部署。

版本 0.5.1；需要 Python 3.12+、DeepAgents 0.7.32。完整依赖见 [pyproject.toml](pyproject.toml)。

## 安装与配置

插件要安装到**使用 DeepAgents 的 Python 项目**中。先进入你编写 Agent 调用代码的项目根目录，也就是该应用的 `pyproject.toml` 所在目录。

例如，应用项目在 `~/code/my-agent`，本仓库在 `~/code/boostkit/AgentMoss`：

```bash
# 1. 进入你的 DeepAgents 应用项目
cd ~/code/my-agent

# 2. 将本仓库中的插件安装为应用依赖
uv add ~/code/boostkit/AgentMoss/AgentMoss/integrations/deepagents-plugin
```

将上面两个路径换成实际位置。`uv add` 会将插件写入应用的 `pyproject.toml`，并安装到该项目的虚拟环境中。之后在应用项目目录使用 `uv run` 运行你的 DeepAgents 程序。

启动 Gate/Moss 后，配置 DeepAgents 进程的环境变量：

```bash
export DEEPAGENTS_GATE_URL=http://127.0.0.1:9100
export DEEPAGENTS_GATE_API_TOKEN='你的 Gate API Token'
export DEEPAGENTS_GATE_FAIL_CLOSED=true
```

已有令牌文件可设置 `DEEPAGENTS_GATE_API_TOKEN_FILE=/path/to/gate_token`，与 `DEEPAGENTS_GATE_API_TOKEN` 二选一。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPAGENTS_GATE_ENABLED` | true | 启用检查 |
| `DEEPAGENTS_GATE_URL` | http://127.0.0.1:9100 | Gate 地址 |
| `DEEPAGENTS_GATE_API_TOKEN` | 无 | Gate 令牌内容，与令牌文件二选一 |
| `DEEPAGENTS_GATE_API_TOKEN_FILE` | 无 | Gate 令牌文件路径，与令牌内容二选一 |
| `DEEPAGENTS_GATE_TIMEOUT_SECONDS` | 5 | 请求超时，单位秒 |
| `DEEPAGENTS_GATE_FAIL_CLOSED` | false | Gate 故障时拒绝执行；上例设为 true |

`DEEPAGENTS_GATE_FAIL_CLOSED=false` 时，Gate 故障会记录 Bypass 并继续执行；明确返回的 Deny 始终拦截。

也可复用应用配置：`GateRuntime.load(settings["agentgate"])`。字段使用 `gate_url`、`api_token`、`fail_closed` 等名称，见 [config.py](src/agentmoss_deepagents/config.py)。显式配置不与环境变量合并。

## 接入

以下使用应用已有的 `model`、`workspace` 和 `existing_checkpointer`：

```python
from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from agentmoss_deepagents import AgentMossMiddleware, GateRuntime

runtime = GateRuntime.load()
backend = LocalShellBackend(root_dir=workspace)
middleware = AgentMossMiddleware(runtime, backend)

agent = create_deep_agent(
    model=model,
    backend=backend,
    context_schema=dict,
    middleware=[middleware],
    checkpointer=existing_checkpointer,
    subagents=[{
        "name": "general-purpose",
        "description": "通用 subagent",
        "middleware": [middleware],
    }],
)

async def chat(session_id: str, user_text: str):
    return await agent.ainvoke(
        {"messages": [("user", user_text)]},
        {"configurable": {"thread_id": session_id}},
        context={"moss_info": {
            "session_id": session_id,
            "user_prompt": user_text,
        }},
    )
```

- **每个隔离 subagent 都要添加该中间件，包括 general-purpose。** 主agent 的自定义 Middleware 不会自动传给隔离 subagent。参考上述代码例子，在 `subagents` 中为每个 subagent 配置 `middleware=[middleware]`。
- 同一聊天使用相同 `session_id`，与 `thread_id` 一致；每次调用新建 `moss_info` 字典。Gate 会话名为 `deepagents:<session_id>`。
- 当前支持 LocalShellBackend；传给插件和实际工具的 backend 必须一致。

同步调用使用 `agent.invoke()`，参数相同。

## 自定义工具

### 定义和注册

用 `@tool` 定义工具，参数标注类型，文档字符串说明用途：

```python
from langchain.tools import tool

@tool
def add_numbers(a: int, b: int) -> int:
    """计算两个整数的和。"""
    return a + b
```

在上述接入示例的 `create_deep_agent()` 中增加 `tools=[add_numbers]`。如果 subagent 也需要这个工具，在对应配置中同样添加 `tools`：

```python
agent = create_deep_agent(
    model=model,
    backend=backend,
    context_schema=dict,
    tools=[add_numbers],
    middleware=[middleware],
    checkpointer=existing_checkpointer,
    subagents=[{
        "name": "general-purpose",
        "description": "通用 subagent",
        "tools": [add_numbers],
        "middleware": [middleware],
    }],
)
```

模型调用 `add_numbers` 时，插件先把工具名和参数送给 Gate；Allow 后执行函数，Deny 时不执行。工具内部调用的其他函数不会被自动拆成独立的送检请求。

### 传给 Moss 的参数

普通自定义工具无需手填 `action_type` 和 `action_detail`。例如调用 `add_numbers(a=1, b=2)`，插件将工具名和参数发给 Gate，Gate 转换成 Moss 的输入：

```json
{
  "a_next": {
    "action_type": "add_numbers",
    "action_detail": "{\"a\":1,\"b\":2}"
  }
}
```

会话编号、用户输入和调用历史由插件补充，`cwd` 取 backend 的工作目录。工具返回值不写入 `action_detail`。

目前只对 `execute`、`read_file`、`write_file` 等已知工具内置转换。其他工具默认使用“工具名＋参数 JSON”；例如自定义 `run_command(cmd=...)` 不会自动按 `bash` 送检，需要另做适配。

## 使用约定

- `moss_info` 由应用生成，调用历史由插件维护，不直接接受外部请求传入这两项。
- 同一会话的请求按顺序执行，等上一个请求结束后再开始下一个。
- 如果有修改工具参数的 Middleware，将它放在 AgentMossMiddleware 之前。
- 应用退出时，等请求结束后调用 `await runtime.aclose()`，释放 HTTP 连接。

## 会话历史与升级

同一会话的主agent 和 subagent 共用调用历史，由应用已有的 checkpointer 保存。插件不再记录主agent/subagent 的身份关系。

本版本使用 schema=4。已结束的 v3 历史会自动转换；升级前先结束请求，确认 checkpoint 没有待执行任务并备份。含 `attempted` 或 `unknown` 的旧历史需要先人工核对，插件会拒绝自动转换。旧插件不能读取新格式，回退时使用升级前备份或新会话。

## 测试

在插件目录执行：

```bash
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check src tests
```

两个 Moss 解析器测试需额外设置 `PYTHONPATH=/path/to/AgentMoss`，指向可导入 `agent_moss` 的源码目录。

[官方 Middleware 文档](https://docs.langchain.com/oss/python/langchain/middleware/custom)
