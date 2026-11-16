# AgentGate 和 AgentMoss 服务部署

AgentGate 接收工具调用和上下文，交给 AgentMoss 分析，再把安全判定返回给插件。
两个服务分别启动，DeepAgents 应用通过 HTTP 访问 AgentGate。

## 安装

需要 Python 3.10 或更新版本，并安装 uv。先获取[项目源码](../../../../README.md#快速开始)，
以下命令均从仓库根目录执行，使用 Python 3.12：

```bash
uv venv --python 3.12 ./AgentMoss/.venv
uv pip install --python ./AgentMoss/.venv/bin/python -e ./AgentMoss
uv venv --python 3.12 ./AgentGate/.venv
uv pip install --python ./AgentGate/.venv/bin/python -e ./AgentGate
```

命令直接使用各自环境的程序，无需激活。以上路径适用于 Linux/macOS；Windows 的入口位于
`Scripts/`，程序名带 `.exe`。AgentGate 服务与 DeepAgents 应用使用独立虚拟环境，避免依赖版本冲突。
AgentGate 的更多配置见[组件说明](../../../../AgentGate/README.md)。

## 配置访问令牌

首次部署时，为两个服务分别生成令牌，保存到仓库外的用户配置目录：

```bash
mkdir -p "$HOME/.config/agentmoss-deployment"
chmod 700 "$HOME/.config/agentmoss-deployment"
(umask 077; openssl rand -hex 32 > "$HOME/.config/agentmoss-deployment/moss.token")
(umask 077; openssl rand -hex 32 > "$HOME/.config/agentmoss-deployment/gate.token")
```

已有部署请使用现有令牌文件，重复生成会更换凭据。AgentGate 使用 Moss 令牌调用 AgentMoss；
DeepAgents 插件使用 Gate 令牌调用 AgentGate。不同用户或机器部署时，为对应服务配置可读的文件路径。

## 启动服务

在第一个终端，从仓库根目录启动 AgentMoss：

```bash
export AGENT_MOSS_RULES_MODE=builtin
export AGENT_MOSS_AUTH_TOKEN_FILE="$HOME/.config/agentmoss-deployment/moss.token"
AGENT_MOSS_DISABLE_LLM=1 ./AgentMoss/.venv/bin/agent-moss server --host 127.0.0.1 --port 9090
```

上例启用 L1/L2 静态检测。使用 L3 模型分析时去掉 `AGENT_MOSS_DISABLE_LLM=1`，并按
[模型配置说明](../../../README.md#配置)设置接口、模型名称和凭据。

在第二个终端，从仓库根目录启动 AgentGate：

```bash
export AGENT_GATE_AUTH_TOKEN_FILE="$HOME/.config/agentmoss-deployment/gate.token"
export AGENT_MOSS_AUTH_TOKEN_FILE="$HOME/.config/agentmoss-deployment/moss.token"
./AgentGate/.venv/bin/agent-gate server --host 127.0.0.1 --port 9100 \
  --security-url http://127.0.0.1:9090
```

AgentMoss 在端口被占用时会选择后续可用端口，以启动日志为准，相应调整 `--security-url`。
如果 Agent 应用在另一台机器上，将 AgentGate 的监听地址改为应用可访问的地址，并按部署需要设置网络访问范围。

## 检查服务和分析链路

```bash
curl -sS http://127.0.0.1:9090/api/v1/health
curl -sS http://127.0.0.1:9100/api/v1/health
```

AgentGate 响应的 `status` 和 `moss_status` 均应为 `healthy`。如果 AgentGate 正常而 AgentMoss
不健康，检查 Moss 的实际监听地址和令牌。继续发起一次分析，确认请求能到达检测引擎：

```bash
GATE_TOKEN=$(cat "$HOME/.config/agentmoss-deployment/gate.token")
curl -sS http://127.0.0.1:9100/api/v1/analyze \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_type":"deployment-check","session_id":"check-001","tool_name":"bash","command":"ls -la","user_prompt":"列出当前目录文件","action_history":[]}'
```

响应应包含有效的 `decision`、`reason` 和 `risk_type`。`risk_type=service_unavailable` 表示服务
连接或鉴权故障；即使 HTTP 状态为 200，也需要修正配置后重新检查。

## 连接 Agent

DeepAgents 的安装与代码接入见[插件说明](../README.md)。将 AgentGate 地址及 Gate 令牌配置到
Agent 应用，例如设置 `DEEPAGENTS_GATE_API_TOKEN_FILE` 指向上述 `gate.token` 文件。
CubeSandbox 的连接及沙箱内部检查见[AIO 接入说明](cubesandbox.md)。
