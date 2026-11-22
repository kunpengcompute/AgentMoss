# 为 Deep Agents 配置 AgentGate 和 AgentMoss

Deep Agents 应用只需连接 AgentGate。AgentGate 使用另一份令牌连接 AgentMoss。三个进程可以运行在不同机器上。

## 同一机器、同一用户

从[项目快速开始](../../../../README.md#快速开始)安装并启动 AgentMoss 和 AgentGate，确认健康状态和第一次分析请求。默认情况下，两个服务都可通过本机地址访问。Deep Agents 应用使用独立的 Python 环境，按[插件安装说明](../README.md#安装)安装并连接 Gate。

| 发起请求的进程 | 默认目标 | 所需凭据 |
| --- | --- | --- |
| Deep Agents 应用 | AgentGate，`http://127.0.0.1:9100` | Gate 令牌，在应用上设置 `DEEPAGENTS_GATE_API_TOKEN_FILE` |
| AgentGate | AgentMoss，`http://127.0.0.1:9090` | Moss 令牌，可在 Gate 上设置 `AGENT_MOSS_AUTH_TOKEN_FILE` |

Linux 默认部署首次启动后，Gate 令牌位于 `~/.config/agentgate/auth_token`，Moss 令牌位于 `~/.config/agentmoss/auth_token`。这里的 `~` 各自对应启动该服务的用户。显式指定了令牌或数据目录时，以实际配置为准。

## 不同机器或不同用户

1. **AgentMoss 主机。** 启动服务时将 `--host` 设为 Gate 能访问的内网地址。两台机器通信时使用受保护的网络或配置 HTTPS 的反向代理，限制访问来源。
2. **AgentGate 主机。** 将 Moss 令牌提供给运行 Gate 的用户，设置 `AGENT_MOSS_AUTH_TOKEN_FILE` 指向该用户可读的文件。启动 Gate 时用 `--security-url` 指向实际的 Moss 地址，用 `--host` 绑定 Deep Agents 应用可访问的地址。
3. **应用主机。** 将 Gate 令牌提供给应用用户，设置 `DEEPAGENTS_GATE_URL` 为实际 Gate 地址，设置 `DEEPAGENTS_GATE_API_TOKEN_FILE` 为应用用户可读的 Gate 令牌文件。令牌文件应只对使用它的账户开放读权限。只跨用户、未跨主机时，两个服务仍可监听 `127.0.0.1`。

## 判断连接是否正常

在应用主机上运行以下命令。先按[插件说明](../README.md#连接-agentgate)设置 Gate 地址和令牌文件。

```bash
curl -fsS "${DEEPAGENTS_GATE_URL}/api/v1/health"
```

`status` 和 `moss_status` 都应为 `healthy`。若 `moss_status` 为 `unreachable`，检查 Gate 使用的 Moss 地址和网络。健康端点不检查分析请求的令牌。再用[项目快速开始](../../../../README.md#快速开始)中的分析请求确认鉴权，或从应用中实际调用一次工具。应用收到 HTTP 401 时先检查 Gate 令牌。Gate 返回 `risk_type=service_unavailable` 时，检查 Gate 到 Moss 的地址、凭据和服务日志。
