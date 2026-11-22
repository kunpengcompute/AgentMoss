# OpenDesk 工具接入 AgentGate

在 OpenDesk 调用工具的位置加入一次网关检查。OpenDesk 将原始工具名、命令和用户请求发给 AgentGate，收到 `Allow` 后再执行工具。同一会话的主代理和子代理都应经过这个入口。

先按[项目快速开始](../../../README.md#快速开始)安装并启动 AgentMoss 和 AgentGate，确认一次分析请求能得到 `Allow`。以下示例假定 OpenDesk 可以访问 `http://127.0.0.1:9100`。跨主机运行时，填写 OpenDesk 所在主机可访问的网关地址。

## Python 接入

在 OpenDesk 的 Python 环境中可以使用 AgentGate 包提供的 `GateClient`。也可将 [gate_client.py](../../sdk/python/gate_client.py) 复制到已有 `httpx` 的应用中，将导入改为 `from gate_client import GateClient`。为 OpenDesk 进程设置 `AGENT_GATE_AUTH_TOKEN`，使用绑定 `opendesk` 的令牌或网关全局令牌。[网关鉴权说明](../../README.md#鉴权)

以下示例在 Linux 上查看工作目录。网关分析 `pwd`，通过后才在本机运行它。实际接入时，将 `client.analyze` 放在现有工具函数之前，传入本次会话和待执行的真实参数。

```python
import os
import subprocess
from uuid import uuid4

from agent_gate.shared.sdk import GateClient

with GateClient(base_url="http://127.0.0.1:9100") as client:
    result = client.analyze(
        agent_type="opendesk",
        session_id=str(uuid4()),  # 接入时使用 OpenDesk 的会话 ID
        tool_name="executeBash",
        command="pwd",
        cwd=os.getcwd(),
        user_prompt="查看当前工作目录",
    )
    if result.get("decision") != "Allow":
        raise PermissionError(result.get("reason") or "未获得执行许可")
    subprocess.run(["pwd"], check=True)
```

连接失败、超时或鉴权失败时，SDK 会抛出异常。将异常反馈给调用方，并结束这次工具调用。对于 `executeZsh`、`executePowerShell` 等其他工具，提交它们各自的工具名和原始命令。

## HTTP 接入

其他语言可以直接发送 `POST /api/v1/analyze`，带上 `Authorization: Bearer <网关令牌>` 和 `Content-Type: application/json`。请求举例如下。

```json
{
  "agent_type": "opendesk",
  "session_id": "opendesk-session-001",
  "tool_name": "executeBash",
  "command": "pwd",
  "user_prompt": "查看当前工作目录"
}
```

同一会话复用 `session_id`，省略 `action_history` 时 AgentGate 会补入已有的调用历史。在看板中可以按会话查看工具名和判定原因。查询接口及错误响应见 [AgentGate HTTP API](../../README.md#http-api)。
