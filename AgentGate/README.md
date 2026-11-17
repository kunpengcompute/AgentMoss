# AgentGate V1

AgentGate 是 AI Agent 数据采集与安全分析中间层。Agent 在每次工具调用前把动作发给
AgentGate，AgentGate 采集系统上下文并转发给 AgentMoss 判定，返回 `Allow` / `Deny`。

本组件使用 Python 实现，提供 V1 HTTP API、Python SDK 和可视化看板。
接入方在执行工具前主动提交动作，并根据返回的判定执行或拒绝该动作。

## 架构

```
┌──────────────┐   POST /api/v1/analyze   ┌──────────────┐   POST /api/v1/analyze   ┌──────────────┐
│  Agent / SDK │ ───────────────────────▶ │  AgentGate   │ ───────────────────────▶ │  AgentMoss   │
│              │ ◀─────────────────────── │     V1       │ ◀─────────────────────── │              │
└──────────────┘   Allow / Deny + reason  └──────────────┘   Allow / Deny + reason  └──────────────┘
```

AgentGate 负责：

- 采集系统 / 进程 / 网络 / 用户 / 环境上下文；
- 按 `session_id` 从 SQLite 自动补齐最近 100 条 `action_history`，保持调用顺序；
- 把请求转发给 AgentMoss，并把判定结果落库；
- 提供看板、查询接口和存储治理。

## 安装

要求 Python >= 3.10，推荐 Python 3.12。完整双服务安装流程见[项目快速开始](../README.md#快速开始)。以下命令在仓库根目录执行，先准备 AgentGate 的独立环境：

```bash
uv venv --python 3.12 ./AgentGate/.venv
uv pip install --python ./AgentGate/.venv/bin/python -e ./AgentGate
```

安装后使用 `./AgentGate/.venv/bin/agent-gate` 启动网关。也可以只拷贝
`sdk/python/gate_client.py` 作为独立客户端使用，不依赖本包；默认鉴权开启时
需传 `api_token=` 或设置 `AGENT_GATE_AUTH_TOKEN`。

**前置依赖：AgentMoss 必须单独安装并启动。** AgentGate 只负责采集上下文和转发，
真实的 Allow/Deny 判定由 AgentMoss 完成：

```bash
uv venv --python 3.12 ./AgentMoss/.venv
uv pip install --python ./AgentMoss/.venv/bin/python -e ./AgentMoss
```

AgentMoss 默认开启鉴权（`AGENT_MOSS_AUTH_MODE=auto`），首次启动会在
`~/.config/agentmoss/auth_token` 生成 token。AgentGate 必须能读到该 token，
否则会返回 `Deny` + `AgentMoss HTTP 401`（见下文「AgentGate 调 AgentMoss 的鉴权」）。

> 如果只启动 AgentGate、没有安装/启动 AgentMoss：AgentGate 本身可以启动，
> `/api/v1/health` 仍返回 200，但 `moss_status` 为 `unreachable`、`moss_version`
> 为 `N/A`；`/api/v1/analyze` 会按 **fail-closed** 返回 `Deny`（HTTP 200，
> `risk_type=service_unavailable`，`source` 仍为 `agent_moss`，reason 类似
> `AgentMoss security service unavailable: ...`），这不是正常的安全判定结果，
> 不能用于验收。

## 快速开始

以下步骤均从仓库根目录执行。

1. 启动 AgentMoss，使用不需要模型凭据的 L1/L2 检测：

```bash
AGENT_MOSS_DISABLE_LLM=1 ./AgentMoss/.venv/bin/agent-moss \
  server --host 127.0.0.1 --port 9090
```

另开终端：

```bash
until curl -sf http://127.0.0.1:9090/api/v1/health >/dev/null; do
  sleep 0.2
done

export MOSS_TOKEN=$(cat ~/.config/agentmoss/auth_token)
```

2. 启动 AgentGate，**显式告诉它 AgentMoss token**：

```bash
MOSS_URL=http://127.0.0.1:9090 \
AGENT_GATE_MOSS_TOKEN="$MOSS_TOKEN" \
./AgentGate/.venv/bin/agent-gate server --host 127.0.0.1 --port 9100
```

3. 等待并检查 AgentGate 健康：

```bash
until curl -sf http://127.0.0.1:9100/api/v1/health >/dev/null; do
  sleep 0.2
done
curl http://127.0.0.1:9100/api/v1/health
```

4. 读取 AgentGate 自己的 token，发起一次分析：

```bash
GATE_TOKEN=$(cat ~/.config/agentgate/auth_token)

curl -X POST http://127.0.0.1:9100/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATE_TOKEN" \
  -d '{
    "agent_type": "my-agent",
    "agent_version": "0.1.0",
    "session_id": "sess-001",
    "tool_name": "bash",
    "command": "ls -la",
    "cwd": "/home/user/project",
    "description": "list files",
    "user_prompt": "show me the files"
  }'
```

`Authorization` 头在默认鉴权模式下必填，见下文「鉴权」。若 AgentGate 已在 systemd/service 用户下运行，`AGENT_GATE_MOSS_TOKEN` 的配置见「AgentGate 调 AgentMoss 的鉴权」。

## CLI

以下命令在 `AgentGate/` 组件目录执行，并先激活其虚拟环境：

```bash
. .venv/bin/activate
agent-gate --version
agent-gate server --host 0.0.0.0 --port 9100 [--config config/agent_gate.yaml] [--db-path /path/to.db]
agent-gate clean --retention-days 30 [--config config/agent_gate.yaml]
```

`server` 支持两种模式：

- `--mode http`（默认）：TCP 监听；
- `--mode socket --socket /path/to.sock`：Unix Domain Socket。

HTTP 端口被占用时，AgentGate 会在同一监听地址上向后查找可用端口，最多尝试
100 个端口。以启动日志中的实际地址访问服务，并同步更新客户端配置；全部占用时
启动失败。

AgentMoss 地址解析优先级：

```
--security-url / --moss-url
  > AGENT_MOSS_URL
  > MOSS_URL
  > config/agent_gate.yaml 的 agent_moss.url
  > http://127.0.0.1:9090
```

## Python import

```python
from agent_gate.v1.sdk import GateClient

client = GateClient(
    base_url="http://127.0.0.1:9100",
    api_token="<token>",  # 也可用环境变量 AGENT_GATE_AUTH_TOKEN
)

result = client.analyze(
    agent_type="my-agent",
    session_id="sess-001",
    tool_name="bash",
    command="cat ~/.ssh/id_rsa",
    cwd="/home/user/project",
    user_prompt="read the key file",
)

if result["decision"] == "Deny":
    raise RuntimeError(result["reason"])
```

服务端也可以作为 FastAPI app 直接嵌入：

```python
from agent_gate.v1.server.app import create_app

app = create_app()
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查（公开） |
| POST | `/api/v1/analyze` | 分析一次工具调用 |
| GET | `/api/v1/sessions/{session_id}/traces` | 某会话的完整调用轨迹 |
| GET | `/api/v1/tool_calls` | 查询调用记录（`limit` 上限 500，超出静默截断） |
| DELETE | `/api/v1/tool_calls` | 清空调用记录；传 `{"ids": [...]}` 可只删指定记录 |
| GET | `/api/v1/agents` | agent 列表与调用统计 |
| GET | `/api/v1/timeline` | 最近 N 分钟的 Allow / Deny 时间线 |
| GET | `/api/v1/storage_stats` | 存储用量与条数 |
| GET | `/dashboard/` | 内置看板 |

#### POST /api/v1/analyze

必填字段：`agent_type`、`session_id`，以及 `tool_name` + `command` **或** `a_next`（二选一；`a_next` 存在时从 `action_type`/`action_detail` 兜底）。

可选字段：`agent_version`、`cwd`、`description`、`user_prompt`、`instance_id`、
`a_next`、`prompt_history`、`action_history`、`session_history`、`metadata`。

嵌套字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `a_next` | object | `{action_type, action_detail, called_at?}`；只发这个字段时 `tool_name`/`command` 从它兜底 |
| `action_history[]` | array | `[{action_type, action_detail, output, called_at}]`。`output` 是工具返回内容；对 `ask_user_question` 是用户回答，供 L2 授权规则判断。`called_at` 是动作时间（ISO 8601），供授权 300s 时效窗使用 |
| `prompt_history[]` | array | `[{text, actions: [ActionHistoryItem]}]`，按“提示词 → 该提示触发的动作”组织；`actions[]` 字段同 `action_history[]` |
| `session_history[]` | array | `[{session_id, action_history: [...]}]`；调用方显式提供时优先使用，否则从本地 SQLite 组装 |
| `metadata` | object | 可选扩展；`hostname` 用于生成 agent 标识，`os_type` 用于覆盖 Agent 侧平台检测 |

`action_history` **未提供（不传或 null）** 时，AgentGate 会根据 `session_id` 从本地 SQLite 自动补齐；显式传 `[]` 表示使用空历史，不会自动补齐。

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision` | string | `Allow` / `Deny` |
| `reason` | string | 决策原因 |
| `risk_level` | string | `low` / `medium` / `high` / `critical` |
| `risk_type` | string | 风险类别 |
| `violated_layers` | array | 命中层，如 `["L1.1", "L2.2"]` |
| `policy` | string | Allow 时生成的最小策略 |
| `confidence` | int | 置信度 0–100 |
| `analysis_duration_ms` | float | 本次分析耗时（毫秒） |
| `violation_graph` | object/null | 组合式拦截时的结构化攻击链图 `{nodes, edges}` |
| `collected_context` | object/null | AgentGate 采集的系统上下文 |
| `source` | string | 结果来源：`agent_moss` / `cache`。AgentMoss 不可用时的 fail-closed Deny 也标为 `agent_moss`，用 `risk_type=service_unavailable` / `analysis_timeout` 区分 |

`GET /api/v1/sessions/{session_id}/traces` 使用 `tool_calls` 字段返回调用轨迹，
同时提供内容相同的 `traces` 兼容字段，均按会话内的 `seq` 递增排序。
命中判定缓存的调用也会保存独立的调用记录和系统快照，并计入会话调用次数。

#### GET /api/v1/health

公开端点，无需鉴权。响应字段：`status`（`healthy`）、`version`、`timestamp`、`instance`、`moss_version`、`moss_status`。

#### DELETE /api/v1/tool_calls

破坏性操作，只接受**全局 token**（逐 agent token 返回 403）。请求体可省略：

| 请求体 | 行为 |
|---|---|
| 不传 body | 清空全部调用记录（`tool_calls` + `system_snapshots`，session 计数归零） |
| `{"ids": [1, 2]}` | 只删除指定记录 |
| `{"ids": []}` | 不删除任何记录 |

响应：`{"deleted": N}`。

看板上的 **Delete Records** 多选模式调用同一接口：可删除所选或全部，删除前弹确认框；浏览器通过 `?token=` 换过 Cookie 后，同源 `fetch` 无需再带 Bearer 头。

## 鉴权与安全加固

### 鉴权

默认 `AGENT_GATE_AUTH_MODE=auto`：首次启动自动生成 token，写入
`~/.config/agentgate/auth_token`（`XDG_CONFIG_HOME` 设置时为
`$XDG_CONFIG_HOME/agentgate/auth_token`，权限 0600），除 `/api/v1/health` 外的请求都要带
`Authorization: Bearer <token>`。

| 环境变量 | 说明 |
|---|---|
| `AGENT_GATE_AUTH_MODE` | `auto`（默认）/ `off` |
| `AGENT_GATE_AUTH_TOKEN` | 显式指定全局 token |
| `AGENT_GATE_AUTH_TOKEN_FILE` | 从文件读取 token |
| `AGENT_GATE_AUTH_TOKEN_FD` | 从文件描述符读取 token |
| `AGENT_GATE_AGENT_TOKENS` | 逐 agent token，如 `opendesk=tok-a,qwenpaw=tok-b` |

看板的浏览器导航无法带 Bearer 头，因此支持 `?token=<token>`
（服务端换取 Cookie）。跨源访问通过 `AGENT_GATE_CORS_ORIGINS` 控制。

#### AgentGate 调 AgentMoss 的鉴权（AgentMoss 401）

AgentGate 和 AgentMoss **各自有独立的 token**，不能混用：

- curl → AgentGate：`AGENT_GATE_*` / `~/.config/agentgate/auth_token`
- AgentGate → AgentMoss：`AGENT_MOSS_*` / `~/.config/agentmoss/auth_token`

AgentGate 读取 AgentMoss token 的优先级：

```text
AGENT_GATE_MOSS_TOKEN
> AGENT_MOSS_AUTH_TOKEN
> AGENT_MOSS_AUTH_TOKEN_FILE
> $AGENT_MOSS_DATA_DIR/auth_token
> ~/.config/agentmoss/auth_token
```

> **root/systemd 注意**：AgentMoss（sudo/systemd/直接运行）和 AgentGate 在 root
> 下默认使用 `/root/.config/agentmoss`，两边默认查找路径一致。若 AgentGate 以**非 root** 用户运行，则读不到 root 家目录
> 下 0600 的 token，必须显式配置 `AGENT_GATE_MOSS_TOKEN` /
> `AGENT_MOSS_AUTH_TOKEN_FILE`，或给两边设置同一个可读的 `AGENT_MOSS_DATA_DIR`。

若 AgentGate 与 AgentMoss 由不同系统用户启动（例如 systemd 的
`User=agent_gate`），AgentGate 读不到 AgentMoss 用户目录下的 0600 token，
必须显式传入：

```bash
MOSS_TOKEN=$(cat /path/to/agentmoss/auth_token)

MOSS_URL=http://127.0.0.1:9090 \
AGENT_GATE_MOSS_TOKEN="$MOSS_TOKEN" \
agent-gate server --host 0.0.0.0 --port 9100
```

systemd 部署可在 `/etc/systemd/system/agentgate.service.d/override.conf` 中设置：

```ini
[Service]
Environment=MOSS_URL=http://127.0.0.1:9090
Environment=AGENT_GATE_MOSS_TOKEN=<AgentMoss token>
```

然后执行 `systemctl daemon-reload && systemctl restart agentgate`。

**401 的典型表现**：curl 到 AgentGate 返回 HTTP 200，但 body 是
`decision=Deny`，reason 包含 `AgentMoss HTTP 401`。这表示 Gate 自己的鉴权已通过，
失败发生在 Gate→Moss 这一段（AgentGate 对 Moss 不可用采用 fail-closed）。
先直连 Moss 验证 token：

```bash
MOSS_TOKEN=$(cat ~/.config/agentmoss/auth_token)
curl -sS -X POST http://127.0.0.1:9090/api/v1/analyze \
  -H "Authorization: Bearer $MOSS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"direct-1","prompt_session":"","action_history":[],"a_next":{"action_type":"bash","action_detail":"ls -la /tmp"},"reason":"direct test"}'
```

直连也返回 401 时，检查 AgentMoss 实际 token 路径（`AGENT_MOSS_DATA_DIR` 是否设置）：

```bash
journalctl -u agent-moss -n 100 --no-pager | grep -i token
sudo find /home /root -path '*/.config/agentmoss/auth_token' 2>/dev/null
```

AgentMoss 使用 `AGENT_MOSS_AGENT_TOKENS`（逐 agent token）时，Gate 必须使用对应
agent 的 token，并保证请求的 `agent_type` 与 token 绑定关系一致。

### 其他加固

- **来源校验**：`AGENT_GATE_ORIGIN_CHECK` 控制浏览器请求的 Origin/Host 校验；
- **限流与并发池**：`AGENT_GATE_MAX_INFLIGHT`、`AGENT_GATE_POOL_BIZ`、
  `AGENT_GATE_RATE_LIMIT_QPS`、`AGENT_GATE_MAX_BODY_BYTES` 等；
- **逐 agent 数据隔离**：`/api/v1/sessions/{id}/traces` 按 token 绑定的
  agent 过滤，并带跨会话批量访问检测（`AGENT_GATE_SESSION_SWEEP_*`）；
- **看板 XSS 防护**：所有不可信字段渲染前转义；HTML 页面带 CSP 与安全响应头；
- **存储治理**：
  - `command` / `action_detail` 超过 `AGENT_GATE_MAX_RECORD_BYTES` 时按 UTF-8 字节保留头部并附长度/校验值；
  - 磁盘剩余低于 `AGENT_GATE_DISK_DEGRADE_PERCENT` 时，新记录的命令头部保留 512 字节、`reason` 保留 256 字节、`description` 清空；
  - 条数达到 `AGENT_GATE_ARCHIVE_THRESHOLD` 时归档旧记录（保留
    `AGENT_GATE_ARCHIVE_KEEP` 条）并 VACUUM；
  - 后台维护线程启动时执行一次，之后每 6 小时执行一次；
- **日志脱敏**：访问日志中的 token 参数打码。

常态下 `reason` / `description` 最多保留 4096 字节头部，截断标记另占少量空间。
`moss_response` 仍保存完整分析响应，以上字段限制不代表整条数据库记录的大小上限。

## 配置

默认配置见 `config/agent_gate.yaml`。支持用 YAML 文件或环境变量覆盖：

```bash
# AGENT_GATE_<SECTION>_<KEY>
export AGENT_GATE_SERVER_PORT=9100
export AGENT_GATE_STORAGE_DB_PATH=/var/lib/agent_gate/agent_gate.db
```

`storage.db_path` 默认是哨兵值 `auto`，解析到：

```
$XDG_DATA_HOME/agent_gate/agent_gate.db
（默认 ~/.local/share/agent_gate/agent_gate.db）
```

## 部署

Linux 环境可使用 systemd 托管网关。在 `AgentGate/` 组件目录执行安装脚本：

```bash
bash scripts/install-systemd-py.sh
```

脚本会检查并按需创建 `AgentGate/venv/`，使用这个环境中的 `agent-gate`，
然后询问是否安装、启动 `agentgate.service`。该环境与快速开始中的 `.venv/`
路径不同。安装前应先完成双服务健康检查与真实分析验证，并按上面的鉴权说明
配置网关访问 AgentMoss 的地址和令牌。

查看状态：

```bash
bash scripts/status-agentgate-py.sh
```

需要卸载服务时执行：

```bash
bash scripts/uninstall-systemd-py.sh
```

模板见 `deploy/agentgate.service.template`，默认执行：

```
agent-gate server --host 0.0.0.0 --port 9100
```

## 开发与测试

在 `AgentGate/` 组件目录执行：

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest agent_gate/tests -q
```

## 目录结构

```
agent_gate/
├── v1/
│   ├── cli.py              # agent-gate server / clean
│   ├── config.py           # YAML + 环境变量配置
│   ├── multi_session.py    # 跨会话分析开关
│   ├── normalizer.py        # 请求归一化 + action_history/session_history 组装
│   ├── sdk.py              # Python SDK
│   ├── collector/          # 系统/进程/网络/用户/环境采集
│   ├── server/             # FastAPI 应用、路由、看板、安全策略
│   ├── storage/            # SQLite、Repository、记录守卫、归档维护
│   └── client/             # AgentMoss 客户端、决策缓存
└── tests/v1/               # V1 回归测试
sdk/python/gate_client.py   # 可独立拷贝的客户端
```

## License

MIT
