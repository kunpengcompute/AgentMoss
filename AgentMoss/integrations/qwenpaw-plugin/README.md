# QwenPaw 接入 AgentMoss

插件在 QwenPaw 执行工具前，把工具名、参数、用户请求、会话历史和 QwenPaw 的权限判定发给 AgentGate。AgentMoss 返回 `Deny` 时，插件阻止工具执行。返回 `Allow` 时，QwenPaw 仍按自己的权限判定执行、询问或拒绝。

## 安装并启用插件

先按[项目快速开始](../../../README.md#快速开始)启动 AgentMoss 和 AgentGate。插件清单声明的 QwenPaw 版本范围为 `1.1.6` 至 `2.9.0`。[插件清单](plugin.json)

在运行 QwenPaw 的 Linux 用户下，从本仓库根目录复制插件。全新安装的工作目录默认为 `~/.qwenpaw`。如果设置了 `QWENPAW_WORKING_DIR`，请在同一环境中运行以下命令。从 CoPaw 升级且仍使用 `~/.copaw` 时，将 `plugin_dir` 改为该目录下的 `plugins/agentmoss-gate`。

```bash
plugin_source=AgentMoss/integrations/qwenpaw-plugin
plugin_dir="${QWENPAW_WORKING_DIR:-$HOME/.qwenpaw}/plugins/agentmoss-gate"
mkdir -p "$plugin_dir"
cp "$plugin_source/plugin.json" "$plugin_source/agentmoss_gate.py" "$plugin_dir/"
if [ ! -e "$plugin_dir/config.json" ]; then
    install -m 600 "$plugin_source/config.example.json" "$plugin_dir/config.json"
fi
```

Windows 下，将同样的文件放入 `%USERPROFILE%\.qwenpaw\plugins\agentmoss-gate\`，首次安装时将 `config.example.json` 复制为 `config.json`。

1. 编辑安装目录中的 `config.json`。`gate_url` 填写从 QwenPaw 所在主机可访问的 AgentGate 地址，`api_token` 填写网关令牌。同机默认安装时，令牌文件是 `~/.config/agentgate/auth_token`。如果分配了专属令牌，须绑定 `agent_type` 中的名称。[AgentGate 鉴权说明](../../../AgentGate/README.md#鉴权)
2. 启动或重启 QwenPaw，在插件界面启用 **AgentMoss Security Gate**。
3. 新建会话，请 QwenPaw 用工具查看工作目录。在 AgentGate 看板中找到 `qwenpaw` 的调用，检查工具名、判定和原因。服务响应超过 5 秒时，调大 `config.json` 的 `timeout`，例如设为 `30`。如果必须取得网关判定才能执行工具，同时设 `fail_closed` 为 `true`。

## 配置

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `gate_url` | `http://127.0.0.1:9100` | AgentGate 的服务地址，插件会追加 `/api/v1/analyze` |
| `api_token` | 空字符串 | 访问 AgentGate 的令牌 |
| `timeout` | `5.0` | 网关请求超时秒数 |
| `fail_closed` | `false` | 网关连接失败、超时或返回 HTTP 错误时，`false` 沿用 QwenPaw 的权限判定，`true` 拒绝此次工具调用 |
| `agent_type` | `qwenpaw` | 在网关记录中标识调用来源 |
| `extra_metadata` | `{}` | 发给网关的附加元数据 |

`AGENTMOSS_GATE_URL`、`AGENTMOSS_GATE_TIMEOUT`、`AGENTMOSS_FAIL_CLOSED` 分别设置地址、超时和故障处理的默认值。同名用途的 `config.json` 字段优先，布尔值 `false` 也会覆盖环境变量。`AGENTMOSS_DISABLE=1` 会在 QwenPaw 加载时停用插件。

## 哪些调用会出现在看板中

插件使用 QwenPaw 的 `on_check_permission` 权限钩子。进入此环节的动作都会上报，包括 QwenPaw 已拒绝或要求用户确认的动作。`run_tool_batch` 的内层动作不逐项经过该钩子。工具不存在或参数解析失败时也不会产生插件记录。
