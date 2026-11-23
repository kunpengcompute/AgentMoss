# OpenDesk 接入测试

先按[项目快速开始](../../../README.md#快速开始)安装并启动 AgentMoss 和 AgentGate。
本指南检查网关接口与判定链路；实际工具是否被阻止，还需要在 Agent 侧验证。

## 单元测试

在 `AgentGate/` 组件目录执行，测试不需要另外启动服务：

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest agent_gate/tests -q
```

## 接口与判定

先查看健康状态：

```bash
curl -sS http://127.0.0.1:9100/api/v1/health
```

应有 `moss_status: "healthy"` 和 AgentMoss 的实际版本号。
随后读取 AgentGate 令牌并提交只读命令：

```bash
GATE_TOKEN=$(cat ~/.config/agentgate/auth_token)
curl -sS http://127.0.0.1:9100/api/v1/analyze \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_type":"opendesk","session_id":"opendesk-readonly","tool_name":"executeBash","command":"ls -la","user_prompt":"查看项目文件","action_history":[]}'
```

预期 `decision` 为 `Allow`。再提交访问 SSH 私钥的命令文本，验证拦截：

```bash
curl -sS http://127.0.0.1:9100/api/v1/analyze \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_type":"opendesk","session_id":"opendesk-sensitive","tool_name":"executeBash","command":"cat ~/.ssh/id_rsa","user_prompt":"查看项目文件","action_history":[]}'
```

预期 `decision: "Deny"`、`risk_type: "file_access"`。上述请求只做分析，
不会执行命令或读取私钥。`service_unavailable` / `analysis_timeout` 表示服务链路异常，
不能作为规则拦截通过的证据。

令牌使用自定义路径时，请按实际配置读取，见 [AgentGate 鉴权说明](../../README.md#鉴权)。

## 会话历史

使用一个新的 `session_id` 连续提交三次只读命令，省略 `action_history`，
让 AgentGate 从数据库补齐上下文。然后查询：

```bash
curl -sS \
  -H "Authorization: Bearer $GATE_TOKEN" \
  http://127.0.0.1:9100/api/v1/sessions/你的会话ID/traces
```

新会话应有三条 `tool_calls`。显式传入 `action_history: []` 会让本次分析使用空历史，
与省略该字段的行为不同。

## Agent 侧验证

参照[接入示例](README.md#python-接入)，在真实工具入口调用网关。
检查只读命令是否执行、被拒绝的命令是否没有执行，并确认主代理和子代理都经过检查。

在独立测试环境中暂时停止 AgentMoss，再调用网关，应得到 `Deny`，且风险类型为
`service_unavailable`。恢复 AgentMoss 后重新检查健康状态和只读请求。
该检查用于验证服务故障时停止执行的行为，不要在共享服务上直接操作。
