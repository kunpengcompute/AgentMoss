# Gate、Moss SDK 与 DeepAgents AIO 测试交付

适用版本：`agent-gate-sec==0.9.0`、`agent-moss==0.17.6`（内含 DeepAgents 插件 0.6.0）、Python 3.12、`agent-sandbox==0.0.30`。部署需要两个 wheel：Gate wheel 提供服务及 `GateClient`；Moss wheel 同时提供服务、进程内 `judge_security()` 和 `agentmoss_deepagents` 插件。普通 Python wheel 含 `.py` 源码，不能当作源码保护方案。

本商业仓库保存插件源码、示例和测试，不保存预构建 wheel 或镜像补丁。在仓库根目录执行 `uv build --wheel`，可将当前版本的 Moss wheel 构建到 `dist/`。Gate wheel 从 [AgentGate-commercial](https://gitcode.com/kenhkl/AgentGate-commercial) 的对应版本单独构建。

下文沿用原测试交付包的目录结构：先在独立的部署目录准备 `wheels/`，放入 Gate wheel 和刚构建的 Moss wheel；镜像补丁 `image/` 可从[固定来源版本的交付目录](https://gitcode.com/wade_wolzzi/AgentMoss/tree/f1049f1c31ebf38facfe16bee37acb018ee9706c/delivery/aio-sdk-20261111)取得；再将本插件的 `examples/verify_aio_deployment.py` 复制到部署目录根部。不要用旧包中的 Moss wheel 替换刚构建的版本。以下命令均从该部署目录根部执行；按实际文件生成或核对 SHA-256 清单后再安装。

## 调用链与验收目标

```text
DeepAgents.execute
  → 插件 AgentMossMiddleware → 外部 Gate → 外部 Moss Server
  → Allow 后，插件 AIOSandboxBackend → agent-sandbox SDK → CubeProxy
  → CubeSandbox AIO /v1/shell/exec → shell.py → 镜像内 Moss SDK
  → 内部 Allow 后，Bash 执行命令
```

`backend: aio` 只改变执行位置，外部 Gate → Moss 仍然必须运行。外部 Moss Server 无法仅凭沙箱路径读取脚本；镜像内的 Moss SDK 与脚本处于同一文件系统，负责检查脚本内容。测试目标是正常命令成功、外部 Gate 拒绝时沙箱未执行、外部放行后镜像内 Moss 拒绝危险脚本。文件上传 API 本身不经过 `shell.py`；上传后执行脚本的命令经过它。CubeSandbox 的 `commands.run()` 走 envd，不能替代这里的 AIO shell 路径。

## 1. 安装 wheel，启动外部 Moss 与 Gate

在运行 Gate/Moss 的主机上，把交付目录的 `wheels/` 放在当前目录。以下命令使用测试专用目录；应用可改成自己的服务管理方式。安装依赖需要可访问 Python 包索引，离线环境需先准备全部依赖 wheel。

```bash
python3.12 -m venv .venv-services
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
export AGENT_MOSS_DISABLE_LLM=1 AGENT_MOSS_RULES_MODE=builtin
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
python -m uvicorn agent_moss.server.app:create_app --factory --host 127.0.0.1 --port 9090
```

在第二个终端启动 Gate。若 DeepAgents 在另一台主机，改用仅测试网络可访问的监听地址，并配好网络访问控制：

```bash
. .venv-services/bin/activate
export AGENT_GATE_AUTH_TOKEN_FILE="$PWD/secrets/gate.token"
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
agent-gate server --host 127.0.0.1 --port 9100 --security-url http://127.0.0.1:9090 --db-path "$PWD/gate-test.db"
```

`GateClient` 是独立的 Gate Python SDK；下面的检查必须看到 `status=healthy` 且 `moss_status=healthy`。Gate 健康但 Moss 不健康时，Gate 会因服务不可用返回 Deny，不能算安全规则验收通过。

```bash
python - <<'PY'
from pathlib import Path
from agent_gate.v1.sdk import GateClient
with GateClient('http://127.0.0.1:9100', timeout=30, api_token=Path('secrets/gate.token').read_text().strip()) as gate:
    health = gate.health()
    print(health)
    assert health['status'] == 'healthy' and health['moss_status'] == 'healthy'
PY
```

外部服务和镜像内 Moss SDK 独立部署；不要把 Gate token 当作 CubeProxy 流量 token。测试关闭 L3 模型分析，只验收静态 L1/L2 规则及 L1.2 脚本检查。

## 2. 构建并部署含 Moss SDK 的 AIO 镜像

交付目录的 `image/` 放置了已核对的 `Dockerfile`、`moss_guard.py`、`patch_shell.py`、`shell.original.py` 和 `install_in_aio.py`。它们针对 **AIO 1.11.0 ARM64 / Python 3.12**；补丁在原始 `shell.py` 不匹配时会停止构建。`moss_guard.py` 在 AIO 进程内调用 Moss wheel 的 `judge_security()`；拒绝时返回 HTTP 403，检查故障时返回 503，均不提交命令。

在 ARM64 构建主机上先准备镜像所需的 wheel。基础镜像已有部分依赖，新增依赖按已验证的构建配置下载；如果基础镜像或版本变化，需重新核对全部依赖。`image/wheels/` 只放 Moss wheel 和镜像所需依赖，Gate wheel 无需放入沙箱。

```bash
mkdir -p image/wheels
cp wheels/agent_moss-0.17.6-py3-none-any.whl image/wheels/
python3 -m pip download --only-binary=:all: \
  --platform manylinux2014_aarch64 --python-version 312 --implementation cp --abi cp312 \
  --dest image/wheels openai==1.109.1 tenacity==9.1.2
docker build --network none -t aio-agentmoss:1.11.0-moss0.17.6-arm64 image
```

使用该镜像创建一个**新的** CubeSandbox 实例，并确认 CubeProxy 地址、沙箱 ID、AIO 8080 路由和可选流量 token。已经运行的普通 AIO 沙箱不会因镜像构建而启用检查。CubeSandbox 模板和实例创建命令取决于团队的控制面配置，交付包不修改控制面。镜像细节见 [CubeSandbox 接入说明](cubesandbox.md)。

## 3. 从 Moss wheel 安装 DeepAgents 插件并运行端到端验收

在 DeepAgents 客户端主机安装**同一枚 Moss wheel**，使用 `[deepagents-aio]` 可选依赖启用插件及 AIO SDK；再安装 Gate wheel，供验收脚本调用 `GateClient`。无需第三枚插件 wheel。已有单独安装的 `agentmoss-deepagents` 应先卸载，以免两个发行包写入同一个 `agentmoss_deepagents` 模块目录。客户端会安装 Moss 引擎代码，但 DeepAgents 的外部检查仍经 Gate → Moss Server，不在客户端调用本地引擎判定。

```bash
python3.12 -m venv .venv-agent
. .venv-agent/bin/activate
python -m pip uninstall -y agentmoss-deepagents
python -m pip install './wheels/agent_moss-0.17.6-py3-none-any.whl[deepagents-aio]' ./wheels/agent_gate_sec-0.9.0-py3-none-any.whl
export MOSS_GATE_URL=http://127.0.0.1:9100
export MOSS_GATE_TOKEN_FILE="$PWD/secrets/gate.token"
export MOSS_AIO_URL=http://cubeproxy.example:8080
export MOSS_SANDBOX_ID=replace-with-created-sandbox-id
python verify_aio_deployment.py
```

如果 CubeProxy 开启流量鉴权，另设 `MOSS_AIO_TRAFFIC_TOKEN_FILE=/path/to/traffic.token`。非默认 Host 路由设 `MOSS_AIO_ROUTE_HOST`；默认值为 `8080-<sandbox_id>.cube.app`。`MOSS_AIO_URL` 填 CubeProxy 的 HTTP(S) 基础地址，不含沙箱 ID。Gate 请求超时在验收脚本中设为 30 秒，因为 devbox 上曾观察到单次分析约 9 秒，默认 5 秒会触发故障拒绝。

脚本使用固定的假模型，不调用外部模型 API。成功时依次打印四行 `PASS`：Gate/Moss 健康、普通命令执行、外部拒绝、镜像内脚本拒绝。最后一项先通过 AIO 文件 API 上传仅会读取沙箱内 `/etc/shadow` 并丢弃输出的测试脚本，再由 DeepAgents 运行 `python3 <脚本>`；若镜像内拦截生效，应看到外部判定 `Allow`、工具执行状态 `executed_error`、适配器退出码 `126` 和 `AGENTMOSS_DENIED`，且测试脚本不会执行。脚本结束时删除上传文件并关闭 AIO shell 会话。

实际应用中，用自己的模型和 checkpointer 创建 `create_deep_agent(model=..., backend=integration.backend, middleware=[integration.middleware])`；子代理也需传同一个 middleware。加载配置、传递 `thread_id`/`moss_info` 和关闭会话的完整示例见 [AIO 示例](../examples/aio.py)及[插件接入说明](cubesandbox.md)。

## 结果判读与已知边界

| 现象 | 应检查的位置 |
| --- | --- |
| Gate 健康但 `moss_status` 不为 `healthy` | 外部 Moss Server 地址及 Moss token；这类 Deny 不是规则检测成功。 |
| 普通命令返回 `fail_closed` | Gate 连接、Gate token 或请求超时；不要按正常安全拒绝验收。 |
| 正常命令成功，危险脚本也成功 | 沙箱可能仍在使用未集成 Moss 的 AIO 镜像，或命令绕过了 `/v1/shell/exec`。 |
| 内部返回 `AGENTMOSS_UNAVAILABLE` | AIO 中 Moss SDK 初始化或检查异常，命令未提交。 |
| 创建沙箱报 `no more resource` | CubeSandbox 控制面/节点资源问题，不能据此判断 SDK 功能。 |

以下是迁移来源版本于 2026-11-11 取得的历史验证：两枚 wheel 已在 devbox 安装；外部 Gate/Moss 服务和 DeepAgents 客户端均从交付 wheel 加载。将同一枚 Moss wheel 装入此前已验证的 AIO 镜像后，验收脚本四项检查全部通过。此前 devbox 上插件完整测试为 213 passed；真实 AIO 镜像直连的 9 项联调在 Gate 超时配置为 30 秒后全部通过。CubeProxy → CubeSandbox 实例路由此前因 `no more resource` 未完成验证；部署人员需要用当前构建的 wheel 和成功创建的实例补齐这一步。这些历史结果不作为商业仓库迁移后的复测结果。记录见 [devbox 验证报告](devbox-validation-2026-11-11.md)。
