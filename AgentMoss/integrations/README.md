# Agent 接入指南

| 使用场景 | 工具在哪里执行 | 接入方式 |
| --- | --- | --- |
| Deep Agents 使用本地工作目录 | Agent 应用所在主机 | [为主代理和子代理注册检查中间件](deepagents-plugin/README.md) |
| 直接调用 CubeSandbox 的 AIO 命令接口 | CubeSandbox 沙箱 | [构建含 AgentMoss 的镜像并创建沙箱](cubesandbox/README.md) |
| Deep Agents 使用 CubeSandbox | Agent 应用连接 CubeProxy，工具在沙箱内执行 | [配置 Deep Agents 的 AIO 后端](deepagents-plugin/docs/cubesandbox.md) |
| QwenPaw | QwenPaw 工具运行环境 | [安装权限检查插件](qwenpaw-plugin/README.md) |
| 自有 Agent 或 OpenDesk | 原有工具运行环境 | [在工具入口调用 AgentGate](../../AgentGate/examples/opendesk/README.md) |

使用 AgentGate 的应用先按[快速开始](../../README.md#快速开始)部署网关和分析引擎。直接使用 AIO 命令接口时，镜像中的 AgentMoss 在沙箱内完成检查。

设置 L3 模型输入预算后，调用方可从分析响应判断语义分析是否跳过或降级。请求字段和处理方式见 [L3 Prompt 接入说明](../docs/integrations/INTEGRATION_GUIDE.md)。
