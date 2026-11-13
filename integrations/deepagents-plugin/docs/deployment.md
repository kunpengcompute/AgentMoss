# Gate、Moss 与 DeepAgents AIO 部署

部署包括三部分：Gate/Moss 外部服务、DeepAgents 客户端，以及用于执行工具的 CubeSandbox AIO。Gate wheel 提供服务和 `GateClient`；Moss wheel 提供服务、`judge_security()` 和 `agentmoss_deepagents` 插件。

```text
DeepAgents 工具调用
  → AgentMossMiddleware → Gate → Moss Server
  → Allow 后，AIOSandboxBackend → agent-sandbox SDK → CubeProxy
  → CubeSandbox AIO /v1/shell/exec
  → 镜像内 Moss SDK 检查后执行 Bash
```

客户端插件负责外部送检，镜像内的 Moss SDK 负责扫描沙箱中的脚本。两部分分别配置。

## 准备安装包

在仓库根目录构建 Moss wheel：

```bash
uv build --wheel
```

Gate wheel 从 [AgentGate 仓库](https://gitcode.com/kenhkl/AgentGate-commercial)单独构建。在部署目录创建 `wheels/`，放入 Gate 和 Moss wheel。下文的文件名使用当前版本，安装时替换为实际构建的文件名。

## 启动 Moss 与 Gate

在服务主机上创建虚拟环境并安装两个 wheel。依赖从 Python 包索引安装；离线环境需提前准备依赖 wheel。

```bash
python3 -m venv .venv-services
. .venv-services/bin/activate
python -m pip install ./wheels/agent_moss-0.17.6-py3-none-any.whl ./wheels/agent_gate_sec-0.9.0-py3-none-any.whl
mkdir -p secrets
chmod 700 secrets
openssl rand -hex 32 > secrets/moss.token
openssl rand -hex 32 > secrets/gate.token
chmod 600 secrets/*.token
```

在第一个终端启动 Moss：

```bash
. .venv-services/bin/activate
export AGENT_MOSS_RULES_MODE=builtin
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
python -m uvicorn agent_moss.server.app:create_app --factory --host 127.0.0.1 --port 9090
```

在第二个终端启动 Gate：

```bash
. .venv-services/bin/activate
export AGENT_GATE_AUTH_TOKEN_FILE="$PWD/secrets/gate.token"
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
agent-gate server --host 127.0.0.1 --port 9100 --security-url http://127.0.0.1:9090 --db-path "$PWD/gate.db"
```

如果 DeepAgents 在另一台主机，使用客户端可访问的 Gate 地址，并配置相应的网络访问控制。Moss 的模型配置见[主项目说明](../../../README.md)；只使用静态规则时设置 `AGENT_MOSS_DISABLE_LLM=1`。

`GateClient.health()` 返回的 `status` 和 `moss_status` 应均为 `healthy`。Gate 健康但 Moss 不健康时，需先检查 Moss 服务地址及令牌。

## 配置 AIO 镜像

内部脚本检查要求 AIO 镜像在 `/v1/shell/exec` 的执行入口调用 Moss 的 `judge_security()`。拒绝返回 HTTP 403，检查故障返回 503，均不提交命令。普通 AIO 镜像需要添加这一层集成。

镜像补丁位于[镜像集成源码目录](https://gitcode.com/wade_wolzzi/AgentMoss/tree/f1049f1c31ebf38facfe16bee37acb018ee9706c/delivery/aio-sdk-20261111)的 `image/` 下。该实现面向 AIO 1.11.0 ARM64 / Python 3.12；`patch_shell.py` 检查原始 `shell.py` 是否匹配。更换 AIO 版本或架构时，需要调整镜像集成。

把 `image/` 复制到部署目录，再准备 Moss wheel 和镜像依赖：

```bash
mkdir -p image/wheels
cp wheels/agent_moss-0.17.6-py3-none-any.whl image/wheels/
python3 -m pip download --only-binary=:all: \
  --platform manylinux2014_aarch64 --python-version 312 --implementation cp --abi cp312 \
  --dest image/wheels openai==1.109.1 tenacity==9.1.2
docker build --network none -t aio-agentmoss:1.11.0-moss0.17.6-arm64 image
```

根据基础镜像准备所需依赖，Gate wheel 无需安装到沙箱。使用集成 Moss 的镜像创建 CubeSandbox 实例，并取得 CubeProxy 地址、沙箱 ID 和可选的流量令牌。实例创建和销毁由应用的控制面负责。

内部检查覆盖 AIO 的 shell 执行入口。文件上传 API 和 CubeSandbox `commands.run()` 使用其他入口；DeepAgents 文件工具仍由客户端插件送 Gate 检查。

## 安装并配置 DeepAgents 客户端

客户端使用 Python 3.11 或更新版本。通过 Moss wheel 的 `[deepagents-aio]` 可选依赖安装插件和 AIO SDK：

```bash
python3 -m venv .venv-agent
. .venv-agent/bin/activate
python -m pip install './wheels/agent_moss-0.17.6-py3-none-any.whl[deepagents-aio]'
```

如果此前单独安装过 `agentmoss-deepagents`，先卸载它，再安装 Moss wheel，避免两个发行包写入同一个模块目录。

应用配置示例：

```python
config = {
    "backend": "aio",
    "gate": {
        "gate_url": "http://127.0.0.1:9100",
        "api_token_file": "/run/secrets/gate-token",
        "timeout_seconds": 30,
        "fail_closed": True,
    },
    "aio": {
        "proxy_url": "http://cubeproxy.example:8080",
        "sandbox_id": "your-existing-sandbox-id",
        "working_dir": "/home/gem",
        "default_timeout": 30,
        "hard_timeout": 30,
    },
}
```

CubeProxy 开启流量鉴权时，在 `aio` 中配置 `traffic_token_file`；Gate 令牌与流量令牌分别配置。默认 AIO 路由为 `8080-<sandbox_id>.cube.app`，其他路由通过 `aio.route_host` 指定。

用 `DeepAgentsIntegration.load(config)` 创建集成，把 `integration.backend` 和 `integration.middleware` 传给 `create_deep_agent()`。主代理和每个隔离子代理都需添加中间件。异步应用在线程中初始化，退出前等待所有调用结束，再执行 `await integration.aclose()`。完整代码见 [AIO 示例](../examples/aio.py)和[接入说明](cubesandbox.md)。

## 故障处理

| 现象 | 处理位置 |
| --- | --- |
| Gate 返回服务不可用或 `fail_closed` | 检查 Gate/Moss 地址、令牌和请求超时。 |
| 危险脚本未被内部检查拦截 | 检查实例使用的镜像，以及命令是否通过 `/v1/shell/exec`。 |
| AIO 返回 `AGENTMOSS_UNAVAILABLE` | 检查镜像内 Moss SDK 的初始化和配置。 |
| AIO 返回 409 或执行状态为 `unknown` | 查询旧 shell 会话，确认命令状态并清理后再创建新实例。 |

外部 Gate 放行后，镜像内 Moss 仍可拒绝命令。此时插件保留外部 `Allow` 判定，并记录工具执行错误及适配器退出码 `126`。
