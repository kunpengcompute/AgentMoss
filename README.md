# AgentGate V1

AgentGate 是 AI Agent 数据采集与安全分析中间层。Agent 在每次工具调用前把动作发给
AgentGate，AgentGate 采集系统上下文并转发给 AgentMoss 判定，返回 `Allow` / `Deny`。

本仓库是 **Python + V1** 裁剪版：

- 只保留 SDK / 主动上报模式（`agent-gate server`）；
- 不包含 TypeScript 实现；
- 不包含 V3 的 API 代理、PTY、eBPF、MCP、trigger、sessionlog、dashboard 代理等模块。

## 架构

```
┌──────────────┐   POST /api/v1/analyze   ┌──────────────┐   POST /api/v1/analyze   ┌──────────────┐
│  Agent / SDK │ ───────────────────────▶ │  AgentGate   │ ───────────────────────▶ │  AgentMoss   │
│              │ ◀─────────────────────── │     V1       │ ◀─────────────────────── │              │
└──────────────┘   Allow / Deny + reason  └──────────────┘   Allow / Deny + reason  └──────────────┘
```

AgentGate 负责：

- 采集系统 / 进程 / 网络 / 用户 / 环境上下文；
- 按 `session_id` 从 SQLite 自动补齐 `action_history`；
- 把请求转发给 AgentMoss，并把判定结果落库；
- 提供看板、查询接口和存储治理。

## 安装

要求 Python >= 3.10。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

安装后会得到 `agent-gate` 命令行。

也可以只拷贝 `sdk/python/gate_client.py` 作为独立客户端使用，不依赖本包。
默认鉴权开启时需传 `api_token=` 或设置 `AGENT_GATE_AUTH_TOKEN`。

## 快速开始

1. 先启动 AgentMoss（默认 `http://127.0.0.1:9090`）。
2. 启动 AgentGate：

```bash
agent-gate server --host 0.0.0.0 --port 9100 \
  --security-url http://127.0.0.1:9090
```

3. 健康检查：

```bash
curl http://127.0.0.1:9100/api/v1/health
```

4. 发起一次分析：

```bash
curl -X POST http://127.0.0.1:9100/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
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

`Authorization` 头在默认鉴权模式下必填，见下文「鉴权」。

## CLI

```bash
agent-gate --version
agent-gate server --host 0.0.0.0 --port 9100 [--config config/agent_gate.yaml] [--db-path /path/to.db]
agent-gate clean --retention-days 30 [--config config/agent_gate.yaml]
```

`server` 支持两种模式：

- `--mode http`（默认）：TCP 监听；
- `--mode socket --socket /path/to.sock`：Unix Domain Socket。

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
| DELETE | `/api/v1/tool_calls` | 清空调用记录 |
| GET | `/api/v1/agents` | agent 列表与调用统计 |
| GET | `/api/v1/timeline` | 最近 N 分钟的 Allow / Deny 时间线 |
| GET | `/api/v1/storage_stats` | 存储用量与条数 |
| GET | `/dashboard/` | 内置看板 |

请求字段（最小集）：`agent_type`、`session_id`、`tool_name`、`command`。

可选字段：`agent_version`、`cwd`、`description`、`user_prompt`、
`prompt_history`、`action_history`、`metadata`。

`action_history` 为空时，AgentGate 会根据 `session_id` 从本地 SQLite 自动补齐。

## 鉴权与安全加固

### 鉴权

默认 `AGENT_GATE_AUTH_MODE=auto`：首次启动自动生成 token，写入
`<data_root>/auth_token`（权限 0600），除 `/api/v1/health` 外的请求都要带
`Authorization: Bearer <token>`。

| 环境变量 | 说明 |
|---|---|
| `AGENT_GATE_AUTH_MODE` | `auto`（默认）/ `off` |
| `AGENT_GATE_AUTH_TOKEN` | 显式指定全局 token |
| `AGENT_GATE_AUTH_TOKEN_FILE` | 从文件读取 token |
| `AGENT_GATE_AUTH_TOKEN_FD` | 从文件描述符读取 token |
| `AGENT_GATE_AGENT_TOKENS` | 逐 agent token，如 `opendesk=tok-a,qwenpaw=tok-b` |

看板和 Console 的浏览器导航无法带 Bearer 头，因此支持 `?token=<token>`
（服务端换取 Cookie）。跨源访问通过 `AGENT_GATE_CORS_ORIGINS` 控制。

### 其他加固

- **来源校验**：`AGENT_GATE_ORIGIN_CHECK` 控制浏览器请求的 Origin/Host 校验；
- **限流与并发池**：`AGENT_GATE_MAX_INFLIGHT`、`AGENT_GATE_POOL_BIZ`、
  `AGENT_GATE_RATE_LIMIT_QPS`、`AGENT_GATE_MAX_BODY_BYTES` 等；
- **逐 agent 数据隔离**：`/api/v1/sessions/{id}/traces` 按 token 绑定的
  agent 过滤，并带跨会话批量访问检测（`AGENT_GATE_SESSION_SWEEP_*`）；
- **看板 XSS 防护**：所有不可信字段渲染前转义；HTML 页面带 CSP 与安全响应头；
- **存储治理**：
  - 单条记录超过 `AGENT_GATE_MAX_RECORD_BYTES` 时截断并附长度/校验值；
  - 磁盘剩余低于 `AGENT_GATE_DISK_DEGRADE_PERCENT` 时新记录降级为摘要；
  - 条数达到 `AGENT_GATE_ARCHIVE_THRESHOLD` 时归档旧记录（保留
    `AGENT_GATE_ARCHIVE_KEEP` 条）并 VACUUM；
  - 后台维护线程启动时执行一次，之后每 6 小时执行一次；
- **日志脱敏**：访问日志中的 token 参数打码。

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

```bash
bash scripts/install-systemd-py.sh
bash scripts/status-agentgate-py.sh
bash scripts/uninstall-systemd-py.sh
```

模板见 `deploy/agentgate.service.template`，默认执行：

```
agent-gate server --host 0.0.0.0 --port 9100
```

## 开发与测试

```bash
PYTHONPATH=. python3 -m pytest agent_gate/tests/v1 -q
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
