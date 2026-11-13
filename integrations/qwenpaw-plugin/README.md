# QwenPaw 安全检查插件

通过 QwenPaw 的权限检查接口，把工具调用发送给 Gate，由 Moss 判断是否允许执行。

## 工作原理

插件挂载在 QwenPaw 的**权限检查**环节（`on_check_permission`），在工具真正执行**之前**：

1. 拿到本次工具调用（工具名 + 参数）
2. 拿到本轮提示词（user prompt）与完整会话历史（`agent.state.context`，按「提示词 → 它触发的动作」分组）
3. 打包上报 AgentGate `/api/v1/analyze`
4. AgentMoss 判决 `Deny` → 直接阻止工具执行（工具不会真的跑）
5. AgentMoss 判决 `Allow` → 放行，行为与不开插件完全一致

与 QwenPaw 自带治理的关系：**双重把关**。QwenPaw 自己拦的，插件不会放行；QwenPaw 没拦的，AgentMoss 还能再拦一道。

## 安装

从本仓库源码复制插件目录，将下面的路径替换为实际位置：

```bash
# 将源码路径换成实际位置
mkdir -p ~/.qwenpaw/plugins/agentmoss-gate
cp -R /path/to/AgentMoss/integrations/qwenpaw-plugin/. ~/.qwenpaw/plugins/agentmoss-gate/

# 创建配置文件
cp ~/.qwenpaw/plugins/agentmoss-gate/config.example.json \
   ~/.qwenpaw/plugins/agentmoss-gate/config.json
```

Windows 插件目录为 `%USERPROFILE%\.qwenpaw\plugins\`，同样将插件放入 `agentmoss-gate` 子目录。

先按[服务部署说明](../deepagents-plugin/docs/deployment.md)启动 Gate 和 Moss，在配置文件中填写 Gate 地址和令牌，再到 QwenPaw 界面开启「AgentMoss Security Gate」。

## 配置（config.json）

| 字段 | 默认 | 说明 |
|---|---|---|
| `gate_url` | `http://127.0.0.1:9100` | AgentGate 服务地址（与其默认端口一致） |
| `timeout` | `5.0` | 上报超时（秒）。超时按"网关不可用"处理 |
| `fail_closed` | `false` | `false`=网关挂了放行（fail-open，agent 不中断）；`true`=网关挂了全拦（fail-closed，更安全但一断全断） |
| `api_token` | `""` | 网关鉴权 token（AgentGate 若开启了鉴权） |
| `agent_type` | `"qwenpaw"` | 上报的 agent 类型标识 |

环境变量可覆盖：`AGENTMOSS_GATE_URL` / `AGENTMOSS_GATE_TIMEOUT` / `AGENTMOSS_FAIL_CLOSED` / `AGENTMOSS_DISABLE`。

## 限制

1. **"一次做一串动作"（批量工具）的内层动作看不到**——唯一真盲区。该功能的内层
   动作绕过了权限检查链路，需要由应用单独处理。
2. **工具不存在 / 参数解析失败**的调用看不到（未进入权限检查环节）。非安全场景：
   这类调用本来就不会执行。

> **被 QwenPaw 治理层拒绝的动作，本插件看得到**（含拒绝原因）。插件挂在"权限检查"
> 的**外层**，QwenPaw 自己的判定发生在插件内部，因此放行/拒绝/待确认三种结果插件
> 都能拿到并上报。

## 测试

```bash
# 先激活 QwenPaw 的 Python 环境
python /path/to/AgentMoss/integrations/qwenpaw-plugin/test_agentmoss_gate_plugin.py
```

测试覆盖允许、拒绝、连接故障处理、调用上下文提取和主机信息上报。
