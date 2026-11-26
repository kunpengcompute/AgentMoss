# boostkit Console 定制范围

本文件说明 boostkit 交付包的 agent / scenario 范围配置。

## 文件

安装环境中的：

```text
agent_moss/edition_scope.json
```

boostkit 默认内容：

```json
{
  "agents": [],
  "scenarios": ["bank"]
}
```

## 招行 Agent 本体团队配置

```json
{
  "agents": ["clawpartner"],
  "scenarios": ["cloud"]
}
```

并在 AgentMoss 进程设置：

```bash
AGENT_MOSS_SCENARIO=cloud
```

从 0.17.43 起，该文件同时控制引擎加载与 Console 展示：
Console 只显示 `clawpartner` 和 `cloud`，不需要再手改
`agent_moss_runtime.json` 删除其他 agent 分区。

范围外的 agent/scenario 增删改接口返回 404。

## 沙箱团队

L1 交付保持：

```json
{
  "agents": [],
  "scenarios": ["bank"]
}
```

环境变量：

```bash
AGENT_MOSS_SCENARIO=bank
```
