# AgentGate V1 × OpenDesk 集成示例

## 1. 先决条件

- AgentMoss 服务已启动（默认 `http://127.0.0.1:9090`）；
- AgentGate V1 服务已启动：

```bash
agent-gate server --mode http --port 9100 \
  --security-url http://127.0.0.1:9090 \
  --db-path /tmp/agent_gate_opendesk_test.db
```

## 2. 验证 AgentGate 接口

另开终端：

```bash
python3 examples/opendesk/test_integration.py
```

这个脚本模拟 Agent 调用 AgentGate 的 analyze API，验证：

- 安全命令被 Allow；
- 危险命令被 Deny（依赖 AgentMoss 运行）；
- Session 内 `action_history` 自动累积；
- 系统上下文被采集；
- AgentMoss 不可达时的 Fail-Closed 行为。

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

## 5. Agent 侧包装器

仓库自带一个通用 shell 包装器 `scripts/agent-gate-wrap.sh`：

```bash
export AGENT_GATE_URL=http://127.0.0.1:9100
export AGENT_TYPE=claude-code

agent-gate-wrap claude
```

它会先调用 `/api/v1/analyze`，Deny 时直接退出，Allow 时执行原命令。
