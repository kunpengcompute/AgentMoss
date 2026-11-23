# AgentGate V1 × OpenDesk 集成示例

## 1. 先决条件

先按[项目快速开始](../../../README.md#快速开始)安装两个组件。
以下命令在 `AgentGate/` 组件目录执行，使用 `.venv/bin/agent-gate`，或先激活虚拟环境。

- AgentMoss 服务已启动（默认 `http://127.0.0.1:9090`）；
- AgentGate V1 服务已启动：

```bash
.venv/bin/agent-gate server --mode http --host 127.0.0.1 --port 9100 \
  --security-url http://127.0.0.1:9090 \
  --db-path /tmp/agent_gate_opendesk_test.db
```

## 2. 验证 AgentGate 接口

另开终端，读取 AgentGate 的访问令牌并提交一个只读命令。这里只分析命令，不会执行它：

```bash
GATE_TOKEN=$(cat ~/.config/agentgate/auth_token)
curl -sS http://127.0.0.1:9100/api/v1/analyze \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_type":"opendesk","session_id":"opendesk-check","tool_name":"executeBash","command":"ls -la","user_prompt":"查看项目文件"}'
```

响应应为 `decision: Allow`。如果返回 `Deny` 且 `risk_type` 为
`service_unavailable` 或 `analysis_timeout`，先检查 AgentMoss 服务地址、令牌与状态。
更多检查方法见[测试指南](TESTING.md)。

## 3. 接入方式

Agent 本体只需要在**每次工具调用前**调用：

```
POST /api/v1/analyze
Authorization: Bearer <token>
Content-Type: application/json
```

请求字段（最小集）：`agent_type`、`session_id`、`tool_name`、`command`。

```json
{
  "agent_type": "opendesk",
  "agent_version": "0.3.10",
  "session_id": "sess-001",
  "tool_name": "executeBash",
  "command": "ls -la",
  "cwd": "/home/user/project",
  "description": "list files",
  "user_prompt": "show me the project files"
}
```

Agent 不需要自己维护 `action_history`，AgentGate 会按 `session_id` 自动补齐。

### Python 接入

```python
from agent_gate.v1.sdk import GateClient

client = GateClient(base_url="http://127.0.0.1:9100", api_token="<token>")

result = client.analyze(
    agent_type="opendesk",
    session_id="sess-001",
    tool_name="executeBash",
    command="ls -la",
    cwd="/home/user/project",
    description="list files",
    user_prompt="show me the project files",
)

if result["decision"] == "Deny":
    raise RuntimeError(result["reason"])
```

### 其他语言接入

按上面的 HTTP 契约实现即可。判定流程：

```
工具调用 → POST /api/v1/analyze
  → AgentGate 采集上下文 + 自动补 action_history
  → AgentMoss 判定
← Allow / Deny + reason
```

Deny 时阻止执行；Allow 时正常执行。

## 4. 拦截哪些工具

由 Agent 侧决定把哪些工具调用送到 AgentGate，例如：

| 工具名 | 说明 |
|---|---|
| `executeBash` | Bash 命令执行 |
| `executeZsh` | Zsh 命令执行 |
| `executePowerShell` | Windows PowerShell |
| 其他工具名 | 按实际工具名传入 `tool_name` |

工具包装器应检查 HTTP 请求是否成功以及响应中的 `decision`，只有明确返回
`Allow` 时才执行原工具。连接失败、超时、鉴权失败或返回 `Deny` 时停止执行，
并把原因提供给调用方。
