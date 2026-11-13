# DeepAgents 外部插件与 CubeSandbox AIO 接入

本插件通过 `agent-sandbox` SDK 连接 CubeSandbox AIO。安装、服务启动和镜像配置见[部署说明](deployment.md)。

## 两层检查分别部署

外部 Agent 侧始终先通过插件调用 Gate → Moss Server。选择 aio 只改变执行后端，不取消这一层。

```text
DeepAgents 主代理 / 子代理
    → AgentMossMiddleware
    → 外部 AgentGate → 外部 Moss Server
    → 外部放行后调用 AIOSandboxBackend
    → agent-sandbox SDK → CubeProxy → CubeSandbox AIO
    → 镜像内 shell.py 调用 Moss SDK（独立的内部检查）
    → 内部放行后执行 Bash
```

Moss SDK 放在 AIO 进程内部，内部检查直接调用 SDK。外部 DeepAgents 插件通过 Gate → Moss Server 进行另一层检查。DeepAgents 客户端从 Moss wheel 安装插件和 AIO SDK，外部判定由 Gate → Moss Server 执行。

外部服务不能仅凭沙箱路径读取脚本，插件也不传输脚本内容。沙箱脚本的内容扫描由已接入 Moss 的 AIO 镜像承担。客户端健康检查和 `integration.status()` 展示外部服务及 backend 状态；镜像内部的检查需要单独配置。

## 安装与连接前提

Moss wheel 已包含插件。如从源码安装，进入 DeepAgents 应用的 `pyproject.toml` 所在目录，执行以下命令，并替换仓库路径：

```bash
uv add --extra deepagents-aio /path/to/AgentMoss-commercial
```

应用提前准备已启动的外部 Gate/Moss、已创建的 CubeSandbox 沙箱及可访问的 CubeProxy 地址。Gate token 与 CubeSandbox 流量 token 不同；AIO 端口/路由域名使用默认值时，插件发送 Host: 8080-<sandbox_id>.cube.app。

## 完整的异步调用示例

model、existing_checkpointer 和真实配置由应用提供。应用应先等待初始化完成，再接受任务；退出前停止并等待图调用，最后关闭集成实例。

```python
import asyncio
from deepagents import create_deep_agent
from agentmoss_deepagents import DeepAgentsIntegration

async def run_once(model, existing_checkpointer, session_id, user_text):
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
            # 沙箱未开启流量鉴权时删除此项。
            "traffic_token_file": "/run/secrets/cube-traffic-token",
        },
    })
    try:
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
        return await agent.ainvoke(
            {"messages": [("user", user_text)]},
            {"configurable": {"thread_id": session_id}},
            context={"moss_info": {
                "session_id": session_id,
                "user_prompt": user_text,
            }},
        )
    finally:
        await integration.aclose()
```

服务应用应在启动时创建并复用集成实例和 agent；上面的单次函数用于展示完整的创建、送检和关闭关系。每个隔离子代理都要添加本插件中间件；DeepAgents 给声明式子代理配置同一 backend，自定义编译图则由应用负责。

不再额外下载官方 sandbox_backend.py，也不把独立 CubeSandbox SDK 的 commands.run() 混入这条路径；后者走 envd，不经过 AIO shell.py。

## 配置和路径

- gate 块复用现有 GateConfig。显式 mapping 不合并环境变量；省略整个 gate 块才读取 DEEPAGENTS_GATE_*。默认故障策略未改变，部署示例显式设 fail_closed=True。
- aio.proxy_url 是 CubeProxy 基础地址，不含内嵌凭据、查询参数或片段。其他域名/端口路由通过 aio.route_host 指定。
- aio.traffic_token 与 aio.traffic_token_file 二选一；不需要流量鉴权时都省略。文件令牌在初始化时读取，不自动热更新。
- 每条 shell 命令显式在 working_dir 执行。单独 cd 不改变下一条命令的起始目录，cd subdir && ... 在同一条命令内有效。
- 文件工具建议传完整沙箱绝对路径。DeepAgents 会把显式 foo.txt 规范化成 /foo.txt，送检与执行均遵循它；直接调用低层 backend 时，相对路径才基于 working_dir。省略 glob/grep 的 path 使用 working_dir，显式 / 表示沙箱根目录。
- 同一实例串行使用一个 shell 会话。要并行执行独立任务，创建不同集成实例及会话。thread_id/session_id 是 Agent 逻辑会话，不等于 sandbox_id 或 shell_session_id。

## 判定、故障和覆盖范围

- 外部 Gate Deny：不调用 AIO，历史为 denied。
- 外部放行、内部 Moss 拒绝：工具返回 AIO 原因及适配器退出码 126，历史为 executed_error，外部判定仍保留 Allow。126 不是 Bash 真实退出码。
- 正常完成：保留实际输出和退出码。AIO HTTP 403/409/503 不当作成功，409 后还需先处理活动命令。
- running、硬超时、连接中断或响应损坏：历史为 unknown，backend 停止接收新操作，不自动重试、不切换本地。先查询旧会话并清理，再创建新实例；新会话不意味着旧命令已经停止。
- 初始化失败时，AIOStartupError 给出 sandbox_id、session_id、cleanup_confirmed 和脱敏失败类型，便于处理可能遗留的会话。
- 文件工具仍经过外部插件，但 AIO 文件 API 不经过内部 shell.py。直接调用 backend.upload_files/download_files 等接口不经过插件中间件。
- 外部 Moss 本地同名文件可能产生额外判断。客户端与沙箱的文件系统不共享；backend 的工作目录不构成路径或符号链接的权限隔离。
