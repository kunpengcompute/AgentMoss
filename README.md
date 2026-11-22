# AgentMoss

AgentMoss 帮助 AI Agent 在执行工具前检查命令、文件操作和脚本。Agent 把下一步动作发给 AgentGate，AgentGate 补充会话历史并交给 AgentMoss 分析。AgentMoss 返回 `Allow` 或 `Deny` 和原因，由接入方决定是否执行工具。你可以在 AgentGate 看板中查看每次调用的判定。

| 我想做什么 | 从哪里开始 |
| --- | --- |
| 在本机部署检测服务，发起第一次分析 | [快速开始](#快速开始) |
| 在 Deep Agents 主代理和子代理中检查工具调用 | [接入 Deep Agents](AgentMoss/integrations/deepagents-plugin/README.md) |
| 在 CubeSandbox 的 AIO 沙箱内检查命令和脚本 | [构建镜像、创建沙箱并运行命令](AgentMoss/integrations/cubesandbox/README.md) |
| 让 Deep Agents 在 CubeSandbox 中执行工具 | [配置 AIO 后端](AgentMoss/integrations/deepagents-plugin/docs/cubesandbox.md) |

## 快速开始

以下示例在同一台 Linux 主机上以同一用户运行两个服务，使用本地规则，无需模型密钥。请准备 Git、curl 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。两个服务要求 Python 3.10 及以上。以下统一使用 Python 3.12，Windows 用户可在 WSL 中执行这些 Bash 命令。接入 CubeSandbox 时还需要可用的集群，以及构建 Linux ARM64 镜像的 Docker 环境。

### 安装

下载与当前文档相同版本的源码。下面以 `dev` 分支为例，在 `master` 或版本标签下阅读时，请将 `--branch dev` 中的 `dev` 换成相应名称。已有源码可直接从仓库根目录执行安装命令。

```bash
git clone --branch dev --depth 1 https://gitcode.com/boostkit/AgentMoss.git
cd AgentMoss
```

为两个组件分别创建虚拟环境。后续命令直接使用各环境中的程序。

```bash
uv venv --python 3.12 ./AgentMoss/.venv
uv pip install --python ./AgentMoss/.venv/bin/python -e ./AgentMoss
uv venv --python 3.12 ./AgentGate/.venv
uv pip install --python ./AgentGate/.venv/bin/python -e ./AgentGate
```

如果主机通过内部镜像安装 Python 包，在运行安装命令的终端设置 `UV_DEFAULT_INDEX`，值为镜像的 Python Simple API 地址。

### 启动两个服务

在第一个终端，从仓库根目录启动 AgentMoss。保持该终端运行。

```bash
AGENT_MOSS_DISABLE_LLM=1 ./AgentMoss/.venv/bin/agent-moss \
  server --host 127.0.0.1 --port 9090
```

在第二个终端，进入同一个仓库根目录，启动 AgentGate。

```bash
./AgentGate/.venv/bin/agent-gate server --host 127.0.0.1 --port 9100 \
  --security-url http://127.0.0.1:9090
```

这两个地址都只供本机访问。端口被占用时，服务会选择后续可用端口。请以各自启动日志为准，并在 `--security-url` 中填写 AgentMoss 的实际地址。

### 提交第一次工具调用

在第三个终端查看网关状态。响应中的 `status` 和 `moss_status` 应为 `healthy`，`moss_version` 应显示引擎版本。

```bash
curl -fsS http://127.0.0.1:9100/api/v1/health
```

两个服务首次启动时会各自生成令牌。下面使用 AgentGate 的令牌，询问是否允许 Agent 列出当前目录。该请求只做判定，命令由调用方执行。

```bash
GATE_TOKEN=$(cat "${XDG_CONFIG_HOME:-$HOME/.config}/agentgate/auth_token")
curl -fsS http://127.0.0.1:9100/api/v1/analyze \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_type":"my-agent","session_id":"list-files-1","tool_name":"bash","command":"ls -la","user_prompt":"列出当前目录的文件"}'
```

使用默认规则时，响应会包含以下字段。接入自己的工具时，仅在得到 `Allow` 后执行。

```json
{"decision": "Allow", "reason": "安全的只读 bash 命令: ls -la"}
```

如果收到 HTTP 401，检查发送给 AgentGate 的令牌。如果收到 `Deny` 且 `risk_type` 为 `service_unavailable`，查看 `reason`，核对 AgentGate 连接 AgentMoss 的地址和令牌。默认情况下，同一用户运行的 AgentGate 会自行读取 `~/.config/agentmoss/auth_token`。[跨用户和跨机器部署](AgentMoss/integrations/deepagents-plugin/docs/deployment.md)

## 使用与配置

| 文档 | 适用场景 |
| --- | --- |
| [AgentMoss 使用说明](AgentMoss/README.md) | 通过 CLI、Python 或 HTTP 分析动作，配置模型、规则和控制台 |
| [AgentGate 使用说明](AgentGate/README.md) | 通过 SDK 或 HTTP 提交动作，查看会话记录，配置鉴权和服务托管 |
| [Agent 接入指南](AgentMoss/integrations/README.md) | 选择 Deep Agents、CubeSandbox、QwenPaw 或自有 Agent 的接入方式 |
| [鲲鹏硬件加速](AgentMoss/docs/预筛加速使用说明.md) | 使用 Ultrascan 在鲲鹏处理器上加速命令规则预筛 |

## 反馈与许可证

欢迎在社区仓库的 Issues 中反馈问题。请附上组件版本、运行环境、复现步骤和已脱敏的错误信息。

AgentMoss 和 AgentGate 采用 MIT License。Deep Agents 插件中的 AIO 适配代码另含 Apache License 2.0 授权的第三方代码。[AgentMoss 许可证](AgentMoss/LICENSE) · [AgentGate 许可证](AgentGate/LICENSE) · [第三方声明](AgentMoss/integrations/deepagents-plugin/THIRD_PARTY_NOTICES.md)
