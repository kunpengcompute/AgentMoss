# AgentMoss QwenPaw 插件

本插件在 QwenPaw 的权限检查环节接入 AgentGate，将待执行的工具、参数、用户提示词和会话上下文交给 AgentMoss 分析。返回 `Allow` 时继续权限检查，返回 `Deny` 时阻止工具执行。

## 工作方式

插件通过 QwenPaw 的 `on_check_permission` 扩展点加载，无需修改 QwenPaw、AgentGate 或 AgentMoss 源码。QwenPaw 自身的权限判定仍然生效；插件会将其拒绝或待确认结果作为上下文上报。

1. QwenPaw 准备执行工具，进入权限检查。
2. 插件整理工具名、参数、用户提示词、会话历史及系统信息。
3. 插件调用 AgentGate 的 `/api/v1/analyze`，由 AgentMoss 返回安全判定。
4. 只有权限检查允许时，QwenPaw 才执行该工具。

## 安装

先按[部署说明](../deepagents-plugin/docs/deployment.md)启动 AgentGate 和 AgentMoss。
随后在仓库根目录执行：

```bash
mkdir -p ~/.qwenpaw/plugins/agentmoss-gate
cp -R ./AgentMoss/integrations/qwenpaw-plugin/. ~/.qwenpaw/plugins/agentmoss-gate/
cp ~/.qwenpaw/plugins/agentmoss-gate/config.example.json \
   ~/.qwenpaw/plugins/agentmoss-gate/config.json
```

编辑安装目录中的 `config.json`，填写 AgentGate 地址和访问令牌。已有配置时保留原文件，按需要更新字段。
启动 QwenPaw 后，在插件界面启用「AgentMoss Security Gate」。目录名 `agentmoss-gate` 与 `plugin.json` 中的 `id` 保持一致。
Windows 插件目录为 `%USERPROFILE%\.qwenpaw\plugins\`。

## 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `gate_url` | `http://127.0.0.1:9100` | AgentGate 服务地址 |
| `timeout` | `5.0` | 请求超时，单位秒 |
| `fail_closed` | `false` | `true` 表示连接失败时拒绝执行；`false` 表示连接失败时继续执行 |
| `api_token` | 空 | AgentGate 访问令牌；服务默认启用鉴权，需要填写 |
| `agent_type` | `qwenpaw` | 请求中上报的 Agent 类型 |
| `extra_metadata` | `{}` | 附加的上下文字段 |

`AGENTMOSS_GATE_URL`、`AGENTMOSS_GATE_TIMEOUT` 和 `AGENTMOSS_FAIL_CLOSED` 提供相应字段的默认值；
`config.json` 中的非空配置优先。`AGENTMOSS_DISABLE` 可禁用插件。
无论连接故障策略如何设置，AgentMoss 明确返回的 `Deny` 都会拦截。

## 覆盖范围

- 通过权限检查环节的工具调用会被送检，包含 QwenPaw 自身拒绝或要求确认的动作。
- `run_tool_batch` 的内层动作绕过该权限检查环节，无法由本插件逐项检查。
- 工具不存在或参数解析失败时不会进入权限检查，因此没有相应的插件上报记录。

## 测试

使用已安装 QwenPaw 和 AgentScope 的 Python 环境，在仓库根目录执行：

```bash
/path/to/qwenpaw-venv/bin/python AgentMoss/integrations/qwenpaw-plugin/test_agentmoss_gate_plugin.py
```

将 Python 路径替换为 QwenPaw 实际使用的环境。测试使用模拟请求，覆盖 Allow 放行、Deny 拦截、连接故障处理、上下文提取以及平台和主机信息上报。
