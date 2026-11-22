# AgentGate V1

AgentGate 在工具执行前接收 Agent 的请求，采集网关所在主机的系统信息，并将工具名、参数和会话历史发给 AgentMoss。网关记录判定结果，原工具由 Agent 在收到 `Allow` 后执行。

## 安装与启动

按[项目快速开始](../README.md#快速开始)安装并启动 AgentMoss 和 AgentGate，提交第一条请求。默认地址是 `http://127.0.0.1:9100`（AgentGate）和 `http://127.0.0.1:9090`（AgentMoss）。如果端口已占用，请按启动日志中的实际地址配置客户端和网关。

## Python 客户端

在安装了 AgentGate 的 Python 环境运行下面的示例。同一用户按默认配置启动网关时，先在客户端终端读取网关令牌。其他环境使用服务管理员分配的令牌。

```bash
export AGENT_GATE_AUTH_TOKEN="$(cat "${XDG_CONFIG_HOME:-$HOME/.config}/agentgate/auth_token")"
```

```python
from agent_gate.shared.sdk import GateClient

with GateClient(base_url="http://127.0.0.1:9100") as client:
    result = client.analyze(
        agent_type="my-agent",
        session_id="my-session-001",
        tool_name="bash",
        command="pwd",
        user_prompt="查看当前工作目录",
    )

if result.get("decision") != "Allow":
    raise PermissionError(result.get("reason") or "未获得执行许可")
print(result["decision"])
```

SDK 从 `AGENT_GATE_AUTH_TOKEN` 读取令牌，也可通过 `GateClient(api_token=...)` 显式传入。连接异常、超时或 HTTP 错误会抛出异常。将检查放在工具入口，收到 `Allow` 后再调用原工具。OpenDesk 的包装方式见[接入示例](examples/opendesk/README.md)。

只需客户端的 Python 应用也可复制 [gate_client.py](sdk/python/gate_client.py)，在有 `httpx` 的环境中使用 `from gate_client import GateClient`。嵌入 FastAPI 服务时可使用 `from agent_gate.shared.server.app import create_app` 创建应用。

## HTTP API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 公开的健康状态，`port` 是网关实际端口，`moss_status` 和 `moss_version` 来自 AgentMoss |
| POST | `/api/v1/analyze` | 提交一次待执行的工具调用，返回判定 |
| GET | `/api/v1/sessions/{session_id}/traces` | 查看会话轨迹，`tool_calls` 按 `seq` 递增排列，兼容字段 `traces` 返回同一列表 |
| GET | `/api/v1/tool_calls` | 按 `session_id`、`decision` 或 `risk_level` 等条件查询记录，`offset`、`limit` 用于分页，每页最多 500 条 |
| GET | `/api/v1/agents` | 查看 Agent 列表及调用统计 |
| GET | `/api/v1/timeline` | 按分钟汇总允许和拒绝次数，需要全局令牌 |
| GET | `/api/v1/storage_stats` | 查看存储占用，需要全局令牌 |
| DELETE | `/api/v1/tool_calls` | 删除调用记录，需要全局令牌。传入 `{"ids": [1, 2]}` 只删指定记录，传入 `{"ids": []}` 保留全部记录，省略请求体将清空全部记录 |
| GET | `/dashboard/` | 在浏览器查看调用记录和判定原因 |

### 提交分析请求

`POST /api/v1/analyze` 使用 JSON 请求体。提供 `agent_type` 和 `session_id` 标识来源，同一次会话使用相同的 `session_id`，不同会话使用不同的值。工具调用用 `tool_name` 和 `command` 描述，也可以只用 `a_next.action_type` 和 `a_next.action_detail` 描述当前动作。

| 可选字段 | 用途 |
| --- | --- |
| `user_prompt`、`description`、`cwd` | 当前用户请求、工具用途和工具实际工作目录 |
| `agent_version`、`instance_id` | 客户端版本和安装实例标识 |
| `action_history` | 已发生的动作列表。每项可含 `action_type`、`action_detail`、`output` 和 ISO 8601 格式的 `called_at`。交互工具可在 `output` 中传入用户回答 |
| `prompt_history` | 按时间排列的轮次，每项包含用户原文 `text` 和该轮的 `actions` 列表 |
| `session_history` | 其他会话的上下文，每项包含 `session_id` 和 `action_history` |
| `metadata` | 可用 `hostname` 和 `os_type` 指明运行 Agent 的主机和系统 |

省略 `action_history` 或传 `null` 时，网关从 SQLite 补入该会话最近 100 条动作。显式传入 `[]` 表示本次使用空历史。

### 读取判定

| 响应字段 | 用途 |
| --- | --- |
| `decision`、`reason` | `Allow` 或 `Deny` 及其原因 |
| `risk_level`、`risk_type` | 风险级别（`low`、`medium`、`high`、`critical`）及类别 |
| `violated_layers`、`violation_graph` | 命中的检测层，组合风险可附带包含 `nodes` 和 `edges` 的关系图 |
| `degraded`、`l3_outcome` | 本次检测的降级提示及模型分析阶段的结果 |
| `policy`、`confidence` | 策略描述、置信度（0 至 100） |
| `analysis_duration_ms`、`collected_context` | 分析耗时（毫秒）和网关采集的系统信息 |
| `source` | `agent_moss` 或 `cache`。命中缓存的调用也会单独记录 |

客户端收到 HTTP 401 时，检查它发给 AgentGate 的令牌。若 HTTP 200 但 `decision` 为 `Deny`、`risk_type` 为 `service_unavailable`，请检查网关访问 AgentMoss 的地址和令牌。`reason` 含 `AgentMoss HTTP 401` 表示 AgentMoss 拒绝了网关凭据。`analysis_timeout` 表示分析超时。

## 鉴权

**Agent 调 AgentGate** 时，请求头使用 `Authorization: Bearer <网关令牌>`。默认 `AGENT_GATE_AUTH_MODE=auto`，首次启动时自动生成全局令牌，写入 `${XDG_CONFIG_HOME:-$HOME/.config}/agentgate/auth_token`，权限为 `0600`。在本机浏览器打开 `http://127.0.0.1:9100/dashboard/?token=<网关全局令牌>`，将占位符换为实际令牌。登录后可直接访问 `/dashboard/`。交互终端启动时也会显示登录地址，重定向到文件或 systemd 日志的地址会隐藏令牌。

| 网关环境变量 | 用途 |
| --- | --- |
| `AGENT_GATE_AUTH_TOKEN` | 指定全局令牌 |
| `AGENT_GATE_AUTH_TOKEN_FILE`、`AGENT_GATE_AUTH_TOKEN_FD` | 从文件或继承的文件描述符读取全局令牌 |
| `AGENT_GATE_AGENT_TOKENS` | 为不同 Agent 分配令牌，格式为 `opendesk=令牌A,qwenpaw=令牌B`。请求中的 `agent_type` 须与令牌匹配 |
| `AGENT_GATE_AUTH_MODE` | `auto` 启用鉴权，`off` 关闭鉴权 |

**AgentGate 调 AgentMoss** 使用另一枚令牌。默认同机、同用户下，网关会读取 `~/.config/agentmoss/auth_token`。跨用户或跨主机部署时，将 AgentMoss 令牌保存在网关账户可读的文件，并为网关设置 `AGENT_MOSS_AUTH_TOKEN_FILE`。网关按以下顺序取第一个可用值。

1. `AGENT_GATE_MOSS_TOKEN`
2. `AGENT_MOSS_AUTH_TOKEN`
3. `AGENT_MOSS_AUTH_TOKEN_FILE` 指向的文件
4. `AGENT_MOSS_DATA_DIR/auth_token`（设置了数据目录时），或 `~/.config/agentmoss/auth_token`

## 配置与存储

配置示例见 [agent_gate.yaml](config/agent_gate.yaml)。启动网关时用 `--config` 指定 YAML 文件，用 `--host`、`--port` 指定监听地址。AgentMoss 地址按 `--security-url`（别名 `--moss-url`）、`AGENT_MOSS_URL`、`MOSS_URL`、YAML 中的 `agent_moss.url` 的顺序取值。

| 参数或环境变量 | 用途 |
| --- | --- |
| `--mode http` | 用 TCP 提供 HTTP 服务（默认）。同机部署可指定 `--host 127.0.0.1` |
| `--mode socket --socket /path/to/agentgate.sock` | 通过 Unix socket 提供服务，将路径换为网关账户可写的位置 |
| `--db-path`、`AGENT_GATE_STORAGE_DB_PATH` | 指定 SQLite 文件路径。默认路径为 `${XDG_DATA_HOME:-$HOME/.local/share}/agent_gate/agent_gate.db` |
| `AGENT_GATE_CORS_ORIGINS`、`AGENT_GATE_ORIGIN_CHECK` | 配置浏览器跨域来源、来源校验 |
| `AGENT_GATE_RATE_LIMIT_QPS`、`AGENT_GATE_POOL_BIZ`、`AGENT_GATE_MAX_BODY_BYTES` | 调整请求速率、业务并发数、请求体字节数上限 |
| `AGENT_GATE_MAX_RECORD_BYTES` | 限制单条 `command`、`action_detail` 字段的留存长度。超限内容保留头部及截断标记 |
| `AGENT_GATE_DISK_DEGRADE_PERCENT` | 剩余磁盘空间低于指定百分比时，进一步缩短命令和原因字段，省略描述 |
| `AGENT_GATE_ARCHIVE_THRESHOLD`、`AGENT_GATE_ARCHIVE_KEEP` | 触发归档的记录数及主库保留条数。维护线程在启动时和运行期间每 6 小时检查一次 |

需要清理指定记录时，用上面的 `DELETE /api/v1/tool_calls` 接口传入记录 ID。当前版本的 `agent-gate clean` 会使用当前时间作为清理截止点，`--retention-days` 参数无法保留最近若干天的历史。

## 部署

在支持 systemd 用户服务的 Linux 上，可用同一账户托管两个服务。先运行 `systemctl --user status` 确认能连接当前用户的服务管理器。若出现 `Failed to connect to bus`，可以使用快速开始中的前台启动方式，或请管理员配置服务托管。

先完成[项目快速开始](../README.md#快速开始)，记下仓库的绝对路径。如果要沿用快速开始的端口，先在两个前台服务的终端中按 `Ctrl+C`。

创建目录 `~/.config/systemd/user/`，将下面两份文件保存到该目录。两处 `/path/to/AgentMoss` 都要替换为仓库的绝对路径。示例使用内置的 L1、L2 检测和默认令牌。

**agent-moss.service**

```ini
[Unit]
Description=AgentMoss

[Service]
Environment=AGENT_MOSS_DISABLE_LLM=1
ExecStart="/path/to/AgentMoss/AgentMoss/.venv/bin/agent-moss" server --host 127.0.0.1 --port 9090
Restart=on-failure

[Install]
WantedBy=default.target
```

**agentgate.service**

```ini
[Unit]
Description=AgentGate
Wants=agent-moss.service
After=agent-moss.service

[Service]
ExecStart="/path/to/AgentMoss/AgentGate/.venv/bin/agent-gate" server --host 127.0.0.1 --port 9100 --security-url http://127.0.0.1:9090
Restart=on-failure

[Install]
WantedBy=default.target
```

以该用户加载并启动服务。`enable --now` 会立即启动服务，下次启动该用户的 systemd 管理器时也会自动加载。

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-moss.service agentgate.service
systemctl --user status agent-moss.service agentgate.service
journalctl --user -u agent-moss.service -u agentgate.service -n 30
```

根据日志中的监听地址再次[提交工具调用](../README.md#提交第一次工具调用)。需要停用时执行 `systemctl --user disable --now agentgate.service agent-moss.service`。分别在不同主机上部署时，在每台主机上只托管对应的服务，移除网关单元中的 `Wants` 和 `After`，并按[跨机部署说明](../AgentMoss/integrations/deepagents-plugin/docs/deployment.md)配置地址和令牌。

## 许可证

[MIT](LICENSE)
