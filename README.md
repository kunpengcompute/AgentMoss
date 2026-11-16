# AgentMoss

多层级 AI Agent 安全分析引擎。输入「下一步动作 + 上下文」，输出 `Allow` / `Deny`、
风险级别与原因。可直接作为 Python 库嵌入，也可以启动 HTTP 服务由 AgentGate 调用。

本仓库是 **Python** 裁剪版：

- 只包含 Python 实现，不包含 TypeScript 版；
- 不包含 Brain 自学习模块（规则 / Skill 自动提取、curator、`/api/v1/brain/*`
  接口与 `agent-moss brain` 子命令均已移除）；
- L1 / L2 / L3 检测、规则、Skill、场景与 agent 扩展均保留。

## 三层防御

| 层 | 内容 |
|---|---|
| L1.1 | 特征匹配：危险命令、注入检测、用户敏感规则 |
| L1.2 | 脚本内容预扫描：递归追踪脚本调用链 |
| L2.1 | 逻辑规则：read-before-write、意图一致性、敏感路径、危险模式 |
| L2.2 | 形式化验证：行为图 + 性质验证，含跨会话组合链 |
| L2.3 | 脚本内容 L2 扫描 |
| L3.1 | LLM + Skill 深度语义分析（可用 `AGENT_MOSS_DISABLE_LLM=1` 关闭） |

## 安装

需要 Python 3.10 或更新版本。建议先创建虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
```

商用裁剪版从本仓库源码安装。公共 PyPI 上的 `agent-moss` 是上游完整版，
不能作为本仓库的安装来源：

```bash
git clone https://gitcode.com/kenhkl/AgentMoss-commercial.git AgentMoss
cd AgentMoss
python -m pip install -e .
```

`-e` 会直接使用当前目录的源码，修改代码后无需重新安装。安装完成后可使用 `agent-moss` 命令。

### 在 DeepAgents 中启用安全检查

如果你的 Agent 使用 DeepAgents 开发，可以接入本插件，在执行工具前检查操作是否安全。
插件把工具名称和参数发给 AgentGate，由 AgentGate 调用 Moss 分析。
返回 `Allow` 时继续执行，返回 `Deny` 时拒绝这次工具调用。

接入分为三步：

1. **启动 Gate 和 Moss 服务。** Gate 接收插件的检查请求，Moss 给出安全判定。部署方法见[部署说明](integrations/deepagents-plugin/docs/deployment.md)。
2. **把插件装到运行 DeepAgents 的 Python 环境中。** 使用 Python 3.11 或更新版本，先激活该应用的虚拟环境，再执行：

   ```bash
   python -m pip install -e '/path/to/AgentMoss[deepagents-aio]'
   ```

   将路径替换为包含本插件的商用源码目录；公共 PyPI 的同名包不能替代该安装。

3. **在 DeepAgents 应用代码中接入插件。** 配置 Gate 地址和访问令牌，并修改创建 Agent 的代码，为主代理和每个子代理添加插件的检查组件。完整代码见[插件接入说明](integrations/deepagents-plugin/README.md)。

## 快速开始

### CLI

```bash
# 生成输入模板
agent-moss init -o input.json

# 分析输入文件
agent-moss analyze input.json

# 或直接分析一条命令
agent-moss analyze "cat ~/.ssh/id_rsa" --json

# 启动 HTTP 服务（默认 127.0.0.1:9090）
agent-moss server --host 0.0.0.0 --port 9090

# 服务状态
agent-moss status

# 安装 / 卸载 systemd 服务
agent-moss install --enable

# 规则目录自查与模板生成
agent-moss rules status
agent-moss rules init --dir ./rules
```

常用环境变量：

```bash
AGENT_MOSS_DISABLE_LLM=1      # 只跑 L1/L2，不调用 LLM
AGENT_MOSS_LLM_BASE_URL=...   # OpenAI 兼容接口
AGENT_MOSS_LLM_API_KEY=...
AGENT_MOSS_LLM_MODEL=...
AGENT_MOSS_CONFIG_PATH=...    # 指定配置文件
AGENT_MOSS_DATA_DIR=...       # 指定数据目录
```

### Python import

```python
from agent_moss.engine.analyzer import judge_security, analyze
from agent_moss.infra.config import get_default_config

config = get_default_config()

# 只做安全判定
judgment = judge_security(
    prompt_session="list files in the project",
    action_history=[],
    a_next={"action_type": "bash", "action_detail": "ls -la"},
    reason="list files",
    config=config,
)
print(judgment.allowed, judgment.reason, judgment.risk_level)

# 判定 + 允许时生成最小 policy
result = analyze(
    session_id="sess-001",
    prompt_session="list files in the project",
    action_history=[],
    a_next={"action_type": "bash", "action_detail": "ls -la"},
    reason="list files",
    config=config,
)
print(result["decision"], result["reason"])
```

也可以直接拿到 FastAPI 应用：

```python
from agent_moss.server.app import create_app

app = create_app()
```

### HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/analyze` | 安全分析 |
| POST | `/api/v1/review` | 低频全量回顾（供 AgentGate 触发） |
| GET | `/console` | 运行时控制台 |

#### POST /api/v1/analyze

请求示例：

```json
{
  "session_id": "sess-001",
  "prompt_session": "list files",
  "prompt_history": [
    {
      "text": "list files",
      "actions": [
        {"action_type": "bash", "action_detail": "ls -la"}
      ]
    }
  ],
  "action_history": [
    {
      "action_type": "bash",
      "action_detail": "pwd",
      "called_at": "2026-11-10T10:00:00"
    }
  ],
  "session_history": [
    {
      "session_id": "other-session",
      "action_history": [
        {"action_type": "bash", "action_detail": "cat /tmp/report.txt"}
      ]
    }
  ],
  "a_next": {
    "action_type": "bash",
    "action_detail": "ls -la",
    "called_at": "2026-11-10T10:00:01"
  },
  "reason": "list files",
  "agent_id": "",
  "instance_id": "",
  "os_type": "linux",
  "cwd": "/home/user/project",
  "metadata": {"stateful_incremental": false}
}
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | 会话唯一标识 |
| `prompt_session` | string | 否 | 最近一条用户提示 |
| `prompt_history` | array | 否 | 交错 prompt 历史：`[{text, actions: [{action_type, action_detail}]}]` |
| `action_history` | array | 否 | 历史动作：`[{action_type, action_detail, called_at?, output?}]`。`ask_user_question` 动作应带 `output`（用户回答），否则改密/删用户授权会被判未授权 |
| `session_history` | array | 否 | 多 session 历史：`[{session_id, action_history: [{action_type, action_detail}]}]`，供 L2.2 跨会话验证 |
| `a_next` | object | 是 | 下一步动作：`{action_type, action_detail, called_at?}` |
| `reason` | string | 否 | 执行理由 |
| `os_type` | string | 否 | `linux` / `windows` / 空=自动检测 |
| `cwd` | string | 否 | 当前工作目录 |
| `agent_id` | string | 否 | 调用方 agent 标识（如 `xiaoo` / `opendesk`），空=通用模式 |
| `instance_id` | string | 否 | agent 安装实例标识，仅用于展示层脱敏 |
| `metadata` | object | 否 | 扩展字段；支持 `llm_config`、`llm_log_path`、`stateful_incremental`、`is_error` 等 |

> `action_type` / `action_detail` 在 `a_next` 和 `action_history` 项中均必填；`called_at` 为 ISO 8601 时间，供 L2 授权时效窗和 P4 提权外传窗口使用。

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision` | string | `Allow` / `Deny` |
| `reason` | string | 决策原因 |
| `risk_level` | string | `low` / `medium` / `high` / `critical` |
| `risk_type` | string | 风险类别，如 `file_access`、`script_execution`、`data_exfiltration`、`formal_verification` |
| `violated_layers` | array | 命中层，如 `["L1.1", "L2.2"]` |
| `policy` | string | Allow 时生成的最小策略（TOML） |
| `violated_policy` | string | Deny 时违反的策略条款 |
| `confidence` | int | 置信度 0–100 |
| `analysis_duration_ms` | float | 分析耗时（毫秒） |
| `violation_chain` | array | 组合链反例路径（按引擎返回的步骤数组） |
| `violation_graph` | object/null | 结构化攻击链图：`{nodes, edges}`，供前端可视化 |

#### POST /api/v1/review

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `agent_id` | string | 否 | 目标 agent（决定状态库分桶） |
| `db_path` | string | 否 | AgentGate SQLite 文件或 tool_calls JSON 目录；为空则用 `sessions` |
| `sessions` | array | 否 | 多 session 历史，结构同 `session_history` |
| `a_next` | object | 否 | 可选，连同当前动作一起判定 |
| `is_error` | bool | 否 | 当前动作是否执行失败（生命周期判定） |
| `output_pending` | bool | 否 | 返回待攻击前序链清单 |

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_id` | string | 目标 agent |
| `seeded_nodes` | int | 写入状态库的节点数 |
| `verified` | bool | 全量形式化验证是否通过 |
| `violations` | array | 全量验证发现的违规 |
| `pending_prefixes` | array | 待补全的攻击前序链 |
| `state_nodes` | int | 状态库累计节点数 |
| `discovered_patterns` | array | 数据驱动发现的跨 session 危险组合 |

#### GET /api/v1/health

响应字段：`status`（服务状态）、`version`（版本）、`instance`（实例归属标识，未配置为空串）。

#### 鉴权

默认 `AGENT_MOSS_AUTH_MODE=auto`：首次启动生成 token，写入
`$AGENT_MOSS_DATA_DIR/auth_token`（未设置时默认 `~/.config/agentmoss/auth_token`，权限 0600）。
`/api/v1/health` 公开，其余接口需要：

```text
Authorization: Bearer <token>
```

> **root/systemd 注意**：root（sudo/systemd/直接运行）下数据目录固定为
> `/root/.config/agentmoss`，**不再**按 `SUDO_USER` / `LOGNAME` / 首个普通用户
> 猜测真实用户目录；token 固定在 `/root/.config/agentmoss/auth_token`。
> 非 root 用户要访问 root 服务的数据（含 token）时，显式设置
> `AGENT_MOSS_DATA_DIR`，或传 `AGENT_MOSS_AUTH_TOKEN` / `AGENT_MOSS_AUTH_TOKEN_FILE`。
>
> 启动日志会打印实际路径：`安全策略: ... auth=on(/path/to/auth_token)`；
> 若显示 `auth=off` 说明显式关闭了鉴权，`auth=on(explicit)` 说明用了
> `AGENT_MOSS_AUTH_TOKEN*` 环境变量，`auth=on(no-token!)` 说明 token 生成失败
> （通常是数据目录不可写），此时服务会以无鉴权方式继续运行，需要立即检查。

读取 token 并直连 `/api/v1/analyze`：

```bash
MOSS_TOKEN=$(cat ~/.config/agentmoss/auth_token)

curl -sS -X POST http://127.0.0.1:9090/api/v1/analyze \
  -H "Authorization: Bearer $MOSS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "moss-direct-1",
    "prompt_session": "",
    "action_history": [],
    "a_next": {"action_type": "bash", "action_detail": "ls -la /tmp"},
    "reason": "direct curl test"
  }'
```

相关环境变量：

| 变量 | 作用 |
|---|---|
| `AGENT_MOSS_AUTH_MODE` | `auto`（默认，自动生成/读取 token）/ `off`（关闭鉴权） |
| `AGENT_MOSS_AUTH_TOKEN` | 显式指定全局 token |
| `AGENT_MOSS_AUTH_TOKEN_FILE` | 从文件读取全局 token |
| `AGENT_MOSS_AGENT_TOKENS` | 逐 agent token，如 `opendesk=tok-a,qwenpaw=tok-b` |

AgentGate 调用 AgentMoss 时也必须携带这个 token，配置方法见 AgentGate README 的“AgentGate 调 AgentMoss 的鉴权”。若 AgentGate 返回 `Deny` 且 reason 包含 `AgentMoss HTTP 401`，说明 Gate 自己鉴权通过，但 Gate→Moss 的 token 没配对。

## 规则与扩展

| 路径 | 内容 |
|---|---|
| `agent_moss/rules/*.json` | L1 / L2 内置规则 |
| `agent_moss/skills/*.md` | L3 Skill 规则（注入 LLM prompt） |
| `agent_moss/agents/<agent>/rules.json` | agent 专属扩展（如 `xiaoo`、`opendesk`） |
| `agent_moss/scenarios/<id>/rules.json` | 场景定制规则（如 `bank` 沙箱） |
| `agent_moss/profiles/` | linux / windows / macos 平台特征 |
| `agent_moss/data/` | 运行时数据模板与规则模板 |

规则目录支持 `extra.json` / `override.json`：

```bash
export AGENT_MOSS_EXTRA_RULES_DIR=/path/to/rules
agent-moss rules status
```

也可以用 `AGENT_MOSS_CUSTOM_RULES` 直接传 JSON 规则数组。

## 配置

配置加载优先级（从高到低）：

1. 环境变量；
2. `AGENT_MOSS_CONFIG_PATH` 指定的文件；
3. 用户配置 `~/.config/agentmoss/config.json`；
4. 包内默认配置。

运行时开关（Console 写入）位于 `~/.config/agentmoss/agent_moss_runtime.json`，
会覆盖源码内置规则。L3 是否真正执行由环境变量 / 配置文件 + LLM 连通性探测共同决定。

更多环境变量：

| 环境变量 | 说明 |
|---|---|
| `AGENT_MOSS_DISABLE_HEURISTIC` | 关闭 L1 特征匹配 |
| `AGENT_MOSS_DISABLE_LOGIC_RULES` | 关闭 L2 逻辑规则 |
| `AGENT_MOSS_DISABLE_LLM` | 关闭 L3 LLM |
| `AGENT_MOSS_LLM_TIMEOUT` | LLM 超时（秒） |
| `AGENT_MOSS_LLM_HISTORY_MAX_ITEMS` | L3 prompt 最多注入历史动作条数（0 = 不限） |
| `AGENT_MOSS_LLM_ACTION_DETAIL_MAX_CHARS` | L3 prompt 单条历史动作详情最大字符数（0 = 不截断） |
| `AGENT_MOSS_LLM_MAX_PROMPT_TOKENS` | L3 输入 prompt 的 token 预算（0 = 不限） |
| `AGENT_MOSS_SCENARIO` | 部署级默认场景 ID |
| `AGENT_MOSS_DATA_DIR` | 数据目录 |
| `AGENT_MOSS_LOG_PATH` | LLM prompt 日志路径 |

## 开发与测试

在仓库根目录执行。完整验证（含 DeepAgents 插件）需要 Python 3.11 或更新版本，
先安装本地源码的可选依赖和测试工具：

```bash
python -m pip install -e '.[deepagents-aio]' pytest pytest-asyncio
```

主仓测试：

```bash
AGENT_MOSS_ROOT="$PWD" AGENT_MOSS_DISABLE_LLM=1 python3 -m pytest -q
```

DeepAgents 插件测试必须单独运行：根目录 `pytest.ini` 的 `testpaths = tests`
不会收集插件用例。显式加载插件配置，以启用异步测试并使用插件源码：

```bash
python -m pytest -c integrations/deepagents-plugin/pyproject.toml integrations/deepagents-plugin/tests -q
```

真实 AIO 测试需要单独配置服务地址，未配置时跳过；配置方式见[插件测试说明](integrations/deepagents-plugin/README.md#测试)。

测试语料位于 `tests/cases/`，双端一致性 golden 数据位于 `tests/parity/`
（裁剪后只校验 Python 侧行为，不依赖 TypeScript 源码）。

## 目录结构

```
agent_moss/
├── cli.py              # agent-moss init / analyze / server / status / install / rules
├── engine/             # L1 / L2 / L3 检测与协调器
│   ├── coordinator.py
│   ├── heuristic.py
│   ├── logic_rules.py
│   ├── llm_analyzer.py
│   ├── skill_engine.py
│   └── formal_engine/
├── infra/              # 配置、LLM 客户端、日志、运行时配置
├── profiles/           # 平台特征（linux / windows / macos）
├── rules/              # 内置规则 JSON
├── skills/             # L3 Skill Markdown
├── agents/             # agent 专属扩展
├── scenarios/          # 场景定制扩展
├── server/             # FastAPI 服务与路由
└── console/            # 运行时控制台
tests/                  # 回归测试与用例
config/                 # 示例配置
integrations/           # 集成示例（QwenPaw 插件）
```

## License

MIT
