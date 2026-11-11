# AgentGate V1 × OpenDesk 效果测试指南

## 测试分 3 层

```
Layer 1: 单元/回归测试   → 不需要外部服务
Layer 2: 接口测试        → 启动 AgentGate，curl 模拟 OpenDesk 调用
Layer 3: 全链路测试      → Agent 侧接入 HTTP API，验证 Allow / Deny
```

以下命令都在 AgentGate 仓库根目录执行。

---

## Layer 1: 单元/回归测试

```bash
PYTHONPATH=. python3 -m pytest agent_gate/tests/v1 -q
# 223 passed
```

---

## Layer 2: 接口测试（不需要改 Agent）

### 2.1 启动服务

```bash
# 终端 1：AgentMoss（默认 9090）
agent-moss server --host 127.0.0.1 --port 9090

# 终端 2：AgentGate V1
agent-gate server --mode http --host 127.0.0.1 --port 9100 \
  --security-url http://127.0.0.1:9090 \
  --db-path /tmp/agent_gate_opendesk_test.db
```

### 2.2 用 curl 模拟 OpenDesk 调用

```bash
curl -s -X POST http://127.0.0.1:9100/api/v1/analyze \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "agent_type": "opendesk",
    "session_id": "opendesk-session-001",
    "tool_name": "executeBash",
    "command": "ls -la",
    "cwd": "/home/user/project",
    "user_prompt": "查看项目文件"
  }' | python3 -m json.tool
```

默认鉴权模式下需要 Bearer token；本地联调可先设置
`AGENT_GATE_AUTH_MODE=off` 启动服务，或从 `<data_root>/auth_token` 读取 token。

预期看到：

```json
{
  "decision": "Allow",
  "reason": "...",
  "risk_level": "low",
  "collected_context": { "...": "..." }
}
```

### 2.3 验证 session 历史自动累积

```bash
for cmd in "ls -la" "cat README.md" "whoami"; do
  curl -s -X POST http://127.0.0.1:9100/api/v1/analyze \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <token>" \
    -d "{
      \"agent_type\": \"opendesk\",
      \"session_id\": \"opendesk-session-001\",
      \"tool_name\": \"executeBash\",
      \"command\": \"$cmd\",
      \"cwd\": \"/home/user/project\"
    }" > /dev/null
done

curl -s -H "Authorization: Bearer <token>" \
  http://127.0.0.1:9100/api/v1/sessions/opendesk-session-001/traces \
  | python3 -m json.tool
```

预期 `tool_call_count: 3`，`tool_calls` 数组有 3 条记录。

### 2.4 自动化脚本

```bash
python3 examples/opendesk/test_integration.py
# 自动启动两个服务并跑完整链路的版本：
python3 examples/opendesk/test_e2e.py
```

---

## Layer 3: Agent 侧全链路接入

本裁剪版只提供 Python 服务端与 HTTP 契约，不包含 TypeScript 插件。
Agent 侧（任何语言）在工具调用前调用 `POST /api/v1/analyze` 即可：

```python
from agent_gate.v1.sdk import GateClient

client = GateClient(base_url="http://127.0.0.1:9100", api_token="<token>")

result = client.analyze(
    agent_type="opendesk",
    session_id="opendesk-session-001",
    tool_name="executeBash",
    command="ls -la",
    cwd="/home/user/project",
    user_prompt="查看项目文件",
)

if result["decision"] == "Deny":
    raise RuntimeError(result["reason"])
```

### 预期效果

| 验证项 | 预期结果 | 如何验证 |
|--------|----------|----------|
| 安全命令放行 | `ls`、`cat`、`echo` 等正常执行 | 返回 `decision: Allow` |
| 危险命令拦截 | `rm -rf /`、`curl \| bash` 等 | 返回 `decision: Deny` |
| Session 历史累积 | 同一会话后续命令包含前面历史 | `GET /api/v1/sessions/{id}/traces` |
| AgentMoss 不可用 | Fail-Closed，返回 Deny | 停掉 AgentMoss 后重试 |
| 系统上下文采集 | 响应带 OS / 用户 / 进程信息 | 查看 `collected_context` |

---

## 快速验证（最简路径）

```bash
# 1. 启动 AgentGate（联调可先关鉴权）
AGENT_GATE_AUTH_MODE=off agent-gate server --mode http --port 9100

# 2. 发一条危险命令
curl -s -X POST http://127.0.0.1:9100/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"agent_type":"opendesk","session_id":"test-001","tool_name":"executeBash","command":"rm -rf /","cwd":"/home/user"}'

# 3. 预期 decision=Deny, risk_level=critical
```
