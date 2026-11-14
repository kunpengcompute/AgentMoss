# AgentMoss DeepAgents 插件

Moss 负责安全检测并返回判定，Gate 负责接收 Agent 请求、调用 Moss，并保存调用和判定记录。本插件将 DeepAgents 的工具调用发送给 Gate，根据返回的 Allow / Deny 执行或拒绝。

当前工具参数和模型调用说明按原文送检，Gate/Moss 需作为可信服务部署。

## 安装

需要 Python 3.11 或更新版本。在运行 DeepAgents 的 Python 环境中安装：

```bash
python -m pip install 'agent-moss[deepagents-aio]'
```

也可以直接使用源码，将路径替换为实际的仓库目录：

```bash
python -m pip install -e '/path/to/AgentMoss[deepagents-aio]'
```

这两种方式都会安装插件所需的依赖，具体约束见 [pyproject.toml](pyproject.toml)。

如果环境中单独安装过 `agentmoss-deepagents`，先卸载该包，再按上面的方式安装，避免同名模块相互覆盖。

## 连接 Gate

先启动 Gate 和 Moss 服务，步骤见[部署说明](docs/deployment.md)。然后配置 DeepAgents 进程的环境变量：

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

## 配置选择 local / aio backend

CubeSandbox 的完整接入流程见 [AIO 接入说明](docs/cubesandbox.md)。

两种模式都使用 **AgentMossMiddleware → AgentGate → 外部 Moss Server**；只切换工具实际执行的位置。也可以按[手动接入](#手动接入本地执行)的方式创建 Gate 连接和检查组件。

安装命令已包含两种模式所需的依赖。`DeepAgentsIntegration.load()` 接收配置字典，以下为 aio 配置：

```python
import asyncio
from agentmoss_deepagents import DeepAgentsIntegration

integration = await asyncio.to_thread(DeepAgentsIntegration.load, {
    "backend": "aio",
    "gate": {
        "gate_url": "http://127.0.0.1:9100",
        "api_token_file": "/run/secrets/gate-token",
        "fail_closed": True,
    },
    "aio": {
        "proxy_url": "http://cubeproxy.example:8080",
        "sandbox_id": "your-existing-sandbox-id",
        "working_dir": "/home/gem",
        "default_timeout": 30,
        "hard_timeout": 30,
        # 沙箱未开启流量鉴权时省略此项。
        "traffic_token_file": "/run/secrets/cube-traffic-token",
    },
})
```

local 改为 `"backend": "local"`、`"local": {"root_dir": workspace, "virtual_mode": False}`，删除 aio 块，gate 块相同。不能同时提供两种 backend 配置。省略 gate 块时才调用原 GateConfig.from_env()；显式 gate 块不合并环境变量。

创建主代理和子代理：

```python
agent = create_deep_agent(
    model=model,
    backend=integration.backend,
    context_schema=dict,
    middleware=[integration.middleware],
    checkpointer=existing_checkpointer,
    subagents=[{
        "name": "general-purpose",
        "description": "通用子代理",
        "middleware": [integration.middleware],
    }],
)
```

DeepAgents 为声明式子代理配置同一个 backend；每个隔离子代理仍须显式添加本插件中间件。自定义编译好的子代理图需要由应用自己绑定对应 backend 和中间件。

调用 `agent.invoke()` / `agent.ainvoke()` 时，`thread_id` 与 `moss_info.session_id` 使用同一会话编号，参数写法见下方[调用示例](#手动接入本地执行)。应用退出前停止并等待所有图调用，再执行 `await integration.aclose()`。`DeepAgentsIntegration.load()` 是同步初始化入口，可能创建远端 shell 会话，异步应用如上在线程中初始化；初始化本身也应在退出前等待完成。

完整的应用函数见 [local 示例](examples/local.py) 和 [aio 示例](examples/aio.py)，model 和 checkpointer 由应用提供。示例不会创建额外模型客户端，也不负责创建或销毁 CubeSandbox 实例。

### AIO 路由、目录和凭据

- 默认发送 `Host: 8080-<sandbox_id>.cube.app`；其他 AIO 端口/域名可显式设置 `aio.route_host`。proxy_url 是客户端可访问的 CubeProxy HTTP(S) 地址。
- `traffic_token` / `traffic_token_file` 二选一，对应 `e2b-traffic-access-token`；Gate 凭据仍在 gate 块，两者不能混用。文件令牌在初始化时读取，不随每个命令重新加载。
- working_dir 是沙箱内的绝对 POSIX 路径。每个命令显式使用该目录并启用 strict；单独 `cd` 不会改变下一条请求的起始目录，`cd subdir && ...` 在当前命令内有效。环境变量仍可属于持久 shell 会话；不承诺会话重建后保留环境。
- DeepAgents 文件工具会先把显式路径规范化为根路径，例如 `foo.txt` 变成 `/foo.txt`，本插件在送检和执行中遵循相同规则；建议工具调用使用完整的沙箱绝对路径。只有直接调用低层 backend 方法时，普通相对路径才基于 working_dir。省略 glob/grep 的 path 使用 working_dir，显式 `/` 表示沙箱根目录。不通过客户端文件系统解析路径或符号链接，working_dir 也不是权限隔离根目录。
- 每实例创建独立 shell 会话并串行提交；主/子代理可共用。要并行执行独立任务，应用创建独立集成实例。更换沙箱不会自动清空逻辑会话历史；需要新会话时同时更换 thread_id/session_id。

### 两层检查与执行结果

外部 Gate Deny 时不会调用 AIO；外部 Allow 后，已接入 Moss 的镜像仍可拒绝命令。后者作为工具执行错误返回，历史保留外部 Allow，不冒充外部 Gate Deny。

正常完成返回 Bash 真实退出码。AIO HTTP 403、409、503 返回适配器生成的退出码 126，并保留脱敏原因；126 不是 Bash 真实执行结果。409 表明可能已有活动命令，因此该 backend 也停止接收后续操作，需先检查并处理旧会话。不能只看 ToolMessage.status，插件通过结构化 exit_code 记录成功或失败。

命令或文件操作响应丢失、网络超时、连接中断、running/未完成或硬超时不能当作成功：工具结果报告错误或 unknown，会话禁止继续提交操作，且插件不自动重试或切换到本地执行。应用先查询旧 shell 会话、确认旧命令状态并清理旧会话，再创建新实例；仅创建新会话并不意味着旧命令已经停止。关闭失败要保留沙箱和 shell_session_id 以便人工处理。若初始化失败，`AIOStartupError` 提供 `sandbox_id`、`session_id`、`cleanup_confirmed` 和脱敏的失败类型；清理未确认时必须先检查该会话，不能假定已关闭。

读取、写入、编辑、目录列表、grep 和二进制传输直接使用 `agent-sandbox` 文件 API；删除调用一次 AIO shell。glob 直接复用 DeepAgents `BaseSandbox.glob`，保持与模型收到的工具说明一致，包括递归文件名匹配和 `{a,b}` 模式。写入仍拒绝覆盖已有文件。

删除目录会递归删除其内容，送 Gate 的合成命令与 AIO 执行共用 `rm -r -- <路径>`。文本读取保留当前页的源文件行号；空白页不表示整文件为空，超出文件末尾的非零 offset 返回读取错误。

异步方法复用 DeepAgents 的 `SandboxBackendProtocol`。通过图调用工具时，中间件会等待已取消的工具执行结束；直接调用 backend 异步方法遵循 DeepAgents 的默认取消行为，取消等待不会终止工作线程。`integration.aclose()` 仍会等待正在进行的 SDK 请求结束后再关闭 shell 和 HTTP 连接。

AIO 的 glob/grep 达到上游搜索等待期限后，图调用仍会等本次工具已提交的 SDK 请求结束，再保存历史状态；响应丢失记为 `unknown`。工具调用结束后才开始的排队请求会被阻止提交，因此搜索超时后的返回可能晚于上游等待期限。

文件 API 不经过沙箱内部 shell.py 的检查，但 DeepAgents 的文件工具调用仍先通过插件送 Gate。直接调用 backend.upload_files/download_files 等方法不经过工具中间件，不能据此认为已完成外部送检。

外部 Moss Server 无法仅凭路径读取沙箱里的脚本，本插件不做文件共享或内容上传。沙箱脚本检测依赖部署时使用已接入 Moss 的 AIO 镜像；镜像内部集成与本插件独立。外部 Moss 若存在同名本地文件可能产生额外判断，此限制尚未解决。`integration.status()` 只展示 Gate 策略和 backend 会话状态，不证明镜像内 Moss 已启用。

## 手动接入（本地执行）

分别创建 `GateRuntime`、`LocalShellBackend` 和 `AgentMossMiddleware`，再传给 `create_deep_agent()`。以下使用应用已有的 `model`、`workspace` 和 `existing_checkpointer`：

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
- 支持 LocalShellBackend 和本包的 AIOSandboxBackend；传给插件和实际工具的 backend 必须一致。

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
- 使用 `DeepAgentsIntegration.load()` 时，等请求结束后调用 `await integration.aclose()`，清理 backend 和 Gate 连接；手动管理 `GateRuntime` 时调用 `await runtime.aclose()` 关闭 Gate 连接。

## 审批暂停与恢复

配置应用的 checkpointer 和 DeepAgents `interrupt_on` 后，可以用原生 `Command(resume=...)` 恢复审批。恢复时仍新建 `moss_info` 字典，保持原来的 `session_id`、`thread_id` 和该轮原始 `user_prompt`；插件从 checkpoint 恢复当前轮次，不把审批恢复计为一次新用户输入。

```python
from langgraph.types import Command

result = await agent.ainvoke(
    Command(resume={"decisions": [{"type": "approve"}]}),
    {"configurable": {"thread_id": session_id}},
    context={"moss_info": {"session_id": session_id, "user_prompt": original_user_text}},
)
```

以上为默认批量审批的单个待审批调用；多个调用需按 SDK 的顺序提供对应决定。审批人修改工具名称或参数后，插件检查修改后的动作，Gate Deny 仍然阻止执行。子代理暂停时，父 `task` 可记录为 `interrupted` 并恢复委派；这不会放开普通工具的执行 ID 去重检查。

如果显式使用 `HumanInTheLoopMiddleware(interrupt_mode="per_call", ...)`，将其放在 `AgentMossMiddleware` 之前，保证审批和参数修改先完成，再进入 Gate 检查。该模式按 SDK 返回的 interrupt ID 提交决定。

## 会话历史与升级

同一会话的主agent 和 subagent 共用调用历史，由应用已有的 checkpointer 保存。插件不再记录主agent/subagent 的身份关系。

持久化历史最多保留 200 条动作、20 次用户输入和 32 KiB；应用可配置更小的窗口。运行时和 checkpoint 使用相同的窗口规则，稳定轮次顺序和裁剪边界防止旧快照恢复已淘汰的记录。超限时整条移除历史记录并标记 `truncated`，当前送检参数不会因此截短。`history_bytes` 必须容纳会话标识和裁剪元数据；过小会在请求开始时明确报错。同一次根调用另行保存已开始的执行 ID，历史被移除后，SDK 节点重试也不会再次执行相同工具调用。这份去重集合只在本次调用内有效，不写入 checkpoint；跨进程恢复仍需应用核对已发生的副作用。

会话历史使用 schema=5。已结束的 v3/v4 历史会自动转换，保留动作内容和执行 ID；升级前先结束请求，确认 checkpoint 没有待执行任务并备份。含 `attempted`、`unknown` 或 `interrupted` 的旧历史需要先人工核对，插件会拒绝自动转换。旧插件不能读取新格式，回退时使用升级前备份或新会话。

[官方 Middleware 文档](https://docs.langchain.com/oss/python/langchain/middleware/custom)
