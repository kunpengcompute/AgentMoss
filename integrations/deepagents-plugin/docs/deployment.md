# Gate 和 Moss 服务部署

Gate 接收 Agent 的工具调用，交给 Moss 分析，再把安全判定返回给插件。两项服务分别启动。

## 安装

需要 Python 3.10 或更新版本。以下命令在同一工作目录执行，先创建虚拟环境：

```bash
python3 -m venv .venv-services
. .venv-services/bin/activate
```

通过 pip 安装：

```bash
python -m pip install agent-moss agent-gate-sec
```

也可以从源码安装，将路径换成实际的源码目录：

```bash
python -m pip install -e /path/to/AgentMoss
python -m pip install -e /path/to/AgentGate
```

Gate 源码见 [AgentGate 仓库](https://gitcode.com/kenhkl/AgentGate-commercial)。

## 配置访问令牌

为 Gate 和 Moss 分别生成令牌：

```bash
mkdir -p secrets
chmod 700 secrets
openssl rand -hex 32 > secrets/moss.token
openssl rand -hex 32 > secrets/gate.token
chmod 600 secrets/*.token
```

Gate 使用 Moss 令牌调用 Moss；Agent 插件使用 Gate 令牌调用 Gate。

## 启动服务

在第一个终端启动 Moss：

```bash
. .venv-services/bin/activate
export AGENT_MOSS_RULES_MODE=builtin
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
agent-moss server --host 127.0.0.1 --port 9090
```

仅使用静态规则时，设置 `AGENT_MOSS_DISABLE_LLM=1`。模型配置见[主项目说明](../../../README.md)。

在第二个终端进入同一工作目录，启动 Gate：

```bash
. .venv-services/bin/activate
export AGENT_GATE_AUTH_TOKEN_FILE="$PWD/secrets/gate.token"
export AGENT_MOSS_AUTH_TOKEN_FILE="$PWD/secrets/moss.token"
agent-gate server --host 127.0.0.1 --port 9100 --security-url http://127.0.0.1:9090 --db-path "$PWD/gate.db"
```

如果 Agent 应用在另一台机器上，需要将 Gate 的监听地址改为应用可访问的地址，并限制网络访问范围。

## 连接 Agent

DeepAgents 的插件安装和代码接入见[插件说明](../README.md)。把 Gate 地址和 Gate 令牌配置到 Agent 应用中。

`GateClient.health()` 返回的 `status` 和 `moss_status` 应均为 `healthy`。如果 Gate 正常而 Moss 不健康，检查 Moss 服务地址和 Moss 令牌。

CubeSandbox 的连接和沙箱内部检查见[接入说明](cubesandbox.md)。
