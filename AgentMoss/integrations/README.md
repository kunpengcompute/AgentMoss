# integrations/ — 第三方 Agent 对接件

本目录提供 Agent 接入插件，将工具调用发送给 Gate，由 Moss 检查并返回安全判定。

AgentMoss 从本项目源码安装，见[安装说明](../README.md#安装)。Gate 和 Moss 的启动方法见[服务部署说明](deepagents-plugin/docs/deployment.md)。

## deepagents-plugin/

在 DeepAgents 应用中注册插件，执行工具前获取 Gate/Moss 的安全判定。安装与接入方法见 [DeepAgents 插件说明](deepagents-plugin/README.md)。

## cubesandbox/

让 DeepAgents 在 CubeSandbox 中执行任务，并在沙箱内部检查命令和脚本。镜像构建、配置示例和接入检查见 [CubeSandbox 接入说明](cubesandbox/README.md)。

## qwenpaw-plugin/

通过 QwenPaw 的权限检查接口接入。安装与配置方法见 [QwenPaw 插件说明](qwenpaw-plugin/README.md)。

### 它做什么

挂在 QwenPaw 的**权限检查环节**（`on_check_permission`），在工具真正执行**之前**：

1. 取本次工具调用（工具名 + 参数）、本轮提示词、完整会话上下文
2. 打包上报 AgentGate `/api/v1/analyze`
3. AgentMoss 判 `Deny` → 阻止执行；判 `Allow` → 放行
4. AgentGate 不可用时 fail-open（可配 `fail_closed`）

### 采集到的字段（与 AgentMoss 需求对齐）

| AgentMoss 字段 | 来源 | 必需 |
|---|---|---|
| `session_id` | `agent._request_context` | ✅ |
| `a_next`（工具名+参数） | 工具调用本身（工具名经 AgentGate 映射为 `bash` 等） | ✅ |
| `prompt_session` | `agent.state.context` 中最后一条用户消息 | |
| `prompt_history` | 同上，按「提示词 → 动作」分组 | |
| `action_history` | 同上，取真实 `ToolCallBlock` 展平 | |
| `cwd` | `agent._workspace_dir` | |
| `reason` | 工具调用旁的 assistant 文本 | |
| `os_type` | `platform.system()`（**agent 所在平台**，非网关平台） | |
| `hostname`（metadata） | `platform.node()`（**agent 所在机器**）。AgentGate 用 `agent_type:hostname` 生成 agent_id，缺了它所有同类型 agent 会并成一条记录 | |
| `session_history` | AgentGate 从 SQLite 补跨会话历史 | |

### 安装

```bash
# 在仓库根目录执行
mkdir -p ~/.qwenpaw/plugins/agentmoss-gate
cp -R ./AgentMoss/integrations/qwenpaw-plugin/. ~/.qwenpaw/plugins/agentmoss-gate/
# 创建配置文件，填写 Gate 地址和令牌
cp ~/.qwenpaw/plugins/agentmoss-gate/config.example.json \
   ~/.qwenpaw/plugins/agentmoss-gate/config.json
```

目录名使用 `agentmoss-gate`，与 `plugin.json` 的 `id` 一致。Windows 插件目录为 `%USERPROFILE%\.qwenpaw\plugins\`。

### 测试

先激活 QwenPaw 的 Python 环境，再从仓库根目录运行：

```bash
python ./AgentMoss/integrations/qwenpaw-plugin/test_agentmoss_gate_plugin.py
```

测试覆盖允许、拒绝、连接故障处理和调用上下文提取。

### 已知边界

1. **被 QwenPaw 自己拦下的调用**：插件能看到（挂在权限检查外层），会一并上报
2. **`run_tool_batch` 内层调用**：看不到——该功能的内层动作绕过了整条权限检查链路
3. **工具不存在 / 参数解析失败**：看不到（未进入权限检查），非安全场景
