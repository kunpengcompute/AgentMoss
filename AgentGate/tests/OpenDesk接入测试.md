# OpenDesk 接入维护说明

修改 AgentGate 或 OpenDesk 的工具包装器后，可按下面的方法检查接口兼容性和工具执行行为。

## 运行网关单元测试

在 `AgentGate/` 目录执行。使用[项目快速开始](../../README.md#快速开始)创建的虚拟环境即可，测试会自行创建数据库。

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest agent_gate/tests -q
```

## 检查工具入口

使用独立的 OpenDesk 会话和网关实例，将包装器中的实际执行函数换成能记录调用次数的函数。按[接入示例](../examples/opendesk/README.md#python-接入)分别触发以下情况。

| 场景 | 检查内容 |
| --- | --- |
| 网关返回 `Allow` | 执行函数被调用一次，收到的参数与送检参数一致 |
| 网关返回 `Deny` | 执行函数调用次数为零，调用方能看到拒绝原因 |
| 连接失败、超时或鉴权失败 | 执行函数调用次数为零，调用方收到错误 |
| 主代理和子代理各调用一次工具 | 两次调用都经过包装器，可以在各自的会话记录中找到 |

历史补齐需要使用相同的 `session_id` 连续提交动作并省略 `action_history`。用 `GET /api/v1/sessions/{session_id}/traces` 检查 `tool_calls` 的数量和 `seq` 顺序。`service_unavailable` 或 `analysis_timeout` 表示服务调用失败，检查规则行为时应查看实际风险类别。[接口说明](../README.md#http-api)
